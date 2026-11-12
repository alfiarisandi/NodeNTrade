import numpy as np
import pandas as pd

from bot.indicators import supertrend
from bot.strategy import compute_features, exit_flags, signal_directions, simulate_exit
from conftest import make_bars


def test_supertrend_flips_on_reversal():
    up = np.linspace(1.0, 1.2, 150)
    close = np.r_[up, np.linspace(1.2, 1.0, 150)]
    b = make_bars(close)
    d = supertrend(b["high"], b["low"], b["close"])
    assert d.iloc[140] == 1
    assert d.iloc[-1] == -1


def test_signal_exit_closes_at_bar_close():
    o, h, l, c = (np.array(v, float) for v in ([1.0, 1.01], [1.02, 1.03], [0.99, 1.0], [1.01, 1.02]))
    flag = np.array([False, True])
    assert simulate_exit(o, h, l, np.zeros(2), 0, 1, 1.0, 0.9, None, close=c, exit_flag=flag) == (1, 1.02, "signal")


def _setting(settings, **kw):
    base = {"trend_filter": "none", "adx_min": 0, "session_hours": False, "exit_rule": "signal",
            "entry_tf": "D1", "trend_tf": "D1"}
    return settings.strategy.model_copy(update={**base, **kw})


def test_tsmom_enters_and_exits_with_return_sign(settings):
    # 300 hari naik lalu turun tajam: sinyal buy di awal tren, flag exit long saat return 63 hari negatif
    close = np.r_[np.linspace(1.0, 1.3, 400), np.linspace(1.3, 1.0, 200)]
    d1 = make_bars(close, freq="D")
    p = _setting(settings, entry_type="tsmom", tsmom_lookback=63)
    f = compute_features(d1, d1, p)
    dirs = signal_directions(f, p, settings.session, 0)
    xl, _ = exit_flags(f, p)
    assert (dirs == -1).any()
    assert xl[-1] and not xl[350]


def test_squeeze_fires_after_compression(settings):
    rng = np.random.default_rng(3)
    quiet = 1.1 + rng.normal(0, 0.0002, 300)
    burst = quiet[-1] + np.cumsum(np.full(30, 0.002))
    h4 = make_bars(np.r_[quiet, burst], freq="4h", wick=0.0001)
    p = _setting(settings, entry_type="squeeze", entry_tf="H4")
    f = compute_features(h4, h4.assign(time=h4["time"]).iloc[::6].reset_index(drop=True), p)
    assert f["sqz_on"].iloc[250]
    dirs = signal_directions(f.assign(trend=1.0), p, settings.session, 0)
    assert (dirs[300:] == 1).any() and not (dirs == -1).any()


def test_exit_flags_none_for_stop_based(settings):
    f = pd.DataFrame({"close": [1.0]})
    assert exit_flags(f, settings.strategy) is None
