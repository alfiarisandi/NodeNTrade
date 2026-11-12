"""Konfirmasi manual lewat tombol Telegram sebelum order."""
from datetime import datetime, timedelta, timezone

import pytest

from bot.config import ConfirmCfg, TelegramCfg
from test_live import FakeLLM, make_bot


@pytest.fixture
def csettings(settings):
    return settings.model_copy(update={"confirm": ConfirmCfg(enabled=True),
                                       "telegram": TelegramCfg(token="t", chat_id="111")})


def click(bot, data, chat="111"):
    bot.notifier.queue.append({"update_id": 1, "callback_query": {
        "id": "cb1", "data": data, "message": {"chat": {"id": int(chat)}}}})
    bot.poll_confirmations()


def signal_row(bot):
    return bot.journal.db.execute("SELECT * FROM signals").fetchone()


def test_ai_approved_signal_asks_instead_of_ordering(tmp_path, csettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, csettings, at_signal, eurusd_meta, FakeLLM("reduce", 0.5), dry_run=False)
    bot.process_symbol("EURUSD")
    assert mt5.orders == []
    labels = [b for row in bot.notifier.buttons[0] for b, _ in row]
    # fixture: risiko dasar 1%, AI reduce x0.5 -> saran 0.5% ditandai bintang
    assert [l.split(" ")[:2] for l in labels] == [["✅", "0.25%"], ["⭐", "0.5%"], ["✅", "1%"], ["❌", "Tidak"]]
    assert signal_row(bot)["user_decision"] == "pending"


def test_user_picks_risk_and_order_is_sent(tmp_path, csettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, csettings, at_signal, eurusd_meta, FakeLLM("reduce", 0.5), dry_run=False)
    bot.process_symbol("EURUSD")
    sid = signal_row(bot)["id"]
    click(bot, f"c:{sid}:0.01")
    assert len(mt5.orders) == 1
    trade = bot.journal.open_trades()[0]
    assert 90.0 < trade["risk_amount"] <= 100.0   # risiko nyata (lot dibulatkan ke bawah) <= 1% pilihan user
    row = signal_row(bot)
    assert (row["user_decision"], row["user_risk"], row["executed"]) == ("approved", 0.01, 1)
    assert bot.notifier.cleared == [42] and "pilihan Anda" in msgs[-1]
    # klik ulang tombol yang sama tidak membuka posisi kedua
    click(bot, f"c:{sid}:0.01")
    assert len(mt5.orders) == 1 and bot.notifier.answers[-1] == "Sinyal ini sudah tidak berlaku"


def test_user_declines(tmp_path, csettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, csettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    bot.process_symbol("EURUSD")
    click(bot, f"c:{signal_row(bot)['id']}:no")
    row = signal_row(bot)
    assert mt5.orders == [] and row["user_decision"] == "declined" and row["shadow_status"] == "open"


def test_timeout_cancels(tmp_path, csettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, csettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    bot.process_symbol("EURUSD")
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    bot.journal.update_signal(signal_row(bot)["id"], confirm_expires=past)
    bot.poll_confirmations()
    row = signal_row(bot)
    assert mt5.orders == [] and row["user_decision"] == "expired" and row["shadow_status"] == "open"
    assert "habis" in msgs[-1]


def test_other_chat_and_invalid_risk_ignored(tmp_path, csettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, csettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    bot.process_symbol("EURUSD")
    sid = signal_row(bot)["id"]
    click(bot, f"c:{sid}:0.01", chat="999")
    click(bot, f"c:{sid}:0.05")          # 5% bukan pilihan & di atas batas keras
    assert mt5.orders == [] and signal_row(bot)["user_decision"] == "pending"


def test_price_moved_too_far_blocks(tmp_path, csettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, csettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    bot.process_symbol("EURUSD")
    row = signal_row(bot)
    bot.journal.update_signal(row["id"], ref_price=row["ref_price"] - 0.01)   # harga kini jauh dari sinyal
    click(bot, f"c:{row['id']}:0.005")
    assert mt5.orders == [] and signal_row(bot)["user_decision"] == "blocked"
    assert "bergerak" in msgs[-1]


def make_setup(price, **kw):
    from bot.ai.analysis import Setup
    base = dict(symbol="EURUSD", side="BUY", entry_low=price - 0.0005, entry_high=price + 0.0005,
                stop_loss=price - 0.0040, take_profit_1=price + 0.0100, take_profit_2=price + 0.0150,
                confidence="High", reason="BOS naik", valid_minutes=60)
    base.update(kw)
    return Setup(**base)


def offer(bot, mt5, **kw):
    from bot.ai.analysis import Analysis
    bot.offer_setups(Analysis(text="x", setups=[make_setup(mt5.tick("EURUSD").ask, **kw)]))


def test_setup_offered_and_executed_at_setup_levels(tmp_path, csettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, csettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    offer(bot, mt5)
    row = signal_row(bot)
    assert row["source"] == "ai_setup" and row["user_decision"] == "pending" and row["ai_decision"] == "analysis"
    assert "SETUP BUY EURUSD" in msgs[-1] and "Risiko dasar" in msgs[-1] and "dalam 30 menit" in msgs[-1]
    click(bot, f"c:{row['id']}:0.005")
    assert len(mt5.orders) == 1
    sym, d, vol, tp_dist = mt5.orders[0]
    assert tp_dist == pytest.approx(0.0100, abs=1e-6)          # TP tepat di TP1 setup, bukan jarak dari zona
    assert signal_row(bot)["executed"] == 1 and bot.setups_today() == 1
    assert "pilihan Anda" in msgs[-1]


def test_setup_blocked_when_price_leaves_zone(tmp_path, csettings, at_signal, eurusd_meta):
    from types import SimpleNamespace
    bot, mt5, msgs = make_bot(tmp_path, csettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    offer(bot, mt5)
    far = mt5.tick("EURUSD").ask + 0.0030
    mt5.tick = lambda symbol: SimpleNamespace(bid=far - 0.0001, ask=far)
    click(bot, f"c:{signal_row(bot)['id']}:0.005")
    row = signal_row(bot)
    assert mt5.orders == [] and row["user_decision"] == "blocked" and row["shadow_status"] == "open"
    assert "di luar zona entry" in msgs[-1]


def test_setup_quota_and_low_confidence_not_offered(tmp_path, csettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, csettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    offer(bot, mt5, confidence="Low")
    assert "confidence Low" in msgs[-1] and bot.notifier.buttons == []
    bot.s = csettings.model_copy(update={"confirm": csettings.confirm.model_copy(update={"max_setups_per_day": 0})})
    offer(bot, mt5)
    assert "Kuota 0 setup/hari" in msgs[-1] and bot.notifier.buttons == [] and signal_row(bot) is None


def test_ai_reject_has_no_buttons(tmp_path, csettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, csettings, at_signal, eurusd_meta, FakeLLM("reject", 0), dry_run=False)
    bot.process_symbol("EURUSD")
    assert bot.notifier.buttons == [] and "ditolak AI" in msgs[-1]


def test_confirm_requires_telegram(tmp_path, csettings, at_signal, eurusd_meta):
    bot, *_ = make_bot(tmp_path, csettings, at_signal, eurusd_meta, None, dry_run=False)
    bot.notifier.enabled = False
    with pytest.raises(ValueError, match="Telegram"):
        bot.startup()


def test_notifier_never_logs_token(caplog):
    import requests

    from bot.notifier import Notifier

    n = Notifier(TelegramCfg(token="123:SECRET", chat_id="1"))
    err = requests.ConnectionError("HTTPSConnectionPool url: /bot123:SECRET/getUpdates timed out")
    assert "SECRET" not in n._safe(err)
