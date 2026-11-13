import numpy as np
import pandas as pd
import pytest

from bot.backtest.engine import backtest_intraday, simulate_portfolio
from bot.backtest.report import extended_metrics, wilson_interval
from bot.indicators import session_vwap
from bot.intraday import (SCORE_WEIGHTS, ExitPolicy, IntradayParams, all_signals, hybrid, mtf_features,
                          server_to_utc, swing_state)
from bot.strategy import simulate_exit
from tests.conftest import make_bars


def resample(m15: pd.DataFrame, rule: str) -> pd.DataFrame:
    g = m15.set_index("time").resample(rule)
    return pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                         "close": g["close"].last(), "tick_volume": g["tick_volume"].sum(),
                         "spread": g["spread"].max()}).dropna().reset_index()


def frames_from(m15: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {"M15": m15, "H1": resample(m15, "1h"), "H4": resample(m15, "4h")}


@pytest.fixture(scope="module")
def m15():
    """Gelombang tren + noise selama ~4 bulan bar M15 (hari kerja saja), cukup untuk warm-up EMA200 H4."""
    n = 12000
    t = np.arange(n)
    rng = np.random.default_rng(3)
    close = 1.10 + 0.01 * np.sin(t / 900) + 0.002 * np.sin(t / 40) + np.cumsum(rng.normal(0, 0.00025, n))
    bars = make_bars(close, start="2024-01-01", freq="15min", wick=0.0003, spread=2)
    bars["tick_volume"] = rng.integers(50, 500, n)
    return bars[bars["time"].dt.day_of_week < 5].reset_index(drop=True)


@pytest.fixture(scope="module")
def feats(m15):
    return mtf_features(frames_from(m15))


def test_no_lookahead_features_and_signals(m15, feats):
    cut = len(m15) - 700
    part = mtf_features(frames_from(m15.iloc[:cut].reset_index(drop=True)))
    pd.testing.assert_frame_equal(feats.iloc[:cut].reset_index(drop=True), part, check_exact=False, rtol=1e-9)
    full_sig, part_sig = all_signals(feats), all_signals(part)
    assert len(part_sig) > 20   # data sintetis memang menghasilkan sinyal
    pd.testing.assert_frame_equal(full_sig[full_sig["i"] < cut].reset_index(drop=True), part_sig)


def test_pivot_visible_only_after_confirmation():
    high = np.array([1, 2, 3, 9, 3, 2, 1, 1, 1, 1], float)
    st = swing_state(high, high - 0.5, high - 0.2, pivot=3)
    assert np.isnan(st["last_ph"][:6]).all()        # pivot di bar 3 baru terlihat di bar 6
    assert st["last_ph"][6] == 9


def test_session_vwap_resets_each_session():
    price = pd.Series([10.0, 20.0, 30.0, 100.0, 200.0])
    day = pd.Series([1, 1, 1, 2, 2])
    v = session_vwap(price, price, price, pd.Series([1, 1, 2, 1, 3]), day)
    assert v.tolist() == [10.0, 15.0, 22.5, 100.0, 175.0]


def test_server_time_follows_us_dst():
    t = pd.Series(pd.to_datetime(["2026-01-15 12:00", "2026-07-15 12:00"]))
    assert server_to_utc(t).tolist() == list(pd.to_datetime(["2026-01-15 10:00", "2026-07-15 09:00"]))


def test_trail_level_only_tightens():
    o = np.array([100.0, 100.0, 100.0, 100.0])
    h, lo, sp = o + 1, np.array([99.5, 99.5, 99.0, 97.0]), np.zeros(4)
    level = np.array([98.0, 90.0, 90.0, 90.0])      # level turun setelah bar 0: SL tidak boleh ikut turun
    j, price, _ = simulate_exit(o, h, lo, sp, 0, 1, 100.0, 95.0, None, trail_level=level)
    assert (j, price) == (3, 98.0)
    # sell: SL mengikuti level + spread, hanya turun
    hi_s = np.array([100.5, 100.5, 101.0, 103.0])
    j, price, _ = simulate_exit(o, hi_s, o - 1, sp, 0, -1, 100.0, 105.0, None,
                                trail_level=np.array([102.0, 110.0, 110.0, 110.0]))
    assert (j, price) == (3, 102.0)


def test_backtest_intraday_costs_score_and_sl_bounds(feats, settings, eurusd_meta):
    ip = IntradayParams()
    cands = all_signals(feats, ip)
    assert set(cands["strat"]) <= set("ABCDE") and len(hybrid(cands)) <= len(cands)
    t = backtest_intraday("EURUSD", feats, cands[cands["strat"] == "C"], eurusd_meta, settings, ip, ExitPolicy())
    assert len(t) > 0
    assert sum(SCORE_WEIGHTS.values()) == 100 and t["score"].between(0, 100).all()
    assert (t["r_net"] < t["r_gross"]).all()
    assert (t["cost_r"] + t["spread_cost_r"] <= ip.max_cost_r + 1e-9).all()
    assert (t["entry_time"].iloc[1:].values >= t["exit_time"].iloc[:-1].values).all()   # satu posisi per simbol
    assert (t["entry_time"] > t["signal_time"]).all()                                   # entry di bar berikutnya
    m = extended_metrics(t)
    assert m["trades"] == len(t) and m["max_dd_R"] >= 0


def test_portfolio_daily_stops():
    entries = pd.to_datetime(["2024-01-01 08:00", "2024-01-01 09:00", "2024-01-01 10:00", "2024-01-02 08:00"])
    t = pd.DataFrame({"symbol": "EURUSD", "entry_time": entries, "exit_time": entries + pd.Timedelta(minutes=30),
                      "r_net": [-1.0, -1.0, 1.0, 1.0]})
    taken, _ = simulate_portfolio(t, 1000, 0.01, 5, stop_after_losses=2)
    assert list(taken["entry_time"]) == [entries[0], entries[1], entries[3]]     # berhenti setelah 2 rugi beruntun
    taken, _ = simulate_portfolio(t, 1000, 0.01, 5, daily_loss=0.01)
    assert list(taken["entry_time"]) == [entries[0], entries[3]]                 # rugi 1% = batas harian
    half, eq = simulate_portfolio(t.assign(size=0.5), 1000, 0.01, 5)
    assert abs(half["pnl"].iloc[0] + 5.0) < 1e-9                                 # size 0.5 = setengah risiko


def test_wilson_interval():
    lo, hi = wilson_interval(50, 100)
    assert 39 < lo < 41 and 59 < hi < 61
    assert wilson_interval(0, 0) == (0.0, 0.0)
