"""Deteksi adaptif: simulasi hasil peluang, statistik filter, risiko per tingkat skor, satu setup aktif, blok EV."""
import pandas as pd
import pytest

from bot.ai.analysis import Analysis, Opportunity
from bot.config import ConfirmCfg, TelegramCfg
from bot.opportunities import (EV_MIN_TRADES, SimResult, format_opportunity_stats, opportunity_stats, reason_key,
                               simulate)
from test_confirm import make_setup
from test_live import FakeLLM, make_bot

T0 = pd.Timestamp("2026-01-14 10:00")


def bars(rows):
    """rows = [(high, low, close)] per candle M15 mulai T0."""
    return pd.DataFrame([{"time": T0 + pd.Timedelta(minutes=15 * i), "high": h, "low": l, "close": c}
                         for i, (h, l, c) in enumerate(rows)])


def res(outcome, r=None, tp2=None, r2=None):
    return SimResult(outcome, r, tp2, r2)


def test_simulate_outcomes():
    # BUY zona 100-101, stop 98, target 104: dari tengah zona 100.5 risiko 2.5, reward 3.5 (RR 1.4)
    args = dict(direction=1, entry_low=100.0, entry_high=101.0, stop=98.0, target=104.0, valid_minutes=60)
    win = bars([(103, 101.5, 102), (101.2, 100.2, 100.8), (102, 100.5, 101.9), (104.2, 101, 104)])
    assert simulate(win, T0, cost_r=0.1, **args) == res("win", 1.3)
    loss = bars([(101, 100.4, 100.6), (100.8, 97.5, 98)])
    assert simulate(loss, T0, cost_r=0.1, **args) == res("loss", -1.1)
    # candle pengisian yang menembus zona lalu SL: urutannya pasti (harga datang dari atas zona) = rugi
    assert simulate(bars([(101.5, 97.9, 98.5)]), T0, **args) == res("loss", -1.0)
    # zona tidak tersentuh sampai batas valid_minutes
    assert simulate(bars([(103, 101.5, 102)] * 5), T0, **args) == res("not_reached")
    assert simulate(bars([(103, 101.5, 102)] * 2), T0, **args) is None          # belum lewat batas: tunggu
    assert simulate(bars([(101, 100.4, 100.6)] * 3), T0, **args) is None        # terisi, belum kena SL/TP
    hold = bars([(101, 100.4, 100.6)] + [(101.5, 99.5, 101.5)] * 97)           # 24 jam tanpa SL/TP
    assert simulate(hold, T0, **args) == res("timeout", 0.4)   # (101.5 - 100.5) / 2.5
    assert simulate(win, T0 - pd.Timedelta(hours=1), **args) == res("untestable")   # histori tidak cukup
    assert simulate(win, T0, **{**args, "stop": 102.0}) == res("untestable")        # stop di sisi yang salah
    # SELL: zona 100-101, stop 103, target 97
    sell = bars([(101, 100.2, 100.4), (100, 96.5, 97)])
    assert simulate(sell, T0, direction=-1, entry_low=100, entry_high=101, stop=103, target=97,
                    valid_minutes=60) == res("win", pytest.approx(3.5 / 2.5))


def test_simulate_ambiguous_and_tp2():
    args = dict(direction=1, entry_low=100.0, entry_high=101.0, stop=98.0, target=104.0, valid_minutes=60)
    # SL dan TP di candle yang sama setelah terisi: urutan tidak diketahui -> ambiguous, bukan rugi / menang
    assert simulate(bars([(101, 100.4, 100.6), (104.5, 97.5, 100)]), T0, **args) == res("ambiguous")
    # TP di candle pengisian: tidak diketahui apakah TP tersentuh sebelum entry
    assert simulate(bars([(104.5, 100.5, 101)]), T0, **args) == res("ambiguous")
    # horizon entry = valid_minutes: zona tersentuh di menit ke-75 tidak dihitung untuk setup 60 menit
    late = bars([(103, 101.5, 102)] * 5 + [(101, 100.4, 100.6), (104.5, 100.6, 104)])
    assert simulate(late, T0, **args) == res("not_reached")
    assert simulate(late, T0, **{**args, "valid_minutes": 120}) == res("win", 1.4)
    # TP2 (SL awal tetap): TP1 lalu TP2; TP1 lalu SL; TP2 belum diputuskan = tunggu
    path = [(101, 100.4, 100.6), (104.2, 101, 104)]
    assert simulate(bars(path + [(106.5, 103, 106)]), T0, target2=106.0, **args) == res("win", 1.4, "win", 2.2)
    assert simulate(bars(path + [(104, 97.5, 98)]), T0, target2=106.0, **args) == res("win", 1.4, "loss", -1.0)
    assert simulate(bars(path + [(105, 103, 104.5)]), T0, target2=106.0, **args) is None
    assert simulate(bars(path), T0, target2=103.0, **args) == res("win", 1.4)    # TP2 di bawah TP1 diabaikan


def row(**kw):
    base = {"id": 0, "kind": "setup", "tier": "Strong", "decision": "offered", "reason": "", "executed": 0,
            "user_decision": None, "outcome": "open", "outcome_r": None, "strategy": "A", "duplicate_of": None,
            "model": "cc/opus", "outcome_tp2": None}
    return {**base, **kw}


def test_opportunity_stats_funnel_and_reasons():
    rows = [row(id=1, executed=1, user_decision="approved", outcome="loss", outcome_r=-1.1),
            row(id=2, decision="rejected", reason="RR bersih 1.10 < 1.3", outcome="win", outcome_r=1.5, strategy="B"),
            row(id=3, decision="blocked", reason="berita High USD: CPI (0.5 jam lagi)", outcome="loss", outcome_r=-1.0),
            row(id=4, kind="opportunity", decision="wait", tier="Weak", outcome="not_reached", model="oc/big-pickle"),
            row(id=5, user_decision="declined", outcome="win", outcome_r=2.0, outcome_tp2="win"),
            row(id=6, user_decision="failed", outcome="ambiguous"),
            row(id=7, decision="rejected", reason="skor 5.5/10 Weak (< 6)", outcome="loss", outcome_r=-1.0)]
    st = opportunity_stats(rows)
    assert (st["detected"], st["ideas"], st["repeats"]) == (7, 7, 0)
    assert st["funnel"] == {"executed": 1, "exec_failed": 1, "owner_declined": 1, "risk_blocked": 1,
                            "gate_rejected": 1, "ai_low_quality": 1, "ai_wait": 1}
    assert (st["executable_offered"], st["rejected_by_gates"], st["wait_or_no_trade"]) == (3, 3, 1)
    assert (st["missed_winners"], st["avoided_losers"], st["false_positives"]) == (2, 2, 1)
    assert st["missed_winners_by_stage"] == {"owner_declined": 1, "gate_rejected": 1}
    assert (st["ambiguous"], st["not_reached"]) == (1, 1)
    assert st["tp2_after_tp1"] == {"n": 1, "reached": 1} and st["by_model"] == {"cc/opus": 6, "oc/big-pickle": 1}
    assert st["inactivity_reasons"] == {"RR bersih kurang": 1, "blok berita": 1, "skor Weak": 1, "AI WAIT": 1}
    assert opportunity_stats([]) == {"detected": 0}
    assert reason_key("sudah ada 1 posisi/limit setup aktif (maks 1)") == "sudah ada setup aktif"
    text = format_opportunity_stats(st, 14)
    assert "7 ide" in text and "ditolak Anda 1" in text and "Peluang menang hilang di tahap" in text


def test_repeated_idea_counted_once():
    # ide yang sama 4x (zona sama, satu dieksekusi belakangan): dihitung satu ide dengan tahap terjauh
    rows = [row(id=1, decision="blocked", reason="kuota", outcome="win", outcome_r=2.0),
            row(id=2, duplicate_of=1, decision="blocked", reason="kuota", outcome="duplicate"),
            row(id=3, duplicate_of=1, executed=1, user_decision="approved", outcome="duplicate"),
            row(id=4, duplicate_of=1, decision="duplicate", reason="sama dengan setup #3", outcome="duplicate")]
    st = opportunity_stats(rows)
    assert (st["detected"], st["ideas"], st["repeats"]) == (4, 1, 3)
    assert st["funnel"] == {"executed": 1} and st["missed_winners"] == 0 and st["simulated_resolved"] == 1


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
    assert "🔍 Peluang 14 hari terakhir: 1 ide (Moderate 1)" in bot.evaluation_text()
    created = (t + pd.Timedelta(minutes=1)).tz_localize("UTC").isoformat()   # offset server 0 di fixture
    bot.journal.db.execute("UPDATE opportunities SET created_at=?", (created,))
    bot.resolve_opportunities()
    row = opps(bot)[0]
    expected = simulate(m15, t + pd.Timedelta(minutes=1), 1, o.entry_low, o.entry_high, o.invalid_level, o.target,
                        600, row["cost_r"])
    assert expected is not None and (row["outcome"], row["outcome_r"]) == (expected.outcome, expected.r)


def test_same_setup_not_offered_twice_and_repeats_linked(tmp_path, osettings, at_signal, eurusd_meta):
    """Audit M7: ide EURUSD SELL yang sama ditawarkan 4x dalam 22 menit (12/11)."""
    bot, mt5, msgs = make_bot(tmp_path, osettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    offer(bot, mt5)
    offer(bot, mt5, entry_low=mt5.tick("EURUSD").ask - 0.0002)   # zona tumpang tindih, masih berlaku
    assert len(bot.notifier.buttons) == 1 and "tidak ditawarkan ulang" in msgs[-1]
    first, second = opps(bot)
    assert (first["decision"], second["decision"], second["duplicate_of"], second["outcome"]) == \
        ("offered", "duplicate", first["id"], "duplicate")
    # peluang WAIT yang diulang AI tiap jam: ditautkan ke baris pertama, tidak disimulasikan ulang
    o = Opportunity(symbol="EURUSD", side="SELL", score=6, entry_low=1.2, entry_high=1.21, invalid_level=1.22,
                    target=1.17)
    bot.offer_setups(Analysis(text="x", opportunities=[o]))
    bot.offer_setups(Analysis(text="x", opportunities=[o.model_copy(update={"entry_low": 1.205})]))
    wait1, wait2 = opps(bot)[2:]
    assert wait1["duplicate_of"] is None and wait2["duplicate_of"] == wait1["id"]
    assert opportunity_stats(opps(bot))["ideas"] == 2
    # zona lain / arah lain = ide baru
    offer(bot, mt5, entry_low=mt5.tick("EURUSD").ask - 0.0030, entry_high=mt5.tick("EURUSD").ask - 0.0020,
          stop_loss=mt5.tick("EURUSD").ask - 0.0060)
    assert opps(bot)[-1]["duplicate_of"] is None


def test_model_recorded_per_decision(tmp_path, osettings, at_signal, eurusd_meta):
    llm = FakeLLM()
    llm.last_model = "oc/big-pickle"   # model cadangan yang menjawab
    bot, mt5, msgs = make_bot(tmp_path, osettings, at_signal, eurusd_meta, llm, dry_run=False)
    offer(bot, mt5)
    assert opps(bot)[0]["model"] == "oc/big-pickle"
    assert bot.journal.db.execute("SELECT ai_model FROM signals").fetchone()[0] == "oc/big-pickle"
    bot.process_symbol("EURUSD")   # review sinyal Ichimoku juga mencatat model
    assert bot.journal.db.execute("SELECT ai_model FROM signals WHERE source='rule'").fetchone()[0] == "oc/big-pickle"


def test_setup_shadow_uses_m15_zone_not_h4(tmp_path, osettings, at_signal, eurusd_meta):
    """Audit H4: shadow setup yang tidak dieksekusi dinilai dari M15 dengan syarat zona tersentuh. Shadow lama
    (metode H4, shadow_method NULL) tidak ditulis ulang, tapi tidak lagi dihitung di statistik."""
    from test_confirm import click
    bot, mt5, msgs = make_bot(tmp_path, osettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    m15 = mt5.rates("EURUSD", "M15", 600)
    i = len(m15) - 60
    px, t = float(m15["close"].iloc[i]), m15["time"].iloc[i]
    far = px + 0.05   # zona yang tidak pernah tersentuh harga
    bot.offer_setups(Analysis(text="x", setups=[make_setup(far)]))
    click(bot, f"c:{bot.journal.pending_confirmations()[0]['id']}:no")   # ditolak pemilik -> shadow
    sid = bot.journal.open_shadows()[0]["id"]
    bot.journal.db.execute("UPDATE signals SET created_at=? WHERE id=?",
                           ((t + pd.Timedelta(minutes=1)).tz_localize("UTC").isoformat(), sid))
    old = bot.journal.log_signal("EURUSD", -1, "setup lama", 1.1, 0.004, 0.01, {}, source="ai_setup", setup={})
    bot.journal.update_signal(old, shadow_status="tp", shadow_r=5.8)   # hasil metode lama: tidak disentuh
    bot.opp_bar.clear()
    bot.resolve_opportunities()
    new = bot.journal.get_signal(sid)
    assert (new["shadow_status"], new["shadow_r"], new["shadow_method"]) == ("not_reached", None, "m15_zone")
    assert bot.journal.get_signal(old)["shadow_r"] == 5.8
    assert "setup_ai_semua" not in bot.journal.ai_vs_rules()   # lama dikecualikan, baru tanpa R
