"""Menyusun data pasar, posisi, dan akun menjadi konteks ringkas untuk Claude."""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from bot.config import StrategyCfg
from bot.indicators import atr
from bot.intraday import REGIME_STRATEGIES, mtf_features
from bot.strategy import Signal, add_indicators, candle_flags

WIB = timezone(timedelta(hours=7))
# Sesi utama dalam jam lokal (DST otomatis lewat zoneinfo); Asia = di luar keduanya sebelum London buka
SESSIONS = {"London": (ZoneInfo("Europe/London"), 8, 17), "New York": (ZoneInfo("America/New_York"), 8, 17)}
ROUND_STEP = {5: 0.005, 4: 0.005, 3: 0.5, 2: 10.0, 1: 10.0, 0: 10.0}   # kelipatan round number per digits


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
        # Bollinger Band (bb_period, bb_std): %B 0 = di band bawah, 100 = di band atas, di luar 0-100 = menembus band
        "bb_upper": _r(last["bb_up"], digits),
        "bb_mid": _r(last["bb_mid"], digits),
        "bb_lower": _r(last["bb_low"], digits),
        "bb_percent_b": _r(100 * (last["close"] - last["bb_low"]) / (last["bb_up"] - last["bb_low"]), 0)
        if last["bb_up"] > last["bb_low"] else None,
        "bb_width_atr": _r((last["bb_up"] - last["bb_low"]) / atr_, 2) if atr_ and not np.isnan(atr_) else None,
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
    """[time, open, high, low, close] untuk n bar terakhir."""
    return [[r.time.strftime("%m-%d %H:%M"), round(r.open, digits), round(r.high, digits),
             round(r.low, digits), round(r.close, digits)] for r in h1.tail(n).itertuples()]


# ---------- level kunci, struktur pasar, pola candle, sesi (day trading) ----------
def key_levels(frames: dict[str, pd.DataFrame], p: StrategyCfg, digits: int, offset_hours: int = 0,
               now: datetime | None = None) -> dict:
    """PDH/PDL, high/low hari ini, range Asia, high/low minggu, round number terdekat, semua dengan jarak dalam ATR H1.
    Waktu bar = jam server; offset_hours mengubahnya ke UTC (tanggal & jam sesi dihitung di UTC)."""
    h1, d1 = frames.get("H1"), frames.get("D1")
    if h1 is None or len(h1) < p.atr_period + 2:
        return {}
    now = now or datetime.now(timezone.utc)
    today = pd.Timestamp(now).tz_convert(None).normalize()
    close = float(h1["close"].iloc[-1])
    a = float(atr(h1["high"], h1["low"], h1["close"], p.atr_period).iloc[-1])

    def lvl(price):
        if price is None or np.isnan(price):
            return None
        return {"price": _r(price, digits), "dist_atr": _r((price - close) / a, 2) if a else None}

    out = {"close": _r(close, digits), "atr_H1": _r(a, digits)}
    if d1 is not None and len(d1):
        last = d1.iloc[-1]   # bar D1 terakhir yang sudah close = hari (server) sebelumnya
        out["prev_day_high"], out["prev_day_low"] = lvl(last["high"]), lvl(last["low"])
        week = d1[d1["time"] >= today - pd.Timedelta(days=today.weekday())]
        if len(week):
            out["week_high"], out["week_low"] = lvl(week["high"].max()), lvl(week["low"].min())
    intraday = frames.get("M15", h1)
    utc = intraday["time"] - pd.Timedelta(hours=offset_hours)
    today_mask = (utc.dt.normalize() == today).to_numpy()
    today_bars = intraday[today_mask]
    if len(today_bars):
        out["today_high"], out["today_low"] = lvl(today_bars["high"].max()), lvl(today_bars["low"].min())
    hours = utc.dt.hour.to_numpy()
    asia = intraday[today_mask & (hours >= p.range_start_utc) & (hours < p.range_end_utc)]
    if len(asia):
        out["asian_range_high"], out["asian_range_low"] = lvl(asia["high"].max()), lvl(asia["low"].min())
        out["asian_range_utc"] = f"{p.range_start_utc:02d}:00-{p.range_end_utc:02d}:00"
    step = ROUND_STEP.get(digits, 10.0 ** -(digits - 2))
    out["round_above"] = lvl(np.ceil(close / step + 1e-9) * step)
    out["round_below"] = lvl(np.floor(close / step - 1e-9) * step)
    return out


def market_structure(df: pd.DataFrame, digits: int, left: int = 3, right: int = 3, lookback: int = 120) -> dict:
    """Struktur pasar dari pivot fractal (left/right bar): label HH/HL/LH/LL, kesimpulan bullish/bearish/range,
    dan penembusan pivot terakhir (BOS searah struktur, CHoCH melawan struktur sebelumnya)."""
    f = df.tail(lookback).reset_index(drop=True)
    n = len(f)
    if n < left + right + 5:
        return {"structure": "unknown"}
    hi, lo, cl = (f[c].to_numpy(float) for c in ("high", "low", "close"))
    piv = []   # (index, "H"/"L", harga): hanya pivot yang sudah terkonfirmasi `right` bar
    for i in range(left, n - right):
        if hi[i] >= hi[i - left:i + right + 1].max() and hi[i] > hi[i - left:i].max():
            piv.append((i, "H", hi[i]))
        if lo[i] <= lo[i - left:i + right + 1].min() and lo[i] < lo[i - left:i].min():
            piv.append((i, "L", lo[i]))
    labeled, last_h, last_l = [], None, None
    for i, k, pz in piv:
        if k == "H":
            lab, last_h = ("HH" if last_h is None or pz > last_h else "LH"), pz
        else:
            lab, last_l = ("HL" if last_l is None or pz > last_l else "LL"), pz
        labeled.append((i, k, pz, lab))

    def trend_of(upto: int | None = None) -> str:
        hs = [x for x in labeled if x[1] == "H" and (upto is None or x[0] <= upto)]
        ls = [x for x in labeled if x[1] == "L" and (upto is None or x[0] <= upto)]
        if not hs or not ls:
            return "unknown"
        if hs[-1][3] == "HH" and ls[-1][3] == "HL":
            return "bullish"
        if hs[-1][3] == "LH" and ls[-1][3] == "LL":
            return "bearish"
        return "range"

    brk = None
    for i, k, pz, _ in labeled:
        after = cl[i + right + 1:]
        hit = np.flatnonzero(after > pz if k == "H" else after < pz)
        if len(hit) and (brk is None or i + right + 1 + hit[0] > brk["bar"]):
            j = i + right + 1 + hit[0]
            direction, before = ("up" if k == "H" else "down"), trend_of(i)
            kind = "CHoCH" if (direction == "up" and before == "bearish") or (
                direction == "down" and before == "bullish") else "BOS"
            brk = {"bar": j, "type": kind, "direction": direction, "level": _r(pz, digits),
                   "time": str(f["time"].iloc[j])}
    if brk:
        brk.pop("bar")
    highs = [x for x in labeled if x[1] == "H"]
    lows = [x for x in labeled if x[1] == "L"]
    return {
        "structure": trend_of(),
        "pivots_recent": [{"type": lab, "price": _r(pz, digits), "time": str(f["time"].iloc[i])}
                          for i, _, pz, lab in labeled[-4:]],
        "last_pivot_high": _r(highs[-1][2], digits) if highs else None,
        "last_pivot_low": _r(lows[-1][2], digits) if lows else None,
        "last_break": brk,
    }


def candle_patterns(df: pd.DataFrame, digits: int, n: int = 3) -> list[dict]:
    """Pola candle n bar terakhir (lihat strategy.candle_flags)."""
    flags = candle_flags(df).tail(n)
    out = []
    for (_, fl), row in zip(flags.iterrows(), df.tail(n).itertuples()):
        color = "bull" if row.close > row.open else "bear" if row.close < row.open else "doji"
        out.append({"time": row.time.strftime("%m-%d %H:%M"), "close": round(row.close, digits), "color": color,
                    "patterns": [k for k, v in fl.items() if v]})
    return out


def session_info(now: datetime) -> dict:
    """Sesi yang sedang aktif (London / New York / overlap / Asia), jam WIB, dan pembukaan/penutupan berikutnya."""
    now = now.astimezone(timezone.utc)
    active, upcoming, opens_wib = [], [], {}
    for name, (tz, open_h, close_h) in SESSIONS.items():
        local_day = now.astimezone(tz).date()
        opens_wib[name] = datetime.combine(local_day, time(open_h), tz).astimezone(WIB).strftime("%H:%M")
        for k in range(8):
            d = local_day + timedelta(days=k)
            if d.weekday() >= 5:
                continue
            o, c = datetime.combine(d, time(open_h), tz), datetime.combine(d, time(close_h), tz)
            if o <= now < c:
                active.append(name)
                upcoming.append((c, f"{name} close"))
                break
            if now < o:
                upcoming.append((o, f"{name} open"))
                break
    wd, hr = now.weekday(), now.hour
    weekend = wd == 5 or (wd == 6 and hr < 22) or (wd == 4 and hr >= 22)
    if weekend:
        label = "weekend (market closed)"
    elif len(active) == 2:
        label = "London-New York overlap"
    elif active:
        label = active[0]
    else:
        label = "Asia" if hr >= 22 or hr < 7 else "between sessions (low liquidity)"
    upcoming.sort()
    nxt = upcoming[0] if upcoming else None
    return {
        "now_utc": now.strftime("%a %d %b %H:%M"), "now_wib": now.astimezone(WIB).strftime("%H:%M WIB"),
        "label": label, "active": active,
        "next": f"{nxt[1]} in {(nxt[0] - now).total_seconds() / 3600:.1f}h ({nxt[0].astimezone(WIB):%H:%M} WIB)"
        if nxt else None,
        "opens_today_wib": opens_wib,
    }


def intraday_snapshot(frames: dict[str, pd.DataFrame], digits: int, fvg_lookback: int = 60) -> dict:
    """Regime, bias H4/H1, VWAP sesi, sweep terakhir, FVG M15 yang belum terisi, order block terakhir dan posisi
    premium/discount, dihitung dengan fungsi yang sama dengan backtest (bot.intraday). {} bila data kurang."""
    need = {"M15": 250, "H1": 210, "H4": 210}
    if any(tf not in frames or len(frames[tf]) < n for tf, n in need.items()):
        return {}
    f = mtf_features({tf: frames[tf] for tf in need})
    last, n = f.iloc[-1], len(f)
    a = float(last["atr"])
    if not a or np.isnan(a):
        return {}
    bias = {1: "bullish", -1: "bearish", 0: "neutral"}
    o, h, l, c = (f[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    gaps = []
    for k in range(max(n - fvg_lookback, 2), n):
        if l[k] > h[k - 2] and l[k + 1:].min(initial=np.inf) > h[k - 2]:      # belum terisi penuh
            gaps.append({"type": "bullish", "low": _r(h[k - 2], digits), "high": _r(l[k], digits),
                         "time": f["time"].iloc[k].strftime("%m-%d %H:%M")})
        elif h[k] < l[k - 2] and h[k + 1:].max(initial=-np.inf) < l[k - 2]:
            gaps.append({"type": "bearish", "low": _r(h[k], digits), "high": _r(l[k - 2], digits),
                         "time": f["time"].iloc[k].strftime("%m-%d %H:%M")})
    block = None
    disp = np.flatnonzero(f["disp"].to_numpy()[-fvg_lookback:] != 0)
    if len(disp):
        k = n - min(fvg_lookback, n) + int(disp[-1])
        d = int(f["disp"].iloc[k])
        prior = [j for j in range(k - 1, max(k - 6, 0) - 1, -1) if d * (c[j] - o[j]) < 0]
        if prior:
            block = {"type": bias[d], "low": _r(l[prior[0]], digits), "high": _r(h[prior[0]], digits),
                     "displacement_time": f["time"].iloc[k].strftime("%m-%d %H:%M")}
    lo50, hi50 = float(last["h1_lo50"]), float(last["h1_hi50"])
    pos = (float(last["close"]) - lo50) / (hi50 - lo50) if hi50 > lo50 else np.nan

    def ago(col):
        return int(last[col]) if last[col] <= 96 else None

    return {
        "regime": last["regime"], "high_volatility": bool(last["high_vol"]),
        "strategies_for_regime": list(REGIME_STRATEGIES[last["regime"]]),
        "h4_bias": bias[int(last["h4_dir"])] + (" (EMA50 slope agrees)" if last["h4_strong"] else ""),
        "h1_bias": bias[int(last["h1_dir"])],
        "h1_adx": _r(last["h1_adx"], 1),
        "ema20_M15": _r(last["ema20"], digits),
        "session_vwap": _r(last["vwap"], digits),
        "close_vs_vwap_atr_M15": _r((last["close"] - last["vwap"]) / a, 2),
        "dealing_range_H1_50bars": {"low": _r(lo50, digits), "high": _r(hi50, digits), "position_%": _r(100 * pos, 0),
                                    "zone": None if np.isnan(pos) else "premium" if pos > 0.5 else "discount"},
        "last_bullish_sweep_M15_bars_ago": ago("since_bull_sweep"),
        "last_bearish_sweep_M15_bars_ago": ago("since_bear_sweep"),
        "open_fvg_M15": gaps[-3:],
        "last_order_block_M15": block,
    }


def build_analysis_context(per_symbol: dict[str, dict[str, pd.DataFrame]], p: StrategyCfg, metas: dict[str, dict],
                           spreads: dict[str, float], now: datetime, offset_hours: int = 0, calendar=None,
                           account: dict | None = None, positions: list[dict] | None = None,
                           recent_trades: list[dict] | None = None, rules: dict | None = None,
                           prices: dict[str, dict] | None = None) -> str:
    """Snapshot multi-timeframe semua pair untuk analisis harian Claude (tanpa kandidat sinyal).
    per_symbol = {symbol: {"H4": df, "H1": df, "M15": df, "M5": df}} (frame kosong boleh dihilangkan)."""
    symbols = {}
    for sym, frames in per_symbol.items():
        digits = metas[sym]["digits"]
        frames = {tf: df for tf, df in frames.items() if df is not None and len(df)}
        intraday = frames.get("M15", frames.get("H1"))
        entry = {
            "price_now": {k: _r(v, digits) for k, v in (prices or {}).get(sym, {}).items()},
            "spread_points": _r(spreads.get(sym), 1),
            "market": market_summary({tf: frames[tf] for tf in ("H4", "H1", "M15") if tf in frames}, p, digits),
            "intraday": intraday_snapshot(frames, digits),
            "key_levels": key_levels(frames, p, digits, offset_hours, now),
            "structure": {tf: market_structure(frames[tf], digits) for tf in ("H1", "M15") if tf in frames},
            "candles": {tf: candle_patterns(frames[tf], digits) for tf in ("H1", "M15") if tf in frames},
        }
        if intraday is not None:
            entry["last_30_M15_candles[time,o,h,l,c]" if "M15" in frames else "last_30_H1_candles[time,o,h,l,c]"] = \
                recent_candles(intraday, digits)
        if "M5" in frames:
            entry["last_20_M5_candles[time,o,h,l,c]"] = recent_candles(frames["M5"], digits, 20)
        if calendar is not None:
            entry["economic_calendar"] = calendar.context(sym, now)
        symbols[sym] = entry
    currencies = {c for sym in per_symbol for c in (sym[:3], sym[3:6])}
    data = {
        "session": session_info(now),
        "high_impact_events_next_24h": (calendar.upcoming_all(now, 24, currencies)
                                        if calendar is not None and getattr(calendar, "available", False)
                                        else "calendar unavailable"),
        "account": account or {},
        "open_positions": positions or [],
        "last_closed_trades": recent_trades or [],
        "owner_rules": rules or {},
        "symbols": symbols,
    }
    return json.dumps(data, ensure_ascii=False, default=str)


def build_review_context(signal: Signal, frames: dict[str, pd.DataFrame], p: StrategyCfg, meta: dict,
                         entry_price: float, spread_points: float, session_utc_hour: int,
                         account: dict, positions: list[dict], exposure: dict[str, int],
                         daily_pnl_pct: float, recent_trades: list[dict],
                         calendar: dict | None = None, offset_hours: int = 0,
                         now: datetime | None = None) -> str:
    digits = meta["digits"]
    now = now or datetime.now(timezone.utc)
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
        "session": session_info(now),
        "economic_calendar": calendar if calendar is not None else {"available": False,
                                                                     "note": "calendar not configured"},
        "market": market_summary(frames, p, digits),
        "key_levels": key_levels(frames, p, digits, offset_hours, now),
        "structure": {tf: market_structure(frames[tf], digits)
                      for tf in dict.fromkeys([p.entry_tf, "H1"]) if tf in frames},
        f"candles_{p.entry_tf}": candle_patterns(frames[p.entry_tf], digits),
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
