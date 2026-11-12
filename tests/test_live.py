"""Alur LiveBot dengan MT5 & LLM palsu: sinyal -> review AI -> order/shadow -> close."""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from bot.journal import Journal
from bot.live import LiveBot
from bot.strategy import compute_features, signal_directions


class FakeMT5:
    def __init__(self, h1, h4, meta):
        self.h1, self.h4, self.meta = h1, h4, meta
        self.open_positions, self.orders, self.closed_positions = [], [], []
        self.closed = {}
        self.limits, self.pending, self.outcomes = [], [], {}
        self.sl_moves, self.partials = [], []

    def account(self):
        return SimpleNamespace(login=1, server="Demo", equity=10_000.0, balance=10_000.0, currency="USD")

    def symbol_meta(self, symbol):
        return dict(self.meta, stops_level=0)

    def rates(self, symbol, tf, count):
        # Kembalikan seluruh data (bukan tail) agar indikator identik dengan fixture
        df = {"H1": self.h1, "H4": self.h4, "D1": self.h4}.get(tf, self.h1)   # M15/M5 palsu = data H1
        return (df.tail(1) if count == 1 else df).reset_index(drop=True)

    def tick(self, symbol):
        c = float(self.h1["close"].iloc[-1])
        return SimpleNamespace(bid=c, ask=c + 0.0001)

    def positions(self, magic):
        return list(self.open_positions)

    def market_order(self, symbol, direction, volume, sl_distance, tp_distance, magic, comment):
        self.orders.append((symbol, direction, volume, tp_distance))
        pos = SimpleNamespace(ticket=777, symbol=symbol, type=0 if direction == 1 else 1, time=1, volume=volume,
                              price_open=self.tick(symbol).ask, sl=0.0, tp=0.0, profit=0.0)
        self.open_positions.append(pos)
        return SimpleNamespace(price=pos.price_open, order=777)

    def limit_order(self, symbol, direction, volume, price, sl, tp, magic, comment):
        self.limits.append((symbol, direction, volume, price, sl, tp))
        self.pending.append(SimpleNamespace(ticket=900 + len(self.limits), magic=magic))
        return SimpleNamespace(order=900 + len(self.limits))

    def pending_orders(self, magic):
        return list(self.pending)

    def cancel_order(self, ticket):
        self.pending = [o for o in self.pending if o.ticket != ticket]
        return True

    def order_outcome(self, ticket):
        return self.outcomes.get(ticket)

    def close_position(self, pos, comment=""):
        self.closed_positions.append(pos.ticket)
        self.open_positions.remove(pos)
        return True

    def modify_sl(self, pos, sl):
        self.sl_moves.append((pos.ticket, sl))
        pos.sl = sl
        return True

    def modify_sltp(self, pos, sl, tp):
        pos.sl, pos.tp = sl, tp
        return True

    def close_partial(self, pos, volume, comment=""):
        self.partials.append((pos.ticket, volume))
        pos.volume = round(pos.volume - volume, 8)
        return True

    def closed_position_result(self, ticket):
        return self.closed.get(ticket)


class FakeLLM:
    def __init__(self, decision="approve", mult=1.0):
        self.calls = 0
        self.reply = json.dumps({"decision": decision, "size_multiplier": mult, "confidence": 0.8,
                                 "reasons": ["tren searah"], "risks": []})

    def complete(self, system, user):
        self.calls += 1
        json.loads(user.split("\n", 1)[1])  # konteks harus JSON valid
        return self.reply


class FakeNotifier:
    def __init__(self, msgs):
        self.msgs, self.enabled = msgs, True
        self.buttons, self.cleared, self.answers, self.queue, self.menus = [], [], [], [], []

    def send(self, text):
        self.msgs.append(text)
        return True

    def send_keyboard(self, text, rows):
        self.msgs.append(text)
        self.menus.append(rows)
        return True

    def send_buttons(self, text, buttons):
        self.msgs.append(text)
        self.buttons.append(buttons)
        return 42

    def clear_buttons(self, mid):
        self.cleared.append(mid)

    def answer(self, cid, text=""):
        self.answers.append(text)

    def updates(self):
        out, self.queue = self.queue, []
        return out

    def set_commands(self, commands):
        pass


def make_bot(tmp_path, settings, at_signal, meta, llm, dry_run):
    h1, h4 = at_signal
    mt5 = FakeMT5(h1, h4, meta)
    msgs = []
    bot = LiveBot(settings, mt5, Journal(tmp_path / "j.db"), FakeNotifier(msgs), llm, dry_run)
    bot.meta = {"EURUSD": mt5.symbol_meta("EURUSD")}
    bot.offset = 0
    return bot, mt5, msgs


def test_dry_run_logs_signal_and_shadow(tmp_path, settings, at_signal, eurusd_meta):
    llm = FakeLLM()
    bot, mt5, msgs = make_bot(tmp_path, settings, at_signal, eurusd_meta, llm, dry_run=True)
    bot.process_symbol("EURUSD")
    assert llm.calls == 1 and mt5.orders == []
    row = bot.journal.db.execute("SELECT * FROM signals").fetchone()
    assert (row["ai_decision"], row["skip_reason"], row["shadow_status"]) == ("approve", "dry_run", "open")
    assert "dry_run" in msgs[-1]
    # sinyal yang sama tidak diproses dua kali
    bot.process_symbol("EURUSD")
    assert llm.calls == 1


def test_live_order_then_close(tmp_path, settings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, settings, at_signal, eurusd_meta, FakeLLM("reduce", 0.5), dry_run=False)
    bot.process_symbol("EURUSD")
    assert len(mt5.orders) == 1
    trade = bot.journal.open_trades()[0]
    assert 0.9 * 50 < trade["risk_amount"] <= 10_000 * 0.01 * 0.5   # risiko nyata <= target
    assert "OPEN" in msgs[-1]

    mt5.open_positions.clear()
    mt5.closed[777] = {"close_price": 1.2, "close_time": __import__("datetime").datetime(2026, 1, 1), "profit": 100.0}
    bot.sync_closed_trades()
    closed = bot.journal.recent_closed(1)[0]
    assert closed["r_multiple"] == pytest.approx(100.0 / trade["risk_amount"])   # R dari risiko nyata
    assert "CLOSE" in msgs[-1]


def test_ai_reject_creates_shadow_without_order(tmp_path, settings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, settings, at_signal, eurusd_meta, FakeLLM("reject", 0), dry_run=False)
    bot.process_symbol("EURUSD")
    assert mt5.orders == []
    row = bot.journal.db.execute("SELECT * FROM signals").fetchone()
    assert (row["skip_reason"], row["shadow_status"]) == ("ditolak AI", "open")


def test_daily_loss_blocks_before_ai(tmp_path, settings, at_signal, eurusd_meta):
    llm = FakeLLM()
    bot, mt5, msgs = make_bot(tmp_path, settings, at_signal, eurusd_meta, llm, dry_run=False)
    bot.daily.start_equity = 11_000  # equity 10.000 = -9% hari ini
    bot.process_symbol("EURUSD")
    assert llm.calls == 0 and mt5.orders == []
    assert "batas rugi harian" in msgs[-1]


def no_tp_settings(settings):
    """Seperti strategi Ichimoku: tanpa TP, keluar lewat sinyal (di sini EMA cross berlawanan)."""
    return settings.model_copy(update={"strategy": settings.strategy.model_copy(
        update={"tp_atr_mult": 0, "exit_rule": "signal", "breakeven_at_r": 0})})


def test_order_without_tp(tmp_path, settings, at_signal, eurusd_meta):
    s = no_tp_settings(settings)
    bot, mt5, msgs = make_bot(tmp_path, s, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    bot.process_symbol("EURUSD")
    assert mt5.orders[0][3] == 0
    assert bot.journal.open_trades()[0]["tp"] is None
    assert "exit sinyal" in msgs[-1]


def test_signal_exit_closes_position(tmp_path, settings, at_signal, eurusd_meta, monkeypatch):
    s = no_tp_settings(settings)
    bot, mt5, msgs = make_bot(tmp_path, s, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    mt5.open_positions.append(SimpleNamespace(ticket=5, symbol="EURUSD", type=0, volume=0.1, time=1,
                                              price_open=1.1, sl=1.09, tp=0.0, profit=0.0))
    monkeypatch.setattr("bot.live.exit_flags", lambda f, p: (np.r_[np.zeros(len(f) - 1, bool), True],
                                                             np.zeros(len(f), bool)))
    bot.check_signal_exits("EURUSD", "EURUSD", compute_features(mt5.h1, mt5.h4, s.strategy))
    assert mt5.closed_positions == [5]
    assert "Exit sinyal" in msgs[-1]


def test_take_profit_usd_and_max_hold_close_positions(tmp_path, settings, at_signal, eurusd_meta):
    from datetime import datetime, timezone

    s = settings.model_copy(deep=True)
    s.risk.take_profit_usd, s.risk.max_hold_days = 25, 3
    bot, mt5, msgs = make_bot(tmp_path, s, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    now = datetime.now(timezone.utc).timestamp()
    pos = lambda t, profit, age: SimpleNamespace(ticket=t, symbol="EURUSD", type=1, volume=0.08, time=int(now - age * 86400),
                                                 price_open=1.1, sl=1.11, tp=0.0, profit=profit, swap=0.0)
    mt5.open_positions += [pos(1, 13.0, 1), pos(2, 25.5, 1), pos(3, -4.0, 3.1)]
    bot.manage_take_profit_and_hold(datetime.now(timezone.utc))
    assert mt5.closed_positions == [2, 3]
    assert "TP $25" in msgs[0] and "3 hari" in msgs[1]


def test_take_profit_lets_winner_run_while_trend_intact(tmp_path, settings, at_signal, eurusd_meta):
    from datetime import datetime, timezone

    s = settings.model_copy(deep=True)
    s.risk.take_profit_usd, s.risk.trail_lock_fraction = 25, 0.6
    bot, mt5, msgs = make_bot(tmp_path, s, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    sls = []
    mt5.modify_sl = lambda p, sl: sls.append(sl) or True
    entry = mt5.tick("EURUSD").bid - 0.0050  # BUY profit 50 pip
    mt5.open_positions.append(SimpleNamespace(ticket=9, symbol="EURUSD", type=0, volume=0.1, price_open=entry,
                                              time=int(datetime.now(timezone.utc).timestamp()), sl=entry - 0.002,
                                              tp=0.0, profit=50.0, swap=0.0))
    bot.trend_intact = lambda pos: True
    bot.manage_take_profit_and_hold(datetime.now(timezone.utc))
    assert mt5.closed_positions == [] and abs(sls[0] - (entry + 0.003)) < 1e-9 and "dibiarkan jalan" in msgs[-1]
    bot.trend_intact = lambda pos: False
    bot.manage_take_profit_and_hold(datetime.now(timezone.utc))
    assert mt5.closed_positions == [9]


def test_shadow_without_tp_resolves_on_exit_signal(tmp_path, settings, at_signal, uptrend_waves, eurusd_meta):
    s = no_tp_settings(settings)
    bot, mt5, msgs = make_bot(tmp_path, s, at_signal, eurusd_meta, FakeLLM(), dry_run=True)
    bot.process_symbol("EURUSD")
    row = bot.journal.open_shadows()[0]
    assert row["shadow_tp"] is None
    mt5.h1 = uptrend_waves[0]  # data berlanjut: cross balik akan muncul
    bot.resolve_shadows()
    done = bot.journal.db.execute("SELECT shadow_status, shadow_r FROM signals").fetchone()
    assert done["shadow_status"] in ("signal", "sl")
    assert done["shadow_r"] is not None


def test_startup_rejects_unsupported_live_features(tmp_path, settings, at_signal, eurusd_meta):
    s = settings.model_copy(update={"strategy": settings.strategy.model_copy(update={"trail_atr_mult": 3.0})})
    bot, *_ = make_bot(tmp_path, s, at_signal, eurusd_meta, None, dry_run=True)
    with pytest.raises(ValueError):
        bot.startup()


def test_news_block_skips_ai_and_shadows(tmp_path, settings, at_signal, eurusd_meta):
    llm = FakeLLM()
    bot, mt5, msgs = make_bot(tmp_path, settings, at_signal, eurusd_meta, llm, dry_run=False)
    event = SimpleNamespace(time=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                            currency="USD", title="Non-Farm Employment Change", impact="High")
    bot.calendar = SimpleNamespace(blocking_event=lambda sym, now: event, refresh=lambda now: None)
    bot.process_symbol("EURUSD")
    assert llm.calls == 0 and mt5.orders == []
    row = bot.journal.db.execute("SELECT * FROM signals").fetchone()
    assert row["ai_decision"] == "news_block" and row["shadow_status"] == "open"
    assert "Non-Farm" in msgs[-1]
