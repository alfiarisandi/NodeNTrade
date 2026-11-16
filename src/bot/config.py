"""Memuat config/settings.yaml + .env menjadi objek Settings tervalidasi."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[2]


class SymbolCfg(BaseModel):
    name: str
    max_spread_points: float
    max_lot: float = Field(0.0, ge=0)   # batas lot per order untuk simbol ini (0 = tanpa batas)


class StrategyCfg(BaseModel):
    # ema_cross = EMA fast memotong EMA slow | pullback = harga kembali di atas EMA fast setelah koreksi
    # donchian = breakout high/low N bar terakhir
    # Mean reversion: bb_reversion = close kembali ke dalam Bollinger Band setelah keluar
    # rsi_reversion = RSI cepat ekstrem (gaya Connors RSI-2)
    # Strategi open source: supertrend, squeeze (LazyBear), tsmom (Moskowitz et al. 2012), ichimoku, macd_ema200
    # Day trade: range_breakout = tembus range Asia (London breakout), pdh_breakout = tembus high/low kemarin
    entry_type: Literal["ema_cross", "pullback", "donchian", "bb_reversion", "rsi_reversion",
                        "supertrend", "squeeze", "tsmom", "ichimoku", "macd_ema200",
                        "range_breakout", "pdh_breakout"] = "ema_cross"
    exit_rule: Literal["stops", "signal"] = "stops"  # signal = juga keluar saat aturan exit strategi terpenuhi
    entry_tf: Literal["M1", "M5", "M15", "M30", "H1", "H4", "D1"] = "H1"
    trend_tf: Literal["M15", "M30", "H1", "H4", "D1"] = "H4"
    trend_filter: Literal["with", "none"] = "with"  # with = hanya searah tren timeframe besar
    session_hours: bool = True   # False = abaikan jam sesi (akhir pekan & Jumat malam tetap diblok)
    donchian_period: int = 55
    trail_atr_mult: float = 0.0  # trailing stop x ATR saat entry (0 = nonaktif)
    tp_mode: Literal["atr", "mid_band"] = "atr"  # mid_band = TP di garis tengah Bollinger saat sinyal
    max_hold_bars: int = 0       # tutup paksa setelah N bar (0 = nonaktif)
    adx_max: float = 101         # hanya entry jika ADX di bawah ini (filter pasar sideways)
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_fast_period: int = 2
    rsi_fast_low: float = 10
    rsi_fast_high: float = 90
    st_period: int = 10
    st_mult: float = 3.0
    kc_mult: float = 1.5
    tsmom_lookback: int = 252
    ema_fast: int = 20
    ema_slow: int = 50
    ema_trend: int = 200
    adx_period: int = 14
    adx_min: float = 20
    rsi_period: int = 14
    rsi_buy_max: float = 70
    rsi_sell_min: float = 30
    atr_period: int = 14
    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 3.0
    breakeven_at_r: float = 1.0
    # Kunci profit: setelah profit floating mencapai lock_at_r (x R), SL mengikuti di lock_fraction dari
    # profit maksimum yang pernah dicapai (0 = nonaktif). Padanan backtest untuk risk.take_profit_usd + trail_lock_fraction.
    lock_at_r: float = 0.0
    lock_fraction: float = 0.0
    # Partial TP + dynamic SL (backtest): tutup partial_frac di partial_r x R, sisa: SL ke breakeven + spread,
    # TP di tp2_r x R (0 = tanpa), trailing post_trail_atr_mult x ATR atau low/high post_swing_bars bar terakhir.
    partial_r: float = 0.0
    partial_frac: float = 0.5
    tp2_r: float = 0.0
    post_trail_atr_mult: float = 0.0
    post_swing_bars: int = 0
    partial_keep_sl: bool = False   # sisa posisi tetap memakai SL awal (tanpa breakeven) setelah TP parsial
    # Filter konfirmasi tambahan (berlaku untuk entry_type apa pun; default nonaktif)
    ma_filter: bool = False       # buy hanya jika close > EMA(ema_trend) di timeframe entry, sell sebaliknya
    macd_filter: bool = False     # buy hanya jika MACD(12,26,9) > garis sinyal, sell sebaliknya
    atr_ratio_min: float = 0.0    # hanya entry jika ATR / rata-rata ATR 50 bar >= nilai ini (0 = nonaktif)
    ma50_filter: bool = False     # buy hanya jika close > EMA(ema_slow) (= EMA50) di timeframe entry, sell sebaliknya
    # Bar sinyal harus pola candle searah: engulfing | pin (pin bar) | any (salah satu) | none
    candle_confirm: Literal["none", "engulfing", "pin", "any"] = "none"
    # Day trade (jam UTC): range dari range_start..range_end, entry hanya sampai entry_window_end
    range_start_utc: int = 0
    range_end_utc: int = 7
    entry_window_end_utc: int = 11
    sl_mode: Literal["atr", "level"] = "atr"  # level = SL di sisi lain range/level, maks sl_atr_mult x ATR
    tp_r: float = 0.0             # TP = tp_r x jarak SL (0 = pakai tp_atr_mult)


class SessionCfg(BaseModel):
    start_hour_utc: int = 7
    end_hour_utc: int = 20
    friday_cutoff_hour_utc: int = 18
    server_utc_offset_hours: int | None = None


class RiskCfg(BaseModel):
    # Batas keras: tidak bisa di-set di atas 1% per trade
    risk_per_trade: float = Field(0.01, gt=0, le=0.01)
    max_daily_loss: float = Field(0.03, gt=0, le=0.10)
    max_open_positions: int = Field(3, ge=1, le=10)
    max_currency_exposure: int = Field(2, ge=1)
    max_trades_per_day: int = Field(0, ge=0)   # entry per hari lintas simbol (0 = tanpa batas); baru dipakai backtest
    # Circuit breaker: stop entry baru jika equity turun sejauh ini dari puncaknya (reset manual)
    max_drawdown_from_peak: float = Field(0.15, gt=0, le=0.5)
    # Akun kecil: jika lot dari risk_per_trade di bawah lot minimum broker, pakai lot minimum selama
    # kerugiannya di SL <= min_lot_max_risk x equity (0 = nonaktif, sinyal dilewati). Batas keras 10%.
    min_lot_max_risk: float = Field(0.0, ge=0, le=0.10)
    # Cek tilt: setelah N rugi beruntun, opsi risiko di atas risk_per_trade disembunyikan (0 = nonaktif)
    tilt_losing_streak: int = Field(3, ge=0)
    # Setelah N rugi beruntun: entry baru diblok sampai pemilik menekan tombol Lanjut di Telegram (0 = nonaktif)
    stop_after_losses: int = Field(0, ge=0)
    # Day/swing pendek: tutup posisi bot saat profit floating >= take_profit_usd, atau setelah max_hold_days (0 = nonaktif)
    take_profit_usd: float = Field(0.0, ge=0)
    max_hold_days: float = Field(0.0, ge=0)
    # Setelah profit >= take_profit_usd dan tren masih searah: jangan tutup, kunci SL di fraksi ini dari gerak profit (0 = tutup di TP)
    trail_lock_fraction: float = Field(0.0, ge=0, lt=1)


class AICfg(BaseModel):
    enabled: bool = True
    on_failure: Literal["skip", "rule_only"] = "skip"
    timeout_seconds: float = 180
    max_tokens: int = 4000
    daily_review_hour_utc: int = 21
    weekly_review_weekday: int = Field(4, ge=0, le=6)   # 0 = Senin ... 4 = Jumat (UTC)
    weekly_review_hour_utc: int = Field(21, ge=0, le=23)
    # Analisis harian otomatis (tombol 📊 Analisis) di jam-jam WIB ini, hari kerja; kosong = hanya saat diminta
    analysis_hours_wib: list[int] = Field(default_factory=list)
    # Analisis di luar jadwal per jam (pemicu pantauan AI + kandidat screener M15), maks N per hari WIB
    # (0 = pantauan nonaktif)
    max_trigger_analyses_per_day: int = Field(12, ge=0)
    # Screener M15: tiap candle M15 close cek aturan A-E (tanpa AI); ada kandidat = AI dipanggil menilai
    screener: bool = False
    # Model cadangan (nama persis di 9Router) bila model utama kena limit / error, dicoba berurutan.
    # Diisi dari AI_FALLBACK_MODELS di .env (pisahkan dengan koma) atau di sini.
    fallback_models: list[str] = Field(default_factory=list)
    fallback_max_tokens: int = 16000   # model cadangan (mis. DeepSeek) memakai token untuk berpikir: 4000 terpotong
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class BacktestCfg(BaseModel):
    initial_balance: float = 10000
    commission_per_lot: float = 7.0
    slippage_points: float = 2
    default_spread_points: float = 15


class MT5Cfg(BaseModel):
    login: int | None = None
    password: str = ""
    server: str = ""
    path: str = ""


class TelegramCfg(BaseModel):
    token: str = ""
    chat_id: str = ""


class NewsCfg(BaseModel):
    enabled: bool = True
    url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    block_impacts: list[str] = ["High"]
    block_before_hours: float = 4.0   # tidak entry jika ada event high impact dalam N jam ke depan
    block_after_hours: float = 1.0    # ... atau baru terjadi kurang dari N jam lalu
    # Jendela blok untuk setup 📊 Analisis (day trade, ditahan jam-an, bukan hari-an seperti Ichimoku H4)
    setup_block_before_hours: float = 1.0
    setup_block_after_hours: float = 0.5
    context_days: float = 5.0         # rentang event yang diberikan ke Claude (≈ lama posisi H4)
    refresh_minutes: int = 180
    cache_path: str = "data/calendar.json"


class ConfirmCfg(BaseModel):
    """Konfirmasi manual lewat tombol Telegram sebelum order dikirim."""
    enabled: bool = True
    timeout_minutes: int = Field(30, ge=1, le=240)
    risk_options: list[float] = [0.0025, 0.005, 0.01]
    max_price_move_atr: float = 0.5   # batalkan jika harga sudah bergerak > N x ATR dari harga sinyal
    max_setups_per_day: int = Field(8, ge=0, le=10)   # setup 📊 Analisis yang boleh dieksekusi per hari (WIB)
    # Risiko awal setup 📊 per tingkat skor: opsi risk_options terbesar yang tidak melebihinya (di atasnya disembunyikan)
    risk_strong: float = 0.005        # skor 8-10/10
    risk_moderate: float = 0.0025     # skor 6-7/10
    max_active_setups: int = Field(1, ge=1, le=5)   # posisi + limit setup 📊 yang boleh aktif bersamaan

    @field_validator("risk_options")
    @classmethod
    def _cap(cls, v: list[float]) -> list[float]:
        # Batas keras yang sama dengan risk_per_trade: maks 1% per trade
        if not v or any(not 0 < r <= 0.01 for r in v):
            raise ValueError("risk_options harus di antara 0 dan 0.01 (maks 1%)")
        return sorted(v)


class ManageCfg(BaseModel):
    """Dynamic stop loss + partial TP untuk posisi setup 📊 Analisis (dijalankan bot tiap siklus; SL hanya mengetat)."""
    enabled: bool = True
    be_at_r: float = Field(1.0, ge=0)        # profit >= N x R: SL ke breakeven + biaya (0 = nonaktif)
    lock_at_r: float = Field(1.5, ge=0)      # profit >= N x R: SL dikunci di lock_r x R (0 = nonaktif)
    lock_r: float = Field(0.5, ge=0)
    partial_frac: float = Field(0.5, ge=0, lt=1)   # bagian lot yang ditutup di target parsial (0 = TP1 menutup semua)
    partial_at_r: float = Field(1.0, ge=0)   # target parsial dalam R (0 = di TP1 setup)
    # Target parsial dalam uang: profit floating posisi >= $N (0 = nonaktif). Dipakai mana yang lebih dulu
    # tercapai antara ini dan partial_at_r / TP1.
    partial_at_usd: float = Field(0.0, ge=0)
    trail: Literal["atr", "swing", "none"] = "atr"   # trailing sisa posisi setelah TP1
    trail_tf: Literal["M5", "M15", "M30", "H1"] = "M15"
    trail_atr_mult: float = Field(1.5, gt=0)
    close_on_choch: bool = True              # setelah TP1: tutup sisa bila struktur trail_tf berbalik (CHoCH)


class CandleReportCfg(BaseModel):
    """Ringkasan teknikal singkat di setiap close candle `timeframe` (tanpa AI), juga saat tidak ada sinyal/setup."""
    enabled: bool = False
    timeframe: Literal["M15", "M30", "H1", "H4"] = "H1"
    start_hour_wib: int = Field(8, ge=0, le=23)    # jam close candle (WIB) pertama yang dilaporkan
    end_hour_wib: int = Field(23, ge=0, le=23)     # jam close candle (WIB) terakhir yang dilaporkan


class Settings(BaseModel):
    mode: Literal["dry_run", "live"] = "dry_run"
    magic: int = 20261103
    symbol_suffix: str = ""
    symbols: list[SymbolCfg]
    strategy: StrategyCfg = StrategyCfg()
    session: SessionCfg = SessionCfg()
    risk: RiskCfg = RiskCfg()
    ai: AICfg = AICfg()
    backtest: BacktestCfg = BacktestCfg()
    mt5: MT5Cfg = MT5Cfg()
    telegram: TelegramCfg = TelegramCfg()
    news: NewsCfg = NewsCfg()
    confirm: ConfirmCfg = ConfirmCfg()
    manage: ManageCfg = ManageCfg()
    manage_symbols: dict[str, dict] = Field(default_factory=dict)   # override ManageCfg per simbol
    candle_report: CandleReportCfg = CandleReportCfg()
    journal_path: str = "data/journal.db"
    data_dir: str = "data"
    reports_dir: str = "reports"
    poll_seconds: int = 20

    def manage_for(self, name: str) -> ManageCfg:
        return ManageCfg.model_validate({**self.manage.model_dump(), **self.manage_symbols.get(name, {})})

    def broker_symbol(self, name: str) -> str:
        return f"{name}{self.symbol_suffix}"

    def symbol_cfg(self, name: str) -> SymbolCfg:
        return next(s for s in self.symbols if s.name == name)

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _read_yaml(path: Path) -> dict:
    """Baca YAML; `extends: file.yaml` = mulai dari file itu lalu timpa dengan isi file ini (list diganti utuh)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    parent = raw.pop("extends", None)
    return _deep_merge(_read_yaml(path.parent / parent), raw) if parent else raw


def load_settings(config_path: str | Path | None = None) -> Settings:
    """config_path > env BOT_CONFIG > config/settings.yaml (path relatif terhadap folder project)."""
    # override: .env menang atas variabel environment Windows lama (mis. NINEROUTER_API_KEY basi di level User)
    load_dotenv(ROOT / ".env", override=True)
    chosen = config_path or os.getenv("BOT_CONFIG", "").strip()
    path = (Path(chosen) if Path(chosen).is_absolute() else ROOT / chosen) if chosen else ROOT / "config" / "settings.yaml"
    raw = _read_yaml(path)

    login = os.getenv("MT5_LOGIN", "").strip()
    raw["mt5"] = {
        "login": int(login) if login.isdigit() else None,
        "password": os.getenv("MT5_PASSWORD", ""),
        "server": os.getenv("MT5_SERVER", ""),
        "path": os.getenv("MT5_PATH", ""),
    }
    raw["telegram"] = {
        "token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
    }
    ai = raw.setdefault("ai", {})
    ai["base_url"] = os.getenv("NINEROUTER_BASE_URL", "http://localhost:20128").rstrip("/")
    ai["api_key"] = os.getenv("NINEROUTER_API_KEY", "")
    ai["model"] = os.getenv("AI_MODEL", "")
    fallback = [m.strip() for m in os.getenv("AI_FALLBACK_MODELS", "").split(",") if m.strip()]
    if fallback:
        ai["fallback_models"] = fallback
    return Settings.model_validate(raw)
