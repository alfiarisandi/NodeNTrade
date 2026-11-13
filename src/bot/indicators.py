"""Indikator teknikal (Wilder smoothing untuk RSI/ATR/ADX), tanpa dependency TA eksternal."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1 / period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = _wilder(delta.clip(lower=0), period)
    loss = _wilder(-delta.clip(upper=0), period)
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # loss == 0 berarti hanya naik -> RSI 100
    out = out.where(loss != 0, 100.0)
    out.iloc[0] = np.nan
    return out


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return _wilder(true_range(high, low, close), period)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    atr_ = atr(high, low, close, period)
    plus_di = 100 * _wilder(plus_dm, period) / atr_
    minus_di = 100 * _wilder(minus_dm, period) / atr_
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / di_sum).fillna(0)
    return _wilder(dx, period)


def session_vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
                 session_id: pd.Series) -> pd.Series:
    """VWAP yang di-reset tiap session_id berganti (mis. tanggal sesi). Harga tipikal (H+L+C)/3 x volume.
    Di forex spot volume = tick volume broker (jumlah perubahan harga), jadi ini pendekatan, bukan VWAP bursa."""
    typical = (high + low + close) / 3
    vol = volume.clip(lower=1).astype(float)   # bar tanpa volume tetap dihitung bobot 1
    key = session_id.to_numpy()
    pv = (typical * vol).groupby(key).cumsum()
    return pv / vol.groupby(key).cumsum()


def supertrend(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 10,
               mult: float = 3.0) -> pd.Series:
    """Arah Supertrend per bar: 1 = naik, -1 = turun (rumus standar TradingView)."""
    a = atr(high, low, close, period).to_numpy()
    hl2 = ((high + low) / 2).to_numpy()
    c = close.to_numpy()
    up_basic, dn_basic = hl2 - mult * a, hl2 + mult * a
    n = len(c)
    up, dn, direction = np.full(n, np.nan), np.full(n, np.nan), np.zeros(n)
    for i in range(n):
        if np.isnan(a[i]):
            continue
        if i == 0 or np.isnan(up[i - 1]):
            up[i], dn[i], direction[i] = up_basic[i], dn_basic[i], 1
            continue
        up[i] = max(up_basic[i], up[i - 1]) if c[i - 1] > up[i - 1] else up_basic[i]
        dn[i] = min(dn_basic[i], dn[i - 1]) if c[i - 1] < dn[i - 1] else dn_basic[i]
        if direction[i - 1] == -1 and c[i] > dn[i - 1]:
            direction[i] = 1
        elif direction[i - 1] == 1 and c[i] < up[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
    return pd.Series(np.where(np.isnan(a), np.nan, direction), index=close.index)


def linreg_end(series: pd.Series, length: int) -> pd.Series:
    """Nilai regresi linear di titik terakhir jendela (= ta.linreg(src, length, 0) di Pine)."""
    x = np.arange(length, dtype=float)
    xm = x.mean()
    sxx = ((x - xm) ** 2).sum()
    w = 1 / length + (x - xm) * (length - 1 - xm) / sxx  # bobot untuk y[0..length-1]
    return series.rolling(length).apply(lambda y: float(np.dot(w, y)), raw=True)
