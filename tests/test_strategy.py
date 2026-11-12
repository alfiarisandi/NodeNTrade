import numpy as np
import pandas as pd

from bot.config import SessionCfg
from bot.strategy import compute_features, in_session, signal_at, signal_directions, simulate_exit


def test_signals_follow_h4_trend(uptrend_waves, settings):
    h1, h4 = uptrend_waves
    f = compute_features(h1, h4, settings.strategy)
    dirs = signal_directions(f, settings.strategy, settings.session, 0)
    sigs = [signal_at(f, i, settings.strategy, settings.session, 0, "EURUSD") for i in np.flatnonzero(dirs)]
    assert sigs, "harus ada sinyal di tren naik bergelombang"
    assert all(s.direction == 1 for s in sigs), "tidak boleh ada sell saat H4 di atas EMA200"
    for s in sigs:
        assert np.isclose(s.tp_distance / s.sl_distance, 2.0)


def test_h4_trend_has_no_lookahead(uptrend_waves, settings):
    h1, h4 = uptrend_waves
    f = compute_features(h1, h4, settings.strategy)
    valid = f.dropna(subset=["avail_time"])
    assert (valid["avail_time"] <= valid["close_time"]).all()


def test_session_filter():
    s = SessionCfg(start_hour_utc=7, end_hour_utc=20, friday_cutoff_hour_utc=18)
    # server GMT+2: 10:00 server = 08:00 UTC Senin
    assert in_session(pd.Timestamp("2026-01-05 10:00"), 2, s)
    assert not in_session(pd.Timestamp("2026-01-05 08:00"), 2, s)       # 06:00 UTC
    assert not in_session(pd.Timestamp("2026-01-09 21:00"), 2, s)       # Jumat 19:00 UTC
    assert not in_session(pd.Timestamp("2026-01-10 12:00"), 2, s)       # Sabtu


def _arr(*v):
    return np.array(v, float)


def test_exit_buy_tp():
    o, h, l = _arr(1.0, 1.0), _arr(1.01, 1.05), _arr(0.99, 0.995)
    assert simulate_exit(o, h, l, np.zeros(2), 0, 1, 1.0, 0.98, 1.04) == (1, 1.04, "tp")


def test_exit_sl_first_when_both_hit_same_bar():
    o, h, l = _arr(1.0), _arr(1.05), _arr(0.97)
    assert simulate_exit(o, h, l, np.zeros(1), 0, 1, 1.0, 0.98, 1.04)[2] == "sl"


def test_exit_breakeven_after_1r():
    # bar 0: naik 1R (+0.02) -> SL ke entry; bar 1: turun ke bawah entry -> keluar breakeven
    o, h, l = _arr(1.0, 1.015), _arr(1.021, 1.016), _arr(0.995, 0.99)
    j, price, reason = simulate_exit(o, h, l, np.zeros(2), 0, 1, 1.0, 0.98, 1.04, be_trigger=0.02)
    assert (j, price, reason) == (1, 1.0, "be")


def test_exit_sell_uses_ask():
    # sell entry 1.0 (bid), SL 1.02. high bid 1.015 + spread 0.006 = ask 1.021 -> kena SL
    o, h, l = _arr(1.0), _arr(1.015), _arr(0.995)
    assert simulate_exit(o, h, l, _arr(0.006), 0, -1, 1.0, 1.02, 0.96)[2] == "sl"
    assert simulate_exit(o, h, l, _arr(0.0), 0, -1, 1.0, 1.02, 0.96) is None


def test_exit_gap_through_stop_fills_at_open():
    o, h, l = _arr(0.97), _arr(0.975), _arr(0.96)
    assert simulate_exit(o, h, l, np.zeros(1), 0, 1, 1.0, 0.98, 1.04) == (0, 0.97, "sl")


def test_trailing_stop_locks_profit():
    # naik ke 1.05 lalu turun: trailing 0.02 dari high 1.05 -> SL 1.03, keluar di 1.03 (di atas entry)
    o, h, l = _arr(1.0, 1.04), _arr(1.05, 1.045), _arr(0.995, 1.02)
    j, price, reason = simulate_exit(o, h, l, np.zeros(2), 0, 1, 1.0, 0.98, None, trail_dist=0.02)
    assert (j, round(price, 6), reason) == (1, 1.03, "trail")


def test_profit_lock_follows_max_profit():
    # bar 0: naik 2R (high 1.04) >= pemicu 1R -> SL dikunci di 50% x 0.04 = 1.02 (berlaku bar berikutnya)
    o, h, l = _arr(1.0, 1.035), _arr(1.04, 1.038), _arr(0.995, 1.01)
    res = simulate_exit(o, h, l, np.zeros(2), 0, 1, 1.0, 0.98, None, lock_trigger=0.02, lock_fraction=0.5)
    assert (res[0], round(res[1], 6), res[2]) == (1, 1.02, "lock")
    # profit maksimum 0.5R < pemicu: SL tidak disentuh, posisi tetap jalan
    o, h, l = _arr(1.0, 1.005), _arr(1.01, 1.006), _arr(0.995, 0.99)
    assert simulate_exit(o, h, l, np.zeros(2), 0, 1, 1.0, 0.98, None, lock_trigger=0.02, lock_fraction=0.5) is None


def test_no_tp_runs_until_stop():
    o, h, l = _arr(1.0, 1.1, 1.2), _arr(1.1, 1.2, 1.3), _arr(0.99, 1.09, 1.19)
    assert simulate_exit(o, h, l, np.zeros(3), 0, 1, 1.0, 0.98, None) is None


def test_donchian_breakout_signal(settings):
    from conftest import make_bars, resample_h4
    # tren naik bergelombang (masih di bawah high channel), lalu breakout tajam di bar terakhir (Rabu 12:00)
    t = np.arange(1572)
    base = 1.10 + 0.00002 * t + 0.002 * np.sin(t / 10)
    close = np.r_[base, base.max() + 0.01]
    h1 = make_bars(close)
    assert h1["time"].iloc[-1].weekday() == 2
    p = settings.strategy.model_copy(update={"entry_type": "donchian", "donchian_period": 55,
                                             "adx_min": 0, "rsi_buy_max": 101})
    f = compute_features(h1, resample_h4(h1), p)
    sig = signal_at(f, len(f) - 1, p, settings.session, 0, "EURUSD")
    assert sig is not None and sig.direction == 1


def test_confirmation_filters_only_remove_signals(uptrend_waves, settings):
    h1, h4 = uptrend_waves
    base = settings.strategy.model_copy(update={"trend_filter": "none", "adx_min": 0})
    f = compute_features(h1, h4, base)
    plain = signal_directions(f, base, settings.session, 0)
    for upd, col_a, col_b in [({"ma_filter": True}, "close", "ema_trend"), ({"macd_filter": True}, "macd", "macd_sig")]:
        p = base.model_copy(update=upd)
        d = signal_directions(compute_features(h1, h4, p), p, settings.session, 0)
        on = d != 0
        assert on.sum() > 0 and on.sum() < (plain != 0).sum()
        assert (d[on] == plain[on]).all()                               # tidak ada sinyal baru
        assert (d[on] * (f[col_a].to_numpy()[on] - f[col_b].to_numpy()[on]) > 0).all()
    p = base.model_copy(update={"atr_ratio_min": 1.2})
    d = signal_directions(compute_features(h1, h4, p), p, settings.session, 0)
    assert (f["atr_ratio"].to_numpy()[d != 0] >= 1.2).all()


def _day_bars(days: int, offset: int = 2):
    """Bar H1 jam server (UTC+offset); harga berosilasi, lalu naik tajam jam 08-11 UTC setiap hari."""
    times = pd.date_range("2026-01-05", periods=24 * days, freq="h") + pd.Timedelta(hours=offset)
    utc_hour = (times - pd.Timedelta(hours=offset)).hour
    jump = (utc_hour >= 8) & (utc_hour < 12)
    close = 1.10 + 0.0005 * np.sin(np.arange(len(times)) / 3) + np.where(jump, 0.003, 0.0)
    from conftest import make_bars

    bars = make_bars(close)
    bars["time"] = times
    return bars


def test_range_breakout_one_signal_per_day_no_lookahead(settings):
    from bot.strategy import _add_day_levels

    p = settings.strategy.model_copy(update={"entry_type": "range_breakout", "range_start_utc": 0,
                                             "range_end_utc": 7, "entry_window_end_utc": 11})
    bars = _day_bars(5)
    f = bars.copy()
    _add_day_levels(f, p, 2)
    utc = f["time"] - pd.Timedelta(hours=2)
    per_day = (f["brk"] != 0).groupby(utc.dt.floor("D")).sum()
    assert per_day.max() == 1 and per_day.sum() >= 4
    assert (f.loc[f["brk"] != 0, "brk"] == 1).all()
    hits = utc[f["brk"] != 0].dt.hour
    assert ((hits >= 7) & (hits < 11)).all()
    # Level di hari pertama tidak berubah bila bar setelah jendela entry diubah (tanpa look-ahead)
    g = bars.copy()
    later = (utc.dt.hour >= 11).to_numpy()
    g.loc[later, ["high", "close"]] += 0.05
    _add_day_levels(g, p, 2)
    win = ((utc.dt.hour >= 7) & (utc.dt.hour < 11)).to_numpy()
    assert np.allclose(f.loc[win, "lvl_hi"], g.loc[win, "lvl_hi"])


def test_ma50_and_candle_confirm_filters(uptrend_waves, settings):
    h1, h4 = uptrend_waves
    base = settings.strategy
    f = compute_features(h1, h4, base)
    n0 = np.count_nonzero(signal_directions(f, base, settings.session, 0))
    n1 = np.count_nonzero(signal_directions(f, base.model_copy(update={"ma50_filter": True}), settings.session, 0))
    n2 = np.count_nonzero(signal_directions(f, base.model_copy(update={"candle_confirm": "any"}), settings.session, 0))
    n3 = np.count_nonzero(signal_directions(f, base.model_copy(update={"candle_confirm": "engulfing"}), settings.session, 0))
    assert n0 > 0 and n1 <= n0 and 0 < n2 < n0 and n3 <= n2


def test_partial_tp_then_breakeven_and_trailing():
    from bot.strategy import simulate_managed
    # buy 1.00, SL 0.98 (R = 0.02). bar0: sentuh TP1 1.02 -> 50% keluar. bar1: turun ke entry -> sisa keluar di BE.
    o, h, l, c = _arr(1.0, 1.015, 1.0), _arr(1.021, 1.016, 1.0), _arr(0.995, 0.999, 0.99), _arr(1.02, 1.0, 0.99)
    j, price, why = simulate_managed(o, h, l, np.zeros(3), c, 0, 1, 1.0, 0.98, 0.02, 0.5)
    assert (j, round(price, 6), why) == (1, 1.01, "partial+be")        # +0.5R total, tetap menang
    # sisa posisi lari ke TP2 1.06: 0.5 x 1.02 + 0.5 x 1.06
    o, h, l, c = _arr(1.0, 1.03, 1.05), _arr(1.021, 1.05, 1.07), _arr(0.995, 1.025, 1.04), _arr(1.02, 1.05, 1.06)
    j, price, why = simulate_managed(o, h, l, np.zeros(3), c, 0, 1, 1.0, 0.98, 0.02, 0.5, tp2=1.06)
    assert (j, round(price, 6), why) == (2, 1.04, "partial+tp")
    # SL kena sebelum TP1: hasil sama dengan simulate_exit biasa
    o, h, l, c = _arr(1.0), _arr(1.01), _arr(0.97), _arr(0.98)
    assert simulate_managed(o, h, l, np.zeros(1), c, 0, 1, 1.0, 0.98, 0.02, 0.5) == (0, 0.98, "sl")
    # trailing swing 2 bar setelah TP1: SL naik ke low terendah 2 bar terakhir
    o, h, l, c = _arr(1.0, 1.03, 1.04, 1.045), _arr(1.021, 1.045, 1.05, 1.046), _arr(0.995, 1.028, 1.035, 1.02), \
        _arr(1.02, 1.04, 1.045, 1.03)
    j, price, why = simulate_managed(o, h, l, np.zeros(4), c, 0, 1, 1.0, 0.98, 0.02, 0.5, post_swing_bars=2)
    assert (j, why) == (3, "partial+trail") and round(price, 6) == round(0.5 * 1.02 + 0.5 * 1.028, 6)
