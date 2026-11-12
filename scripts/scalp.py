"""Scalping trend following di timeframe kecil (M1-M30), disimulasikan dengan akun kecil & lot nyata.

  python scripts/scalp.py                    # trend following, in-sample (70% awal data tiap timeframe)
  python scripts/scalp.py --family mr        # mean reversion (Bollinger, RSI-2)
  python scripts/scalp.py --family bo        # breakout (Donchian, Squeeze) & Ichimoku
  python scripts/scalp.py --family cf        # konfluensi MA + MACD + ATR
  python scripts/scalp.py --validate NAMA    # varian terpilih di out-of-sample (30% akhir data)
  python scripts/scalp.py --balance 100 --max-risk 0.01

Data dari download_data.py --tf M1 M5 M15 M30. Riwayat menit di MT5 dibatasi 'Max bars in chart',
jadi rentang tiap timeframe berbeda (M1 hanya beberapa bulan).
"""
import argparse
import json

import pandas as pd
from _common import setup_logging

from bot.backtest.engine import load_frames, run_all, simulate_account
from bot.backtest.report import metrics
from bot.config import load_settings

# Searah tren timeframe besar, hanya sesi London/NY, SL 1.5 ATR, TP kecil 1.5 ATR (= 1R)
SC = {"trend_filter": "with", "session_hours": True, "breakeven_at_r": 0, "adx_min": 20,
      "sl_atr_mult": 1.5, "tp_atr_mult": 1.5, "ema_trend": 200}
EMA = {**SC, "entry_type": "ema_cross", "ema_fast": 9, "ema_slow": 21}
PB = {**SC, "entry_type": "pullback", "ema_fast": 20, "ema_slow": 50}
ST = {**SC, "entry_type": "supertrend", "adx_min": 0}
MACD = {**SC, "entry_type": "macd_ema200", "adx_min": 0}

TF_VARIANTS: dict[str, dict] = {}
for entry_tf, trend_tf in [("M1", "M15"), ("M5", "M30"), ("M5", "H1"), ("M15", "H1"), ("M15", "H4"), ("M30", "H4")]:
    tfs = {"entry_tf": entry_tf, "trend_tf": trend_tf}
    TF_VARIANTS[f"ema9_21_{entry_tf}_{trend_tf}"] = {**EMA, **tfs}
    TF_VARIANTS[f"pullback_{entry_tf}_{trend_tf}"] = {**PB, **tfs}
    TF_VARIANTS[f"supertrend_{entry_tf}_{trend_tf}"] = {**ST, **tfs}
    TF_VARIANTS[f"macd_{entry_tf}_{trend_tf}"] = {**MACD, **tfs}
    TF_VARIANTS[f"ema9_21_{entry_tf}_{trend_tf}_tp0.67R"] = {**EMA, **tfs, "tp_atr_mult": 1.0}  # TP lebih kecil lagi

# Mean reversion & breakout, parameter default dari sumbernya (tidak dioptimasi)
FREE = {"breakeven_at_r": 0, "adx_min": 0, "rsi_buy_max": 101, "rsi_sell_min": -1, "session_hours": True}
BB = {**FREE, "entry_type": "bb_reversion", "trend_filter": "none", "adx_max": 20, "tp_mode": "mid_band",
      "sl_atr_mult": 1.5}
RSI2 = {**FREE, "entry_type": "rsi_reversion", "trend_filter": "with", "sl_atr_mult": 2.0, "tp_atr_mult": 1.0,
        "max_hold_bars": 10}
DC = {**FREE, "entry_type": "donchian", "donchian_period": 20, "trend_filter": "with", "sl_atr_mult": 2.0,
      "tp_atr_mult": 0, "trail_atr_mult": 3.0}
SQZ = {**FREE, "entry_type": "squeeze", "trend_filter": "none", "sl_atr_mult": 2.0, "tp_atr_mult": 0,
       "exit_rule": "signal"}
ICHI = {**FREE, "entry_type": "ichimoku", "trend_filter": "none", "sl_atr_mult": 2.5, "tp_atr_mult": 0,
        "exit_rule": "signal"}
MR_VARIANTS: dict[str, dict] = {}
BO_VARIANTS: dict[str, dict] = {}
for entry_tf, trend_tf in [("M1", "M15"), ("M5", "H1"), ("M15", "H4"), ("M30", "H4")]:
    tfs = {"entry_tf": entry_tf, "trend_tf": trend_tf}
    MR_VARIANTS[f"bb_{entry_tf}"] = {**BB, **tfs}
    MR_VARIANTS[f"bb_{entry_tf}_{trend_tf}_searah"] = {**BB, **tfs, "trend_filter": "with"}
    MR_VARIANTS[f"rsi2_{entry_tf}_{trend_tf}_searah"] = {**RSI2, **tfs}
    BO_VARIANTS[f"donchian20_{entry_tf}_{trend_tf}_trail"] = {**DC, **tfs}
    BO_VARIANTS[f"squeeze_{entry_tf}"] = {**SQZ, **tfs}
    BO_VARIANTS[f"ichimoku_{entry_tf}"] = {**ICHI, **tfs}

# Konfluensi MA + MACD + ATR: tren searah EMA200, momentum MACD searah, volatilitas sedang aktif
ALL3 = {"ma_filter": True, "macd_filter": True, "atr_ratio_min": 1.0}
CF_VARIANTS: dict[str, dict] = {}
for entry_tf, trend_tf in [("M1", "M15"), ("M5", "H1"), ("M15", "H4"), ("M30", "H4")]:
    tfs = {"entry_tf": entry_tf, "trend_tf": trend_tf}
    CF_VARIANTS[f"ema9_21+MA+MACD+ATR_{entry_tf}_{trend_tf}"] = {**EMA, **tfs, **ALL3}
    CF_VARIANTS[f"pullback+MA+MACD+ATR_{entry_tf}_{trend_tf}"] = {**PB, **tfs, **ALL3}
    CF_VARIANTS[f"ichimoku+MA+MACD+ATR_{entry_tf}"] = {**ICHI, **tfs, **ALL3}

FAMILIES = {"tf": TF_VARIANTS, "mr": MR_VARIANTS, "bo": BO_VARIANTS, "cf": CF_VARIANTS}
VARIANTS = {**TF_VARIANTS, **MR_VARIANTS, **BO_VARIANTS, **CF_VARIANTS}


def split_time(data_dir, names, tf) -> tuple[pd.Timestamp, pd.Timestamp]:
    starts, ends = [], []
    for n in names:
        frames, _ = load_frames(data_dir, n)
        starts.append(frames[tf]["time"].iloc[0])
        ends.append(frames[tf]["time"].iloc[-1])
    start, end = max(starts), min(ends)
    return start + (end - start) * 0.7, start


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", choices=list(FAMILIES), default="tf",
                    help="tf = trend following, mr = mean reversion, bo = breakout/Ichimoku, "
                         "cf = konfluensi MA + MACD + ATR")
    ap.add_argument("--validate", metavar="NAMA", choices=list(VARIANTS))
    ap.add_argument("--balance", type=float, default=100.0)
    ap.add_argument("--max-risk", type=float, default=0.01, help="batas risiko saat terpaksa pakai lot minimum")
    ap.add_argument("--symbols", nargs="*")
    args = ap.parse_args()
    setup_logging()

    base = load_settings()
    names = args.symbols or [x.name for x in base.symbols]
    data_dir = base.path(base.data_dir)
    metas = {n: json.loads((data_dir / f"{n}_meta.json").read_text(encoding="utf-8")) for n in names}
    chosen = {args.validate: VARIANTS[args.validate]} if args.validate else FAMILIES[args.family]

    rows = {}
    for name, ov in chosen.items():
        s = base.model_copy(update={"strategy": base.strategy.model_copy(update=ov)})
        split, first = split_time(data_dir, names, s.strategy.entry_tf)
        trades = run_all(s, names, data_dir)
        trades = trades[trades["signal_time"] >= first]
        trades = trades[trades["signal_time"] >= split] if args.validate else trades[trades["signal_time"] < split]
        if trades.empty:
            rows[name] = {"trades": 0}
            continue
        r = metrics(trades)   # semua sinyal, dalam R (tidak tergantung modal)
        taken, eq = simulate_account(trades, metas, args.balance, base.risk.risk_per_trade, args.max_risk,
                                     base.risk.max_open_positions)
        acc = metrics(taken, eq, args.balance) if len(taken) else {}
        days = max(1, (trades["signal_time"].max() - trades["signal_time"].min()).days)
        rows[name] = {**r, "trade/hari": round(len(trades) / days * 7 / 5, 1),
                      "periode": f"{trades['signal_time'].min():%y-%m-%d}..{trades['signal_time'].max():%y-%m-%d}",
                      "akun_trade": len(taken), "akun_return_%": acc.get("return_%"),
                      "akun_DD_%": acc.get("max_drawdown_%"),
                      "risk_%_maks": round(taken["risk_%"].max(), 2) if len(taken) else None,
                      "biaya_R": round(trades["cost_r"].mean(), 3)}

    cols = ["trades", "trade/hari", "winrate_%", "profit_factor", "expectancy_R", "biaya_R", "max_losing_streak",
            "akun_trade", "akun_return_%", "akun_DD_%", "risk_%_maks", "periode"]
    label = "OUT-OF-SAMPLE (30% akhir)" if args.validate else "IN-SAMPLE (70% awal)"
    print(f"\n=== {label} | akun ${args.balance:g}, lot min, risiko maks {args.max_risk:.1%} | {', '.join(names)} ===")
    df = pd.DataFrame(rows).T.reindex(columns=cols)
    print(df.sort_values("profit_factor", ascending=False).to_string())


if __name__ == "__main__":
    main()
