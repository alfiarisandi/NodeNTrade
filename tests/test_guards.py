"""Circuit breaker drawdown, cek tilt, dan laporan mingguan."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot.config import ConfirmCfg, TelegramCfg
from bot.journal import Journal
from bot.performance import current_losing_streak, trade_stats, weekly_stats
from bot.risk import DrawdownGuard
from test_confirm import click, signal_row
from test_live import FakeLLM, make_bot


def add_closed(j: Journal, ticket: int, profit: float, close_time: str):
    j.log_trade_open(None, ticket, "EURUSD", 1, 0.1, 1.1, 1.09, None, 50.0)
    j.close_trade(ticket, 1.1, close_time, profit)


# ---------- circuit breaker ----------
def test_drawdown_guard_tracks_peak_and_latches():
    g = DrawdownGuard(0.15)
    assert not g.update(10_000)
    assert not g.update(12_000) and g.peak == 12_000
    assert not g.update(10_300)                 # -14.2%: belum
    assert g.update(10_200) and g.halted        # -15%: terpicu
    assert not g.update(13_000) and g.halted    # tetap aktif walau equity pulih: reset manual
    g.reset(13_000)
    assert not g.halted and g.peak == 13_000


@pytest.fixture
def csettings(settings):
    return settings.model_copy(update={
        "confirm": ConfirmCfg(enabled=True), "telegram": TelegramCfg(token="t", chat_id="111"),
        "risk": settings.risk.model_copy(update={"risk_per_trade": 0.005})})


def test_breaker_blocks_entries_and_resets_from_telegram(tmp_path, csettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, csettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    bot.journal.set_state("dd_peak", "12000")       # puncak lama; equity sekarang 10.000 = -16.7%
    bot.update_drawdown(10_000)
    assert bot.journal.get_state("dd_halted") == "1"
    assert bot.notifier.buttons[-1] == [[("🔓 Reset circuit breaker", "dd:reset")]]
    bot.update_drawdown(10_000)                     # tidak mengirim peringatan dua kali
    assert len(bot.notifier.buttons) == 1

    bot.process_symbol("EURUSD")
    assert mt5.orders == [] and "circuit breaker" in signal_row(bot)["skip_reason"]

    click(bot, "dd:reset")
    assert bot.journal.get_state("dd_halted") == "0"
    assert float(bot.journal.get_state("dd_peak")) == 10_000
    assert "di-reset" in msgs[-1]
    click(bot, "dd:reset")
    assert bot.notifier.answers[-1] == "Circuit breaker tidak aktif"


def test_breaker_reset_from_other_chat_ignored(tmp_path, csettings, at_signal, eurusd_meta):
    bot, *_ = make_bot(tmp_path, csettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    bot.journal.set_state("dd_peak", "12000")
    bot.update_drawdown(10_000)
    click(bot, "dd:reset", chat="999")
    assert bot.journal.get_state("dd_halted") == "1"


# ---------- cek tilt ----------
def test_losing_streak_counts_from_latest(tmp_path):
    j = Journal(tmp_path / "j.db")
    add_closed(j, 1, -50, "2026-01-05T10:00:00+00:00")
    add_closed(j, 2, +80, "2026-01-06T10:00:00+00:00")
    add_closed(j, 3, -50, "2026-01-07T10:00:00+00:00")
    add_closed(j, 4, -50, "2026-01-08T10:00:00+00:00")
    assert current_losing_streak(j) == 2
    st = trade_stats(j.recent_closed(10))
    assert st["trades"] == 4 and st["winrate_%"] == 25.0 and st["max_losing_streak"] == 2


def test_tilt_hides_risk_above_normal(tmp_path, csettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, csettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    for i in range(3):
        add_closed(bot.journal, 100 + i, -50, f"2026-01-0{5 + i}T10:00:00+00:00")
    bot.process_symbol("EURUSD")
    labels = [b for row in bot.notifier.buttons[0] for b, _ in row]
    assert not any("1%" in l for l in labels) and any("0.5%" in l for l in labels)
    assert "Cek tilt: 3 rugi beruntun" in msgs[-1]
    sid = signal_row(bot)["id"]
    click(bot, f"c:{sid}:0.01")                     # tombol 1% lama / manipulasi: ditolak
    assert mt5.orders == [] and bot.notifier.answers[-1] == "Pilihan tidak valid"
    click(bot, f"c:{sid}:0.005")
    assert len(mt5.orders) == 1


def test_no_tilt_after_a_win(tmp_path, csettings, at_signal, eurusd_meta):
    bot, *_ = make_bot(tmp_path, csettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    for i in range(3):
        add_closed(bot.journal, 100 + i, -50, f"2026-01-0{5 + i}T10:00:00+00:00")
    add_closed(bot.journal, 200, +120, "2026-01-09T10:00:00+00:00")
    assert bot.tilt_streak() == 0 and bot.allowed_risk_options() == [0.0025, 0.005, 0.01]


# ---------- laporan mingguan ----------
def test_weekly_stats_structure(tmp_path):
    j = Journal(tmp_path / "j.db")
    add_closed(j, 1, +100, "2099-01-05T10:00:00+00:00")
    sid = j.log_signal("GBPUSD", 1, "2099-01-05 08:00:00", 1.3, 0.004, 0.0, {})
    j.update_signal(sid, ai_decision="approve", user_decision="declined", skip_reason="user: declined")
    j.open_shadow(sid, 1.3, 1.296, None)
    j.resolve_shadow(sid, "signal", 1.2)
    st = weekly_stats(j, "2000-01-01T00:00:00+00:00", {"peak": 10_000})
    assert st["real_trades_closed"]["trades"] == 1
    assert st["signals"]["user_decision"] == {"declined": 1}
    assert st["shadow_trades_resolved"]["by_reason"] == {"user": 1.2}


def test_weekly_review_sent_once_on_friday(tmp_path, csettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, csettings, at_signal, eurusd_meta, None, dry_run=False)
    friday = datetime(2026, 11, 6, 21, 30, tzinfo=timezone.utc)
    bot.maybe_weekly_review(friday.replace(hour=20))   # belum jamnya
    assert not any("mingguan" in m for m in msgs)
    bot.maybe_weekly_review(friday)
    assert sum("Laporan mingguan" in m for m in msgs) == 1
    bot.maybe_weekly_review(friday.replace(hour=23))   # tidak dikirim ulang
    assert sum("Laporan mingguan" in m for m in msgs) == 1


# ---------- akun kecil: lot minimum ----------
def test_small_account_uses_min_lot_within_cap(tmp_path, csettings, at_signal, eurusd_meta):
    s = csettings.model_copy(update={"risk": csettings.risk.model_copy(update={"min_lot_max_risk": 0.10}),
                                     "confirm": csettings.confirm.model_copy(update={"risk_options": [0.005]})})
    bot, mt5, msgs = make_bot(tmp_path, s, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    mt5.account = lambda: SimpleNamespace(login=1, server="Demo", equity=100.0, balance=100.0, currency="USD")
    bot.process_symbol("EURUSD")
    labels = [b for row in bot.notifier.buttons[0] for b, _ in row]
    assert labels[0].startswith("⭐ 0.01 lot · $")                 # risiko nyata dalam $ dan %
    sid = signal_row(bot)["id"]
    click(bot, f"c:{sid}:0.005")
    assert len(mt5.orders) == 1 and mt5.orders[0][2] == 0.01


def test_small_account_skips_when_min_lot_exceeds_cap(tmp_path, csettings, at_signal, eurusd_meta):
    s = csettings.model_copy(update={"risk": csettings.risk.model_copy(update={"min_lot_max_risk": 0.001})})
    bot, mt5, msgs = make_bot(tmp_path, s, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    mt5.account = lambda: SimpleNamespace(login=1, server="Demo", equity=100.0, balance=100.0, currency="USD")
    bot.process_symbol("EURUSD")
    assert mt5.orders == [] and "modal terlalu kecil" in signal_row(bot)["skip_reason"]


def test_small_profile_extends_main_settings():
    from bot.config import load_settings

    s = load_settings("config/settings.small.yaml")
    assert [x.name for x in s.symbols] == ["EURUSD"] and s.risk.min_lot_max_risk == 0.10
    assert s.strategy.entry_type == "ichimoku" and s.strategy.ma_filter   # diwarisi dari settings.yaml
    assert s.journal_path.endswith("journal_small.db") and s.magic != load_settings().magic
