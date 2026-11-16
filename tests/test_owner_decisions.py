"""Keputusan pemilik Tahap 5 audit 2026-11-16: Ichimoku EURUSD mode shadow (H6)."""
import pytest

from bot.config import ConfirmCfg, SymbolCfg, TelegramCfg
from test_confirm import click, make_setup
from test_live import FakeLLM, make_bot


@pytest.fixture
def shadow_settings(settings):
    return settings.model_copy(update={
        "symbols": [SymbolCfg(name="EURUSD", max_spread_points=20, rule_mode="shadow")],
        "confirm": ConfirmCfg(enabled=True), "telegram": TelegramCfg(token="t", chat_id="111")})


def test_shadow_mode_records_signal_without_ai_or_order(tmp_path, shadow_settings, at_signal, eurusd_meta):
    llm = FakeLLM()
    bot, mt5, msgs = make_bot(tmp_path, shadow_settings, at_signal, eurusd_meta, llm, dry_run=False)
    bot.process_symbol("EURUSD")
    row = bot.journal.db.execute("SELECT * FROM signals").fetchone()
    assert llm.calls == 0 and mt5.orders == [] and mt5.limits == [] and bot.notifier.buttons == []
    assert (row["ai_decision"], row["shadow_status"], row["executed"]) == ("shadow_mode", "open", 0)
    assert "Mode shadow" in msgs[-1]
    bot.process_symbol("EURUSD")                       # sinyal yang sama tidak dicatat dua kali
    assert len(bot.journal.shadow_mode_signals()) == 1
    bot.journal.resolve_shadow(row["id"], "sl", -1.0)
    assert "EURUSD mode shadow: 1 sinyal, 1 selesai, total -1.00R" in bot.evaluation_text()
    assert bot.journal.ai_vs_rules()["mode_shadow"]["n"] == 1   # tidak tercampur dengan "diloloskan" AI


def test_shadow_mode_keeps_ai_setups_and_live_symbols(tmp_path, shadow_settings, settings, at_signal, eurusd_meta):
    from bot.ai.analysis import Analysis
    bot, mt5, msgs = make_bot(tmp_path, shadow_settings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    bot.offer_setups(Analysis(text="x", setups=[make_setup(mt5.tick("EURUSD").ask)]))   # setup AI tetap jalan
    click(bot, f"c:{bot.journal.pending_confirmations()[0]['id']}:0.005")
    assert len(mt5.orders) == 1
    live_s = shadow_settings.model_copy(update={"symbols": [SymbolCfg(name="EURUSD", max_spread_points=20)]})
    live_bot, live_mt5, _ = make_bot(tmp_path / "live", live_s, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    live_bot.process_symbol("EURUSD")                  # mode live (default): sinyal ditawarkan seperti biasa
    assert live_bot.notifier.buttons and live_bot.journal.shadow_mode_signals() == []
