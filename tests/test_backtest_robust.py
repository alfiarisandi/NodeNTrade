"""Backtest lebih realistis (audit 2026-11-16, Commit D): swap broker, rollover, stress biaya, entry terlambat,
Monte Carlo, ketergantungan pada trade terbaik, dan kriteria OOS."""
import numpy as np
import pandas as pd
import pytest

from bot.backtest.engine import backtest_symbol, rollover_nights, swap_r, to_frame
from bot.backtest.robustness import criteria, monte_carlo, r_stats, stressed, without_top

MON = pd.Timestamp("2026-01-12 10:00")   # Senin, jam server


def test_rollover_nights_with_wednesday_triple_and_weekend():
    assert rollover_nights(MON, MON + pd.Timedelta(hours=8)) == 0                       # tutup hari yang sama
    assert rollover_nights(MON, MON + pd.Timedelta(days=1)) == 1                        # Senin malam
    assert rollover_nights(MON + pd.Timedelta(days=2), MON + pd.Timedelta(days=3)) == 3  # Rabu malam = 3x
    assert rollover_nights(MON + pd.Timedelta(days=1), MON + pd.Timedelta(days=3)) == 4  # Selasa 1 + Rabu 3
    assert rollover_nights(MON + pd.Timedelta(days=4), MON + pd.Timedelta(days=7)) == 1  # Jumat malam saja
    assert rollover_nights(MON, MON + pd.Timedelta(days=7)) == 7                         # satu minggu penuh
    assert rollover_nights(MON + pd.Timedelta(days=1), MON + pd.Timedelta(days=2), triple_mt5_day=2) == 3   # Selasa


def test_swap_r_modes_long_short():
    base = {"point": 0.01, "tick_size": 0.01, "tick_value": 1.0, "swap_rollover3days": 3}
    one_night = (MON, MON + pd.Timedelta(days=1))
    # mode 5 (bunga % per tahun, Monex XAUUSD): long -5.16%, short +3.16%; harga 3600, SL 30 -> 0.172 / 30 R
    xau = {**base, "swap_mode": 5, "swap_long": -5.16, "swap_short": 3.16}
    assert swap_r(xau, 1, 3600.0, 30.0, *one_night) == pytest.approx(-3600 * 0.0516 / 360 / 30)
    assert swap_r(xau, -1, 3600.0, 30.0, *one_night) == pytest.approx(3600 * 0.0316 / 360 / 30)
    # mode 1 (point per malam): -40 point x 0.01 = -0.4 harga, SL 20 -> -0.02 R per malam, Rabu 3x
    pts = {**base, "swap_mode": 1, "swap_long": -40.0, "swap_short": 10.0}
    assert swap_r(pts, 1, 2000.0, 20.0, MON + pd.Timedelta(days=2), MON + pd.Timedelta(days=3)) == pytest.approx(-0.06)
    # mode 4 (uang per lot): -$5/lot/malam, rugi di SL per lot = 20 / 0.01 x 1 = $2000 -> -0.0025 R
    money = {**base, "swap_mode": 4, "swap_long": -5.0, "swap_short": 1.0}
    assert swap_r(money, 1, 2000.0, 20.0, *one_night) == pytest.approx(-0.0025)
    assert swap_r(base, 1, 2000.0, 20.0, *one_night) == 0.0          # meta lama tanpa data swap
    assert swap_r({**xau, "swap_mode": 7}, 1, 2000.0, 20.0, *one_night) == 0.0   # mode tidak didukung


def test_backtest_applies_swap_and_entry_delay(uptrend_waves, settings, eurusd_meta):
    h1, h4 = uptrend_waves
    meta = {**eurusd_meta, "swap_mode": 5, "swap_long": -3.25, "swap_short": 0.25, "swap_rollover3days": 3}
    base = to_frame(backtest_symbol("EURUSD", h1, h4, eurusd_meta, settings, 0))
    sw = to_frame(backtest_symbol("EURUSD", h1, h4, meta, settings, 0))
    assert (base["swap_r"] == 0).all() and (sw["r_gross"] == base["r_gross"]).all()
    held = sw[sw["exit_time"].dt.normalize() > sw["entry_time"].dt.normalize()]
    assert len(held) and (held.loc[held["direction"] == 1, "swap_r"] < 0).all()
    assert np.allclose(sw["r_net"], sw["r_gross"] - sw["cost_r"] + sw["swap_r"])
    assert (sw["exec_cost_r"] > 0).all()                               # spread + slippage dalam R
    late = to_frame(backtest_symbol("EURUSD", h1, h4, meta, settings, 0, entry_delay=1))
    first, first_late = base.iloc[0], late.iloc[0]
    assert first_late["signal_time"] == first["signal_time"] and first_late["entry_time"] > first["entry_time"]


def trades(r, cost=0.05, exec_cost=0.1):
    return pd.DataFrame({"r_net": r, "cost_r": cost, "exec_cost_r": exec_cost})


def test_r_stats_stress_and_concentration():
    r = [2.0, -1.0, -1.0, 3.0, -1.0, -1.0, 8.0]
    st = r_stats(r)
    assert (st["n"], st["total_R"], st["pf"], st["max_dd_R"], st["max_loss_streak"]) == (7, 9.0, 3.25, 2.0, 2)
    t = trades(r)
    assert np.allclose(stressed(t, 2), np.array(r) - 0.15)            # seluruh biaya ikut dikali
    assert np.allclose(stressed(t, 3), np.array(r) - 0.30)
    assert without_top(r, 1)["total_R"] == 1.0 and without_top(r, 2)["total_R"] == -2.0   # bergantung trade besar
    # urutan waktu sisa trade tetap: [2, -1, -1, -1, -1] -> DD 4R, rugi beruntun 4 (bukan diurutkan ulang)
    assert (without_top(r, 2)["max_dd_R"], without_top(r, 2)["max_loss_streak"]) == (4.0, 4)
    assert r_stats([])["n"] == 0


def test_monte_carlo_deterministic_and_sensible():
    rng = np.random.default_rng(1)
    good = rng.normal(0.3, 1.0, 150)
    a, b = monte_carlo(good), monte_carlo(good)
    assert a == b and a["p_total_le_0_%"] < 5 and a["dd_p95_R"] >= a["dd_median_R"] > 0
    assert monte_carlo(-np.abs(good))["p_total_le_0_%"] == 100.0
    assert monte_carlo([1.0]) == {}


def test_oos_criteria_reported_honestly():
    rng = np.random.default_rng(3)
    edge = trades(rng.normal(0.35, 1.0, 120))
    cr = criteria(edge)
    assert cr["pass"] and all(cr["checks"].values())
    lucky = trades([-1.0] * 40 + [30.0, 25.0] + [0.5] * 20)            # untung hanya dari 2 trade
    cr = criteria(lucky)
    assert not cr["pass"] and not cr["checks"]["tetap untung tanpa 2 trade terbaik"]
    thin = trades(rng.normal(0.08, 1.0, 120))                          # tipis: gagal setelah biaya x2
    assert criteria(thin)["pf_stressed"] < criteria(edge)["pf_stressed"]
