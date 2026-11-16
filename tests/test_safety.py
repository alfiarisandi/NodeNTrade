"""Keamanan eksekusi (audit 2026-11-16, Commit A): satu instance, Telegram 409, batas rugi harian tahan restart,
kedaluwarsa order limit di server, dan kesegaran data harga."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.config import ConfirmCfg, TelegramCfg, load_settings
from bot.instance import EXIT_ALREADY_RUNNING, AlreadyRunning, InstanceLock
from bot.live import WIB, market_closed
from bot.mt5_client import MT5Client
from bot.notifier import CONFLICT_HOLD_SECONDS, Notifier
from test_confirm import click, make_setup
from test_live import FakeLLM, make_bot

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


@pytest.fixture
def ssettings(settings):
    return settings.model_copy(update={"confirm": ConfirmCfg(enabled=True),
                                       "telegram": TelegramCfg(token="t", chat_id="111")})


# ---------- A. satu instance ----------
def test_second_instance_on_same_machine_is_refused(tmp_path):
    first = InstanceLock(tmp_path / "bot.lock")
    first.acquire()
    with pytest.raises(AlreadyRunning):
        InstanceLock(tmp_path / "bot.lock").acquire()
    first.release()
    with InstanceLock(tmp_path / "bot.lock"):   # kunci dilepas: instance baru boleh jalan
        pass


def test_run_bot_exits_without_restart_when_locked(tmp_path, monkeypatch):
    import run_bot

    import bot.config
    s = load_settings()
    monkeypatch.setattr(bot.config, "ROOT", tmp_path)   # kunci di folder sementara, bukan data/ repo
    held = InstanceLock(tmp_path / "data" / "bot.lock")
    held.acquire()
    try:
        with pytest.raises(SystemExit) as e:
            run_bot.run(s, dry_run=True)   # berhenti sebelum menyentuh MT5 / Telegram
        assert e.value.code == EXIT_ALREADY_RUNNING
    finally:
        held.release()
    ps1 = (Path(__file__).resolve().parents[1] / "scripts" / "run_forever.ps1").read_text(encoding="utf-8")
    assert "$code -eq 3" in ps1   # run_forever tidak me-restart kode ini


# ---------- Telegram 409 ----------
class Resp:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


def test_telegram_409_marks_conflict_and_slows_polling(monkeypatch):
    calls = []
    reply = {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates request"}
    monkeypatch.setattr("bot.notifier.requests.post", lambda url, json, timeout: calls.append(url) or Resp(reply))
    n = Notifier(TelegramCfg(token="t", chat_id="1"))
    assert n.updates() == [] and n.conflict_active and not n.down
    assert n.updates() == [] and len(calls) == 1   # polling diperlambat 60 detik, tidak retry terus-menerus
    n.retry_at = 0
    reply = {"ok": True, "result": []}
    n.updates()
    assert n.conflict_active                        # tetap aktif sampai 10 menit tanpa 409
    n.conflict_at -= CONFLICT_HOLD_SECONDS + 1
    assert not n.conflict_active


def test_conflict_blocks_entries_and_warns_once(tmp_path, ssettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, ssettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    bot.notifier.conflict_active = True
    bot.check_conflict()
    bot.check_conflict()
    assert sum("Instance bot lain terdeteksi" in m for m in msgs) == 1
    assert "instance bot lain terdeteksi" in bot.entry_block("EURUSD", "EURUSD", 1, 10_000.0, 1.0)
    bot.process_symbol("EURUSD")
    assert mt5.orders == [] and bot.notifier.buttons == []      # sinyal Ichimoku tidak ditawarkan
    bot.notifier.conflict_active = False
    bot.check_conflict()
    assert "entry baru diizinkan lagi" in msgs[-1] and bot.entry_block("EURUSD", "EURUSD", 1, 10_000.0, 1.0) is None


# ---------- B. batas rugi harian tahan restart ----------
def test_daily_loss_survives_restart_and_resets_on_wib_day(tmp_path, ssettings, at_signal, eurusd_meta):
    bot, *_ = make_bot(tmp_path, ssettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    morning = datetime(2026, 1, 14, 2, 0, tzinfo=timezone.utc)          # 09:00 WIB
    bot.update_daily(morning, 10_000.0)
    bot.update_daily(morning + timedelta(hours=3), 9_650.0)    # -3.5%, floating ikut dihitung (batas fixture 3%)
    assert bot.daily.halted(9_650.0)
    restarted, *_ = make_bot(tmp_path, ssettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)   # jurnal sama
    restarted.update_daily(morning + timedelta(hours=4), 9_650.0)
    assert restarted.daily.start_equity == 10_000.0 and restarted.daily.halted(9_650.0)
    assert "batas rugi harian 3% tercapai" in restarted.entry_block("EURUSD", "EURUSD", 1, 9_650.0, 1.0)
    # 23:59 WIB masih hari yang sama; 00:01 WIB = hari baru dengan equity awal baru
    restarted.update_daily(datetime(2026, 1, 14, 16, 59, tzinfo=timezone.utc), 9_650.0)
    assert restarted.daily.start_equity == 10_000.0
    restarted.update_daily(datetime(2026, 1, 14, 17, 1, tzinfo=timezone.utc), 9_650.0)
    assert restarted.daily.day.isoformat() == "2026-01-15" and not restarted.daily.halted(9_650.0)
    assert restarted.journal.get_state("daily_start_equity") == "9650.00"


# ---------- C. kedaluwarsa order limit di server ----------
class FakeMT5Module:
    ORDER_TIME_GTC, ORDER_TIME_DAY, ORDER_TIME_SPECIFIED = 0, 1, 2
    TRADE_ACTION_PENDING, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 5, 2, 3
    ORDER_FILLING_RETURN, TRADE_RETCODE_DONE = 2, 10009

    def __init__(self, expiration_mode):
        self.expiration_mode, self.sent = expiration_mode, []

    def symbol_select(self, symbol, on):
        return True

    def symbol_info(self, symbol):
        return SimpleNamespace(point=0.00001, digits=5, trade_tick_size=0.00001, trade_tick_value=1.0,
                               trade_contract_size=100_000, volume_min=0.01, volume_max=50, volume_step=0.01,
                               trade_stops_level=1, filling_mode=1, currency_profit="USD",
                               expiration_mode=self.expiration_mode)

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=1.1000, ask=1.1001, time=1_000_000)

    def order_send(self, req):
        self.sent.append(req)
        return SimpleNamespace(retcode=10009, order=55, comment="done")


def client(expiration_mode):
    c = MT5Client.__new__(MT5Client)
    c.mt5, c.cfg = FakeMT5Module(expiration_mode), None
    return c


def test_limit_expires_on_server_even_if_bot_stops():
    expires = datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc)
    c = client(15)   # Monex-Demo: GTC | DAY | SPECIFIED | SPECIFIED_DAY
    c.limit_order("EURUSD", 1, 0.1, 1.0950, 1.0920, 1.1010, 7, "bot user limit", expires=expires, offset_hours=2)
    req = c.mt5.sent[-1]
    assert req["type_time"] == FakeMT5Module.ORDER_TIME_SPECIFIED
    assert req["expiration"] == int(expires.timestamp()) + 2 * 3600   # waktu MT5 = jam server (GMT+2)
    assert c.expiry_mode("EURUSD") == "server"
    day = client(1 | 2)
    day.limit_order("EURUSD", 1, 0.1, 1.0950, 1.0920, 1.1010, 7, "x", expires=expires, offset_hours=2)
    assert day.mt5.sent[-1]["type_time"] == FakeMT5Module.ORDER_TIME_DAY and day.expiry_mode("EURUSD") == "day"
    gtc = client(1)
    gtc.limit_order("EURUSD", 1, 0.1, 1.0950, 1.0920, 1.1010, 7, "x", expires=expires, offset_hours=2)
    assert gtc.mt5.sent[-1]["type_time"] == FakeMT5Module.ORDER_TIME_GTC and gtc.expiry_mode("EURUSD") == "bot"


def test_setup_limit_sends_expiry_and_warns_when_broker_cannot(tmp_path, ssettings, at_signal, eurusd_meta):
    from bot.ai.analysis import Analysis
    bot, mt5, msgs = make_bot(tmp_path, ssettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    bot.offset = 2
    base = mt5.tick("EURUSD").ask
    bot.offer_setups(Analysis(text="x", setups=[make_setup(base, valid_minutes=90)]))
    far = base + 0.0030   # harga di atas zona BUY: limit
    mt5.tick = lambda symbol: SimpleNamespace(bid=far - 0.0001, ask=far)
    before = datetime.now(timezone.utc)
    click(bot, f"c:{bot.journal.pending_confirmations()[0]['id']}:0.005")
    expires, offset = mt5.limit_expiry[-1]
    assert offset == 2 and timedelta(minutes=89) < expires - before < timedelta(minutes=91)
    assert "batal otomatis di server MT5" in msgs[-1]
    bot.journal.update_signal(bot.journal.limit_orders()[0]["id"], user_decision="limit_cancelled")
    mt5.expiry_mode = lambda symbol: "bot"
    mt5.tick = lambda symbol: SimpleNamespace(bid=base - 0.0001, ask=base)
    bot.offer_setups(Analysis(text="x", setups=[make_setup(base + 0.0002, valid_minutes=90)]))
    mt5.tick = lambda symbol: SimpleNamespace(bid=far - 0.0001, ask=far)
    click(bot, f"c:{bot.journal.pending_confirmations()[0]['id']}:0.005")
    assert "broker tidak mendukung kedaluwarsa" in msgs[-1]


# ---------- D. kesegaran tick & pasar tutup ----------
def test_market_closed_hint():
    wed = datetime(2026, 1, 14, 12, 0)
    assert not market_closed(wed, "EURUSD.m") and not market_closed(wed, "XAUUSD.m")
    assert market_closed(datetime(2026, 1, 17, 12, 0), "EURUSD.m")             # Sabtu
    assert market_closed(datetime(2026, 1, 12, 0, 2), "EURUSD.m")              # Senin sebelum buka
    night = datetime(2026, 1, 14, 0, 30)
    assert market_closed(night, "XAUUSD.m") and not market_closed(night, "EURUSD.m")   # jeda harian logam


def test_stale_tick_and_disconnect_block_entries(tmp_path, ssettings, at_signal, eurusd_meta):
    bot, mt5, msgs = make_bot(tmp_path, ssettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    assert bot.feed_block("EURUSD") is None
    mt5.feed = {**mt5.feed, "tick_age": 300.0}                                   # Rabu siang, 5 menit tanpa tick
    assert bot.feed_block("EURUSD") == "tick basi 300 detik saat pasar seharusnya buka: feed terputus?"
    from bot.ai.analysis import Analysis
    bot.offer_setups(Analysis(text="x", setups=[make_setup(mt5.tick("EURUSD").ask)]))
    assert "tick basi 300 detik" in msgs[-1] and bot.notifier.buttons == []
    assert bot.journal.opportunities_since("2000-01-01")[-1]["reason"].startswith("tick basi")   # alasan tercatat
    mt5.feed = {**mt5.feed, "tick_age": 7200.0, "server_time": datetime(2026, 1, 17, 12, 0)}
    assert bot.feed_block("EURUSD") == "pasar tutup (tick terakhir 120 menit lalu)"
    mt5.feed = {**mt5.feed, "connected": False}
    assert "terminal MT5 tidak tersambung" in bot.feed_block("EURUSD")


def test_offset_redetected_hourly_while_market_open(tmp_path, ssettings, at_signal, eurusd_meta):
    """Bot yang start saat pasar tutup memakai cadangan GMT+2; bila server sebenarnya GMT+3 (DST), umur tick
    akan salah 1 jam dan semua entry terblok. Offset dicek ulang tiap jam saat pasar buka."""
    bot, mt5, _ = make_bot(tmp_path, ssettings, at_signal, eurusd_meta, FakeLLM(), dry_run=False)
    bot.offset = 2
    now = datetime(2026, 3, 30, 8, 0, tzinfo=timezone.utc)
    bot.refresh_offset(now)                     # pasar tutup / tick basi: offset tetap
    assert bot.offset == 2
    mt5.detected_offset = 3
    bot.refresh_offset(now + timedelta(minutes=20))
    assert bot.offset == 2                      # jam yang sama: tidak dicek ulang
    bot.refresh_offset(now + timedelta(hours=1))
    assert bot.offset == 3
    manual = ssettings.model_copy(update={"session": ssettings.session.model_copy(update={"server_utc_offset_hours": 2})})
    bot.s, bot.offset, bot.offset_checked = manual, 2, None
    bot.refresh_offset(now + timedelta(hours=2))
    assert bot.offset == 2                      # offset manual di setelan tidak ditimpa


def test_feed_status_uses_server_clock(monkeypatch):
    c = client(15)
    c.mt5.terminal_info = lambda: SimpleNamespace(connected=True)
    monkeypatch.setattr("bot.mt5_client.time.time", lambda: 1_000_000 - 2 * 3600 + 30.0)
    st = c.feed_status("EURUSD", 2)
    assert st["connected"] and st["tick_age"] == pytest.approx(30.0)
    assert st["server_time"] == datetime(1970, 1, 12, 13, 47, 10)
    assert WIB.utcoffset(None) == timedelta(hours=7)
