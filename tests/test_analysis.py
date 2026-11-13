"""Analisis harian Claude: parsing JSON, batas keras setup, konteks analisis."""
import json
from datetime import datetime, timezone

import pytest

from bot.ai.analysis import (Setup, analyze_market, format_setup, format_watch, parse_analysis, validate_setup,
                             watch_problem)
from bot.ai.context import build_analysis_context
from conftest import resample_h4


def setup(**kw):
    base = dict(symbol="xauusd", side="BUY", entry_low=2000.0, entry_high=2002.0, stop_loss=1995.0,
                take_profit_1=2013.0, take_profit_2=2020.0, confidence="High", reason="r", wait_for="w", invalid_if="i",
                score=dict(trend=18, structure=18, key_level=13, price_action=17, momentum=12, rr=8))
    base.update(kw)
    return Setup(**base)


def test_parse_tolerates_fences_and_normalizes():
    raw = '```json\n{"text": "WAIT", "setups": [%s]}\n```' % json.dumps(
        dict(symbol="eurusd", side="SELL", entry_low=1.1010, entry_high=1.1000, stop_loss=1.1030, take_profit_1=1.0950))
    a = parse_analysis(raw)
    s = a.setups[0]
    assert a.text == "WAIT" and s.symbol == "EURUSD" and (s.entry_low, s.entry_high) == (1.1000, 1.1010)
    assert s.confidence == "Low" and s.valid_minutes == 120
    assert s.direction == -1 and s.rr == pytest.approx((1.1005 - 1.0950) / (1.1030 - 1.1005))
    with pytest.raises(ValueError):
        parse_analysis("tidak ada json")
    assert a.watch == []   # jawaban lama tanpa "watch" tetap valid


def test_watch_parse_drops_bad_items_and_checks_price():
    raw = json.dumps({"text": "WAIT", "setups": [], "watch": [
        dict(symbol="xauusd", side="SELL", trigger="close_below", level=3888.4, invalid_level=3914, strategy="d"),
        dict(symbol="EURUSD", side="SELL", trigger="zone", zone_low=1.1068, zone_high=1.1063, invalid_level=1.1073),
        dict(symbol="EURUSD", side="BUY", trigger="breakout", level=1.1),          # pemicu tidak dikenal: dibuang
        "bukan objek"]})
    a = parse_analysis(raw)
    assert len(a.watch) == 2
    w, z = a.watch
    assert w.symbol == "XAUUSD" and w.strategy == "D" and w.valid_minutes == 240
    assert (z.zone_low, z.zone_high) == (1.1063, 1.1068)
    assert watch_problem(w, 3896.0) is None
    assert "sudah terpenuhi" in watch_problem(w, 3880.0)
    assert "pembatal" in watch_problem(w.model_copy(update={"level": 3910.0}), 3916.0)
    assert watch_problem(z, 1.1050) is None and "sudah di zona" in watch_problem(z, 1.1065)
    assert w.triggered(3895, 3885, 3887.1) and not w.triggered(3895, 3885, 3889)
    assert z.triggered(1.1064, 1.1050, 1.1055) and not z.triggered(1.1062, 1.1050, 1.1055)
    assert w.invalidated(3915) and not w.invalidated(3913)
    assert "close M15 di bawah 3888.40, batal bila close di atas 3914.00" in format_watch(w, 2)


def test_validate_setup_hard_limits():
    atr = 4.0
    assert validate_setup(setup(), atr) is None                                   # SL 6 = 1.5 ATR, RR 2.0
    assert "RR" in validate_setup(setup(take_profit_1=2008.0), atr)               # RR 1.17
    assert "SL di sisi" in validate_setup(setup(stop_loss=2005.0), atr)
    assert "TP1 di sisi" in validate_setup(setup(take_profit_1=1990.0), atr)
    assert "jarak SL" in validate_setup(setup(stop_loss=1980.0, take_profit_1=2050.0), atr)   # 5.25 ATR
    assert "zona" in validate_setup(setup(entry_low=1996.0, entry_high=2006.0, stop_loss=1992.0,
                                          take_profit_1=2020.0), atr)
    sell = setup(side="SELL", entry_low=2000.0, entry_high=2002.0, stop_loss=2007.0, take_profit_1=1989.0)
    assert validate_setup(sell, atr) is None and sell.in_zone(2002.3, 0.5) and not sell.in_zone(2003.0)
    assert "RR 2.0" in format_setup(setup(), 2) and "TP2 2020.00" in format_setup(setup(), 2)


def test_analyze_market_rejects_bad_reply():
    class LLM:
        def __init__(self, reply):
            self.reply = reply

        def complete(self, system, user):
            assert "Market snapshot" in user
            return self.reply

    assert analyze_market(LLM('{"text": "ok", "setups": []}'), "{}").text == "ok"
    with pytest.raises(ValueError):
        analyze_market(LLM('{"text": "ok", "setups": [{"symbol": "X"}]}'), "{}")   # field wajib hilang


def test_analysis_context_structure(uptrend_waves, settings, eurusd_meta):
    h1, h4 = uptrend_waves
    ctx = json.loads(build_analysis_context(
        {"EURUSD": {"H4": h4, "H1": h1, "M15": h1.tail(300), "M5": h1.tail(60)}}, settings.strategy,
        {"EURUSD": eurusd_meta}, {"EURUSD": 12.0}, datetime(2026, 1, 14, 9, 0, tzinfo=timezone.utc), 2,
        rules={"min_reward_risk": 2}, prices={"EURUSD": {"bid": 1.123456, "ask": 1.1236}}))
    sym = ctx["symbols"]["EURUSD"]
    assert set(sym["market"]) == {"H4", "H1", "M15"} and sym["spread_points"] == 12.0
    assert set(sym["structure"]) == {"H1", "M15"} and len(sym["last_30_M15_candles[time,o,h,l,c]"]) == 30
    m15 = sym["market"]["M15"]
    assert m15["bb_lower"] < m15["bb_mid"] < m15["bb_upper"] and m15["bb_width_atr"] > 0
    assert m15["bb_percent_b"] == round(100 * (m15["close"] - m15["bb_lower"]) / (m15["bb_upper"] - m15["bb_lower"]))
    assert len(sym["last_20_M5_candles[time,o,h,l,c]"]) == 20 and "prev_day_high" not in sym["key_levels"]
    assert ctx["session"]["label"] == "London" and ctx["high_impact_events_next_24h"] == "calendar unavailable"
    assert ctx["owner_rules"] == {"min_reward_risk": 2} and sym["price_now"] == {"bid": 1.12346, "ask": 1.1236}
    assert resample_h4


def test_score_and_cost_limits():
    atr = 4.0
    # bobot & ambang 12/11 (branch uji/setelan-kemarin): < 70 WAIT, 70-79 Valid, >= 80 Premium
    low = setup(score=dict(trend=20, structure=20, key_level=15, price_action=0, momentum=10, rr=0))
    assert low.score_total == 65 and "skor 65/100 < 70" in validate_setup(low, atr) and low.grade == "No Trade"
    capped = setup(score=dict(trend=99, structure=20, key_level=15, price_action=20, momentum=15, rr=10, bonus=50))
    assert capped.score_total == 100                                   # tiap komponen dibatasi bobotnya
    ok = setup(score=dict(trend=20, structure=20, key_level=15, price_action=0, momentum=15))
    assert (ok.score_total, ok.grade) == (70, "Valid") and validate_setup(ok, atr) is None
    labeled = setup(strategy="b", regime="trend")
    assert (labeled.strategy, labeled.regime) == ("B", "TREND") and setup(strategy="zzz").strategy == ""
    assert "strategi B Smart Money Concepts" in format_setup(labeled, 2)
    assert "skor 0/100" in validate_setup(setup(score={}), atr)        # tanpa skor = tidak ditawarkan
    assert validate_setup(setup(), atr, cost=0.8) is None              # biaya 0.8 dari SL 6 = 13%
    assert "SL terlalu sempit" in validate_setup(setup(), atr, cost=1.0)   # 17% > 15%
    assert "skor 86/100 Premium" in format_setup(setup(), 2)


def test_intraday_snapshot_in_context(settings, eurusd_meta):
    from test_intraday import frames_from

    import numpy as np
    from conftest import make_bars

    t = np.arange(9000)
    close = 1.10 + 0.01 * np.sin(t / 900) + 0.002 * np.sin(t / 40)
    bars = make_bars(close, start="2026-01-05", freq="15min", wick=0.0003, spread=2)
    frames = frames_from(bars[bars["time"].dt.day_of_week < 5].reset_index(drop=True))
    ctx = json.loads(build_analysis_context({"EURUSD": frames}, settings.strategy, {"EURUSD": eurusd_meta},
                                            {"EURUSD": 2.0}, datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc), 2))
    snap = ctx["symbols"]["EURUSD"]["intraday"]
    assert snap["regime"] in ("TREND", "RANGE", "MIXED") and snap["h4_bias"].split()[0] in ("bullish", "bearish", "neutral")
    assert snap["dealing_range_H1_50bars"]["zone"] in ("premium", "discount") and snap["session_vwap"] > 1
    # data terlalu pendek: blok dikosongkan, analisis tetap jalan
    short = {tf: df.tail(100) for tf, df in frames.items()}
    ctx = json.loads(build_analysis_context({"EURUSD": short}, settings.strategy, {"EURUSD": eurusd_meta},
                                            {"EURUSD": 2.0}, datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc), 2))
    assert ctx["symbols"]["EURUSD"]["intraday"] == {}
