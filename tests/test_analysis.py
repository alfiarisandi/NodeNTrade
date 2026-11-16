"""Analisis harian Claude: parsing JSON, batas keras setup, konteks analisis."""
import json
from datetime import datetime, timezone

import pytest

from bot.ai.analysis import (Setup, analyze_market, format_opportunity, format_setup, format_watch, parse_analysis,
                             validate_setup, watch_problem)
from bot.ai.context import build_analysis_context
from conftest import resample_h4


def setup(**kw):
    base = dict(symbol="xauusd", side="BUY", entry_low=2000.0, entry_high=2002.0, stop_loss=1995.0,
                take_profit_1=2013.0, take_profit_2=2020.0, confidence="High", reason="r", wait_for="w", invalid_if="i",
                score=dict(context=22, structure=17, liquidity=12, price_action=17, location=13, rr=5))
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
    # Strong >= 80, Moderate 60-79, Weak < 60 (tidak dieksekusi); konteks tren/EMA/ADX satu komponen
    weak = setup(score=dict(context=25, structure=20, liquidity=0, price_action=0, location=10, rr=0))
    assert weak.score_total == 55 and weak.grade == "Weak" and "skor 5.5/10 Weak" in validate_setup(weak, atr)
    capped = setup(score=dict(context=99, structure=20, liquidity=15, price_action=20, location=15, rr=5, ema=50))
    assert capped.score_total == 100                                   # tiap komponen dibatasi bobotnya
    mod = setup(score=dict(context=20, structure=15, liquidity=10, price_action=0, location=10, rr=5))
    assert (mod.score_total, mod.grade) == (60, "Moderate") and validate_setup(mod, atr) is None
    assert setup().grade == "Strong" and "skor 8.6/10 Strong" in format_setup(setup(), 2)
    labeled = setup(strategy="r", regime="reversal", missing=["MSS M15"])
    assert (labeled.strategy, labeled.regime) == ("R", "REVERSAL") and setup(strategy="zzz").strategy == ""
    assert "strategi R Range H1" in format_setup(labeled, 2) and "belum ada: MSS M15" in format_setup(labeled, 2)
    assert "skor 0/10" in validate_setup(setup(score={}), atr)         # tanpa skor = tidak ditawarkan
    # RR bersih: biaya mengurangi untung dan menambah rugi. RR kotor 2.0 (SL 6, TP 12)
    assert validate_setup(setup(), atr, cost=0.8) is None              # (12-0.8)/(6+0.8) = 1.65
    tight = setup(take_profit_1=2010.0)                                # RR kotor 1.5 -> bersih (9-0.8)/6.8 = 1.21
    assert tight.rr == pytest.approx(1.5) and "RR bersih 1.21 < 1.3" in validate_setup(tight, atr, cost=0.8)
    assert "SL terlalu sempit" in validate_setup(setup(), atr, cost=1.0)   # 17% > 15%


def test_opportunities_parse_and_format():
    raw = json.dumps({"text": "WAIT", "setups": [], "opportunities": [
        dict(symbol="xauusd", side="SELL", status="WAIT", strategy="b", regime="reversal", score=6.5,
             missing=["MSS M15"], entry_low=3901, entry_high=3896, invalid_level=3910, target=3880,
             reason="belum ada MSS", activate_if="close M15 di bawah 3890"),
        dict(symbol="EURUSD", side="BUY", status="NO_TRADE", score=4, entry_low=1.1),   # tanpa stop/target
        dict(symbol="EURUSD", side="BUY", score=11)]})                                     # skor > 10: dibuang
    a = parse_analysis(raw)
    o, n = a.opportunities
    assert (o.symbol, o.strategy, o.regime, o.tier, o.entry_low, o.entry_high) == ("XAUUSD", "B", "REVERSAL",
                                                                                    "Moderate", 3896, 3901)
    assert o.rr == pytest.approx((3898.5 - 3880) / (3910 - 3898.5))
    assert n.tier == "Weak" and n.entry_high == 1.1 and n.rr is None
    assert format_opportunity(o, 2) == "SELL XAUUSD B (WAIT, skor 6.5/10 Moderate) zona 3896.00-3901.00, batal 3910.00, RR ~1.6"


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
