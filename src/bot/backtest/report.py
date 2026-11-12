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
