"""Menyusun data pasar, posisi, dan akun menjadi konteks ringkas untuk Claude."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from bot.config import StrategyCfg
from bot.strategy import Signal, add_indicators


def _r(x, nd=5):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def tf_summary(df: pd.DataFrame, p: StrategyCfg, digits: int, swing_bars: int = 50) -> dict:
    """Ringkasan satu timeframe dari bar mentah."""
    f = add_indicators(df, p)
    last = f.iloc[-1]
    atr_ = last["atr"]
    recent = f.tail(swing_bars)
    swing_high, swing_low = recent["high"].max(), recent["low"].min()
    atr_hist = f["atr"].dropna().tail(100)
    pct = float((atr_hist <= atr_).mean() * 100) if len(atr_hist) else None
    change = (last["close"] / f["close"].iloc[-21] - 1) * 100 if len(f) > 21 else None

    def dist_atr(level):
        return _r((level - last["close"]) / atr_, 2) if atr_ and not np.isnan(atr_) else None

    return {
        "close": _r(last["close"], digits),
        "ema_fast": _r(last["ema_fast"], digits),
        "ema_slow": _r(last["ema_slow"], digits),
        "ema_trend": _r(last["ema_trend"], digits),
        "close_vs_ema_trend": "above" if last["close"] > last["ema_trend"] else "below",
        "ema_fast_slope_5bars_atr": _r((last["ema_fast"] - f["ema_fast"].iloc[-6]) / atr_, 2)
        if len(f) > 6 and atr_ else None,
        "dist_close_to_ema_slow_atr": dist_atr(last["ema_slow"]),
        "adx": _r(last["adx"], 1),
        "adx_5bars_ago": _r(f["adx"].iloc[-6], 1) if len(f) > 6 else None,
        "rsi": _r(last["rsi"], 1),
        "atr": _r(atr_, digits),
        "atr_percentile_100bars": _r(pct, 0),
        f"swing_high_{swing_bars}bars": _r(swing_high, digits),
        f"swing_low_{swing_bars}bars": _r(swing_low, digits),
        "dist_to_swing_high_atr": dist_atr(swing_high),
        "dist_to_swing_low_atr": dist_atr(swing_low),
        "change_20bars_%": _r(change, 2),
    }


def market_summary(frames: dict[str, pd.DataFrame], p: StrategyCfg, digits: int) -> dict:
    return {tf: tf_summary(df, p, digits) for tf, df in frames.items() if len(df) > p.ema_trend}


def recent_candles(h1: pd.DataFrame, digits: int, n: int = 30) -> list[list]:
    """[time, open, high, low, close] untuk n bar H1 terakhir."""
    return [[r.time.strftime("%m-%d %H:%M"), round(r.open, digits), round(r.high, digits),
             round(r.low, digits), round(r.close, digits)] for r in h1.tail(n).itertuples()]


def build_review_context(signal: Signal, frames: dict[str, pd.DataFrame], p: StrategyCfg, meta: dict,
                         entry_price: float, spread_points: float, session_utc_hour: int,
                         account: dict, positions: list[dict], exposure: dict[str, int],
                         daily_pnl_pct: float, recent_trades: list[dict],
                         calendar: dict | None = None) -> str:
    digits = meta["digits"]
    d = signal.direction
    sl = entry_price - d * signal.sl_distance
    tp = entry_price + d * signal.tp_distance if signal.tp_distance > 0 else None
    pip = meta["point"] * (10 if digits in (3, 5) else 1)
    data = {
        "candidate": {
            "symbol": signal.symbol,
            "side": signal.side,
            "signal_bar_time_server": str(signal.bar_time),
            "utc_hour_now": session_utc_hour,
            "entry_price": round(entry_price, digits),
            "stop_loss": round(sl, digits),
            "take_profit": round(tp, digits) if tp is not None else "none (exit on strategy signal)",
            "risk_reward": round(signal.tp_distance / signal.sl_distance, 2) if tp is not None else None,
            "sl_distance_atr": round(signal.sl_distance / signal.atr, 2),
            "sl_distance_pips": round(signal.sl_distance / pip, 1),
            "spread_points": round(spread_points, 1),
            "spread_vs_atr_%": round(100 * spread_points * meta["point"] / signal.atr, 1),
            "strategy_indicators": signal.snapshot,
        },
        "economic_calendar": calendar if calendar is not None else {"available": False,
                                                                     "note": "calendar not configured"},
        "market": market_summary(frames, p, digits),
        f"last_30_{p.entry_tf}_candles[time,o,h,l,c]": recent_candles(frames[p.entry_tf], digits),
        "portfolio": {
            "account": account,
            "today_pnl_%": round(100 * daily_pnl_pct, 2),
            "open_positions": positions,
            "net_currency_exposure": exposure,
            "last_closed_trades": recent_trades,
        },
    }
    return json.dumps(data, ensure_ascii=False, default=str)
