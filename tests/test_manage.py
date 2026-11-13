"""Dynamic SL + partial TP untuk posisi setup 📊: tahap BE -> kunci -> partial -> trailing, SL hanya mengetat."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot.config import TelegramCfg
from test_live import FakeLLM, make_bot

NOW = datetime(2026, 1, 14, 9, 0, tzinfo=timezone.utc)
R = 0.0040


@pytest.fixture
def env(tmp_path, settings, at_signal, eurusd_meta):
    s = settings.model_copy(update={"telegram": TelegramCfg(token="t", chat_id="111")})
    s = s.model_copy(update={"manage": s.manage.model_copy(update={"partial_at_r": 0.0})})   # parsial di TP1
    bot, mt5, msgs = make_bot(tmp_path, s, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    entry = round(float(mt5.h1["close"].iloc[-1]), 5)
    state = {"px": entry}
    mt5.tick = lambda symbol: SimpleNamespace(bid=state["px"], ask=state["px"] + 0.0001)

    def open_pos(ticket=5, volume=0.10, source="ai_setup"):
        setup = dict(symbol="EURUSD", side="BUY", entry_low=entry, entry_high=entry, stop_loss=entry - R,
                     take_profit_1=entry + 2 * R, take_profit_2=entry + 4 * R)
        sid = bot.journal.log_signal("EURUSD", 1, f"t{ticket}", entry, R, 2 * R, {}, source=source,
                                     setup=setup if source == "ai_setup" else None)
        bot.journal.log_trade_open(sid, ticket, "EURUSD", 1, volume, entry, entry - R, entry + 2 * R, 40.0)
        pos = SimpleNamespace(ticket=ticket, symbol="EURUSD", type=0, time=1, volume=volume, price_open=entry,
                              sl=entry - R, tp=entry + 2 * R, profit=0.0, swap=0.0, magic=1)
        mt5.open_positions.append(pos)
        return pos

    def at(r):
        state["px"] = entry + r * R
        bot.manage_setup_positions(NOW)

    return SimpleNamespace(bot=bot, mt5=mt5, msgs=msgs, entry=entry, open_pos=open_pos, at=at)


def test_stages_be_lock_partial_trail_never_loosen(env):
    pos = env.open_pos()
    env.at(0.5)
    assert pos.tp == pytest.approx(env.entry + 4 * R) and env.mt5.sl_moves == []      # TP server -> TP2
    env.at(0.5)
    assert env.mt5.sl_moves == []                                                      # belum 1R: SL diam
    env.at(1.05)
    be = env.mt5.sl_moves[-1][1]
    assert env.entry < be < env.entry + 0.1 * R + 0.0003 and "breakeven" in env.msgs[-1]
    env.at(1.6)
    assert pos.sl == pytest.approx(env.entry + 0.5 * R) and "kunci 0.5R" in env.msgs[-1]
    env.at(0.8)
    assert pos.sl == pytest.approx(env.entry + 0.5 * R)                                # harga turun: SL tidak mundur
    env.at(2.0)
    assert env.mt5.partials == [(5, 0.05)] and pos.volume == pytest.approx(0.05) and "parsial" in env.msgs[-1]
    env.at(3.0)
    assert pos.sl > env.entry + 0.5 * R and "trailing" in env.msgs[-1]
    locked = pos.sl
    env.at(2.2)
    assert pos.sl == locked


def test_small_lot_keeps_tp1_and_rule_positions_untouched(env):
    small = env.open_pos(ticket=6, volume=0.01)
    rule = env.open_pos(ticket=7, source="rule")
    env.at(1.2)
    assert small.tp == pytest.approx(env.entry + 2 * R)            # tidak bisa dibagi: TP1 tetap di server
    assert small.sl > env.entry and env.mt5.partials == []         # BE tetap jalan
    assert rule.sl == pytest.approx(env.entry - R) and all(t != 7 for t, _ in env.mt5.sl_moves)


def test_toggle_off_stops_management(env):
    pos = env.open_pos()
    env.bot.notifier.queue.append({"update_id": 1, "callback_query": {
        "id": "cb", "data": "ds:5", "message": {"chat": {"id": 111}, "message_id": 9}}})
    env.bot.poll_confirmations()
    assert env.bot.manage_state(5)["off"] is True
    env.at(1.8)
    assert pos.sl == pytest.approx(env.entry - R) and env.mt5.sl_moves == []


def test_partial_at_usd_fires_before_r_target(env):
    # 0.10 lot EURUSD: $10 = 10 pip = 0.25R (R = 40 pip), lebih dulu dari target 1R
    env.bot.s = env.bot.s.model_copy(update={"manage": env.bot.s.manage.model_copy(
        update={"partial_at_r": 1.0, "partial_at_usd": 10.0})})
    pos = env.open_pos()
    env.at(0.1)                      # siklus 1: TP server -> TP2
    env.at(0.2)
    assert env.mt5.partials == []    # profit baru $8
    env.at(0.26)
    assert env.mt5.partials == [(5, 0.05)] and pos.volume == pytest.approx(0.05)
    env.at(0.26)
    assert pos.sl > env.entry        # sisa posisi: SL breakeven + biaya
    small = env.open_pos(ticket=6, volume=0.01)
    env.at(0.5)
    assert small.volume == 0.01 and small.tp == pytest.approx(env.entry + 2 * R)   # lot minimum: tidak dibagi


def test_symbol_max_lot_caps_order_size(env):
    assert env.bot.lot_for("EURUSD", 10_000, 0.01, 0.0010) == pytest.approx(1.0)   # $100 / 10 pip
    sym = env.bot.s.symbols[0].model_copy(update={"max_lot": 0.1})
    env.bot.s = env.bot.s.model_copy(update={"symbols": [sym]})
    assert env.bot.lot_for("EURUSD", 10_000, 0.01, 0.0010) == pytest.approx(0.1)
    assert env.bot.lot_for("EURUSD", 10_000, 0.0025, 0.0050) == pytest.approx(0.05)  # di bawah batas: tidak berubah


def test_partial_at_1r_default(env):
    env.bot.s = env.bot.s.model_copy(update={"manage": env.bot.s.manage.model_copy(update={"partial_at_r": 1.0})})
    pos = env.open_pos()
    env.at(0.2)                      # siklus 1: TP server -> TP2
    env.at(1.0)
    assert env.mt5.partials == [(5, 0.05)] and "+1.0R" in env.msgs[-1]
    env.at(1.0)
    assert pos.sl > env.entry        # sisa posisi: SL minimal breakeven + biaya
