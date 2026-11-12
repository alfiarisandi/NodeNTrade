"""Perintah & tombol Telegram: hanya dari chat yang diizinkan, pause, tutup 2 langkah, antrean kirim ulang."""
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

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
    assert "▶️ Resume" in bot.notifier.menus[-1][2]      # tombol toggle berganti
    say(bot, "▶️ Resume")
    assert bot.journal.get_state("paused") == "0"
    assert "⏸️ Pause" in bot.notifier.menus[-1][2]


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
                  invalid_if="close < 1.1950", valid_minutes=90)


def test_analysis_button_sends_text_and_respects_cooldown(bot):
    bot.llm = FakeAnalysisLLM([dict(GOOD_SETUP, take_profit_1=1.2030)])   # RR 0.6 -> ditolak batas keras
    say(bot, "📊 Analisis")
    msgs = bot.notifier.msgs
    assert msgs[-3].startswith("⏳ Menganalisis") and "EURUSD naik" in msgs[-2]
    assert msgs[-1].startswith("ℹ️ Setup tidak ditawarkan (RR 0.6 < 2)") and bot.llm.calls == 1
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
