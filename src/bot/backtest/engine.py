"""Backtest bar-by-bar untuk lapisan teknikal (tanpa AI)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from bot.config import Settings
from bot.strategy import compute_features, exit_flags, make_signal, signal_directions, simulate_exit


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
        res = simulate_exit(o, h, l, spread, e, d, entry, sl, tp, be, p.trail_atr_mult * sig.atr,
                            c, p.max_hold_bars, None if exits is None else exits[0 if d == 1 else 1])
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


def to_frame(trades: list[BTTrade]) -> pd.DataFrame:
    cols = list(BTTrade.__dataclass_fields__)
    return pd.DataFrame([asdict(t) for t in trades], columns=cols)


def simulate_portfolio(trades: pd.DataFrame, initial: float, risk: float, max_open: int,
                       start: pd.Timestamp | None = None) -> tuple[pd.DataFrame, pd.Series]:
    """Gabungkan trade semua simbol: risiko = risk x equity terealisasi saat entry,
    dan trade dilewati jika sudah ada max_open posisi terbuka.
    Return (trade yang diambil + kolom pnl, kurva equity berindeks waktu exit)."""
    if trades.empty:
        return trades.assign(pnl=[]), pd.Series([initial], index=[start or pd.Timestamp(0)])
    t = trades.sort_values("entry_time").reset_index(drop=True)
    equity = initial
    open_: list[tuple[pd.Timestamp, int, float]] = []   # (exit_time, row, risk_amount)
    taken, pnl = [], {}
    curve = [(start or t["entry_time"].iloc[0], initial)]

    def realize(until: pd.Timestamp | None):
        nonlocal equity
        open_.sort(key=lambda x: x[0])
        while open_ and (until is None or open_[0][0] <= until):
            exit_time, row, risk_amt = open_.pop(0)
            pnl[row] = risk_amt * t.at[row, "r_net"]
            equity += pnl[row]
            curve.append((exit_time, equity))

    for row in t.itertuples():
        realize(row.entry_time)
        if len(open_) >= max_open:
            continue
        open_.append((row.exit_time, row.Index, equity * risk))
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
