"""Claude sebagai analis harian XAUUSD/EURUSD: analisis teks + setup terstruktur (zona entry, SL, TP, RR >= 2).
Batas keras (sisi SL/TP, RR, jarak SL, lebar zona) dicek di kode, bukan dipercayakan ke model."""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from bot.ai.client import LLM
from bot.ai.prompts import ANALYSIS_SYSTEM, POSITION_SYSTEM

MIN_RR = 1.5              # minimal reward:risk TP1 (ideal 2+)
MIN_SCORE = 70            # skor setup di bawah ini = WAIT
FULL_RISK_SCORE = 80      # di bawah ini hanya boleh risiko <= risk_per_trade
MAX_COST_R = 0.15         # spread + komisi maksimal 15% dari R: SL lebih sempit dari itu tidak layak
SCORE_WEIGHTS = {"trend": 20, "structure": 20, "key_level": 15, "price_action": 20, "momentum": 15, "rr": 10}
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

    def model_post_init(self, __context) -> None:
        self.symbol = self.symbol.upper().strip()
        if self.entry_low > self.entry_high:
            self.entry_low, self.entry_high = self.entry_high, self.entry_low

    @property
    def score_total(self) -> int:
        """Total skor dihitung ulang di kode: tiap komponen dibatasi bobot maksimumnya."""
        return round(sum(min(max(float(self.score.get(k, 0)), 0.0), w) for k, w in SCORE_WEIGHTS.items()))

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


class Analysis(BaseModel):
    text: str
    setups: list[Setup] = Field(default_factory=list)


def parse_analysis(text: str) -> Analysis:
    """Ambil objek JSON dari jawaban (toleran terhadap code fence / teks tambahan)."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("tidak ada JSON di jawaban")
    return Analysis.model_validate(json.loads(m.group(0)))


def validate_setup(s: Setup, atr: float, cost: float = 0.0) -> str | None:
    """Alasan setup tidak boleh dieksekusi, atau None jika lolos batas keras. atr = ATR H1 simbol,
    cost = spread + komisi round-turn dalam satuan harga."""
    if s.sl_distance <= 0:
        return "SL di sisi yang salah"
    if s.tp_distance <= 0:
        return "TP1 di sisi yang salah"
    if s.rr < MIN_RR - 1e-9:
        return f"RR {s.rr:.1f} < {MIN_RR:g}"
    if s.score_total < MIN_SCORE:
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
    lines = [f"{s.side} {s.symbol} ({s.timeframe}, skor {s.score_total}/100, confidence {s.confidence})",
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


class PositionReview(BaseModel):
    text: str = ""
    advice: list[Advice] = Field(default_factory=list)


def review_positions(llm: LLM, context_json: str, system: str = POSITION_SYSTEM) -> PositionReview:
    """Saran pengelolaan posisi terbuka. Raise LLMError / ValueError jika gagal atau jawaban tidak valid."""
    raw = llm.complete(system, f"Market snapshot and open positions (JSON):\n{context_json}")
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError("tidak ada JSON di jawaban")
    try:
        return PositionReview.model_validate(json.loads(m.group(0)))
    except (ValidationError, json.JSONDecodeError) as e:
        raise ValueError(f"jawaban AI tidak valid: {e}") from e
