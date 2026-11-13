"""Backtest bar-by-bar untuk lapisan teknikal (tanpa AI)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from bot.config import Settings
from bot.intraday import ExitPolicy, IntradayParams, opposing_level, score_parts
from bot.strategy import compute_features, exit_flags, make_signal, signal_directions, simulate_exit, simulate_managed


@dataclass
class BTTrade:
    symbol: str
    direction: int
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    sl: float
    tp: float | None
    exit_price: float
    reason: str
    r_gross: float
    cost_r: float
    r_net: float


def backtest_symbol(symbol: str, entry_df: pd.DataFrame, trend_df: pd.DataFrame, meta: dict,
                    settings: Settings, offset_hours: int) -> list[BTTrade]:
    """Satu posisi per simbol. Entry di open bar setelah bar sinyal, termasuk spread & slippage.
    entry_df/trend_df = bar timeframe strategy.entry_tf / strategy.trend_tf."""
    p, bt = settings.strategy, settings.backtest
    max_spread = settings.symbol_cfg(symbol).max_spread_points
    point, tick_size, tick_value = meta["point"], meta["tick_size"], meta["tick_value"]

    f = compute_features(entry_df, trend_df, p, offset_hours)
    o, h, l = (f[c].to_numpy(float) for c in ("open", "high", "low"))
    c = f["close"].to_numpy(float)
    spread_pts = f["spread"].to_numpy(float) if "spread" in f else np.zeros(len(f))
    # Bar dengan spread 0 = data kosong, isi dengan median spread nyata simbol ini
    positive = spread_pts[spread_pts > 0]
    fill = float(np.median(positive)) if len(positive) else bt.default_spread_points
    spread_pts = np.where(spread_pts > 0, spread_pts, fill)
    spread = spread_pts * point
    slip = bt.slippage_points * point
    times = f["time"]

    dirs = signal_directions(f, p, settings.session, offset_hours)
    exits = exit_flags(f, p)
    trades: list[BTTrade] = []
    busy_until = 0
    for i in np.flatnonzero(dirs[:-1]):
        if i < busy_until:
            continue
        sig = make_signal(f, i, int(dirs[i]), p, symbol)
        e = i + 1
        if spread_pts[e] > max_spread:
            continue
        d = sig.direction
        entry = o[e] + spread[e] + slip if d == 1 else o[e] - slip
        sl = entry - d * sig.sl_distance
        tp = entry + d * sig.tp_distance if sig.tp_distance > 0 else None
        be = p.breakeven_at_r * sig.sl_distance
        first = dict(be_trigger=be, trail_dist=p.trail_atr_mult * sig.atr,
                     exit_flag=None if exits is None else exits[0 if d == 1 else 1],
                     lock_trigger=p.lock_at_r * sig.sl_distance, lock_fraction=p.lock_fraction)
        if p.partial_r > 0:
            res = simulate_managed(o, h, l, spread, c, e, d, entry, sl, p.partial_r * sig.sl_distance, p.partial_frac,
                                   entry + d * p.tp2_r * sig.sl_distance if p.tp2_r > 0 else None, spread[e],
                                   p.post_trail_atr_mult * sig.atr, p.post_swing_bars, p.max_hold_bars,
                                   keep_sl=p.partial_keep_sl, **first)
        else:
            res = simulate_exit(o, h, l, spread, e, d, entry, sl, tp, close=c, max_bars=p.max_hold_bars, **first)
        if res is None:
            j = len(f) - 1
            exit_price = c[j] if d == 1 else c[j] + spread[j]
            reason = "eod"
        else:
            j, exit_price, reason = res
            exit_price -= d * slip
        r_gross = d * (exit_price - entry) / sig.sl_distance
        # Komisi dalam satuan R: tidak tergantung equity karena lot sebanding dengan risiko
        loss_per_lot = sig.sl_distance / tick_size * tick_value
        cost_r = bt.commission_per_lot / loss_per_lot if loss_per_lot > 0 else 0.0
        trades.append(BTTrade(symbol, d, sig.bar_time, times.iloc[e], times.iloc[j], entry, sl, tp,
                              exit_price, reason, r_gross, cost_r, r_gross - cost_r))
        busy_until = j
    return trades


def _spread_price(f: pd.DataFrame, point: float, default_points: float) -> tuple[np.ndarray, np.ndarray]:
    """(spread dalam point, spread dalam harga) per bar; bar dengan spread 0 = data kosong, diisi median simbol."""
    pts = f["spread"].to_numpy(float) if "spread" in f else np.zeros(len(f))
    positive = pts[pts > 0]
    pts = np.where(pts > 0, pts, float(np.median(positive)) if len(positive) else default_points)
    return pts, pts * point


def _exit_kwargs(pol: ExitPolicy, a: dict[str, np.ndarray], atr_: float) -> dict:
    """Argumen trailing untuk simulate_exit sesuai ExitPolicy.trail."""
    if pol.trail == "atr":
        return {"trail_dist": pol.trail_atr_mult * atr_}
    if pol.trail == "swing":
        return {"swing_bars": pol.swing_bars}
    if pol.trail in ("ema20", "vwap"):
        return {"trail_level": a[pol.trail], "trail_buffer": pol.trail_buffer_atr * atr_}
    return {}


def backtest_intraday(symbol: str, f: pd.DataFrame, cands: pd.DataFrame, meta: dict, settings: Settings,
                      ip: IntradayParams = IntradayParams(), pol: ExitPolicy = ExitPolicy(),
                      room_filter: bool = True, one_position: bool = True) -> pd.DataFrame:
    """Simulasikan kandidat sinyal intraday (bot.intraday) di bar M15. Entry di open bar setelah bar sinyal.
    SL = di luar sl_ref + buffer ATR, dilebarkan sampai jarak minimum (ATR H1 dan batas biaya), dilewati bila
    melebihi jarak maksimum. Biaya: spread per bar, slippage, komisi (dalam R). Kolom hasil = BTTrade +
    strategy, regime, high_vol, session, score, room_r, hold_bars."""
    bt = settings.backtest
    max_spread = settings.symbol_cfg(symbol).max_spread_points
    point, tick_size, tick_value = meta["point"], meta["tick_size"], meta["tick_value"]
    a = {k: f[k].to_numpy() for k in f.columns if k not in ("time", "close_time", "day", "session", "regime")}
    o, h, l, c = (a[k].astype(float) for k in ("open", "high", "low", "close"))
    spread_pts, spread = _spread_price(f, point, bt.default_spread_points)
    slip = bt.slippage_points * point
    commission = bt.commission_per_lot * tick_size / tick_value   # komisi round-turn dalam satuan harga
    times, session, regime, ny_h = f["time"], f["session"].to_numpy(), f["regime"].to_numpy(), a["ny_h"]
    weekday = f["time"].dt.day_of_week.to_numpy()
    rows, busy_until = [], 0
    for cand in cands.itertuples():
        i, d, e = int(cand.i), int(cand.dir), int(cand.i) + 1
        if e >= len(f) or (one_position and i < busy_until):
            continue
        atr_, atr_h1 = float(a["atr"][i]), float(a["h1_atr"][i])
        # Tidak entry saat rollover (spread melebar) dan Jumat sore New York
        if spread_pts[e] > max_spread or 16.5 <= ny_h[e] < 18.5 or (weekday[e] == 4 and ny_h[e] >= 12):
            continue
        entry = o[e] + spread[e] + slip if d == 1 else o[e] - slip
        cost = spread[e] + 2 * slip + commission
        dist = d * (entry - (cand.sl_ref - d * ip.sl_buffer_atr * atr_))
        dist = max(dist, ip.sl_min_atr_h1 * atr_h1, cost / ip.max_cost_r)
        if not np.isfinite(dist) or dist > ip.sl_max_atr_h1 * atr_h1:
            continue
        level = opposing_level(a, i, d, entry, cand.strat)
        room_r = d * (level - entry) / dist if np.isfinite(level) else np.inf
        if room_filter and cand.strat != "E" and room_r < ip.room_r:
            continue
        sl = entry - d * dist
        tp = entry + d * pol.tp_r * dist if pol.tp_r > 0 else None
        if pol.tp_sr and 1.0 <= room_r <= 3.0:
            tp = level
        first = dict(be_trigger=pol.be_at_r * dist, lock_trigger=pol.lock_at_r * dist, lock_fraction=pol.lock_fraction)
        trail = _exit_kwargs(pol, a, atr_)
        if pol.partial_r > 0:
            res = simulate_managed(o, h, l, spread, c, e, d, entry, sl, pol.partial_r * dist, pol.partial_frac,
                                   entry + d * pol.tp2_r * dist if pol.tp2_r > 0 else None, spread[e] + commission,
                                   trail.get("trail_dist", 0.0), trail.get("swing_bars", 0), pol.max_hold_bars,
                                   trail.get("trail_level"), trail.get("trail_buffer", 0.0), **first)
        else:
            res = simulate_exit(o, h, l, spread, e, d, entry, sl, tp, close=c, max_bars=pol.max_hold_bars,
                                **first, **trail)
        if res is None:
            j, exit_price, reason = len(f) - 1, (c[-1] if d == 1 else c[-1] + spread[-1]), "eod"
        else:
            j, exit_price, reason = res
            exit_price -= d * slip
        r_gross = d * (exit_price - entry) / dist
        cost_r = commission / dist
        parts = score_parts(a, i, d, room_r)
        rows.append({"symbol": symbol, "direction": d, "signal_time": times.iloc[i], "entry_time": times.iloc[e],
                     "exit_time": times.iloc[j], "entry": entry, "sl": sl, "tp": tp, "exit_price": exit_price,
                     "reason": reason, "r_gross": r_gross, "cost_r": cost_r, "r_net": r_gross - cost_r,
                     "strategy": cand.strat, "regime": regime[i], "high_vol": bool(a["high_vol"][i]),
                     "session": session[e], "score": sum(parts.values()),
                     "room_r": room_r if np.isfinite(room_r) else np.nan, "hold_bars": j - e + 1,
                     "spread_cost_r": spread[e] / dist})
        busy_until = j
    cols = list(BTTrade.__dataclass_fields__) + ["strategy", "regime", "high_vol", "session", "score", "room_r",
                                                 "hold_bars", "spread_cost_r"]
    return pd.DataFrame(rows, columns=cols)


def to_frame(trades: list[BTTrade]) -> pd.DataFrame:
    cols = list(BTTrade.__dataclass_fields__)
    return pd.DataFrame([asdict(t) for t in trades], columns=cols)


def simulate_portfolio(trades: pd.DataFrame, initial: float, risk: float, max_open: int,
                       start: pd.Timestamp | None = None, max_per_day: int = 0, daily_loss: float = 0.0,
                       stop_after_losses: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    """Gabungkan trade semua simbol: risiko = risk x equity terealisasi saat entry,
    dan trade dilewati jika sudah ada max_open posisi terbuka atau entry hari itu sudah max_per_day (0 = tanpa batas).
    daily_loss > 0: tidak entry lagi hari itu bila rugi terealisasi hari itu >= daily_loss x equity awal hari.
    stop_after_losses > 0: tidak entry lagi hari itu setelah N rugi beruntun. Kolom `size` (opsional) = pengali risiko.
    Return (trade yang diambil + kolom pnl, kurva equity berindeks waktu exit)."""
    if trades.empty:
        return trades.assign(pnl=[]), pd.Series([initial], index=[start or pd.Timestamp(0)])
    t = trades.sort_values("entry_time").reset_index(drop=True)
    equity = initial
    open_: list[tuple[pd.Timestamp, int, float]] = []   # (exit_time, row, risk_amount)
    taken, pnl = [], {}
    curve = [(start or t["entry_time"].iloc[0], initial)]

    day_pnl: dict = {}      # rugi/untung terealisasi per tanggal exit
    streak, halted = 0, None   # rugi beruntun, tanggal entry yang dihentikan karena streak

    def realize(until: pd.Timestamp | None):
        nonlocal equity, streak, halted
        open_.sort(key=lambda x: x[0])
        while open_ and (until is None or open_[0][0] <= until):
            exit_time, row, risk_amt = open_.pop(0)
            pnl[row] = risk_amt * t.at[row, "r_net"]
            equity += pnl[row]
            curve.append((exit_time, equity))
            day_pnl[exit_time.date()] = day_pnl.get(exit_time.date(), 0.0) + pnl[row]
            streak = streak + 1 if pnl[row] < 0 else 0
            if stop_after_losses and streak >= stop_after_losses:
                halted, streak = exit_time.date(), 0

    per_day: dict = {}
    day_start: dict = {}
    size = t["size"].to_numpy(float) if "size" in t else np.ones(len(t))
    for row in t.itertuples():
        realize(row.entry_time)
        day = row.entry_time.date()
        day_start.setdefault(day, equity)
        if len(open_) >= max_open or (max_per_day and per_day.get(day, 0) >= max_per_day):
            continue
        if halted == day or (daily_loss and day_pnl.get(day, 0.0) <= -daily_loss * day_start[day]):
            continue
        per_day[day] = per_day.get(day, 0) + 1
        open_.append((row.exit_time, row.Index, equity * risk * size[row.Index]))
        taken.append(row.Index)
    realize(None)

    out = t.loc[taken].copy()
    out["pnl"] = out.index.map(pnl)
    eq = pd.Series([v for _, v in curve], index=pd.DatetimeIndex([k for k, _ in curve]))
    return out.reset_index(drop=True), eq


def simulate_account(trades: pd.DataFrame, metas: dict[str, dict], initial: float, risk: float,
                     max_risk: float, max_open: int) -> tuple[pd.DataFrame, pd.Series]:
    """Seperti simulate_portfolio, tapi dengan lot nyata (volume_step/volume_min) untuk akun kecil.
    Lot = calc_lot(risk x equity); jika itu di bawah lot minimum, pakai lot minimum selama risikonya
    <= max_risk x equity, selain itu trade dilewati. Berhenti jika equity habis.
    Kolom tambahan: lot, risk_% (risiko sebenarnya saat entry)."""
    from bot.risk import lot_with_min

    if trades.empty:
        return trades.assign(pnl=[], lot=[], **{"risk_%": []}), pd.Series([initial], index=[pd.Timestamp(0)])
    t = trades.sort_values("entry_time").reset_index(drop=True)
    equity = initial
    open_: list[tuple[pd.Timestamp, int, float]] = []   # (exit_time, row, uang per 1R)
    taken, pnl, lots, risks = [], {}, {}, {}
    curve = [(t["entry_time"].iloc[0], initial)]

    def realize(until: pd.Timestamp | None):
        nonlocal equity
        open_.sort(key=lambda x: x[0])
        while open_ and (until is None or open_[0][0] <= until):
            exit_time, row, one_r = open_.pop(0)
            pnl[row] = one_r * t.at[row, "r_net"]
            equity += pnl[row]
            curve.append((exit_time, equity))

    for row in t.itertuples():
        realize(row.entry_time)
        if equity <= 0 or len(open_) >= max_open:
            continue
        m = metas[row.symbol]
        loss_per_lot = abs(row.entry - row.sl) / m["tick_size"] * m["tick_value"]
        lot = lot_with_min(equity, risk, max_risk, abs(row.entry - row.sl), m)
        if lot == 0:
            continue
        open_.append((row.exit_time, row.Index, lot * loss_per_lot))
        taken.append(row.Index)
        lots[row.Index], risks[row.Index] = lot, 100 * lot * loss_per_lot / equity
    realize(None)

    out = t.loc[taken].copy()
    out["pnl"], out["lot"], out["risk_%"] = (out.index.map(d) for d in (pnl, lots, risks))
    eq = pd.Series([v for _, v in curve], index=pd.DatetimeIndex([k for k, _ in curve]))
    return out.reset_index(drop=True), eq


FALLBACK_OFFSET = 2


def load_frames(data_dir: Path, name: str) -> tuple[dict[str, pd.DataFrame], dict]:
    """Baca data/<SYMBOL>_<TF>.csv (hasil download_data.py) + spesifikasi simbol."""
    frames = {tf: pd.read_csv(data_dir / f"{name}_{tf}.csv", parse_dates=["time"])
              for tf in ("M1", "M5", "M15", "M30", "H1", "H4", "D1") if (data_dir / f"{name}_{tf}.csv").exists()}
    meta = json.loads((data_dir / f"{name}_meta.json").read_text(encoding="utf-8"))
    return frames, meta


def run_all(settings: Settings, names: list[str], data_dir: Path) -> pd.DataFrame:
    """Backtest semua simbol dengan settings.strategy. Indikator dihitung dari seluruh data (warm-up);
    filter periode dilakukan pemanggil lewat kolom signal_time."""
    p = settings.strategy
    out = []
    for name in names:
        frames, meta = load_frames(data_dir, name)
        offset = settings.session.server_utc_offset_hours
        if offset is None:
            offset = meta.get("server_utc_offset")
        if offset is None:
            offset = FALLBACK_OFFSET
        out.append(to_frame(backtest_symbol(name, frames[p.entry_tf], frames[p.trend_tf], meta, settings, offset)))
    return pd.concat(out, ignore_index=True) if out else to_frame([])
