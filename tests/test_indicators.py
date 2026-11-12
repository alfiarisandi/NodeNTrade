import numpy as np
import pandas as pd

from bot.indicators import adx, atr, ema, rsi


def test_ema_matches_recursive_definition():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    k = 2 / (3 + 1)
    expected = [1.0]
    for v in s.iloc[1:]:
        expected.append(v * k + expected[-1] * (1 - k))
    assert np.allclose(ema(s, 3), expected)


def test_rsi_extremes():
    up = pd.Series(np.arange(1.0, 60.0))
    down = pd.Series(np.arange(60.0, 1.0, -1))
    assert rsi(up).iloc[-1] == 100
    assert rsi(down).iloc[-1] < 1e-9


def test_rsi_balanced_is_50():
    s = pd.Series([1.0, 2.0] * 100)
    assert abs(rsi(s).iloc[-1] - 50) < 5


def test_atr_constant_range():
    n = 100
    close = pd.Series(np.full(n, 10.0))
    high, low = close + 1, close - 1
    assert np.isclose(atr(high, low, close).iloc[-1], 2.0)


def test_adx_strong_trend_high_and_flat_low():
    n = 300
    close = pd.Series(np.arange(n, dtype=float))
    trend = adx(close + 0.5, close - 0.5, close).iloc[-1]
    flat = pd.Series(10 + 0.5 * np.sin(np.arange(n)))
    chop = adx(flat + 0.5, flat - 0.5, flat).iloc[-1]
    assert trend > 90
    assert chop < 30
