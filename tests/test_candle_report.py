"""Ringkasan tiap close candle (tanpa AI): sekali per candle, hanya di jam WIB yang diatur, dilewati bila basi."""
from datetime import timedelta

import pandas as pd

from bot.config import CandleReportCfg
from test_live import FakeLLM, make_bot


def bot_with_report(tmp_path, settings, at_signal, meta, **kw):
    cfg = CandleReportCfg(**{"enabled": True, "start_hour_wib": 0, "end_hour_wib": 23, **kw})
    s = settings.model_copy(update={"candle_report": cfg})
    bot, mt5, msgs = make_bot(tmp_path, s, at_signal, meta, FakeLLM(), dry_run=True)
    last = mt5.h1["time"].iloc[-1]
    close = (last + pd.Timedelta(hours=1)).tz_localize("UTC").to_pydatetime()   # offset 0: jam server = UTC
    return bot, msgs, close


def test_report_once_per_candle(tmp_path, settings, at_signal, eurusd_meta):
    bot, msgs, close = bot_with_report(tmp_path, settings, at_signal, eurusd_meta)
    bot.maybe_candle_report(close + timedelta(minutes=1))
    assert len(msgs) == 1 and msgs[0].startswith("🕐 Candle H1 close") and "EURUSD" in msgs[0]
    assert "Posisi terbuka: 0" in msgs[0]
    bot.maybe_candle_report(close + timedelta(minutes=5))
    assert len(msgs) == 1                                   # candle yang sama tidak dilaporkan dua kali


def test_report_skips_stale_candle_and_hours(tmp_path, settings, at_signal, eurusd_meta):
    bot, msgs, close = bot_with_report(tmp_path, settings, at_signal, eurusd_meta)
    bot.maybe_candle_report(close + timedelta(hours=3))     # bot baru menyala / pasar tutup: tidak dikirim
    assert msgs == []
    hour_wib = (close + timedelta(hours=7)).hour
    bot, msgs, close = bot_with_report(tmp_path / "b", settings, at_signal, eurusd_meta,
                                       start_hour_wib=(hour_wib + 1) % 24, end_hour_wib=(hour_wib + 1) % 24)
    bot.maybe_candle_report(close + timedelta(minutes=1))
    assert msgs == []                                       # di luar jam laporan


def test_report_disabled_by_default(tmp_path, settings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, settings, at_signal, eurusd_meta, FakeLLM(), dry_run=True)
    bot.maybe_candle_report(pd.Timestamp.now(tz="UTC").to_pydatetime())
    assert msgs == []
