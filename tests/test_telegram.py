"""Perintah & tombol Telegram: hanya dari chat yang diizinkan, pause, tutup 2 langkah, antrean kirim ulang."""
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from bot.config import TelegramCfg
from bot.notifier import Notifier
from test_live import FakeLLM, make_bot


@pytest.fixture
def tsettings(settings):
    return settings.model_copy(update={"telegram": TelegramCfg(token="t", chat_id="111")})


@pytest.fixture
def bot(tmp_path, tsettings, at_signal, eurusd_meta):
    b, mt5, msgs = make_bot(tmp_path, tsettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    mt5.open_positions.append(SimpleNamespace(ticket=5, symbol="EURUSD", type=0, time=1, volume=0.1,
                                              price_open=1.0, sl=0.9, tp=0.0, profit=12.5, swap=0.0))
    return b


def say(bot, text, chat=111):
    bot.notifier.queue.append({"update_id": 1, "message": {"chat": {"id": chat}, "text": text}})
    bot.poll_confirmations()


def click(bot, data, chat=111):
    bot.notifier.queue.append({"update_id": 2, "callback_query": {
        "id": "cb", "data": data, "message": {"chat": {"id": chat}, "message_id": 9}}})
    bot.poll_confirmations()


def test_other_chat_is_ignored(bot):
    say(bot, "/pause", chat=999)
    click(bot, "pcy:5:1", chat=999)
    assert bot.journal.get_state("paused") is None and bot.mt5.closed_positions == []
    assert bot.notifier.answers[-1] == "Tidak diizinkan"


def test_read_only_commands_reply(bot):
    msgs = bot.notifier.msgs
    for c in ("/status", "/pnl", "/sinyal", "/help", "/status@MyBot"):
        say(bot, c)
    assert len(msgs) == 5
    assert "Equity 10000.00" in msgs[0] and "Candle entry berikut" in msgs[0]
    assert "Hari ini: 0 trade" in msgs[1]
    assert "/pause" in msgs[3]


def test_posisi_has_buttons(bot):
    say(bot, "/posisi")
    assert "#5" in bot.notifier.msgs[-1] and "+12.50" in bot.notifier.msgs[-1]
    assert [d for _, d in bot.notifier.buttons[-1][0]] == ["pc:5", "be:5"]


def test_pause_blocks_entry_and_resume_allows(bot):
    say(bot, "/pause")
    assert bot.journal.get_state("paused") == "1"
    assert "dijeda" in bot.entry_block("EURUSD", "EURUSD", 1, 10_000, 1)
    say(bot, "/resume")
    assert "dijeda" not in (bot.entry_block("EURUSD", "EURUSD", 1, 10_000, 1) or "")


def test_menu_buttons_run_commands(bot):
    say(bot, "📟 Status")
    assert "Equity 10000.00" in bot.notifier.msgs[-1]
    say(bot, "📂 Posisi")
    assert "#5" in bot.notifier.msgs[-1]
    say(bot, "⏸️ Pause")
    assert bot.journal.get_state("paused") == "1"
    assert "▶️ Resume" in bot.notifier.menus[-1][3]      # tombol toggle berganti
    say(bot, "▶️ Resume")
    assert bot.journal.get_state("paused") == "0"
    assert "⏸️ Pause" in bot.notifier.menus[-1][3]


class FakeAnalysisLLM:
    def __init__(self, setups=()):
        self.calls = 0
        self.reply = json.dumps({"text": "MARKET OVERVIEW\nEURUSD naik", "setups": list(setups)})

    def complete(self, system, user):
        self.calls += 1
        snap = json.loads(user.split("\n", 1)[1])
        assert "EURUSD" in snap["symbols"] and "session" in snap
        return self.reply


GOOD_SETUP = dict(symbol="EURUSD", side="BUY", entry_low=1.1990, entry_high=1.2010, stop_loss=1.1950,
                  take_profit_1=1.2110, take_profit_2=1.2200, confidence="High", reason="BOS naik", wait_for="pin bar",
                  invalid_if="close < 1.1950", valid_minutes=90,
                  score=dict(trend=18, structure=18, key_level=13, price_action=17, momentum=12, rr=8))


def test_analysis_button_sends_text_and_respects_cooldown(bot):
    bot.llm = FakeAnalysisLLM([dict(GOOD_SETUP, take_profit_1=1.2030)])   # RR 0.6 -> ditolak batas keras
    say(bot, "📊 Analisis")
    msgs = bot.notifier.msgs
    assert msgs[-3].startswith("⏳ Menganalisis") and "EURUSD naik" in msgs[-2]
    assert msgs[-1].startswith("ℹ️ Setup tidak ditawarkan (RR 0.6 < 1.5)") and bot.llm.calls == 1
    say(bot, "📊 Analisis")
    assert "Coba lagi dalam" in bot.notifier.msgs[-1] and bot.llm.calls == 1
    assert "📊 Analisis" in bot.menu_rows()[2]


def test_analysis_without_llm_and_failure(bot):
    bot.llm = None
    say(bot, "/analisis")
    assert "butuh AI aktif" in bot.notifier.msgs[-1]

    class Broken:
        def complete(self, system, user):
            raise RuntimeError("9Router mati")

    bot.llm = Broken()
    bot.journal.set_state("analysis_last_ts", "0")
    say(bot, "/analisis")
    assert "Analisis gagal: 9Router mati" in bot.notifier.msgs[-1]


def test_scheduled_analysis_once_per_hour(bot, tsettings):
    bot.s = tsettings.model_copy(update={"ai": tsettings.ai.model_copy(update={"analysis_hours_wib": [14]})})
    bot.llm = FakeAnalysisLLM()
    wed_14_wib = datetime(2026, 1, 14, 7, 0, tzinfo=timezone.utc)
    bot.maybe_scheduled_analysis(wed_14_wib)
    bot.maybe_scheduled_analysis(wed_14_wib)
    bot.maybe_scheduled_analysis(datetime(2026, 1, 14, 8, 0, tzinfo=timezone.utc))    # 15 WIB: bukan jadwal
    bot.maybe_scheduled_analysis(datetime(2026, 1, 17, 7, 0, tzinfo=timezone.utc))    # Sabtu
    assert bot.llm.calls == 1


def test_free_text_and_start_show_menu(bot):
    for text in ("halo", "/start", "/menu"):
        say(bot, text)
    assert len(bot.notifier.menus) == 3
    assert bot.notifier.menus[-1][0] == ["📟 Status", "📂 Posisi"]


def test_close_needs_two_steps(bot):
    click(bot, "pc:5")
    assert bot.mt5.closed_positions == []            # langkah 1 hanya minta konfirmasi
    assert bot.notifier.buttons[-1][0][0][1].startswith("pcy:5:")
    click(bot, bot.notifier.buttons[-1][0][0][1])
    assert bot.mt5.closed_positions == [5]


def test_close_confirm_expires_and_cancel(bot):
    click(bot, f"pcy:5:{int(time.time()) - 1000}")
    assert bot.mt5.closed_positions == [] and "kedaluwarsa" in bot.notifier.answers[-1]
    click(bot, "pcn:5")
    assert bot.mt5.closed_positions == []


def test_breakeven_button(bot):
    click(bot, "be:5")          # harga ~1.2 > open 1.0, SL 0.9 -> boleh
    assert "SL di breakeven" in bot.notifier.answers[-1]


def test_r_alert_once(bot):
    bot.journal.log_trade_open(1, 5, "EURUSD", 1, 0.1, 1.0, 0.9, None, 10.0)   # risiko $10, profit 12.5 = 1.25R
    bot.send_alerts(datetime.now(timezone.utc))
    bot.send_alerts(datetime.now(timezone.utc))
    assert sum("tembus 1R" in m for m in bot.notifier.msgs) == 1


def test_failed_send_is_retried(monkeypatch):
    n = Notifier(TelegramCfg(token="t", chat_id="1"))
    state = {"up": False, "sent": []}

    def post(url, json=None, timeout=0):
        if not state["up"]:
            raise requests.ConnectionError("down")
        state["sent"].append(json["text"])
        return SimpleNamespace(ok=True, status_code=200, text="", json=lambda: {"ok": True, "result": []})

    monkeypatch.setattr("bot.notifier.requests.post", post)
    assert n.send("a") is False and n.pending == ["a"]
    state["up"] = True
    assert n.send("b") is True
    assert state["sent"] == ["a", "b"] and n.pending == []


def test_keyboard_payload_and_fallback(monkeypatch):
    n = Notifier(TelegramCfg(token="t", chat_id="1"))
    state = {"up": True, "sent": []}

    def post(url, json=None, timeout=0):
        if not state["up"]:
            raise requests.ConnectionError("down")
        state["sent"].append(json)
        return SimpleNamespace(ok=True, status_code=200, text="", json=lambda: {"ok": True, "result": {}})

    monkeypatch.setattr("bot.notifier.requests.post", post)
    assert n.send_keyboard("menu", [["A", "B"]]) is True
    kb = state["sent"][-1]["reply_markup"]
    assert kb["keyboard"] == [[{"text": "A"}, {"text": "B"}]] and kb["resize_keyboard"]
    state["up"] = False
    assert n.send_keyboard("x", [["A"]]) is False and n.pending == ["x"]   # isi pesan tetap dikirim ulang
    assert n.updates() == [] and n.retry_at > 0                              # backoff saat internet putus


def test_two_losses_pause_until_owner_continues(bot, tsettings):
    bot.s = tsettings.model_copy(update={"risk": tsettings.risk.model_copy(update={"stop_after_losses": 2})})
    for i, profit in enumerate((-10.0, -12.0)):
        bot.journal.log_trade_open(1, 100 + i, "EURUSD", 1, 0.1, 1.0, 0.99, None, 10.0)
        bot.mt5.closed[100 + i] = {"close_price": 0.99, "close_time": datetime(2026, 1, 14, 9 + i, tzinfo=timezone.utc),
                                   "profit": profit}
    bot.journal.log_trade_open(1, 5, "EURUSD", 1, 0.1, 1.0, 0.9, None, 10.0)    # posisi fixture masih terbuka
    bot.sync_closed_trades()
    assert bot.loss_pause() and "rugi beruntun" in bot.entry_block("EURUSD", "EURUSD", 1, 10_000, 1)
    assert bot.notifier.buttons[-1][0][0][1] == "go:1" and sum("2 rugi beruntun" in m for m in bot.notifier.msgs) == 1
    click(bot, "go:1", chat=999)
    assert bot.loss_pause()                                             # chat lain tidak bisa melanjutkan
    click(bot, "go:1")
    assert not bot.loss_pause() and "rugi beruntun" not in (bot.entry_block("EURUSD", "EURUSD", 1, 10_000, 1) or "")


class FakePositionLLM:
    def __init__(self, advice):
        self.reply = json.dumps({"text": "ringkasan", "advice": advice})

    def complete(self, system, user):
        snap = json.loads(user.split("\n", 1)[1])
        assert snap["open_positions"][0]["ticket"] == 5 and "EURUSD" in snap["symbols"]
        return self.reply


def test_position_review_buttons_and_guards(bot):
    price = bot.mt5.tick("EURUSD").bid                      # posisi fixture: BUY 0.1 lot, open 1.0, SL 0.9
    bot.llm = FakePositionLLM([
        {"ticket": 5, "action": "MOVE_SL", "new_sl": 1.05, "reason": "di bawah swing"},
        {"ticket": 5, "action": "MOVE_SL", "new_sl": 0.80, "reason": "melebarkan"},          # ditolak: menjauh
        {"ticket": 5, "action": "MOVE_SL", "new_sl": price + 0.01, "reason": "lewat harga"},  # ditolak
        {"ticket": 5, "action": "PARTIAL_CLOSE", "close_frac": 0.5, "reason": "momentum melemah"},
        {"ticket": 5, "action": "CLOSE", "reason": "struktur patah"},
        {"ticket": 999, "action": "CLOSE", "reason": "tiket tidak ada"},
    ])
    say(bot, "🩺 Posisi AI")
    datas = [row[0][0][1] for row in bot.notifier.buttons]
    assert datas == ["pa:5:sl:1.05000", "pa:5:pc:0.05", "pc:5"]
    assert sum("tidak bisa diterapkan" in m for m in bot.notifier.msgs) == 2
    click(bot, "pa:5:sl:1.05000")
    assert bot.mt5.sl_moves == [(5, 1.05)] and "disetujui Anda" in bot.notifier.msgs[-1]
    click(bot, "pa:5:sl:0.95000")                           # sekarang SL 1.05: 0.95 menjauh -> ditolak saat klik
    assert bot.mt5.sl_moves == [(5, 1.05)] and "Tidak diterapkan" in bot.notifier.answers[-1]
    click(bot, "pa:5:pc:0.05")
    assert bot.mt5.partials == [(5, 0.05)]


class FakeLimitLLM:
    def __init__(self, limit_advice):
        self.reply = json.dumps({"text": "ringkasan", "advice": [], "limit_advice": limit_advice})
        self.snap = None

    def complete(self, system, user):
        self.snap = json.loads(user.split("\n", 1)[1])
        return self.reply


def add_sell_limit(bot, price, ticket=801):   # FakeMT5 memberi tiket 901+ untuk limit baru
    """Order limit SELL dari setup 📊 yang sedang menunggu: SL +30 pip, TP -60 pip, risiko 0.5%."""
    setup = dict(symbol="EURUSD", side="SELL", entry_low=price, entry_high=price, stop_loss=price + 0.0030,
                 take_profit_1=price - 0.0060, take_profit_2=price - 0.0090, reason="supply H1", valid_minutes=120,
                 score=dict(trend=18, structure=18, key_level=13, price_action=17, momentum=12, rr=8))
    sid = bot.journal.log_signal("EURUSD", -1, f"setup {ticket}", price, 0.0030, 0.0060, {}, source="ai_setup",
                                 setup=setup)
    expires = datetime.now(timezone.utc).replace(microsecond=0) + __import__("datetime").timedelta(hours=1)
    bot.journal.update_signal(sid, user_decision="limit", user_risk=0.005, limit_ticket=ticket,
                              limit_expires=expires.isoformat())
    bot.mt5.pending.append(SimpleNamespace(ticket=ticket, magic=bot.s.magic, volume_current=0.16))
    return sid


def test_limit_review_move_cancel_and_guards(bot):
    bid = bot.mt5.tick("EURUSD").bid
    sid = add_sell_limit(bot, round(bid + 0.0020, 5))
    p1 = round(bid + 0.0025, 5)
    bot.llm = FakeLimitLLM([
        {"ticket": 801, "action": "MOVE", "new_price": p1, "new_sl": p1 + 0.0030, "new_tp": p1 - 0.0060, "reason": "supply baru"},
        {"ticket": 801, "action": "MOVE", "new_price": bid - 0.0010, "new_sl": bid + 0.0020, "new_tp": bid - 0.0080,
         "reason": "kejar harga"},                                             # ditolak: SELL limit di bawah bid
        {"ticket": 801, "action": "MOVE", "new_price": p1, "new_sl": p1 + 0.0030, "new_tp": p1 - 0.0020,
         "reason": "TP dekat"},                                                # ditolak: RR < 1.5
        {"ticket": 801, "action": "CANCEL", "reason": "struktur patah"},
        {"ticket": 801, "action": "KEEP", "reason": "level masih valid"},
        {"ticket": 999, "action": "CANCEL", "reason": "tiket tidak ada"},
    ])
    say(bot, "🩺 Posisi AI")
    lim = bot.llm.snap["pending_limit_orders"]
    assert [x["ticket"] for x in lim] == [801] and lim[0]["side"] == "SELL" and lim[0]["reward_risk"] == 2.0
    assert "1 posisi dan 1 order limit" in " ".join(bot.notifier.msgs)
    datas = [row[0][0][1] for row in bot.notifier.buttons]
    assert len(datas) == 2 and datas[0].startswith(f"la:801:mv:{p1:.5f}:") and datas[1].startswith("la:801:cx:")
    assert sum("tidak bisa diterapkan" in m for m in bot.notifier.msgs) == 2
    assert any(m.startswith("🩺 KEEP | LIMIT SELL EURUSD") for m in bot.notifier.msgs)

    click(bot, datas[0])                                    # pindahkan: limit lama batal, baru dipasang
    _, d, vol, price, sl, tp = bot.mt5.limits[-1]
    assert (d, price) == (-1, p1) and sl == pytest.approx(p1 + 0.0030) and tp == pytest.approx(p1 - 0.0060)
    assert vol == pytest.approx(0.16)                       # risiko 0.5% dari $10k / 30 pip, tidak bertambah
    row = bot.journal.get_signal(sid)
    assert row["limit_ticket"] == 901
    assert [o.ticket for o in bot.mt5.pending] == [row["limit_ticket"]]
    assert json.loads(row["setup"])["take_profit_1"] == pytest.approx(p1 - 0.0060) and "dipindah" in bot.notifier.msgs[-1]

    click(bot, datas[1])                                    # tombol batal untuk tiket lama: sudah tidak ada
    assert "sudah tidak ada" in bot.notifier.answers[-1] and len(bot.mt5.pending) == 1
    new = row["limit_ticket"]
    click(bot, f"la:{new}:cx:{int(time.time()) - 3600}")    # tombol kedaluwarsa
    assert "kedaluwarsa" in bot.notifier.answers[-1] and len(bot.mt5.pending) == 1
    click(bot, f"la:{new}:cx:{int(time.time())}")
    assert bot.mt5.pending == [] and bot.journal.get_signal(sid)["user_decision"] == "limit_cancelled"


def test_review_with_only_limit_and_nothing(bot):
    bot.mt5.open_positions.clear()
    say(bot, "🩺 Posisi AI")
    assert "Tidak ada posisi terbuka atau order limit" in bot.notifier.msgs[-1]
    add_sell_limit(bot, round(bot.mt5.tick("EURUSD").bid + 0.0020, 5))
    bot.llm = FakeLimitLLM([{"ticket": 801, "action": "KEEP", "reason": "ok"}])
    say(bot, "🩺 Posisi AI")
    assert bot.llm.snap["open_positions"] == [] and len(bot.llm.snap["pending_limit_orders"]) == 1
    assert "1 order limit" in " ".join(bot.notifier.msgs)


class FakeWatchLLM:
    """Selalu WAIT dengan daftar pantauan yang sama; mencatat snapshot tiap panggilan."""
    def __init__(self, watch):
        self.snaps, self.watch = [], watch

    @property
    def calls(self):
        return len(self.snaps)

    def complete(self, system, user):
        self.snaps.append(json.loads(user.split("\n", 1)[1]))
        return json.dumps({"text": "WAIT / NO TRADE", "setups": [], "watch": self.watch})


WED_15_WIB = datetime(2026, 1, 14, 8, 5, tzinfo=timezone.utc)


def watch_setup(bot, tsettings, watch, cap=6):
    bot.s = tsettings.model_copy(update={"ai": tsettings.ai.model_copy(update={
        "analysis_hours_wib": [15, 16], "max_trigger_analyses_per_day": cap})})
    bot.mt5.open_positions.clear()
    bot.llm = FakeWatchLLM(watch)
    say(bot, "📊 Analisis")
    bot.journal.set_state("analysis_last_ts", "0")
    # pantauan dibuat 07:45 UTC (15 menit sebelum candle uji pertama close); expires tetap dari jam sekarang
    bot.journal.set_state("watchlist", json.dumps([dict(it, created="2026-01-14T07:45:00+00:00")
                                                   for it in bot.watchlist()]))


def m15_bar(bot, minute, close, high=None, low=None):
    """Ganti data M15 FakeMT5: candle terakhir yang sudah close, mulai 08:00 UTC + `minute` (offset 0)."""
    t = pd.Timestamp("2026-01-14 08:00") + pd.Timedelta(minutes=minute)
    df = pd.DataFrame({"time": [t], "open": [close], "high": [high or close], "low": [low or close],
                       "close": [close]})
    orig = type(bot.mt5).rates
    bot.mt5.rates = lambda s, tf, n: df if tf == "M15" else orig(bot.mt5, s, tf, n)


def test_watch_trigger_reanalyses_once(bot, tsettings):
    p = bot.mt5.tick("EURUSD").bid
    watch_setup(bot, tsettings, [
        dict(symbol="EURUSD", side="BUY", trigger="close_above", level=round(p + 0.0020, 5),
             invalid_level=round(p - 0.0030, 5), strategy="D", note="retest"),
        dict(symbol="EURUSD", side="BUY", trigger="close_above", level=round(p - 0.0010, 5)),   # sudah terpenuhi
        dict(symbol="XAUUSD", side="SELL", trigger="close_below", level=3888.4)])            # pair tidak diperdagangkan
    assert "👀 Dipantau" in bot.notifier.msgs[-1] and len(bot.watchlist()) == 1
    m15_bar(bot, -30, p + 0.0025)                     # close 07:45, sebelum/saat pantauan dibuat: diabaikan
    bot.maybe_watch_triggers(WED_15_WIB)
    assert bot.llm.calls == 1 and len(bot.watchlist()) == 1
    m15_bar(bot, 0, p + 0.0010)                       # belum menembus level
    bot.maybe_watch_triggers(WED_15_WIB)
    assert bot.llm.calls == 1 and len(bot.watchlist()) == 1
    m15_bar(bot, 15, p + 0.0025, high=p + 0.0030)     # close di atas level -> analisis ulang
    bot.maybe_watch_triggers(WED_15_WIB)
    bot.maybe_watch_triggers(WED_15_WIB)              # candle sama tidak dicek dua kali
    assert bot.llm.calls == 2
    assert any(m.startswith("🎯 Pemicu kena: BUY EURUSD (D)") for m in bot.notifier.msgs)
    trig = bot.llm.snaps[-1]["owner_rules"]["triggered_by"]
    assert trig["trigger"] == "close_above" and trig["fired"]["close"] == round(p + 0.0025, 5)
    assert "1h before" in bot.llm.snaps[-1]["owner_rules"]["news_block_for_setups"]
    assert bot.journal.get_state("trigger_count:2026-01-14") == "1"
    assert len(bot.watchlist()) == 1   # analisis baru mengganti pantauan


def test_watch_invalid_expired_and_outside_hours(bot, tsettings):
    p = bot.mt5.tick("EURUSD").bid
    item = dict(symbol="EURUSD", side="BUY", trigger="close_above", level=round(p + 0.0020, 5),
                invalid_level=round(p - 0.0030, 5))
    watch_setup(bot, tsettings, [item])
    m15_bar(bot, 0, p + 0.0025)
    bot.maybe_watch_triggers(datetime(2026, 1, 14, 2, 0, tzinfo=timezone.utc))   # 09 WIB: di luar jam
    assert bot.llm.calls == 1 and len(bot.watchlist()) == 1
    m15_bar(bot, 15, p - 0.0040)                     # close di bawah pembatal
    bot.maybe_watch_triggers(WED_15_WIB)
    assert bot.llm.calls == 1 and bot.watchlist() == []
    bot.journal.set_state("watchlist", json.dumps([dict(item, id="x", created="2026-01-14T07:00:00+00:00",
                                                        expires="2026-01-14T08:00:00+00:00")]))
    m15_bar(bot, 30, p + 0.0025)
    bot.maybe_watch_triggers(WED_15_WIB)              # kedaluwarsa 08:00 UTC
    assert bot.llm.calls == 1 and bot.watchlist() == []


def test_watch_held_by_cooldown_pause_and_daily_cap(bot, tsettings):
    p = bot.mt5.tick("EURUSD").bid
    watch_setup(bot, tsettings, [dict(symbol="EURUSD", side="BUY", trigger="close_above",
                                      level=round(p + 0.0020, 5))], cap=1)
    bot.journal.set_state("analysis_last_ts", f"{time.time():.0f}")   # cooldown aktif
    m15_bar(bot, 0, p + 0.0025)
    bot.maybe_watch_triggers(WED_15_WIB)
    bot.maybe_watch_triggers(WED_15_WIB)              # siklus berikut, masih cooldown: tanpa pesan berulang
    assert bot.llm.calls == 1 and len(bot.watchlist()) == 1
    assert not any("🎯" in m or "Coba lagi" in m for m in bot.notifier.msgs)
    bot.journal.set_state("analysis_last_ts", "0")
    say(bot, "/pause")
    bot.maybe_watch_triggers(WED_15_WIB)              # cooldown lewat, tapi entry dijeda
    assert bot.llm.calls == 1 and len(bot.watchlist()) == 1
    say(bot, "/resume")
    m15_bar(bot, 15, p + 0.0025)
    bot.maybe_watch_triggers(WED_15_WIB)
    assert bot.llm.calls == 2
    bot.journal.set_state("analysis_last_ts", "0")
    m15_bar(bot, 30, p + 0.0025)
    bot.maybe_watch_triggers(WED_15_WIB)              # batas 1 analisis pemicu/hari
    assert bot.llm.calls == 2 and len(bot.watchlist()) == 1


def screener_setup(bot, tsettings, cands, minute=0):
    bot.s = tsettings.model_copy(update={"ai": tsettings.ai.model_copy(update={
        "analysis_hours_wib": [15, 16], "screener": True, "max_trigger_analyses_per_day": 2})})
    bot.mt5.open_positions.clear()
    bot.llm = FakeWatchLLM([])
    p = bot.mt5.tick("EURUSD").bid
    m15_bar(bot, minute, p)
    bar = bot.mt5.rates("EURUSD", "M15", 1).iloc[-1]
    bot.screen_candidates = lambda sym: (bar, [dict(c, symbol=sym) for c in cands])


SELL_C = dict(side="SELL", strategy="C", regime="TREND", rule_stop_ref=1.2345)


def test_screener_calls_ai_on_candidate_once(bot, tsettings):
    screener_setup(bot, tsettings, [SELL_C, dict(SELL_C, side="BUY", strategy="E")])
    bot.maybe_screener(WED_15_WIB)
    bot.maybe_screener(WED_15_WIB)                    # candle sama: tidak dicek ulang
    assert bot.llm.calls == 1
    assert any(m.startswith("🔎 Screener M15 15:15 WIB: kandidat SELL EURUSD strategi C VWAP + EMA") for m in bot.notifier.msgs)
    trig = bot.llm.snaps[-1]["owner_rules"]["triggered_by"]
    assert trig["source"] == "screener" and trig["rule_stop_ref"] == 1.2345 and trig["other_candidates"][0]["side"] == "BUY"
    bot.journal.set_state("analysis_last_ts", "0")
    m15_bar(bot, 15, bot.mt5.tick("EURUSD").bid)    # candle baru, pair + arah sama < 60 menit: tidak dikirim lagi
    bot.maybe_screener(WED_15_WIB)
    assert bot.llm.calls == 1


def test_screener_skips_seen_bar_cooldown_and_blocks(bot, tsettings):
    screener_setup(bot, tsettings, [SELL_C])
    bot.journal.set_state("analysis_last_ts", f"{time.time():.0f}")   # analisis terjadwal baru jalan (cooldown)
    bot.maybe_screener(WED_15_WIB)
    assert bot.llm.calls == 0 and "EURUSD" in bot.screen_bar          # candle sudah dilihat analisis itu
    screener_setup(bot, tsettings, [SELL_C], minute=15)
    bot.journal.set_state("analysis_last_ts", "1")
    say(bot, "/pause")
    bot.maybe_screener(WED_15_WIB)
    assert bot.llm.calls == 0
    say(bot, "/resume")
    screener_setup(bot, tsettings, [SELL_C], minute=30)
    bot.maybe_screener(datetime(2026, 1, 14, 2, 0, tzinfo=timezone.utc))   # 09 WIB: di luar jam
    bot.s = bot.s.model_copy(update={"ai": bot.s.ai.model_copy(update={"screener": False})})
    bot.maybe_screener(WED_15_WIB)
    assert bot.llm.calls == 0


def test_screen_candidates_on_real_frames(bot):
    res = bot.screen_candidates("EURUSD")
    if res is not None:   # fixture cukup panjang: hasil berupa (bar, daftar kandidat) yang bisa dikirim ke AI
        bar, cands = res
        assert "close" in bar and all(c["side"] in ("BUY", "SELL") and c["strategy"] in "ABCDE" for c in cands)
        json.dumps(cands)


def test_gap_notice_after_sleep(bot):
    bot.check_gap(1_000_000.0)
    bot.check_gap(1_000_020.0)
    assert not any("💤" in m for m in bot.notifier.msgs)
    bot.check_gap(1_000_020.0 + 2 * 3600 + 15 * 60)
    assert sum("💤 Bot tidak berjalan 2j15m" in m for m in bot.notifier.msgs) == 1


def test_evaluation_report(bot):
    say(bot, "📋 Evaluasi")
    assert "Belum ada trade tertutup" in bot.notifier.msgs[-1]
    sid = bot.journal.log_signal("EURUSD", 1, "s1", 1.0, 0.01, 0.02, {}, source="ai_setup", setup={"take_profit_1": 1.02})
    for i, profit in enumerate((20.0, -10.0, 15.0)):
        bot.journal.log_trade_open(sid, 200 + i, "EURUSD", 1, 0.1, 1.0, 0.99, 1.02, 10.0)
        bot.journal.close_trade(200 + i, 1.01, f"2026-01-14T1{i}:30:00+00:00", profit)
    bot.journal.db.execute("UPDATE trades SET open_time='2026-01-14T09:00:00+00:00' WHERE ticket >= 200")
    say(bot, "/evaluasi")
    text = bot.notifier.msgs[-1]
    assert "3 trade tertutup" in text and "Win rate 66.7%" in text and "Profit factor 3.5" in text
    assert "Expectancy per trade +8.33" in text and "Per sesi: London 3x" in text and "Sampel baru 3 trade" in text
