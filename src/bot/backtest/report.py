"""Metrik performa & grafik equity curve."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def max_losing_streak(r: pd.Series) -> int:
    best = cur = 0
    for v in r:
        cur = cur + 1 if v < 0 else 0
        best = max(best, cur)
    return best


def metrics(trades: pd.DataFrame, equity: pd.Series | None = None, initial: float | None = None) -> dict:
    """trades harus punya kolom r_net; jika ada kolom pnl dan equity, metrik uang ikut dihitung."""
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    r = trades["r_net"]
    wins, losses = r[r > 0], r[r <= 0]
    gross_loss = -losses.sum()
    out = {
        "trades": n,
        "winrate_%": round(100 * len(wins) / n, 1),
        "profit_factor": round(wins.sum() / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "expectancy_R": round(r.mean(), 3),
        "total_R": round(r.sum(), 1),
        "max_losing_streak": max_losing_streak(r),
    }
    if equity is not None and len(equity) > 1 and initial:
        peak = equity.cummax()
        dd = (equity - peak) / peak
        daily = equity.resample("D").last().ffill()
        rets = daily.pct_change().dropna()
        sharpe = rets.mean() / rets.std() * np.sqrt(252) if len(rets) > 1 and rets.std() > 0 else 0.0
        out.update({
            "net_profit": round(equity.iloc[-1] - initial, 2),
            "return_%": round(100 * (equity.iloc[-1] / initial - 1), 1),
            "max_drawdown_%": round(100 * dd.min(), 1),
            "sharpe": round(float(sharpe), 2),
        })
    return out


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Interval kepercayaan Wilson (default 95%) untuk win rate, dalam persen."""
    if n == 0:
        return 0.0, 0.0
    p = wins / n
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return round(100 * (center - half), 1), round(100 * (center + half), 1)


def extended_metrics(trades: pd.DataFrame, equity: pd.Series | None = None, initial: float | None = None) -> dict:
    """metrics() + rata-rata menang/kalah, interval kepercayaan win rate, drawdown & recovery factor dalam R,
    frekuensi per hari kerja, lama tahan, dan biaya. Butuh kolom entry_time/exit_time; cost_r dan
    spread_cost_r dipakai bila ada."""
    out = metrics(trades, equity, initial)
    n = len(trades)
    if n == 0:
        return out
    t = trades.sort_values("exit_time")
    r = t["r_net"]
    wins, losses = r[r > 0], r[r <= 0]
    lo, hi = wilson_interval(len(wins), n)
    cum = r.cumsum()
    dd_r = float((cum.cummax().clip(lower=0) - cum).max())
    days = max(len(pd.bdate_range(t["entry_time"].min().normalize(), t["entry_time"].max().normalize())), 1)
    cost = t["cost_r"].sum() + (t["spread_cost_r"].sum() if "spread_cost_r" in t else 0.0)
    out.update({
        "winrate_ci95": f"{lo}-{hi}",
        "avg_win_R": round(wins.mean(), 2) if len(wins) else 0.0,
        "avg_loss_R": round(losses.mean(), 2) if len(losses) else 0.0,
        "max_dd_R": round(dd_r, 1),
        "recovery_factor": round(r.sum() / dd_r, 2) if dd_r > 0 else float("inf"),
        "trades_per_day": round(n / days, 2),
        "avg_hold_h": round((t["exit_time"] - t["entry_time"]).dt.total_seconds().mean() / 3600, 1),
        "cost_R": round(float(cost), 1),
    })
    if "r_gross" in t:   # sebelum komisi (spread & slippage sudah termasuk di harga entry/exit)
        out["exp_before_commission_R"] = round(t["r_gross"].mean(), 3)
    return out


def breakdown(trades: pd.DataFrame, by: str | list[str]) -> pd.DataFrame:
    """extended_metrics per kelompok (mis. per sesi, regime, strategi)."""
    rows = {k if isinstance(k, str) else " | ".join(map(str, k)) if isinstance(k, tuple) else str(k):
            extended_metrics(g) for k, g in trades.groupby(by, observed=True)}
    return pd.DataFrame(rows).T


def print_table(rows: dict[str, dict]) -> str:
    df = pd.DataFrame(rows).T
    text = df.to_string()
    print(text)
    return text


def plot_equity(equity: pd.Series, path: Path, title: str) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    path.parent.mkdir(parents=True, exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 6), sharex=True, height_ratios=[3, 1])
    ax1.plot(equity.index, equity.values, color="#2a6fdb", linewidth=1.4)
    ax1.set_title(title)
    ax1.set_ylabel("Equity")
    ax1.grid(alpha=0.3)
    dd = 100 * (equity - equity.cummax()) / equity.cummax()
    ax2.fill_between(dd.index, dd.values, 0, color="#d9534f", alpha=0.5)
    ax2.set_ylabel("Drawdown %")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path
