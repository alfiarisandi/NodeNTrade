"""Uji ketahanan hasil backtest per pair dan periode: biaya dinaikkan (stress), urutan trade diacak (Monte Carlo),
ketergantungan pada trade terbaik, dan kriteria kelayakan. Semua dalam R bersih (spread, slippage, komisi, swap)."""
from __future__ import annotations

import numpy as np
import pandas as pd

MC_RUNS = 5000
MC_SEED = 7


def r_stats(r) -> dict:
    """Ringkasan deret R per trade (urut waktu): jumlah, win rate, PF, expectancy, total, DD maks (R), rugi beruntun."""
    r = np.asarray(r, float)
    if len(r) == 0:
        return {"n": 0, "winrate_%": None, "pf": None, "exp_R": None, "total_R": 0.0, "max_dd_R": 0.0, "max_loss_streak": 0}
    eq = np.cumsum(r)
    dd = float((np.maximum.accumulate(np.r_[0.0, eq]) - np.r_[0.0, eq]).max())
    gl = -r[r < 0].sum()
    streak = best = 0
    for x in r:
        streak = streak + 1 if x < 0 else 0
        best = max(best, streak)
    return {"n": len(r), "winrate_%": round(100 * float((r > 0).mean()), 1),
            "pf": round(float(r[r > 0].sum() / gl), 2) if gl > 0 else None,
            "exp_R": round(float(r.mean()), 3), "total_R": round(float(r.sum()), 1), "max_dd_R": round(dd, 1),
            "max_loss_streak": best}


def stressed(trades: pd.DataFrame, mult: float) -> pd.Series:
    """R bersih bila seluruh biaya (spread + slippage + komisi) dikali `mult`. Swap tidak dikali."""
    extra = (mult - 1) * (trades["cost_r"] + trades.get("exec_cost_r", 0.0))
    return trades["r_net"] - extra


def without_top(r, k: int) -> dict:
    """Hasil tanpa k trade terbaik (urutan waktu sisa trade dipertahankan, supaya drawdown tetap bermakna):
    apakah profit hanya datang dari satu-dua trade besar."""
    r = np.asarray(r, float)
    drop = set(np.argsort(r)[::-1][:k].tolist())
    return r_stats([x for i, x in enumerate(r) if i not in drop])


def monte_carlo(r, runs: int = MC_RUNS, seed: int = MC_SEED) -> dict:
    """Bootstrap urutan trade (dengan pengembalian): sebaran total R dan drawdown maksimum."""
    r = np.asarray(r, float)
    if len(r) < 2:
        return {}
    rng = np.random.default_rng(seed)
    s = rng.choice(r, size=(runs, len(r)), replace=True)
    eq = np.cumsum(s, axis=1)
    peak = np.maximum.accumulate(np.concatenate([np.zeros((runs, 1)), eq], axis=1), axis=1)[:, 1:]
    dd = (peak - eq).max(axis=1)
    tot = eq[:, -1]
    return {"p_total_le_0_%": round(100 * float((tot <= 0).mean()), 1), "total_p5": round(float(np.percentile(tot, 5)), 1),
            "total_median": round(float(np.median(tot)), 1), "dd_median_R": round(float(np.median(dd)), 1),
            "dd_p95_R": round(float(np.percentile(dd, 95)), 1)}


def criteria(oos: pd.DataFrame, min_pf: float = 1.2, cost_mult: float = 2.0, top_k: int = 2,
             max_dd_r: float = 15.0) -> dict:
    """Kriteria kelayakan out-of-sample (setelah swap): PF > min_pf dengan biaya x cost_mult, expectancy > 0,
    drawdown maks <= max_dd_r R (15R = 7,5% pada risiko 0,5%), dan masih untung tanpa top_k trade terbaik."""
    base = r_stats(oos["r_net"])
    st = r_stats(stressed(oos, cost_mult))
    top = without_top(oos["r_net"], top_k)
    checks = {
        f"PF OOS > {min_pf:g} dengan biaya x{cost_mult:g}": st["pf"] is not None and st["pf"] > min_pf,
        "expectancy > 0": base["exp_R"] is not None and base["exp_R"] > 0,
        f"DD maks <= {max_dd_r:g}R": base["max_dd_R"] <= max_dd_r,
        f"tetap untung tanpa {top_k} trade terbaik": top["total_R"] > 0,
    }
    return {"checks": checks, "pass": all(checks.values()), "pf_stressed": st["pf"], "exp_R": base["exp_R"],
            "max_dd_R": base["max_dd_R"], f"total_R_without_top{top_k}": top["total_R"]}
