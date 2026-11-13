"""Claude sebagai analis harian XAUUSD/EURUSD: analisis teks + setup terstruktur (zona entry, SL, TP, RR >= 2).
Batas keras (sisi SL/TP, RR, jarak SL, lebar zona) dicek di kode, bukan dipercayakan ke model."""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from bot.ai.client import LLM
from bot.ai.prompts import ANALYSIS_SYSTEM, POSITION_SYSTEM
from bot.intraday import STRATEGIES

MIN_RR = 1.5              # minimal reward:risk TP1 (ideal 2+)
# Branch uji/setelan-kemarin: skor & ambang seperti commit e451a9c (12/11): 70-79 = risiko <= risk_per_trade,
# >= 80 = semua opsi risiko, < 70 = WAIT. Tidak ada tingkat "conditional".
MIN_SCORE = 70            # skor setup di bawah ini = WAIT
VALID_SCORE = 70
FULL_RISK_SCORE = 80      # di bawah ini hanya boleh risiko <= risk_per_trade
SCORE_WEIGHTS = {"trend": 20, "structure": 20, "key_level": 15, "price_action": 20, "momentum": 15, "rr": 10}
MAX_COST_R = 0.15         # spread + komisi maksimal 15% dari R: SL lebih sempit dari itu tidak layak
SL_ATR_RANGE = (0.3, 3.0)     # jarak SL dalam ATR H1 yang masuk akal untuk day trade
MAX_ZONE_ATR = 1.0            # zona entry lebih lebar dari 1 ATR H1 = bukan zona


class Setup(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None = None
    timeframe: str = "M15"
    confidence: Literal["High", "Medium", "Low"] = "Low"
    reason: str = ""
    wait_for: str = ""
    invalid_if: str = ""
    valid_minutes: int = Field(120, ge=15, le=480)
    score: dict[str, float] = Field(default_factory=dict)   # komponen skor, lihat SCORE_WEIGHTS
    strategy: str = ""    # A-E (bot.intraday.STRATEGIES); kosong = tidak diberi label
    regime: str = ""      # TREND | RANGE | MIXED saat setup dibuat

    def model_post_init(self, __context) -> None:
        self.symbol = self.symbol.upper().strip()
        self.strategy = self.strategy.upper().strip()[:1]
        if self.strategy not in STRATEGIES:
            self.strategy = ""
        self.regime = self.regime.upper().strip()
        if self.entry_low > self.entry_high:
            self.entry_low, self.entry_high = self.entry_high, self.entry_low

    @property
    def score_total(self) -> int:
        """Total skor dihitung ulang di kode: tiap komponen dibatasi bobot maksimumnya."""
        return round(sum(min(max(float(self.score.get(k, 0)), 0.0), w) for k, w in SCORE_WEIGHTS.items()))

    @property
    def grade(self) -> str:
        t = self.score_total
        return ("Premium" if t >= FULL_RISK_SCORE else "Valid" if t >= VALID_SCORE
                else "Conditional" if t >= MIN_SCORE else "No Trade")

    @property
    def direction(self) -> int:
        return 1 if self.side == "BUY" else -1

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2

    @property
    def sl_distance(self) -> float:
        return self.direction * (self.entry_mid - self.stop_loss)

    @property
    def tp_distance(self) -> float:
        return self.direction * (self.take_profit_1 - self.entry_mid)

    @property
    def rr(self) -> float:
        return self.tp_distance / self.sl_distance if self.sl_distance > 0 else 0.0

    def in_zone(self, price: float, tolerance: float = 0.0) -> bool:
        return self.entry_low - tolerance <= price <= self.entry_high + tolerance


class Watch(BaseModel):
    """Skenario "tunggu" dari jawaban WAIT: bot memantau close M15 dan memanggil analisis ulang saat pemicu kena.
    close_below / close_above: close M15 melewati `level`; zone: candle M15 menyentuh zona_low-zone_high."""
    symbol: str
    side: Literal["BUY", "SELL"]
    trigger: Literal["close_below", "close_above", "zone"]
    level: float | None = None
    zone_low: float | None = None
    zone_high: float | None = None
    invalid_level: float | None = None   # close M15 melewati level ini (melawan arah side) = skenario batal
    valid_minutes: int = Field(240, ge=15, le=480)
    strategy: str = ""
    note: str = ""

    def model_post_init(self, __context) -> None:
        self.symbol = self.symbol.upper().strip()
        self.strategy = self.strategy.upper().strip()[:1]
        if self.strategy not in STRATEGIES:
            self.strategy = ""
        if self.zone_low is not None and self.zone_high is not None and self.zone_low > self.zone_high:
            self.zone_low, self.zone_high = self.zone_high, self.zone_low

    @property
    def direction(self) -> int:
        return 1 if self.side == "BUY" else -1

    def triggered(self, bar_high: float, bar_low: float, bar_close: float) -> bool:
        """Pemicu terpenuhi oleh candle M15 yang sudah close."""
        if self.trigger == "close_below":
            return bar_close < self.level
        if self.trigger == "close_above":
            return bar_close > self.level
        return bar_high >= self.zone_low and bar_low <= self.zone_high

    def invalidated(self, bar_close: float) -> bool:
        """Close M15 melewati level pembatal: BUY batal bila close di bawahnya, SELL bila di atasnya."""
        if self.invalid_level is None:
            return False
        return bar_close < self.invalid_level if self.direction == 1 else bar_close > self.invalid_level


class Analysis(BaseModel):
    text: str
    setups: list[Setup] = Field(default_factory=list)
    watch: list[Watch] = Field(default_factory=list)

    @field_validator("watch", mode="before")
    @classmethod
    def _drop_bad_watch(cls, v):
        """Item pantauan yang rusak dibuang satu per satu, jangan menggagalkan seluruh analisis."""
        out = []
        for item in v or []:
            try:
                out.append(Watch.model_validate(item))
            except ValidationError:
                continue
        return out


def parse_analysis(text: str) -> Analysis:
    """Ambil objek JSON dari jawaban (toleran terhadap code fence / teks tambahan)."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("tidak ada JSON di jawaban")
    return Analysis.model_validate(json.loads(m.group(0)))


def validate_setup(s: Setup, atr: float, cost: float = 0.0, check_score: bool = True) -> str | None:
    """Alasan setup tidak boleh dieksekusi, atau None jika lolos batas keras. atr = ATR H1 simbol,
    cost = spread + komisi round-turn dalam satuan harga. check_score=False untuk setup yang sudah disetujui
    pemilik (mis. order limit yang dipindah), sehingga hanya batas harga yang dicek."""
    if s.sl_distance <= 0:
        return "SL di sisi yang salah"
    if s.tp_distance <= 0:
        return "TP1 di sisi yang salah"
    if s.rr < MIN_RR - 1e-9:
        return f"RR {s.rr:.1f} < {MIN_RR:g}"
    if check_score and s.score_total < MIN_SCORE:
        return f"skor {s.score_total}/100 < {MIN_SCORE} (WAIT)"
    if cost > 0 and cost / s.sl_distance > MAX_COST_R:
        return (f"biaya {cost / s.sl_distance:.0%} dari R, batas {MAX_COST_R:.0%}: SL terlalu sempit untuk "
                f"spread + komisi")
    if atr > 0:
        lo, hi = SL_ATR_RANGE
        if not lo <= s.sl_distance / atr <= hi:
            return f"jarak SL {s.sl_distance / atr:.1f} ATR di luar {lo:g}-{hi:g}"
        if s.entry_high - s.entry_low > MAX_ZONE_ATR * atr:
            return f"zona entry terlalu lebar ({(s.entry_high - s.entry_low) / atr:.1f} ATR)"
    return None


def watch_problem(w: Watch, price: float) -> str | None:
    """Alasan item pantauan tidak dipakai, atau None jika masuk akal terhadap harga sekarang."""
    if w.trigger == "zone":
        if w.zone_low is None or w.zone_high is None:
            return "zona tidak lengkap"
        if w.zone_low <= price <= w.zone_high:
            return "harga sudah di zona"
    elif w.level is None:
        return "level pemicu kosong"
    elif (w.trigger == "close_below") == (price < w.level):
        return "pemicu sudah terpenuhi di harga sekarang"
    if w.invalidated(price):
        return "harga sudah melewati level pembatal"
    return None


def format_watch(w: Watch, digits: int) -> str:
    f = f".{digits}f"
    if w.trigger == "zone":
        what = f"candle M15 menyentuh {w.zone_low:{f}}-{w.zone_high:{f}}"
    else:
        what = f"close M15 {'di bawah' if w.trigger == 'close_below' else 'di atas'} {w.level:{f}}"
    label = f" ({w.strategy})" if w.strategy else ""
    inv = f", batal bila close {'di bawah' if w.direction == 1 else 'di atas'} {w.invalid_level:{f}}" \
        if w.invalid_level is not None else ""
    return f"{w.side} {w.symbol}{label}: {what}{inv}"


def analyze_market(llm: LLM, context_json: str, system: str = ANALYSIS_SYSTEM) -> Analysis:
    """Minta analisis ke Claude. Raise LLMError / ValueError jika gagal atau jawaban tidak valid."""
    raw = llm.complete(system, f"Market snapshot (JSON):\n{context_json}")
    try:
        return parse_analysis(raw)
    except (ValidationError, json.JSONDecodeError) as e:
        raise ValueError(f"jawaban AI tidak valid: {e}") from e


def format_setup(s: Setup, digits: int) -> str:
    f = f".{digits}f"
    tp2 = f" | TP2 {s.take_profit_2:{f}}" if s.take_profit_2 is not None else ""
    label = f"strategi {s.strategy} {STRATEGIES[s.strategy]}, " if s.strategy else ""
    lines = [f"{s.side} {s.symbol} ({label}{s.timeframe}, skor {s.score_total}/100 {s.grade}, confidence {s.confidence})",
             f"Zona {s.entry_low:{f}}-{s.entry_high:{f}} | SL {s.stop_loss:{f}} | TP1 {s.take_profit_1:{f}}{tp2} "
             f"| RR {s.rr:.1f}"]
    if s.reason:
        lines.append(f"Alasan: {s.reason}")
    if s.wait_for:
        lines.append(f"Tunggu: {s.wait_for}")
    if s.invalid_if:
        lines.append(f"Batal jika: {s.invalid_if}")
    return "\n".join(lines)


class Advice(BaseModel):
    ticket: int
    action: Literal["HOLD", "MOVE_SL", "PARTIAL_CLOSE", "CLOSE"]
    new_sl: float | None = None
    close_frac: float | None = None
    reason: str = ""
    reverse_scenario: str = ""


class LimitAdvice(BaseModel):
    """Saran untuk order limit yang masih menunggu: KEEP (biarkan), MOVE (pindah entry/SL/TP), CANCEL."""
    ticket: int
    action: Literal["KEEP", "MOVE", "CANCEL"]
    new_price: float | None = None
    new_sl: float | None = None
    new_tp: float | None = None
    reason: str = ""


class PositionReview(BaseModel):
    text: str = ""
    advice: list[Advice] = Field(default_factory=list)
    limit_advice: list[LimitAdvice] = Field(default_factory=list)


def review_positions(llm: LLM, context_json: str, system: str = POSITION_SYSTEM) -> PositionReview:
    """Saran pengelolaan posisi terbuka. Raise LLMError / ValueError jika gagal atau jawaban tidak valid."""
    raw = llm.complete(system, f"Market snapshot, open positions and pending limit orders (JSON):\n{context_json}")
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError("tidak ada JSON di jawaban")
    try:
        return PositionReview.model_validate(json.loads(m.group(0)))
    except (ValidationError, json.JSONDecodeError) as e:
        raise ValueError(f"jawaban AI tidak valid: {e}") from e
