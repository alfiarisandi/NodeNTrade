"""Deteksi adaptif: simulasi hasil peluang, statistik filter, risiko per tingkat skor, satu setup aktif, blok EV."""
import pandas as pd
import pytest

from bot.ai.analysis import Analysis, Opportunity
from bot.config import ConfirmCfg, TelegramCfg
from bot.opportunities import EV_MIN_TRADES, opportunity_stats, reason_key, simulate
from test_confirm import make_setup
from test_live import FakeLLM, make_bot

T0 = pd.Timestamp("2026-01-14 10:00")


def bars(rows):
    """rows = [(high, low, close)] per candle M15 mulai T0."""
    return pd.DataFrame([{"time": T0 + pd.Timedelta(minutes=15 * i), "high": h, "low": l, "close": c}
                         for i, (h, l, c) in enumerate(rows)])


def test_simulate_outcomes():
    # BUY zona 100-101, stop 98, target 104: dari tengah zona 100.5 risiko 2.5, reward 3.5 (RR 1.4)
    args = dict(direction=1, entry_low=100.0, entry_high=101.0, stop=98.0, target=104.0, valid_minutes=60)
    win = bars([(103, 101.5, 102), (101.2, 100.2, 100.8), (102, 100.5, 101.9), (104.2, 101, 104)])
    assert simulate(win, T0, cost_r=0.1, **args) == ("win", 1.3)
    loss = bars([(101, 100.4, 100.6), (100.8, 97.5, 98)])
    assert simulate(loss, T0, cost_r=0.1, **args) == ("loss", -1.1)
    # candle pengisian yang juga menyentuh SL = rugi (urutan di dalam candle tidak diketahui)
    assert simulate(bars([(104.5, 97.9, 103)]), T0, **args) == ("loss", -1.0)
    # zona tidak tersentuh sampai batas valid_minutes
    assert simulate(bars([(103, 101.5, 102)] * 5), T0, **args) == ("not_reached", None)
    assert simulate(bars([(103, 101.5, 102)] * 2), T0, **args) is None          # belum lewat batas: tunggu
    assert simulate(bars([(101, 100.4, 100.6)] * 3), T0, **args) is None        # terisi, belum kena SL/TP
    hold = bars([(101, 100.4, 100.6)] + [(101.5, 99.5, 101.5)] * 97)           # 24 jam tanpa SL/TP
    assert simulate(hold, T0, **args) == ("timeout", 0.4)   # (101.5 - 100.5) / 2.5
    assert simulate(win, T0 - pd.Timedelta(hours=1), **args) == ("untestable", None)   # histori tidak cukup
    assert simulate(win, T0, **{**args, "stop": 102.0}) == ("untestable", None)        # stop di sisi yang salah
    # SELL: zona 100-101, stop 103, target 97
    sell = bars([(101, 100.2, 100.4), (100, 96.5, 97)])
    assert simulate(sell, T0, direction=-1, entry_low=100, entry_high=101, stop=103, target=97,
                    valid_minutes=60) == ("win", pytest.approx(3.5 / 2.5))


def test_opportunity_stats_and_reasons():
    def row(**kw):
        return {"kind": "setup", "tier": "Strong", "decision": "offered", "reason": "", "executed": 0,
                "outcome": "open", "outcome_r": None, "strategy": "A", **kw}

    rows = [row(executed=1, outcome="loss", outcome_r=-1.1),
            row(decision="rejected", reason="RR bersih 1.10 < 1.3", outcome="win", outcome_r=1.5, strategy="B"),
            row(decision="blocked", reason="berita High USD: CPI (0.5 jam lagi)", outcome="loss", outcome_r=-1.0),
            row(kind="opportunity", decision="wait", tier="Weak", outcome="not_reached")]
    st = opportunity_stats(rows)
    assert (st["detected"], st["executable_offered"], st["rejected_by_gates"], st["wait_or_no_trade"]) == (4, 1, 2, 1)
    assert (st["missed_winners"], st["avoided_losers"], st["false_positives"], st["not_reached"]) == (1, 1, 1, 1)
    assert st["sim_avg_R_offered"] == -1.1 and st["sim_avg_R_filtered"] == 0.25
    assert st["inactivity_reasons"] == {"RR bersih kurang": 1, "blok berita": 1, "AI WAIT": 1}
    assert st["by_tier"] == {"Strong": 3, "Weak": 1} and opportunity_stats([]) == {"detected": 0}
    assert reason_key("sudah ada 1 posisi/limit setup aktif (maks 1)") == "sudah ada setup aktif"


@pytest.fixture
def osettings(settings):
    return settings.model_copy(update={"confirm": ConfirmCfg(enabled=True),
                                       "telegram": TelegramCfg(token="t", chat_id="111")})


def offer(bot, mt5, **kw):
    bot.offer_setups(Analysis(text="x", setups=[make_setup(mt5.tick("EURUSD").ask, **kw)]))


def opps(bot):
    return bot.journal.opportunities_since("2000-01-01")


def test_moderate_setup_offers_quarter_percent_only(tmp_path, osettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, osettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    offer(bot, mt5, score=dict(context=20, structure=15, liquidity=10, price_action=0, location=10, rr=5))
    labels = [b for r in bot.notifier.buttons[-1] for b, _ in r]
    assert [l.split(" ")[1] for l in labels[:-1]] == ["0.25%"] and "Risiko awal Moderate: 0.25%" in msgs[-1]
    assert [(o["decision"], o["tier"], o["score"]) for o in opps(bot)] == [("offered", "Moderate", 6.0)]


def test_one_active_setup_at_a_time(tmp_path, osettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, osettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    sid = bot.journal.log_signal("EURUSD", 1, "setup lama", 1.1, 0.004, 0.01, {}, source="ai_setup", setup={})
    bot.journal.update_signal(sid, user_decision="limit")   # limit setup sebelumnya masih menunggu
    offer(bot, mt5)
    assert "sudah ada 1 posisi/limit setup aktif (maks 1)" in msgs[-1] and bot.notifier.buttons == []
    assert opps(bot)[0]["decision"] == "blocked"
    bot.journal.update_signal(sid, user_decision="limit_cancelled")
    offer(bot, mt5)
    assert bot.notifier.buttons and opps(bot)[-1]["decision"] == "offered"
    # dua setup ditawarkan bersamaan: setelah yang pertama terisi, klik yang kedua diblok
    first = bot.journal.pending_confirmations()[0]["id"]
    bot.offer_setups(Analysis(text="x", setups=[make_setup(mt5.tick("EURUSD").ask + 0.0002)]))
    second = bot.journal.pending_confirmations()[-1]["id"]
    from test_confirm import click
    click(bot, f"c:{first}:0.005")
    assert len(mt5.orders) == 1 and bot.journal.active_setups() == 1
    click(bot, f"c:{second}:0.005")
    assert len(mt5.orders) == 1 and "setup aktif" in msgs[-1]


def test_negative_forward_ev_blocks_strategy(tmp_path, osettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, osettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    for i in range(EV_MIN_TRADES):
        sid = bot.journal.log_signal("EURUSD", 1, f"s{i}", 1.1, 0.004, 0.01, {}, source="ai_setup",
                                     setup={"strategy": "A"})
        bot.journal.log_trade_open(sid, 1000 + i, "EURUSD", 1, 0.1, 1.1, 1.096, 1.11, 10.0)
        bot.journal.close_trade(1000 + i, 1.096, f"2026-01-14T0{i % 10}:00:00+00:00", -10.0 if i % 4 else 12.0)
    assert bot.forward_evidence()["A"] == {"trades": 20, "win_%": 25, "expectancy_R": -0.45, "calibrated": True}
    offer(bot, mt5, strategy="A")
    assert "EV forward strategi A -0.45R dari 20 trade" in msgs[-1] and bot.notifier.buttons == []
    offer(bot, mt5, strategy="B")
    assert bot.notifier.buttons and "EV: UNCALIBRATED (forward 0/20 trade strategi B" in msgs[-1]
    assert "EV forward per strategi: A 20 trade -0.45R" in bot.evaluation_text()


def test_opportunities_recorded_and_resolved(tmp_path, osettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, osettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    m15 = mt5.rates("EURUSD", "M15", 600)
    t = m15["time"].iloc[-40]
    px = float(m15["close"].iloc[-40])
    o = Opportunity(symbol="EURUSD", side="BUY", status="WAIT", score=6.5, strategy="R", entry_low=px - 0.0003,
                    entry_high=px + 0.0003, invalid_level=px - 0.0030, target=px + 0.0060, valid_minutes=600,
                    reason="tunggu rejection", activate_if="pin bar M15 di zona")
    bot.offer_setups(Analysis(text="x", opportunities=[o, o.model_copy(update={"symbol": "XAUUSD"})]))
    rows = opps(bot)
    assert len(rows) == 1 and (rows[0]["kind"], rows[0]["decision"], rows[0]["tier"]) == ("opportunity", "wait", "Moderate")
    assert "🔍 Peluang 14 hari terakhir: 1 terdeteksi (Moderate 1)" in bot.evaluation_text()
    created = (t + pd.Timedelta(minutes=1)).tz_localize("UTC").isoformat()   # offset server 0 di fixture
    bot.journal.db.execute("UPDATE opportunities SET created_at=?", (created,))
    bot.resolve_opportunities()
    row = opps(bot)[0]
    expected = simulate(m15, t + pd.Timedelta(minutes=1), 1, o.entry_low, o.entry_high, o.invalid_level, o.target,
                        600, row["cost_r"])
    assert expected is not None and (row["outcome"], row["outcome_r"]) == expected
