"""Bandingkan varian strategi di data in-sample, lalu validasi SATU varian terpilih di out-of-sample.

  python scripts/variants.py                       # varian trend following, in-sample 2019-2023
  python scripts/variants.py --family mr           # varian mean reversion, in-sample 2019-2023
  python scripts/variants.py --family os           # strategi open source populer, in-sample 2019-2023
  python scripts/variants.py --family cf           # Ichimoku H4 + filter MA / MACD / ATR
  python scripts/variants.py --family dt           # day trade 1-3 hari, akun $100 lot 0.01 (5 pair volatilitas rendah)
  python scripts/variants.py --family sm           # Ichimoku H4 + MA200 di akun $100 (profil settings.small.yaml)
  python scripts/variants.py --family ex           # proteksi profit (BE / trailing / kunci / TP) di setelan live
  python scripts/variants.py --family dd           # day trade spek Daily Trading Strategist (EMA50/200, PDH, range Asia, RR 2)
  python scripts/variants.py --validate NAMA       # varian terpilih di out-of-sample 2024-sekarang
"""
import argparse
import json

import pandas as pd
from _common import setup_logging

from bot.backtest.engine import run_all, simulate_account, simulate_portfolio
from bot.backtest.report import metrics
from bot.config import SessionCfg, SymbolCfg, load_settings

NO_FILTER = {"adx_min": 0, "rsi_buy_max": 101, "rsi_sell_min": -1}
SWING = {"entry_tf": "H4", "trend_tf": "D1", "session_hours": False, "breakeven_at_r": 0}

TREND_VARIANTS: dict[str, dict] = {
    "v1_baseline": {},
    "v1_tanpa_BE": {"breakeven_at_r": 0},
    "emacross_H4": {**SWING},
    "pullback_H1": {"entry_type": "pullback", "breakeven_at_r": 0},
    "pullback_H4": {"entry_type": "pullback", **SWING},
    # Gaya Turtle: breakout channel, SL 2 ATR, tanpa TP, keluar lewat trailing stop 3 ATR
    "donchian55_H4_trail": {"entry_type": "donchian", "donchian_period": 55, **SWING, **NO_FILTER,
                            "sl_atr_mult": 2.0, "tp_atr_mult": 0, "trail_atr_mult": 3.0},
    "donchian20_D1_trail": {"entry_type": "donchian", "donchian_period": 20, "entry_tf": "D1", "trend_tf": "D1",
                            "session_hours": False, "breakeven_at_r": 0, **NO_FILTER,
                            "sl_atr_mult": 2.0, "tp_atr_mult": 0, "trail_atr_mult": 3.0},
}

# Mean reversion: tanpa filter tren & RSI trend-following; ADX rendah = pasar sideways
MR = {"trend_filter": "none", "breakeven_at_r": 0, "adx_min": 0, "rsi_buy_max": 101, "rsi_sell_min": -1}
BB = {**MR, "entry_type": "bb_reversion", "adx_max": 20, "tp_mode": "mid_band", "sl_atr_mult": 1.5}
RSI2 = {**MR, "entry_type": "rsi_reversion", "trend_filter": "with", "sl_atr_mult": 2.0, "tp_atr_mult": 1.0,
        "max_hold_bars": 10}

MR_VARIANTS: dict[str, dict] = {
    "bb_H1_sesi": {**BB},
    "bb_H1_24jam": {**BB, "session_hours": False},
    "bb_H1_24jam_timestop": {**BB, "session_hours": False, "max_hold_bars": 12},
    "bb_H4": {**BB, "entry_tf": "H4", "trend_tf": "D1", "session_hours": False},
    # Gaya Connors: beli koreksi tajam searah tren besar, keluar cepat
    "rsi2_H1_searah_tren": {**RSI2},
    "rsi2_H4_searah_tren": {**RSI2, "entry_tf": "H4", "trend_tf": "D1", "session_hours": False},
}

# Strategi open source / publik, parameter default dari sumbernya (tidak dioptimasi)
OS = {"trend_filter": "none", "breakeven_at_r": 0, "adx_min": 0, "session_hours": False}
OS_VARIANTS: dict[str, dict] = {
    # Supertrend(10, 3) - TradingView/KivancOzbilgic: balik arah = keluar & masuk arah sebaliknya
    "supertrend_H4": {**OS, "entry_type": "supertrend", "entry_tf": "H4", "trend_tf": "D1",
                      "sl_atr_mult": 3.0, "tp_atr_mult": 0, "exit_rule": "signal"},
    "supertrend_D1": {**OS, "entry_type": "supertrend", "entry_tf": "D1", "trend_tf": "D1",
                      "sl_atr_mult": 3.0, "tp_atr_mult": 0, "exit_rule": "signal"},
    # LazyBear Squeeze Momentum: masuk saat squeeze lepas, keluar saat momentum berganti warna
    "squeeze_H4": {**OS, "entry_type": "squeeze", "entry_tf": "H4", "trend_tf": "D1",
                   "sl_atr_mult": 2.0, "tp_atr_mult": 0, "exit_rule": "signal"},
    # Time-series momentum (Moskowitz, Ooi & Pedersen 2012): arah return 12 / 3 bulan
    "tsmom_12bln_D1": {**OS, "entry_type": "tsmom", "entry_tf": "D1", "trend_tf": "D1", "tsmom_lookback": 252,
                       "sl_atr_mult": 5.0, "tp_atr_mult": 0, "exit_rule": "signal"},
    "tsmom_3bln_D1": {**OS, "entry_type": "tsmom", "entry_tf": "D1", "trend_tf": "D1", "tsmom_lookback": 63,
                      "sl_atr_mult": 5.0, "tp_atr_mult": 0, "exit_rule": "signal"},
    # Ichimoku klasik (9, 26, 52): TK cross di luar awan, keluar saat TK cross balik
    "ichimoku_H4": {**OS, "entry_type": "ichimoku", "entry_tf": "H4", "trend_tf": "D1",
                    "sl_atr_mult": 2.5, "tp_atr_mult": 0, "exit_rule": "signal"},
    # MACD(12,26,9) + EMA200: setup retail populer, SL 1.5 ATR, TP 1.5R
    "macd_ema200_H1": {**OS, "entry_type": "macd_ema200", "session_hours": True,
                       "sl_atr_mult": 1.5, "tp_atr_mult": 2.25},
}

# Ichimoku H4 + filter konfirmasi MA / MACD / ATR
ICHI_H4 = OS_VARIANTS["ichimoku_H4"]
CF_VARIANTS: dict[str, dict] = {
    "ichimoku_H4": ICHI_H4,
    "ichimoku_H4+MA200": {**ICHI_H4, "ma_filter": True},
    "ichimoku_H4+MACD": {**ICHI_H4, "macd_filter": True},
    "ichimoku_H4+ATR": {**ICHI_H4, "atr_ratio_min": 1.0},
    "ichimoku_H4+MA+MACD+ATR": {**ICHI_H4, "ma_filter": True, "macd_filter": True, "atr_ratio_min": 1.0},
}

# Day trade 1-3 hari untuk akun kecil: entry H1, tren D1 + EMA200 H1, sesi London/NY, time stop 72 bar H1.
# Dievaluasi dengan akun $100 & lot nyata (lot min 0.01, risiko maks 1%) di pair volatilitas rendah.
DT_SYMBOLS = ["EURUSD", "AUDUSD", "NZDUSD", "USDCAD", "EURGBP"]
DT = {"entry_tf": "H1", "trend_tf": "D1", "trend_filter": "with", "ma_filter": True, "session_hours": True,
      "max_hold_bars": 72, "breakeven_at_r": 0, "adx_min": 0, "rsi_buy_max": 101, "rsi_sell_min": -1,
      "exit_rule": "stops", "trail_atr_mult": 0}
RB = {**DT, "entry_type": "range_breakout", "sl_mode": "level", "sl_atr_mult": 1.0}
PDH = {**DT, "entry_type": "pdh_breakout", "sl_mode": "level", "sl_atr_mult": 1.0, "entry_window_end_utc": 20}
PBK = {**DT, "entry_type": "pullback", "ema_fast": 20, "ema_slow": 50, "sl_atr_mult": 1.0}
DT_VARIANTS: dict[str, dict] = {
    "london_breakout_1R": {**RB, "tp_r": 1.0},
    "london_breakout_1.5R": {**RB, "tp_r": 1.5},
    "london_breakout_1R_tanpa_tren": {**RB, "tp_r": 1.0, "trend_filter": "none", "ma_filter": False},
    "pdh_breakout_1R": {**PDH, "tp_r": 1.0},
    "pdh_breakout_1.5R": {**PDH, "tp_r": 1.5},
    "pullback_H1_1R": {**PBK, "tp_r": 1.0},
    "pullback_H1_1.5R": {**PBK, "tp_r": 1.5},
}

# Akun kecil ($100, profil settings.small.yaml): Ichimoku H4 + MA200 dengan tambahan pengaman / filter.
# Pair kandidat: EURUSD + pair volatilitas rendah, supaya lebih banyak sinyal muat di batas rugi 10%.
SM_SYMBOLS = ["EURUSD", "AUDUSD", "NZDUSD", "USDCAD", "EURGBP", "USDCHF"]
SM = {**ICHI_H4, "ma_filter": True}
SM_VARIANTS: dict[str, dict] = {
    "sm_baseline": SM,
    "sm_BE1R": {**SM, "breakeven_at_r": 1.0},
    "sm_TP2R": {**SM, "tp_r": 2.0},
    "sm_TP3R": {**SM, "tp_r": 3.0},
    "sm_SL2ATR": {**SM, "sl_atr_mult": 2.0},
    "sm_SL2ATR_BE1R": {**SM, "sl_atr_mult": 2.0, "breakeven_at_r": 1.0},
    "sm_trenD1": {**SM, "trend_filter": "with"},
    "sm_ADX20": {**SM, "adx_min": 20},
    "sm_MACD": {**SM, "macd_filter": True},
}

# Swing pendek 1-3 hari (18 bar H4 = 3 hari): Ichimoku H4 + MA200 (setelan live) dengan batas tahan, trailing, TP-R
DY_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "EURGBP", "EURJPY", "GBPJPY", "XAUUSD"]
DY = {**ICHI_H4, "ma_filter": True}
DY_VARIANTS: dict[str, dict] = {
    "dy_live_sekarang": DY,
    "dy_hold3d": {**DY, "max_hold_bars": 18},
    "dy_hold3d_trail3": {**DY, "max_hold_bars": 18, "trail_atr_mult": 3.0},
    "dy_hold3d_trail2": {**DY, "max_hold_bars": 18, "trail_atr_mult": 2.0},
    "dy_hold3d_TP2R": {**DY, "max_hold_bars": 18, "tp_r": 2.0},
    "dy_hold3d_TP1.5R_BE": {**DY, "max_hold_bars": 18, "tp_r": 1.5, "breakeven_at_r": 1.0},
    "dy_hold3d_SL2": {**DY, "max_hold_bars": 18, "sl_atr_mult": 2.0},
    "dy_hold3d_ADX20": {**DY, "max_hold_bars": 18, "adx_min": 20},
    "dy_hold3d_trenD1": {**DY, "max_hold_bars": 18, "trend_filter": "with"},
    "dy_supertrend_hold3d": {**OS_VARIANTS["supertrend_H4"], "ma_filter": True, "max_hold_bars": 18},
    "dy_ichi_H1_hold3d": {**DY, "entry_tf": "H1", "trend_tf": "D1", "max_hold_bars": 72, "trend_filter": "with",
                          "session_hours": True},
}

# Proteksi profit di atas setelan live (config/settings.yaml, pair live): breakeven, trailing ATR, kunci persentase, TP.
# Catatan: risk.take_profit_usd / trail_lock_fraction tidak dimodelkan backtest; padanannya ex_kunci* (akun $10k,
# risiko 0.5% -> $25 = 0.5R) dan ex_TP0.5R (tutup di $25 tanpa trail).
EX_VARIANTS: dict[str, dict] = {
    "ex_live_tanpa_kunci": {},
    "ex_kunci0.5R_60%": {"lock_at_r": 0.5, "lock_fraction": 0.6},
    "ex_kunci1R_50%": {"lock_at_r": 1.0, "lock_fraction": 0.5},
    "ex_kunci1R_70%": {"lock_at_r": 1.0, "lock_fraction": 0.7},
    "ex_kunci2R_50%": {"lock_at_r": 2.0, "lock_fraction": 0.5},
    "ex_BE0.5R": {"breakeven_at_r": 0.5},
    "ex_BE1R": {"breakeven_at_r": 1.0},
    "ex_trail2ATR": {"trail_atr_mult": 2.0},
    "ex_trail3ATR": {"trail_atr_mult": 3.0},
    "ex_trail4ATR": {"trail_atr_mult": 4.0},
    "ex_BE1R_trail3ATR": {"breakeven_at_r": 1.0, "trail_atr_mult": 3.0},
    "ex_TP0.5R": {"tp_r": 0.5},
    "ex_TP2R": {"tp_r": 2.0},
    "ex_TP3R": {"tp_r": 3.0},
}

# Day trade versi spek "Daily Trading Strategist": EURUSD + XAUUSD, $10k, risiko 0.5%, sesi London+NY (07-20 UTC),
# tren D1 EMA200 + close di sisi EMA200 H1, SL di level (maks 1.5 ATR), TP 2R, tutup paksa 24 bar, maks 3 entry/hari.
# Varian menambah filter EMA50 (ma50_filter) dan konfirmasi candle (engulfing / pin bar) di bar sinyal.
# HASIL 2026-11-12: GAGAL gerbang (OOS PF > 1.2, DD < 15%, >= 100 trade, kedua pair PF > 1). In-sample 2019-2023 semua
# PF < 1 (0.63-0.98); out-of-sample 2024-2026 terbaik dd_pdh_2R PF 1.09 (+27R, DD 13%), EURUSD selalu PF < 1.
# Konfirmasi candle memangkas 80-90% sinyal dan memperburuk hasil. Tidak dilanjutkan ke live.
DD_SYMBOLS = ["EURUSD", "XAUUSD"]
DD = {"entry_tf": "H1", "trend_tf": "D1", "trend_filter": "with", "ma_filter": True, "session_hours": True,
      "max_hold_bars": 24, "breakeven_at_r": 0, "adx_min": 0, "rsi_buy_max": 101, "rsi_sell_min": -1,
      "exit_rule": "stops", "trail_atr_mult": 0, "sl_mode": "level", "sl_atr_mult": 1.5, "tp_r": 2.0}
DD_RB = {**DD, "entry_type": "range_breakout"}
DD_PDH = {**DD, "entry_type": "pdh_breakout", "entry_window_end_utc": 20}
DD_PB = {**DD, "entry_type": "pullback", "ema_fast": 20, "ema_slow": 50, "sl_mode": "atr"}
DD_VARIANTS: dict[str, dict] = {
    "dd_london_2R": DD_RB,
    "dd_london_2R_ma50": {**DD_RB, "ma50_filter": True},
    "dd_london_2R_engulf": {**DD_RB, "candle_confirm": "engulfing"},
    "dd_london_2R_candle": {**DD_RB, "candle_confirm": "any"},
    "dd_london_3R": {**DD_RB, "tp_r": 3.0},
    "dd_pdh_2R": DD_PDH,
    "dd_pdh_2R_ma50": {**DD_PDH, "ma50_filter": True},
    "dd_pdh_2R_engulf": {**DD_PDH, "candle_confirm": "engulfing"},
    "dd_pullback_2R_ma50": {**DD_PB, "ma50_filter": True},
    "dd_pullback_2R_engulf": {**DD_PB, "candle_confirm": "engulfing"},
}

# Studi kebijakan exit intraday (dynamic SL / partial TP) di atas entri day-trade yang sama (family dd).
# Tujuan: win rate tertinggi yang masih untung setelah biaya. tpX = TP penuh di X R (pembanding win rate tinggi).
# HASIL 2026-11-12 (EURUSD+XAUUSD, entri pdh & pullback): tidak ada kebijakan yang lolos PF > 1.2 di kedua periode
# (entrinya sendiri rugi), jadi yang dibaca adalah efek relatif exit:
# - TP makin dekat = win rate naik tapi makin rugi: tp0.2R win 74-81% dengan PF 0.43-0.65; win rate 90% tidak tercapai.
# - Parsial 50% di 1R + sisa jalan (BE, TP 3R / trailing swing): win rate 36% -> 48-51%, PF hampir sama dengan tp2R.
# - Parsial di 0.5R: win rate 60-64% tapi PF turun (0.75-0.96). BE di 0.5R: buruk. BE di 1R: PF netral.
# Dipakai live (config manage): parsial 50% di 1R, BE+biaya di 1R, kunci 0.5R di 1.5R, trailing swing/ATR.
PART = {"tp_r": 0, "tp_atr_mult": 0, "partial_r": 1.0, "partial_frac": 0.5}
XS_POLICIES: dict[str, dict] = {
    "tp2R": {},
    "tp1R": {"tp_r": 1.0},
    "tp0.5R": {"tp_r": 0.5},
    "tp0.3R": {"tp_r": 0.3},
    "tp0.2R": {"tp_r": 0.2},
    "tp2R_be1R": {"breakeven_at_r": 1.0},
    "tp2R_be0.5R": {"breakeven_at_r": 0.5},
    "tp2R_be1R_lock1.5R": {"breakeven_at_r": 1.0, "lock_at_r": 1.5, "lock_fraction": 0.33},
    "part1R_be_tp2R": {**PART, "tp2_r": 2.0},
    "part1R_be_tp3R": {**PART, "tp2_r": 3.0},
    "part1R_trailATR1": {**PART, "post_trail_atr_mult": 1.0},
    "part1R_trailATR1.5": {**PART, "post_trail_atr_mult": 1.5},
    "part1R_swing5": {**PART, "post_swing_bars": 5},
    "part0.5R_be_tp2R": {**PART, "partial_r": 0.5, "tp2_r": 2.0},
    "part0.5R_trailATR1": {**PART, "partial_r": 0.5, "post_trail_atr_mult": 1.0},
}
XS_VARIANTS: dict[str, dict] = {f"{b}|{k}": {**base, **pol}
                                for b, base in (("pdh", DD_PDH), ("pullback", {**DD_PB, "ma50_filter": True}))
                                for k, pol in XS_POLICIES.items()}

VARIANTS = {**TREND_VARIANTS, **MR_VARIANTS, **OS_VARIANTS, **CF_VARIANTS, **DT_VARIANTS, **SM_VARIANTS,
            **DY_VARIANTS, **EX_VARIANTS, **DD_VARIANTS, **XS_VARIANTS}


def evaluate(s, names, data_dir, start, end) -> tuple[dict, dict]:
    trades = run_all(s, names, data_dir)
    trades = trades[(trades["signal_time"] >= start) & (trades["signal_time"] < end)]
    bt, risk = s.backtest, s.risk
    taken, eq = simulate_portfolio(trades, bt.initial_balance, risk.risk_per_trade, risk.max_open_positions,
                                   max_per_day=risk.max_trades_per_day)
    port = metrics(taken, eq, bt.initial_balance)
    per_symbol = {}
    for name in names:
        sub = trades[trades["symbol"] == name]
        per_symbol[name] = metrics(sub).get("profit_factor", 0) if len(sub) else None
    port["pair_PF>1"] = f"{sum(1 for v in per_symbol.values() if v and v > 1)}/{len(names)}"
    return port, per_symbol


def dt_settings(base, names):
    """Settings akun kecil: risiko 1% (= batas lot minimum), maks 2 posisi, semua pair kandidat."""
    return base.model_copy(update={
        "symbols": [SymbolCfg(name=n, max_spread_points=20) for n in names],
        "risk": base.risk.model_copy(update={"risk_per_trade": 0.01, "max_open_positions": 2})})


def evaluate_account(s, names, data_dir, start, end, balance) -> tuple[dict, dict]:
    """Seperti evaluate, tapi dengan akun kecil & lot nyata. Metrik R dihitung dari trade yang benar-benar
    bisa diambil akun ini (sinyal dengan SL terlalu lebar untuk lot minimum dilewati)."""
    metas = {n: json.loads((data_dir / f"{n}_meta.json").read_text(encoding="utf-8")) for n in names}
    trades = run_all(s, names, data_dir)
    trades = trades[(trades["signal_time"] >= start) & (trades["signal_time"] < end)]
    risk = s.risk.risk_per_trade
    taken, eq = simulate_account(trades, metas, balance, risk, max(risk, s.risk.min_lot_max_risk),
                                 s.risk.max_open_positions)
    if taken.empty:
        return {"trades": 0, "sinyal": len(trades)}, {}
    port = metrics(taken, eq, balance)
    hold = (taken["exit_time"] - taken["entry_time"]).dt.total_seconds() / 86400
    port.update({"sinyal": len(trades), "hold_median_hari": round(hold.median(), 1),
                 "risk_%_median": round(taken["risk_%"].median(), 2)})
    per_symbol = {n: metrics(taken[taken["symbol"] == n]).get("profit_factor") for n in names}
    port["pair_PF>1"] = f"{sum(1 for v in per_symbol.values() if v and v > 1)}/{len(names)}"
    return port, per_symbol


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="2024-01-01")
    ap.add_argument("--from", dest="start", default="2019-01-01")
    ap.add_argument("--family", choices=["tf", "mr", "os", "cf", "dt", "sm", "dy", "ex", "dd", "xs"], default="tf",
                    help="tf = trend following, mr = mean reversion, os = strategi open source, "
                         "cf = Ichimoku H4 + filter MA/MACD/ATR, dt = day trade akun kecil, "
                         "sm = Ichimoku H4 + MA200 akun kecil, ex = proteksi profit di setelan live, "
                         "dd = day trade spek Daily Trading Strategist (EURUSD+XAUUSD)")
    ap.add_argument("--balance", type=float, default=100.0, help="modal awal untuk family dt / sm")
    ap.add_argument("--symbols", nargs="*", help="pair untuk family dt / sm (default: DT_SYMBOLS / SM_SYMBOLS)")
    ap.add_argument("--validate", metavar="NAMA", choices=list(VARIANTS))
    args = ap.parse_args()
    setup_logging()

    small = args.family == "sm" or args.validate in SM_VARIANTS
    day_trade = args.family == "dt" or args.validate in DT_VARIANTS
    base = load_settings("config/settings.small.yaml" if small else None)
    if small:
        names = args.symbols or SM_SYMBOLS
        base = base.model_copy(update={"symbols": [SymbolCfg(name=n, max_spread_points=20) for n in names]})
    elif day_trade:
        names = args.symbols or DT_SYMBOLS
        base = dt_settings(base, names)
    elif args.family in ("dd", "xs") or args.validate in DD_VARIANTS or args.validate in XS_VARIANTS:
        names = args.symbols or DD_SYMBOLS
        base = base.model_copy(update={
            "symbols": [SymbolCfg(name=n, max_spread_points=50 if n == "XAUUSD" else 25) for n in names],
            "session": SessionCfg(start_hour_utc=7, end_hour_utc=20, friday_cutoff_hour_utc=18),
            "risk": base.risk.model_copy(update={"max_open_positions": 2, "max_trades_per_day": 3})})
    elif args.family == "dy" or args.validate in DY_VARIANTS:
        names = args.symbols or DY_PAIRS
        base = base.model_copy(update={"symbols": [SymbolCfg(name=n, max_spread_points=50 if n == "XAUUSD" else 25)
                                                   for n in names]})
    else:
        names = [x.name for x in base.symbols]
    data_dir = base.path(base.data_dir)
    split = pd.Timestamp(args.split)

    if args.validate:
        chosen = {args.validate: VARIANTS[args.validate]}
        start, end, label = split, pd.Timestamp.max, "OUT-OF-SAMPLE"
    else:
        chosen = {"tf": TREND_VARIANTS, "mr": MR_VARIANTS, "os": OS_VARIANTS, "cf": CF_VARIANTS,
                  "dt": DT_VARIANTS, "sm": SM_VARIANTS, "dy": DY_VARIANTS, "ex": EX_VARIANTS,
                  "dd": DD_VARIANTS, "xs": XS_VARIANTS}[args.family]
        start, end, label = pd.Timestamp(args.start), split, "IN-SAMPLE"

    rows, pairs = {}, {}
    for name, overrides in chosen.items():
        s = base.model_copy(update={"strategy": base.strategy.model_copy(update=overrides)})
        if day_trade or small:
            rows[name], pairs[name] = evaluate_account(s, names, data_dir, start, end, args.balance)
        else:
            rows[name], pairs[name] = evaluate(s, names, data_dir, start, end)

    cols = ["trades", "winrate_%", "profit_factor", "expectancy_R", "total_R", "return_%", "max_drawdown_%",
            "sharpe", "max_losing_streak", "pair_PF>1"]
    if day_trade or small:
        cols[1:1] = ["sinyal"]
        cols += ["hold_median_hari", "risk_%_median"]
        cap = max(base.risk.risk_per_trade, base.risk.min_lot_max_risk)
        label += f" | akun ${args.balance:g}, lot min 0.01, risiko maks {cap:.0%}"
    print(f"\n=== {label} {start:%Y-%m-%d} s/d {'sekarang' if end == pd.Timestamp.max else f'{end:%Y-%m-%d}'} ===")
    df = pd.DataFrame(rows).T.reindex(columns=cols)
    print(df.sort_values("profit_factor", ascending=False).to_string())
    print("\nProfit factor per pair:")
    print(pd.DataFrame(pairs).T.to_string())


if __name__ == "__main__":
    main()
