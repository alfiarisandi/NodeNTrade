import numpy as np
import pandas as pd
import pytest

from bot.config import ConfirmCfg, Settings, SessionCfg, SymbolCfg
from bot.strategy import compute_features, signal_directions


def make_bars(close: np.ndarray, start="2024-01-01", freq="1h", wick=0.0005, spread=10) -> pd.DataFrame:
    close = np.asarray(close, float)
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({
        "time": pd.date_range(start, periods=len(close), freq=freq),
        "open": open_,
        "high": np.maximum(open_, close) + wick,
        "low": np.minimum(open_, close) - wick,
        "close": close,
        "tick_volume": 100,
        "spread": spread,
    })


def resample_h4(h1: pd.DataFrame) -> pd.DataFrame:
    g = h1.set_index("time").resample("4h")
    return pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                         "close": g["close"].last(), "spread": g["spread"].max()}).dropna().reset_index()


@pytest.fixture
def uptrend_waves():
    """Tren naik dengan gelombang, sehingga EMA20/50 sering bersilangan, H4 tetap di atas EMA200."""
    n = 3000
    t = np.arange(n)
    rng = np.random.default_rng(7)
    close = 1.10 + 0.00004 * t + 0.003 * np.sin(t / 15) + rng.normal(0, 0.0008, n)
    h1 = make_bars(close)
    return h1, resample_h4(h1)


@pytest.fixture
def settings():
    return Settings(
        symbols=[SymbolCfg(name="EURUSD", max_spread_points=20)],
        session=SessionCfg(start_hour_utc=0, end_hour_utc=24, friday_cutoff_hour_utc=24),
        confirm=ConfirmCfg(enabled=False),  # test konfirmasi menyalakannya sendiri
    )


@pytest.fixture
def eurusd_meta():
    return {"point": 0.00001, "digits": 5, "tick_size": 0.00001, "tick_value": 1.0,
            "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01}


@pytest.fixture
def at_signal(uptrend_waves, settings):
    """Potong data tepat di bar sinyal pertama, sehingga bar terakhir yang close = bar sinyal."""
    h1, h4 = uptrend_waves
    f = compute_features(h1, h4, settings.strategy)
    i = int(np.flatnonzero(signal_directions(f, settings.strategy, settings.session, 0))[0])
    return h1.iloc[: i + 1].reset_index(drop=True), h4
