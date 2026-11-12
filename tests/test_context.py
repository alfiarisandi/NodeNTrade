"""Konteks day trading untuk Claude: level kunci, struktur pasar, pola candle, sesi."""
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from bot.ai.context import build_review_context, candle_patterns, key_levels, market_structure, session_info
from bot.strategy import Signal, candle_flags
from conftest import make_bars, resample_h4


def _bars(open_, high, low, close):
    return pd.DataFrame({"time": pd.date_range("2024-01-01", periods=len(close), freq="1h"),
                         "open": open_, "high": high, "low": low, "close": close, "spread": 10})


def test_candle_flags_engulfing_and_pin():
    #            bar0 netral, bar1 bearish, bar2 bullish engulfing, bar3 bullish pin bar (ekor bawah panjang)
    df = _bars([1.00, 1.01, 0.99, 1.005], [1.01, 1.012, 1.015, 1.007], [0.99, 0.985, 0.985, 0.98],
               [1.005, 0.99, 1.012, 1.006])
    fl = candle_flags(df)
    assert bool(fl["bull_engulf"].iloc[2]) and not bool(fl["bear_engulf"].iloc[2])
    assert bool(fl["bull_pin"].iloc[3]) and not bool(fl["bull_pin"].iloc[2])
    pats = candle_patterns(df, 5, n=2)
    assert pats[0]["patterns"] == ["bull_engulf"] and "bull_pin" in pats[1]["patterns"]


def test_market_structure_uptrend_and_downtrend():
    t = np.arange(200)
    up = make_bars(1.10 + 0.0004 * t + 0.002 * np.sin(t / 4))
    st = market_structure(up, 5)
    assert st["structure"] == "bullish"
    assert {p["type"] for p in st["pivots_recent"]} <= {"HH", "HL"}
    assert st["last_break"]["direction"] == "up" and st["last_break"]["type"] == "BOS"
    down = make_bars(1.20 - 0.0004 * t + 0.002 * np.sin(t / 4))
    assert market_structure(down, 5)["structure"] == "bearish"
    assert market_structure(up.head(5), 5) == {"structure": "unknown"}


def test_market_structure_choch_after_reversal():
    t = np.arange(200)
    # 150 bar turun lalu naik tajam: penembusan pertama pivot high (struktur masih bearish) = CHoCH ...
    close = np.where(t < 150, 1.20 - 0.0004 * t, 1.14 + 0.0012 * (t - 150)) + 0.002 * np.sin(t / 4)
    st = market_structure(make_bars(close[:160]), 5)
    assert st["last_break"]["direction"] == "up" and st["last_break"]["type"] == "CHoCH"
    # ... dan setelah rally membentuk HH, penembusan berikutnya hanya BOS
    assert market_structure(make_bars(close), 5)["last_break"]["type"] == "BOS"


def test_key_levels_pdh_asia_round(settings):
    # 5 hari H1 (server = UTC, offset 0); jam terakhir = hari ke-5 pukul 10:00
    n = 24 * 4 + 11
    h1 = make_bars(1.1000 + 0.0001 * np.arange(n), start="2024-01-01")
    h1.loc[h1.index[-9], "high"] = 1.2000      # 02:00 hari ini: puncak range Asia (00-07 UTC)
    d1 = pd.DataFrame({"time": pd.date_range("2024-01-01", periods=4, freq="1D"), "open": 1.1, "high": [1.11, 1.12, 1.13, 1.1500],
                       "low": [1.09, 1.08, 1.07, 1.0600], "close": 1.1, "spread": 10})
    now = datetime(2024, 1, 5, 11, 0, tzinfo=timezone.utc)
    lv = key_levels({"H1": h1, "D1": d1}, settings.strategy, 5, 0, now)
    assert lv["prev_day_high"]["price"] == 1.15 and lv["prev_day_low"]["price"] == 1.06
    assert lv["asian_range_high"]["price"] == 1.2 and lv["today_high"]["price"] == 1.2
    assert lv["round_below"]["price"] < lv["close"] < lv["round_above"]["price"]
    assert lv["round_above"]["price"] - lv["round_below"]["price"] == pytest.approx(0.005)
    assert lv["week_high"]["price"] == 1.15
    # Offset server GMT+2: bar 02:00 server = 00:00 UTC, tetap masuk range Asia
    assert "asian_range_high" in key_levels({"H1": h1, "D1": d1}, settings.strategy, 5, 2, now)
    assert key_levels({"H1": h1.head(3)}, settings.strategy, 5) == {}


def test_session_info_dst_and_weekend():
    summer = session_info(datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc))   # Rabu: London 07-16, NY 12-21 UTC
    assert summer["active"] == ["London", "New York"] and summer["label"] == "London-New York overlap"
    assert summer["opens_today_wib"] == {"London": "14:00", "New York": "19:00"}
    winter = session_info(datetime(2026, 1, 14, 9, 0, tzinfo=timezone.utc))    # Rabu: London 08-17, NY 13-22 UTC
    assert winter["active"] == ["London"] and winter["next"].startswith("New York open in 4.0h")
    assert winter["opens_today_wib"]["New York"] == "20:00"
    asia = session_info(datetime(2026, 1, 14, 2, 0, tzinfo=timezone.utc))
    assert asia["label"] == "Asia" and asia["now_wib"] == "09:00 WIB"
    assert session_info(datetime(2026, 1, 17, 12, 0, tzinfo=timezone.utc))["label"] == "weekend (market closed)"


def test_review_context_has_day_trading_keys(uptrend_waves, settings, eurusd_meta):
    h1, h4 = uptrend_waves
    d1 = h1.set_index("time").resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                                    "spread": "max"}).dropna().reset_index()
    sig = Signal("EURUSD", 1, h1["time"].iloc[-1], float(h1["close"].iloc[-1]), 0.001, 0.0025, 0.0)
    ctx = json.loads(build_review_context(sig, {"H1": h1, "H4": h4, "D1": d1}, settings.strategy, eurusd_meta,
                                          1.2, 10, 9, {}, [], {}, 0.0, [], None, offset_hours=2,
                                          now=datetime(2026, 1, 14, 9, 0, tzinfo=timezone.utc)))
    assert ctx["session"]["label"] == "London"
    assert "prev_day_high" in ctx["key_levels"] and "round_above" in ctx["key_levels"]
    assert set(ctx["structure"]) == {"H1"} and ctx["structure"]["H1"]["structure"] in ("bullish", "bearish", "range")
    assert len(ctx["candles_H1"]) == 3
    assert resample_h4  # dipakai fixture lain; impor dijaga agar linter tidak menghapusnya
