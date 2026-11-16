"""Keputusan pemilik Tahap 5 audit 2026-11-16: Ichimoku EURUSD mode shadow (H6), blokir posisi per sumber (H3) dan
batas total risiko terbuka 1% (M4)."""
from types import SimpleNamespace

import pytest

from bot.ai.analysis import Analysis
from bot.config import ConfirmCfg, SymbolCfg, TelegramCfg
from test_confirm import click, make_setup
from test_live import FakeLLM, make_bot


def pos(ticket, side, volume, open_, sl, comment="bot user", symbol="EURUSD"):
    return SimpleNamespace(ticket=ticket, symbol=symbol, type=0 if side == "BUY" else 1, volume=volume,
                           price_open=open_, sl=sl, tp=0.0, profit=0.0, time=ticket, comment=comment)


def pending(ticket, side, volume, price, sl, magic):
    return SimpleNamespace(ticket=ticket, magic=magic, symbol="EURUSD", type=2 if side == "BUY" else 3,
                           volume_current=volume, price_open=price, sl=sl, comment="bot user limit")


@pytest.fixture
def risk_settings(settings):
    return settings.model_copy(update={
        "risk": settings.risk.model_copy(update={"max_open_risk": 0.01}),
        "confirm": ConfirmCfg(enabled=True), "telegram": TelegramCfg(token="t", chat_id="111")})


def test_open_risk_counts_positions_and_pending_once_and_survives_restart(tmp_path, risk_settings, at_signal,
                                                                         eurusd_meta):
    bot, mt5, _ = make_bot(tmp_path, risk_settings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    mt5.open_positions += [pos(1, "BUY", 0.1, 1.1000, 1.0950),                  # 0.1 x 50 pip = $50
                           pos(2, "SELL", 0.05, 1.1000, 1.1000, "bot user setup")]   # SL di breakeven = $0
    mt5.pending.append(pending(3, "SELL", 0.05, 1.1050, 1.1100, bot.s.magic))  # 0.05 x 50 pip = $25
    assert bot.open_risk() == (pytest.approx(75.0), None)
    # limit terisi: hilang dari pending, muncul sebagai posisi -> total sama, tidak terhitung ganda
    mt5.pending.clear()
    mt5.open_positions.append(pos(3, "SELL", 0.05, 1.1050, 1.1100, "bot user limit"))
    assert bot.open_risk()[0] == pytest.approx(75.0)
    restarted, *_ = make_bot(tmp_path, risk_settings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    restarted.mt5 = mt5                                   # bot baru, state MT5 yang sama: hasil identik
    assert restarted.open_risk()[0] == pytest.approx(75.0)
    assert "Risiko terbuka (posisi + limit, bila semua SL kena): 75.00 = 0.75%" in bot.open_risk_text(10_000.0)
    # posisi tanpa SL dan tanpa catatan risiko di jurnal: risiko tidak diketahui -> entry ditolak
    mt5.open_positions.append(pos(9, "BUY", 0.1, 1.1, 0.0))
    assert "tanpa SL" in bot.open_risk_block(10_000.0, 0.0025)


def test_entries_rejected_or_capped_by_one_percent(tmp_path, risk_settings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, risk_settings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    mt5.open_positions.append(pos(1, "SELL", 0.15, 1.1000, 1.1050, symbol="XAUUSD"))  # $75 = 0.75%
    bot.meta["XAUUSD"] = eurusd_meta
    assert bot.open_risk_block(10_000.0, 0.0025) is None                         # 0.75 + 0.25 = 1.00%: boleh
    assert "akan melewatinya" in bot.open_risk_block(10_000.0, 0.005)
    bot.offer_setups(Analysis(text="x", setups=[make_setup(mt5.tick("EURUSD").ask)]))   # Strong: 0.25 / 0.5
    labels = [b for r in bot.notifier.buttons[-1] for b, _ in r][:-1]
    assert [l.split(" ")[1] for l in labels] == ["0.25%"]                        # 0.5% disembunyikan
    sid = bot.journal.pending_confirmations()[0]["id"]
    mt5.open_positions.append(pos(2, "SELL", 0.05, 1.1000, 1.1050, symbol="XAUUSD"))  # +$25 sebelum diklik
    click(bot, f"c:{sid}:0.0025")
    assert mt5.orders == [] and "batas risiko terbuka 1%" in msgs[-1]           # dicek ulang saat eksekusi
    bot.offer_setups(Analysis(text="x", setups=[make_setup(mt5.tick("EURUSD").ask - 0.0030)]))
    assert "batas risiko terbuka 1%" in msgs[-1] and len(bot.notifier.buttons) == 1   # penuh: tidak ditawarkan


def test_rule_and_ai_positions_no_longer_block_each_other_h3(tmp_path, risk_settings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, risk_settings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    mt5.open_positions.append(pos(1, "BUY", 0.02, 1.1000, 1.0950))              # Ichimoku EURUSD, $10
    assert bot.entry_block("EURUSD", "EURUSD", 1, 10_000.0, 1.0, source="ai_setup") is None
    assert "sudah ada posisi di EURUSD" in bot.entry_block("EURUSD", "EURUSD", 1, 10_000.0, 1.0)   # rule + rule
    bot.offer_setups(Analysis(text="x", setups=[make_setup(mt5.tick("EURUSD").ask)]))
    assert bot.notifier.buttons and "SETUP BUY EURUSD" in msgs[-1]               # dulu: "sudah ada posisi"
    mt5.open_positions[:] = [pos(5, "BUY", 0.02, 1.1000, 1.0950, "bot user setup")]  # setup AI terbuka
    assert bot.entry_block("EURUSD", "EURUSD", 1, 10_000.0, 1.0) is None          # Ichimoku tidak diblok
    assert "sudah ada posisi di EURUSD" in bot.entry_block("EURUSD", "EURUSD", 1, 10_000.0, 1.0, source="ai_setup")
    # batas jumlah posisi dan eksposur mata uang tetap memakai semua posisi
    mt5.open_positions += [pos(6, "BUY", 0.01, 1.1, 1.09), pos(7, "BUY", 0.01, 1.1, 1.09, symbol="GBPUSD")]
    assert "maks 3 posisi" in bot.entry_block("EURUSD", "EURUSD", 1, 10_000.0, 1.0, source="ai_setup")


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
