"""Loop utama bot live (akun demo): sinyal -> cek risiko -> review AI -> order -> jurnal & notifikasi."""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.ai.analysis import (MIN_RR, STRONG_SCORE, Analysis, Setup, Watch, analyze_market, format_opportunity,
                             format_setup, format_watch, net_rr, review_positions, validate_setup, watch_problem)
from bot.ai.client import LLM
from bot.ai.context import (build_analysis_context, build_review_context, intraday_snapshot, market_structure,
                            market_summary, session_info, tf_summary)
from bot.ai.daily_review import daily_report, weekly_report
from bot.ai.prompts import review_system
from bot.ai.reviewer import ReviewOutcome, review_signal
from bot.config import Settings
from bot.indicators import atr as atr_series
from bot.intraday import REGIME_STRATEGIES, STRATEGIES, ExitPolicy, IntradayParams, all_signals, mtf_features
from bot.journal import Journal
from bot.mt5_client import MT5Client, MT5Error
from bot.news import Calendar
from bot.notifier import Notifier
from bot.opportunities import (EV_MIN_TRADES, find_duplicate, format_opportunity_stats, format_screener_stats,
                               opportunity_stats, screener_stats, simulate)
from bot.performance import (current_losing_streak, format_evaluation, format_weekly_fallback, intraday_stats,
                             trade_stats, weekly_stats)
from bot.risk import DailyGuard, DrawdownGuard, check_new_trade, currency_exposure, lot_with_min
from bot.strategy import (DAY_LEVEL_TYPES, TF_DELTA, Signal, compute_features, exit_flags, in_session, signal_at,
                          simulate_exit, simulate_managed)

log = logging.getLogger(__name__)

FALLBACK_OFFSET = 2          # mayoritas broker MT5 memakai GMT+2 / GMT+3
MAX_CONSECUTIVE_ERRORS = 5
WIB = timezone(timedelta(hours=7))
COMMANDS = [("status", "Equity, P/L hari ini, drawdown, jam entry berikut"),
            ("posisi", "Posisi terbuka + tombol tutup / breakeven"),
            ("pnl", "Hasil hari ini, minggu ini, bulan ini"),
            ("sinyal", "5 sinyal terakhir"),
            ("pause", "Hentikan entry baru (posisi tetap dikelola)"),
            ("resume", "Lanjutkan entry baru"),
            ("analisis", "Analisis pasar XAUUSD & EURUSD oleh Claude"),
            ("cekposisi", "Saran Claude untuk posisi terbuka (HOLD / geser SL / tutup sebagian / tutup) "
                          "dan order limit (biarkan / pindah / batalkan)"),
            ("evaluasi", "Statistik setup: win rate, profit factor, expectancy, per pair & sesi"),
            ("lanjut", "Lanjutkan entry setelah jeda rugi beruntun"),
            ("menu", "Tampilkan tombol menu"),
            ("help", "Daftar perintah")]
# Tombol menu permanen di bawah chat: label -> perintah (bot menerima label sebagai teks biasa)
MENU = {"📟 Status": "status", "📂 Posisi": "posisi", "📈 P/L": "pnl", "🔔 Sinyal": "sinyal",
        "📊 Analisis": "analisis", "🩺 Posisi AI": "cekposisi", "📋 Evaluasi": "evaluasi", "⏸️ Pause": "pause", "▶️ Resume": "resume", "❓ Bantuan": "help"}
ANALYSIS_COOLDOWN = 180       # detik antar analisis: tiap analisis ±15-25 ribu token
ANALYSIS_FRAMES = {"H4": 300, "H1": 400, "M15": 300, "M5": 120}
NEWS_ALERT_HOURS = 1.0
CLOSE_CONFIRM_SECONDS = 120   # tombol "Ya, tutup" kedaluwarsa: cegah klik tak sengaja di pesan lama
LIMIT_ADVICE_SECONDS = 900    # tombol saran order limit (pindah / batal) berlaku 15 menit
SCREEN_MIN_BARS = {"M15": 250, "H1": 210, "H4": 210}   # sama dengan intraday_snapshot
SCREEN_REPEAT_MINUTES = 60    # kandidat screener pair + arah yang sama tidak dikirim ulang ke AI dalam N menit
SCREEN_AI_KEYS = ("symbol", "side", "strategy", "regime", "rule_stop_ref")   # isi kandidat yang dikirim ke AI
SCREEN_ENTRY_MINUTES = 30     # kandidat aturan = entry market di candle berikutnya: harga harus tersentuh dalam 2 candle
SLEEP_GAP_SECONDS = 600      # jarak antar siklus lebih dari ini = bot sempat tertahan (normal ±20 detik)
OPPORTUNITY_REVIEW_DAYS = 14  # rentang statistik peluang untuk AI dan 📋 Evaluasi
MAX_TICK_AGE_SECONDS = 60     # tick lebih tua dari ini saat pasar buka = feed dianggap terputus, entry ditolak
LIMIT_EXPIRY_TEXT = {"server": "batal otomatis di server MT5 {t} WIB (tetap batal walau bot mati)",
                     "day": "⚠️ broker hanya mendukung kedaluwarsa akhir hari: bot membatalkan {t} WIB selama "
                            "berjalan, server membatalkan di akhir hari",
                     "bot": "⚠️ broker tidak mendukung kedaluwarsa: hanya bot yang membatalkan {t} WIB; bila bot "
                            "mati, limit tetap aktif di MT5"}


def market_closed(server_time: datetime, symbol: str) -> bool:
    """Perkiraan pasar tutup (jam server broker GMT+2/+3): Sabtu-Minggu, Senin sebelum 00:05, dan jeda harian
    logam 23:55-01:05. Dipakai untuk membedakan pasar tutup dari feed yang terputus."""
    wd, minute = server_time.weekday(), server_time.hour * 60 + server_time.minute
    if wd >= 5 or (wd == 0 and minute < 5):
        return True
    return symbol.upper().startswith(("XAU", "XAG")) and (minute >= 23 * 60 + 55 or minute < 65)


def keep_awake(on: bool) -> None:
    """Windows: minta sistem tidak tidur / masuk standby karena idle selama bot berjalan (tutup lid atau tombol
    power tetap menidurkan laptop). Di OS lain tidak melakukan apa-apa."""
    try:
        import ctypes
        es_continuous, es_system_required = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | (es_system_required if on else 0))
    except (AttributeError, OSError):
        pass


class LiveBot:
    def __init__(self, settings: Settings, mt5c: MT5Client, journal: Journal, notifier: Notifier,
                 llm: LLM | None, dry_run: bool, calendar: Calendar | None = None):
        self.s = settings
        self.mt5 = mt5c
        self.journal = journal
        self.notifier = notifier
        self.notify = notifier.send
        self.llm = llm
        self.dry_run = dry_run
        self.calendar = calendar
        self.daily = DailyGuard(settings.risk.max_daily_loss)
        self.dd = DrawdownGuard(settings.risk.max_drawdown_from_peak)
        self.meta: dict[str, dict] = {}
        self.last_bar: dict[str, pd.Timestamp] = {}
        self.offset = FALLBACK_OFFSET
        self.errors = 0
        self.error_halt = False
        self.last_review_date = None
        self.last_equity_hour = None
        self.watch_bar: dict[str, pd.Timestamp] = {}   # candle M15 terakhir yang sudah dicek untuk pantauan
        self.screen_bar: dict[str, pd.Timestamp] = {}  # ... dan untuk screener M15
        self.opp_bar: dict[str, pd.Timestamp] = {}     # ... dan untuk penilaian hasil peluang
        self.screen_logged: dict[str, tuple] = {}      # simbol -> (candle M15, {(strategi, arah): id baris jurnal})
        self.conflict_warned = False                   # peringatan instance lain (Telegram 409) sudah dikirim
        self.offset_checked: tuple | None = None       # (tanggal, jam) UTC terakhir offset server dicek ulang
        self.last_step_wall: float | None = None
        self.review_prompt = review_system(settings.strategy, settings.risk.risk_per_trade)

    # ---------- siklus hidup ----------
    def startup(self) -> None:
        p = self.s.strategy
        if p.trail_atr_mult or p.max_hold_bars:
            raise ValueError("Trailing stop / time stop baru didukung di backtest, belum di bot live")
        if p.entry_type in DAY_LEVEL_TYPES:
            raise ValueError(f"{p.entry_type} baru didukung di backtest (scripts/variants.py --family dt), "
                             "belum di bot live")
        if p.tp_mode == "atr" and p.tp_atr_mult <= 0 and p.exit_rule != "signal":
            raise ValueError("Strategi tanpa TP wajib memakai exit_rule: signal")
        if self.s.confirm.enabled and not self.dry_run and not getattr(self.notifier, "enabled", False):
            raise ValueError("Konfirmasi manual (confirm.enabled) butuh Telegram: isi TELEGRAM_BOT_TOKEN dan "
                             "TELEGRAM_CHAT_ID di .env, atau set confirm.enabled: false")
        self.mt5.connect()
        self.mt5.ensure_demo()
        if not self.dry_run and not self.mt5.trading_enabled():
            raise MT5Error("Algo Trading di terminal MT5 mati. Klik tombol 'Algo Trading' (Ctrl+E) sampai hijau, "
                           "lalu jalankan ulang. Agar tidak mati sendiri: Tools > Options > Expert Advisors, "
                           "hapus centang 'Disable algorithmic trading when the account has been changed'.")
        for sym in self.s.symbols:
            self.meta[sym.name] = self.mt5.symbol_meta(self.s.broker_symbol(sym.name))

        cfg_offset = self.s.session.server_utc_offset_hours
        detected = self.mt5.server_utc_offset(self.s.broker_symbol(self.s.symbols[0].name))
        if cfg_offset is not None:
            self.offset = cfg_offset
        elif detected is not None:
            self.offset = detected
        else:
            log.warning("Offset jam server tidak terdeteksi (pasar tutup?), pakai GMT+%d", FALLBACK_OFFSET)

        now = datetime.now(timezone.utc)
        if now.hour >= self.s.ai.daily_review_hour_utc:
            self.last_review_date = now.date()  # jangan kirim laporan ulang saat bot di-restart malam hari

        self.notifier.set_commands(COMMANDS)
        acc = self.mt5.account()
        mode = "DRY-RUN (tanpa order)" if self.dry_run else "LIVE di akun DEMO"
        ai = f"AI: {self.s.ai.model}" if self.llm else "AI: nonaktif (aturan saja)"
        self.send_menu(f"🤖 Bot trading mulai\nMode: {mode}\nAkun: {acc.login} @ {acc.server}\n"
                    f"Equity: {acc.equity:.2f} {acc.currency}\nPair: {', '.join(self.meta)}\n"
                    f"{ai}\nOffset server: GMT{self.offset:+d}\n"
                    f"Circuit breaker: DD maks {self.s.risk.max_drawdown_from_peak:.0%} dari puncak\n"
                    f"Konfirmasi manual: {f'aktif ({self.s.confirm.timeout_minutes} menit)' if self.s.confirm.enabled and not self.dry_run else 'nonaktif'}")

    def run(self) -> None:
        self.startup()
        keep_awake(True)
        try:
            while True:
                try:
                    self.step()
                    self.errors = 0
                    if self.error_halt:
                        self.error_halt = False
                        self.notify("✅ Koneksi pulih, entry baru diizinkan lagi.")
                except Exception as e:
                    self.errors += 1
                    log.exception("Error di loop (%d berturut-turut)", self.errors)
                    if self.errors >= MAX_CONSECUTIVE_ERRORS and not self.error_halt:
                        self.error_halt = True
                        self.notify(f"⛔ Kill switch: {self.errors} error berturut-turut, entry baru dihentikan.\n{e}")
                    if self.errors >= MAX_CONSECUTIVE_ERRORS:
                        try:
                            self.mt5.connect()
                        except MT5Error:
                            pass
                self.idle(self.s.poll_seconds)
        except KeyboardInterrupt:
            self.notify("🛑 Bot dihentikan manual.")
        finally:
            keep_awake(False)
            self.mt5.shutdown()

    def idle(self, seconds: float) -> None:
        """Tunggu sampai siklus berikutnya sambil cek klik tombol & perintah tiap 2 detik, supaya tombol terasa
        langsung merespons (Telegram juga hanya menerima jawaban klik inline dalam ~15 detik)."""
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            try:
                self.poll_confirmations()
            except Exception:
                log.exception("Error saat memproses konfirmasi")
            time.sleep(min(2.0, left))

    def check_gap(self, wall: float) -> None:
        """Siklus normal ±20 detik. Jeda jauh lebih lama = proses tertahan (laptop tidur / standby, atau
        jaringan / AI macet): beri tahu pemilik bahwa laporan & analisis di rentang itu terlewat."""
        last, self.last_step_wall = self.last_step_wall, wall
        if last is None or wall - last < SLEEP_GAP_SECONDS:
            return
        start, end = (datetime.fromtimestamp(t, timezone.utc).astimezone(WIB) for t in (last, wall))
        minutes = round((wall - last) / 60)
        log.warning("Bot tertahan %d menit (%s-%s WIB)", minutes, f"{start:%H:%M}", f"{end:%H:%M}")
        self.notify(f"💤 Bot tidak berjalan {minutes // 60}j{minutes % 60:02d}m ({start:%H:%M}-{end:%H:%M} WIB): "
                    f"laptop tidur/standby atau jaringan macet. Laporan candle dan analisis terjadwal di rentang "
                    f"itu terlewat. SL/TP yang sudah terpasang tetap berlaku di server broker, tapi pengelolaan "
                    f"posisi (parsial, trailing, exit sinyal) ikut berhenti selama itu.")

    def update_daily(self, now: datetime, equity: float) -> None:
        """Batas rugi harian per hari WIB. Equity awal hari disimpan di jurnal, jadi restart di tengah hari tidak
        mereset batas. Equity sudah termasuk floating P/L posisi terbuka."""
        wib = now.astimezone(WIB)
        if self.daily.day is None and self.journal.get_state("daily_day") == wib.date().isoformat():
            self.daily.day = wib.date()
            self.daily.start_equity = float(self.journal.get_state("daily_start_equity") or equity)
            log.info("Batas rugi harian dipulihkan: equity awal %s %.2f", self.daily.day, self.daily.start_equity)
        if self.daily.update(wib, equity):
            self.journal.set_state("daily_day", self.daily.day.isoformat())
            self.journal.set_state("daily_start_equity", f"{equity:.2f}")
            log.info("Hari baru %s (WIB), equity awal %.2f", self.daily.day, equity)

    def check_conflict(self) -> None:
        """Telegram 409 = instance bot lain memakai token yang sama (mis. PC lain di akun ini): beri tahu sekali
        saat mulai dan sekali saat selesai. Selama konflik, entry_block menolak entry baru."""
        active = getattr(self.notifier, "conflict_active", False)
        if active and not self.conflict_warned:
            self.conflict_warned = True
            log.error("Telegram 409: instance bot lain terdeteksi, entry baru dihentikan")
            self.notify("🚨 Instance bot lain terdeteksi (Telegram 409: token yang sama dipakai proses lain, mis. "
                        "PC lain). Entry baru DIHENTIKAN agar tidak ada order ganda. Posisi tetap dikelola. Matikan "
                        "salah satu bot; entry pulih otomatis 10 menit setelah konflik berhenti.")
        elif not active and self.conflict_warned:
            self.conflict_warned = False
            self.notify("✅ Konflik instance bot sudah tidak terdeteksi 10 menit, entry baru diizinkan lagi.")

    def feed_block(self, bsym: str) -> str | None:
        """Tolak entry bila data harga tidak segar: terminal putus, atau tick basi saat pasar seharusnya buka."""
        st = self.mt5.feed_status(bsym, self.offset)
        if not st["connected"]:
            return "feed terputus: terminal MT5 tidak tersambung ke server broker"
        age = st["tick_age"]
        if age <= MAX_TICK_AGE_SECONDS:
            return None
        if market_closed(st["server_time"], bsym):
            return f"pasar tutup (tick terakhir {age / 60:.0f} menit lalu)"
        log.warning("Tick %s basi %.0f detik saat pasar seharusnya buka", bsym, age)
        return f"tick basi {age:.0f} detik saat pasar seharusnya buka: feed terputus?"

    def refresh_offset(self, now: datetime) -> None:
        """Deteksi ulang offset jam server tiap jam selama pasar buka (bot yang start saat pasar tutup memakai
        cadangan GMT+2; broker bisa pindah GMT+2/+3 saat DST). Offset dipakai untuk umur tick, candle, dan expiry
        limit. Tidak dipakai bila session.server_utc_offset_hours diisi manual."""
        key = (now.date(), now.hour)
        if self.s.session.server_utc_offset_hours is not None or self.offset_checked == key or not self.s.symbols:
            return
        self.offset_checked = key
        detected = self.mt5.server_utc_offset(self.s.broker_symbol(self.s.symbols[0].name))
        if detected is not None and detected != self.offset:
            log.warning("Offset jam server berubah: GMT%+d -> GMT%+d", self.offset, detected)
            self.offset = detected

    def step(self) -> None:
        self.check_gap(time.time())
        now = datetime.now(timezone.utc)
        self.refresh_offset(now)
        acc = self.mt5.account()
        self.update_daily(now, acc.equity)
        self.update_drawdown(acc.equity)
        self.check_conflict()
        if self.last_equity_hour != (now.date(), now.hour):
            self.last_equity_hour = (now.date(), now.hour)
            self.journal.log_equity(acc.balance, acc.equity)

        if self.calendar:
            self.calendar.refresh(now)
        self.manage_breakeven()
        self.manage_take_profit_and_hold(now)
        self.manage_setup_positions(now)
        self.manage_limit_orders(now, acc.equity)
        self.sync_closed_trades()
        self.poll_confirmations()
        self.send_alerts(now)

        new_bar = False
        for sym in self.s.symbols:
            last = self.mt5.rates(self.s.broker_symbol(sym.name), self.s.strategy.entry_tf, 1)
            if last.empty:
                continue
            t = last["time"].iloc[-1]
            if self.last_bar.get(sym.name) == t:
                continue
            self.last_bar[sym.name] = t
            new_bar = True
            self.process_symbol(sym.name)
        if new_bar:
            self.resolve_shadows()
        try:
            self.maybe_candle_report(now)
        except Exception:   # laporan informatif: jangan ikut memicu kill switch
            log.exception("Laporan candle gagal")
        self.maybe_daily_review(now)
        self.maybe_weekly_review(now)
        self.maybe_scheduled_analysis(now)
        self.maybe_watch_triggers(now)
        try:
            self.maybe_screener(now)
        except Exception:   # screener tambahan: jangan ikut memicu kill switch
            log.exception("Screener M15 gagal")
        try:
            self.resolve_opportunities()
        except Exception:   # statistik saja: jangan ikut memicu kill switch
            log.exception("Penilaian peluang gagal")

    # ---------- sinyal ----------
    def frames(self, bsym: str) -> dict[str, pd.DataFrame]:
        p = self.s.strategy
        counts = {"H1": 600, "H4": 600, "D1": 400, p.entry_tf: 600, p.trend_tf: 600}
        return {tf: self.mt5.rates(bsym, tf, n) for tf, n in counts.items()}

    def process_symbol(self, sym: str) -> None:
        p, bsym, meta = self.s.strategy, self.s.broker_symbol(sym), self.meta[sym]
        frames = self.frames(bsym)
        f = compute_features(frames[p.entry_tf], frames[p.trend_tf], p, self.offset)
        if f.empty:
            return
        self.check_signal_exits(sym, bsym, f)
        sig = signal_at(f, len(f) - 1, p, self.s.session, self.offset, sym)
        if sig is None or self.journal.signal_exists(sym, str(sig.bar_time)):
            return

        sid = self.journal.log_signal(sym, sig.direction, str(sig.bar_time), sig.close,
                                      sig.sl_distance, sig.tp_distance, sig.snapshot)
        acc = self.mt5.account()
        tick = self.mt5.tick(bsym)
        spread_pts = (tick.ask - tick.bid) / meta["point"]
        entry = tick.ask if sig.direction == 1 else tick.bid
        self.journal.update_signal(sid, ref_price=entry)
        open_list = self.open_list()
        head = f"{sig.side} {sym} @ {entry:.{meta['digits']}f}"
        sl = entry - sig.direction * sig.sl_distance
        tp = entry + sig.direction * sig.tp_distance if sig.tp_distance > 0 else None
        price_fmt = f".{meta['digits']}f"
        tp_txt = format(tp, price_fmt) if tp is not None else "- (exit sinyal)"

        if self.s.symbol_cfg(sym).rule_mode == "shadow":
            # Edge belum terbukti (audit 2026-11-16 H6): catat + shadow trade untuk dievaluasi, tanpa AI dan order
            self.journal.update_signal(sid, ai_decision="shadow_mode", ai_multiplier=0.0,
                                       skip_reason="mode shadow (symbols[].rule_mode)")
            self.journal.open_shadow(sid, entry, sl, tp)
            self.notify(f"🧪 Sinyal {p.entry_type} {p.entry_tf} {head} | SL {sl:{price_fmt}} | TP {tp_txt}\n"
                        f"Mode shadow: hanya dicatat dan dievaluasi, tidak ada order.")
            return

        block = self.entry_block(sym, bsym, sig.direction, acc.equity, spread_pts,
                                 new_risk=min(self.allowed_risk_options()))
        if block:
            self.journal.update_signal(sid, skip_reason=block)
            self.notify(f"⏭️ Sinyal {head} dilewati: {block}")
            return

        news = self.news_block(bsym)
        if news:
            # Tetap di-shadow supaya nanti terukur apakah filter berita membantu
            self.journal.update_signal(sid, ai_decision="news_block", ai_multiplier=0.0, skip_reason=news)
            self.journal.open_shadow(sid, entry, sl, tp)
            self.notify(f"📰 Sinyal {head} diblok: {news}")
            return

        outcome = self.review(sig, frames, meta, entry, spread_pts, acc, open_list)
        self.journal.update_signal(sid, ai_decision=outcome.decision, ai_multiplier=outcome.multiplier,
                                   ai_confidence=outcome.confidence, ai_reasons=outcome.reasons,
                                   ai_risks=outcome.risks, ai_error=outcome.error, ai_model=self.model_used())

        suggested = self.s.risk.risk_per_trade * outcome.multiplier
        lot = self.lot_for(sym, acc.equity, suggested, sig.sl_distance)
        ai_text = self.format_ai(outcome)

        skip = None
        if outcome.multiplier == 0:
            skip = "AI error" if outcome.decision == "error" else "ditolak AI"
        elif lot == 0:
            skip = self.min_lot_reason(sym, acc.equity, sig.sl_distance)
        elif self.dry_run:
            skip = "dry_run"
        elif not self.s.confirm.enabled:   # eksekusi otomatis: risiko saran harus muat di batas risiko terbuka
            skip = self.open_risk_block(acc.equity, suggested)

        if skip is None and self.s.confirm.enabled:
            if self.ask_confirmation(sid, sym, sig, head, sl, tp_txt, price_fmt, acc.equity, suggested, lot, ai_text):
                return
            skip = "pesan konfirmasi Telegram gagal terkirim"

        if skip is None:
            try:
                lot, fill = self.open_trade(sid, sym, sig.direction, sig.sl_distance, sig.tp_distance,
                                            acc.equity, suggested, f"bot {outcome.decision}")
            except MT5Error as e:
                skip = f"order gagal: {e}"
            else:
                risk_usd = self.risk_amount(sym, lot, sig.sl_distance)
                self.notify(f"✅ OPEN {head}\nLot {lot} | SL {sl:{price_fmt}} | TP {tp_txt}\n"
                            f"Risiko {risk_usd:.2f} {acc.currency} ({risk_usd / acc.equity:.1%} equity; "
                            f"AI {outcome.multiplier:.0%} dari {self.s.risk.risk_per_trade:.1%})\n{ai_text}")
                return

        self.journal.update_signal(sid, skip_reason=skip)
        self.journal.open_shadow(sid, entry, sl, tp)
        icon = "🧪" if skip == "dry_run" else "🚫"
        self.notify(f"{icon} {head} tidak dieksekusi ({skip})\nLot rencana {lot} | SL {sl:{price_fmt}} | "
                    f"TP {tp_txt}\n{ai_text}")

    # ---------- pengecekan & eksekusi (dipakai entry langsung maupun konfirmasi) ----------
    def entry_block(self, sym: str, bsym: str, direction: int, equity: float, spread_pts: float,
                    source: str = "rule", new_risk: float | None = None) -> str | None:
        """Alasan entry baru tidak boleh dibuka. source = rule (strategi aturan) | ai_setup: aturan satu posisi per
        pair hanya berlaku dalam sumber yang sama (audit H3). new_risk = risiko entry baru (fraksi equity) untuk batas
        total risiko terbuka (audit M4); None = tidak dicek di sini."""
        if self.error_halt:
            return "kill switch aktif (error berulang)"
        if getattr(self.notifier, "conflict_active", False):
            return "instance bot lain terdeteksi (Telegram 409): entry dihentikan agar tidak ada order ganda"
        feed = self.feed_block(bsym)
        if feed:
            return feed
        if self.loss_pause():
            return (f"{self.s.risk.stop_after_losses} rugi beruntun: evaluasi dulu, lalu tekan ▶️ Lanjut "
                    f"(atau kirim /lanjut)")
        if self.journal.get_state("paused") == "1":
            return "entry dijeda (tekan ▶️ Resume atau kirim /resume untuk lanjut)"
        if self.daily.halted(equity):
            return f"batas rugi harian {self.s.risk.max_daily_loss * 100:g}% tercapai"
        if self.dd.halted:
            return (f"circuit breaker aktif: equity turun {self.dd.drawdown(equity):.1%} dari puncak "
                    f"{self.dd.peak:.2f} (batas {self.s.risk.max_drawdown_from_peak:.0%}), reset manual di Telegram")
        if spread_pts > self.s.symbol_cfg(sym).max_spread_points:
            return f"spread {spread_pts:.0f} point terlalu lebar"
        entries = self.open_entries()
        block = check_new_trade(bsym, direction, [(s, d) for s, d, _ in entries], self.s.risk.max_open_positions,
                                self.s.risk.max_currency_exposure, [(s, d) for s, d, src in entries if src == source])
        return block or (self.open_risk_block(equity, new_risk) if new_risk is not None else None)

    # ---------- batas total risiko terbuka (audit M4) ----------
    def position_source(self, pos) -> str:
        """ai_setup bila posisi berasal dari setup 📊 (komentar order MT5 'setup' / 'limit', atau tercatat di jurnal),
        selain itu rule. Komentar ikut tersimpan di MT5, jadi tetap benar setelah restart / dari PC lain."""
        comment = getattr(pos, "comment", "") or ""
        return "ai_setup" if "setup" in comment or "limit" in comment or self.setup_of(pos.ticket) else "rule"

    def open_entries(self) -> list[tuple[str, int, str]]:
        """(simbol broker, arah, sumber) posisi terbuka bot + order limit setup yang menunggu."""
        return ([(p.symbol, 1 if p.type == 0 else -1, self.position_source(p)) for p in self.mt5.positions(self.s.magic)]
                + [(self.s.broker_symbol(r["symbol"]), r["direction"], "ai_setup") for r in self.journal.limit_orders()])

    def meta_for(self, bsym: str) -> dict:
        names = {self.s.broker_symbol(n): n for n in self.meta}
        return self.meta[names[bsym]] if bsym in names else self.mt5.symbol_meta(bsym)

    def open_risk(self) -> tuple[float, str | None]:
        """(uang yang hilang bila semua SL kena, masalah | None). Dibaca langsung dari MT5 (sumber kebenaran, aman saat
        restart, termasuk order dari instance lain dengan magic sama): posisi = lot x jarak harga open -> SL (0 bila SL
        sudah di breakeven / mengunci profit); order pending = lot sisa x jarak harga order -> SL. Limit yang terisi
        pindah dari daftar pending ke posisi, jadi tidak terhitung ganda. Posisi tanpa SL memakai risk_amount jurnal;
        bila tidak ada, risikonya tidak diketahui (masalah)."""
        total = 0.0
        for p in self.mt5.positions(self.s.magic):
            d, m = (1 if p.type == 0 else -1), self.meta_for(p.symbol)
            if p.sl:
                total += p.volume * max(0.0, d * (p.price_open - p.sl)) / m["tick_size"] * m["tick_value"]
                continue
            row = next((r for r in self.journal.open_trades() if r["ticket"] == p.ticket), None)
            if row is None or not row["risk_amount"]:
                return total, f"posisi #{p.ticket} {p.symbol} tanpa SL (risiko tidak diketahui)"
            total += row["risk_amount"]
        for o in self.mt5.pending_orders(self.s.magic):
            d, m = (1 if o.type in (2, 4) else -1), self.meta_for(o.symbol)   # BUY_LIMIT/STOP genap, SELL ganjil
            if not o.sl:
                return total, f"order #{o.ticket} {o.symbol} tanpa SL (risiko tidak diketahui)"
            total += o.volume_current * max(0.0, d * (o.price_open - o.sl)) / m["tick_size"] * m["tick_value"]
        return total, None

    def risk_room(self, equity: float) -> float | None:
        """Sisa ruang risiko (fraksi equity) di bawah risk.max_open_risk; None = batas nonaktif."""
        cap = self.s.risk.max_open_risk
        if not cap:
            return None
        used, problem = self.open_risk()
        return 0.0 if problem else cap - used / equity

    def open_risk_block(self, equity: float, new_risk: float) -> str | None:
        cap = self.s.risk.max_open_risk
        if not cap:
            return None
        used, problem = self.open_risk()
        if problem:
            return f"batas risiko terbuka: {problem}"
        if used / equity + new_risk > cap + 1e-9:
            return (f"batas risiko terbuka {cap * 100:g}%: terpakai {used / equity:.2%} ({used:.2f}), entry baru "
                    f"{new_risk * 100:g}% akan melewatinya")
        return None

    def news_block(self, bsym: str, setup: bool = False) -> str | None:
        """setup=True: jendela pendek news.setup_block_* untuk setup 📊 Analisis (day trade)."""
        n = self.s.news
        window = {"before": n.setup_block_before_hours, "after": n.setup_block_after_hours} if setup else {}
        news = self.calendar.blocking_event(bsym, datetime.now(timezone.utc), **window) if self.calendar else None
        if news is None:
            return None
        hours = (news.time - datetime.now(timezone.utc)).total_seconds() / 3600
        when = f"{hours:.1f} jam lagi" if hours >= 0 else f"{-hours:.1f} jam lalu"
        return f"berita {news.impact} {news.currency}: {news.title} ({when})"

    def lot_for(self, sym: str, equity: float, risk: float, sl_distance: float) -> float:
        """Lot untuk risiko `risk` x equity; akun kecil memakai lot minimum bila risikonya <= min_lot_max_risk.
        Dibatasi symbols[].max_lot bila diisi."""
        lot = lot_with_min(equity, risk, max(risk, self.s.risk.min_lot_max_risk), sl_distance, self.meta[sym])
        cap = self.s.symbol_cfg(sym).max_lot
        return min(lot, cap) if cap > 0 else lot

    def risk_amount(self, sym: str, lot: float, sl_distance: float) -> float:
        """Kerugian sebenarnya (mata uang akun) jika SL kena."""
        m = self.meta[sym]
        return lot * sl_distance / m["tick_size"] * m["tick_value"]

    def min_lot_reason(self, sym: str, equity: float, sl_distance: float) -> str:
        cap = max(self.s.risk.risk_per_trade, self.s.risk.min_lot_max_risk)
        loss = self.risk_amount(sym, self.meta[sym]["volume_min"], sl_distance)
        return (f"modal terlalu kecil: lot minimum rugi {loss:.2f} di SL ({loss / equity:.1%} equity), "
                f"batas {cap:.1%}")

    def open_trade(self, sid: int, sym: str, direction: int, sl_distance: float, tp_distance: float,
                   equity: float, risk: float, comment: str) -> tuple[float, float]:
        """Kirim order & catat ke jurnal. Return (lot, harga fill). Raise MT5Error jika gagal."""
        bsym = self.s.broker_symbol(sym)
        lot = self.lot_for(sym, equity, risk, sl_distance)
        if lot == 0:
            raise MT5Error(self.min_lot_reason(sym, equity, sl_distance))
        res = self.mt5.market_order(bsym, direction, lot, sl_distance, tp_distance, self.s.magic, comment)
        t = self.mt5.tick(bsym)
        fill = res.price or (t.ask if direction == 1 else t.bid)
        ticket = self.find_ticket(bsym) or res.order
        self.journal.log_trade_open(sid, ticket, sym, direction, lot, fill, fill - direction * sl_distance,
                                    fill + direction * tp_distance if tp_distance > 0 else None,
                                    self.risk_amount(sym, lot, sl_distance))
        self.journal.update_signal(sid, executed=1)
        return lot, fill

    # ---------- konfirmasi manual lewat Telegram ----------
    def ask_confirmation(self, sid: int, sym: str, sig: Signal, head: str, sl: float, tp_txt: str, price_fmt: str,
                         equity: float, suggested: float, lot: float, ai_text: str,
                         timeout_minutes: int | None = None, suggest_label: str = "Saran AI",
                         max_risk: float | None = None) -> bool:
        c = self.s.confirm
        timeout_minutes = timeout_minutes or c.timeout_minutes
        rows = []
        streak = self.tilt_streak()
        seen = set()
        room = self.risk_room(equity)
        for r in self.allowed_risk_options():
            if max_risk is not None and r > max_risk + 1e-12:   # setup skor di bawah premium: tanpa opsi risiko besar
                continue
            if room is not None and r > room + 1e-9:            # melewati batas total risiko terbuka (M4)
                continue
            opt_lot = self.lot_for(sym, equity, r, sig.sl_distance)
            if opt_lot > 0 and opt_lot not in seen:   # akun kecil: beberapa opsi bisa jadi lot minimum yang sama
                seen.add(opt_lot)
                star = "⭐" if opt_lot == lot else "✅"
                usd = self.risk_amount(sym, opt_lot, sig.sl_distance)
                label = (f"{star} {r * 100:g}% ${usd:.0f} ({opt_lot} lot)" if usd <= equity * r + 1e-9
                         else f"{star} {opt_lot} lot · ${usd:.2f} ({usd / equity:.1%})")  # lot minimum dipaksa
                rows.append((label, f"c:{sid}:{r}"))
        if not rows:
            return False
        meta = self.meta[sym]
        pip = meta["point"] * (10 if meta["digits"] in (3, 5) else 1)
        tilt = (f"⚠️ Cek tilt: {streak} rugi beruntun. Opsi di atas {self.s.risk.risk_per_trade * 100:g}% "
                f"disembunyikan sampai ada trade untung.\n" if streak else "")
        text = (f"📈 {head}\nSL {sl:{price_fmt}} ({sig.sl_distance / pip:.0f} pip) | TP {tp_txt}\n{ai_text}\n\n"
                f"{tilt}{suggest_label}: {suggested * 100:g}% → lot {lot} "
                f"(rugi di SL ${self.risk_amount(sym, lot, sig.sl_distance):.2f})\n"
                f"Pilih dalam {timeout_minutes} menit, lewat dari itu otomatis batal:")
        buttons = [rows[i:i + 2] for i in range(0, len(rows), 2)] + [[("❌ Tidak", f"c:{sid}:no")]]
        mid = self.notifier.send_buttons(text, buttons)
        if mid is None:
            return False
        expires = datetime.now(timezone.utc) + timedelta(minutes=timeout_minutes)
        self.journal.update_signal(sid, user_decision="pending", confirm_msg_id=mid,
                                   confirm_expires=expires.isoformat(timespec="seconds"))
        return True

    def poll_confirmations(self) -> None:
        if not getattr(self.notifier, "enabled", False):
            return
        for upd in self.notifier.updates():
            if upd.get("message"):
                try:
                    self.handle_message(upd["message"])
                except Exception:
                    log.exception("Error saat memproses perintah Telegram")
                continue
            cq = upd.get("callback_query")
            if not cq:
                continue
            chat = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
            if chat != str(self.s.telegram.chat_id):
                self.notifier.answer(cq["id"], "Tidak diizinkan")
                log.warning("Callback dari chat tak dikenal %s diabaikan", chat)
                continue
            if cq.get("data") == "go:1":
                self.resume_after_losses()
                self.notifier.answer(cq["id"], "Entry dilanjutkan")
                self.notifier.clear_buttons((cq.get("message") or {}).get("message_id"))
                continue
            if cq.get("data") == "dd:reset":
                self.reset_drawdown(cq)
                continue
            if str(cq.get("data", "")).split(":")[0] in ("pc", "pcy", "pcn", "be", "ds", "pa"):
                self.position_action(cq)
                continue
            if str(cq.get("data", "")).startswith("la:"):
                self.limit_action(cq)
                continue
            parts = str(cq.get("data", "")).split(":")
            row = self.journal.get_signal(int(parts[1])) if len(parts) == 3 and parts[1].isdigit() else None
            if row is None or row["user_decision"] != "pending":
                self.notifier.answer(cq["id"], "Sinyal ini sudah tidak berlaku")
                continue
            if parts[2] == "no":
                self.notifier.answer(cq["id"], "Dibatalkan")
                self.finish_confirmation(row, "declined", "❌ Dibatalkan oleh Anda")
                continue
            try:
                risk = float(parts[2])
            except ValueError:
                risk = -1
            if not any(abs(risk - r) < 1e-9 for r in self.allowed_risk_options()):
                self.notifier.answer(cq["id"], "Pilihan tidak valid")
                continue
            self.notifier.answer(cq["id"], "Memproses order...")
            self.execute_confirmed(row, risk)

        now = datetime.now(timezone.utc)
        for row in self.journal.pending_confirmations():
            if row["confirm_expires"] and datetime.fromisoformat(row["confirm_expires"]) <= now:
                self.finish_confirmation(row, "expired",
                                         f"⌛ Waktu {self.s.confirm.timeout_minutes} menit habis, sinyal dibatalkan")

    def tilt_streak(self) -> int:
        """Jumlah rugi beruntun jika sudah mencapai ambang cek tilt, selain itu 0."""
        n = self.s.risk.tilt_losing_streak
        streak = current_losing_streak(self.journal) if n else 0
        return streak if n and streak >= n else 0

    def allowed_risk_options(self) -> list[float]:
        opts = self.s.confirm.risk_options
        if self.tilt_streak():
            # Setelah rugi beruntun: tidak boleh menaikkan risiko di atas ukuran normal (anti revenge trading)
            opts = [r for r in opts if r <= self.s.risk.risk_per_trade + 1e-12] or opts[:1]
        return opts

    # ---------- circuit breaker drawdown ----------
    def update_drawdown(self, equity: float) -> None:
        """State disimpan di jurnal supaya bertahan saat restart dan bisa di-reset dari CLI."""
        self.dd.peak = float(self.journal.get_state("dd_peak") or 0)
        self.dd.halted = self.journal.get_state("dd_halted") == "1"
        before = (self.dd.peak, self.dd.halted)
        newly = self.dd.update(equity)
        if (self.dd.peak, self.dd.halted) != before:
            self.journal.set_state("dd_peak", f"{self.dd.peak:.2f}")
            self.journal.set_state("dd_halted", "1" if self.dd.halted else "0")
        if newly:
            self.notifier.send_buttons(
                f"🛑 CIRCUIT BREAKER: equity {equity:.2f} turun {self.dd.drawdown(equity):.1%} dari puncak "
                f"{self.dd.peak:.2f} (batas {self.s.risk.max_drawdown_from_peak:.0%}).\n"
                "Entry baru dihentikan. Posisi terbuka tetap dikelola (SL & exit sinyal).\n"
                "Ini lebih buruk dari drawdown terburuk di backtest: evaluasi dulu sebelum reset.",
                [[("🔓 Reset circuit breaker", "dd:reset")]])

    def reset_drawdown(self, cq: dict) -> None:
        if self.journal.get_state("dd_halted") != "1":
            self.notifier.answer(cq["id"], "Circuit breaker tidak aktif")
            return
        equity = self.mt5.account().equity
        self.dd.reset(equity)
        self.journal.set_state("dd_peak", f"{equity:.2f}")
        self.journal.set_state("dd_halted", "0")
        self.notifier.answer(cq["id"], "Circuit breaker di-reset")
        self.notifier.clear_buttons((cq.get("message") or {}).get("message_id"))
        self.notify(f"🔓 Circuit breaker di-reset. Puncak baru = equity sekarang {equity:.2f}. Entry baru diizinkan.")

    def finish_confirmation(self, row, decision: str, text: str) -> None:
        """Konfirmasi selesai tanpa order: catat, shadow trade, hapus tombol."""
        d, ref = row["direction"], row["ref_price"] or row["entry_ref"]
        tp = ref + d * row["tp_distance"] if row["tp_distance"] else None
        self.journal.update_signal(row["id"], user_decision=decision, skip_reason=f"user: {decision}")
        self.journal.open_shadow(row["id"], ref, ref - d * row["sl_distance"], tp)
        self.notifier.clear_buttons(row["confirm_msg_id"])
        self.notify(f"{text}\n{'BUY' if d == 1 else 'SELL'} {row['symbol']}")

    def execute_confirmed(self, row, risk: float) -> None:
        sym, d = row["symbol"], row["direction"]
        bsym, meta = self.s.broker_symbol(sym), self.meta[sym]
        acc = self.mt5.account()
        tick = self.mt5.tick(bsym)
        spread_pts = (tick.ask - tick.bid) / meta["point"]
        price = tick.ask if d == 1 else tick.bid
        atr = json.loads(row["snapshot"] or "{}").get("atr") or row["sl_distance"] / self.s.strategy.sl_atr_mult
        moved = abs(price - (row["ref_price"] or price)) / atr if atr else 0.0
        sl_dist, tp_dist, setup_block = row["sl_distance"], row["tp_distance"], None
        setup = json.loads(row["setup"]) if row["source"] == "ai_setup" and row["setup"] else None
        zone_ahead = False
        if setup:
            # Setup 📊 Analisis: SL & TP tetap di level setup, bukan jarak dari fill
            f = f".{meta['digits']}f"
            tol = 0.1 * atr
            sl_dist, tp_dist = d * (price - setup["stop_loss"]), d * (setup["take_profit_1"] - price)
            moved = 0.0
            # Zona belum tercapai (SELL: harga di bawah zona, BUY: di atas): pasang order LIMIT di tengah zona.
            # Di zona / sudah lewat zona ke arah lebih baik: order market, asal RR di harga itu masih cukup.
            zone_ahead = (price < setup["entry_low"] - tol) if d == -1 else (price > setup["entry_high"] + tol)
            rr_here = net_rr(tp_dist, sl_dist, self.atr_and_cost(sym)[1]) if sl_dist > 0 and tp_dist > 0 else 0.0
            if sl_dist <= 0 or tp_dist <= 0:
                setup_block = "harga sudah melewati SL/TP setup"
            elif self.setups_today() >= self.s.confirm.max_setups_per_day:
                setup_block = f"kuota {self.s.confirm.max_setups_per_day} setup/hari sudah terpakai"
            elif self.active_setup_block():
                setup_block = self.active_setup_block()
            elif not zone_ahead and rr_here < MIN_RR - 1e-9:
                setup_block = (f"harga {price:{f}} di luar zona entry {setup['entry_low']:{f}}-{setup['entry_high']:{f}}, "
                               f"RR bersih di harga ini {rr_here:.2f} < {MIN_RR:g}")

        # Kondisi bisa berubah selama menunggu jawaban: cek ulang semuanya
        streak = self.tilt_streak()
        block = (setup_block or self.entry_block(sym, bsym, d, acc.equity, spread_pts,
                                                 source="ai_setup" if setup else "rule", new_risk=risk)
                 or self.news_block(bsym, setup=bool(setup))
                 or (f"cek tilt: {streak} rugi beruntun, maksimal {self.s.risk.risk_per_trade * 100:g}%"
                     if not any(abs(risk - r) < 1e-9 for r in self.allowed_risk_options()) else None)
                 or (f"harga sudah bergerak {moved:.2f} ATR dari harga sinyal"
                     if moved > self.s.confirm.max_price_move_atr else None))
        if block:
            self.finish_confirmation(row, "blocked", f"⏭️ Tidak dieksekusi: {block}")
            return
        if setup and zone_ahead:
            self.place_limit(row, setup, risk, acc)
            return
        try:
            lot, fill = self.open_trade(row["id"], sym, d, sl_dist, tp_dist, acc.equity, risk,
                                        "bot user setup" if setup else "bot user")
        except MT5Error as e:
            self.finish_confirmation(row, "failed", f"⚠️ Order gagal: {e}")
            return
        self.journal.update_signal(row["id"], user_decision="approved", user_risk=risk)
        self.notifier.clear_buttons(row["confirm_msg_id"])
        tp = f"{fill + d * row['tp_distance']:.{meta['digits']}f}" if row["tp_distance"] else "- (exit sinyal)"
        self.notify(f"✅ OPEN {'BUY' if d == 1 else 'SELL'} {sym} @ {fill:.{meta['digits']}f} (pilihan Anda)\n"
                    f"Lot {lot} | SL {fill - d * row['sl_distance']:.{meta['digits']}f} | TP {tp}\n"
                    f"Risiko {acc.equity * risk:.2f} {acc.currency} ({risk * 100:g}%)")

    def place_limit(self, row, setup: dict, risk: float, acc) -> None:
        """Setup 📊 Analisis, harga belum di zona: pasang LIMIT di tengah zona (SL/TP tetap level setup)."""
        sym, d = row["symbol"], row["direction"]
        bsym, digits = self.s.broker_symbol(sym), self.meta[sym]["digits"]
        price = (setup["entry_low"] + setup["entry_high"]) / 2
        sl, tp = setup["stop_loss"], setup["take_profit_1"]
        sl_dist = d * (price - sl)
        expires = datetime.now(timezone.utc) + timedelta(minutes=setup.get("valid_minutes") or 120)
        try:
            lot = self.lot_for(sym, acc.equity, risk, sl_dist)
            if lot == 0:
                raise MT5Error(self.min_lot_reason(sym, acc.equity, sl_dist))
            res = self.mt5.limit_order(bsym, d, lot, price, sl, tp, self.s.magic, "bot user limit",
                                       expires=expires, offset_hours=self.offset)
        except MT5Error as e:
            self.finish_confirmation(row, "failed", f"⚠️ Order limit gagal: {e}")
            return
        self.journal.update_signal(row["id"], user_decision="limit", user_risk=risk, limit_ticket=res.order,
                                   limit_expires=expires.isoformat(timespec="seconds"))
        self.notifier.clear_buttons(row["confirm_msg_id"])
        f = f".{digits}f"
        when = LIMIT_EXPIRY_TEXT[self.mt5.expiry_mode(bsym)].format(t=f"{expires.astimezone(WIB):%H:%M}")
        self.notify(f"⏳ LIMIT {'BUY' if d == 1 else 'SELL'} {sym} @ {price:{f}} (tengah zona "
                    f"{setup['entry_low']:{f}}-{setup['entry_high']:{f}})\nLot {lot} | SL {sl:{f}} | TP {tp:{f}}\n"
                    f"Risiko {self.risk_amount(sym, lot, sl_dist):.2f} {acc.currency} | {when}; juga dibatalkan bot "
                    f"jika ada berita / batas rugi tercapai")

    def manage_limit_orders(self, now: datetime, equity: float) -> None:
        """Order limit setup: catat saat terisi, batalkan saat kedaluwarsa / kondisi entry tidak lagi aman."""
        rows = self.journal.limit_orders()
        if not rows:
            return
        pending = {o.ticket for o in self.mt5.pending_orders(self.s.magic)}
        for row in rows:
            sym, d, ticket = row["symbol"], row["direction"], row["limit_ticket"]
            side = f"{'BUY' if d == 1 else 'SELL'} {sym}"
            if ticket in pending:
                why = ("kedaluwarsa" if datetime.fromisoformat(row["limit_expires"]) <= now
                       else self.news_block(self.s.broker_symbol(sym), setup=True)
                       or ("batas rugi harian tercapai" if self.daily.halted(equity) else None)
                       or ("circuit breaker aktif" if self.dd.halted else None))
                if why and self.mt5.cancel_order(ticket):
                    self.journal.update_signal(row["id"], user_decision="limit_cancelled", skip_reason=f"limit: {why}")
                    self.notify(f"⌛ Limit {side} dibatalkan: {why}")
                continue
            out = self.mt5.order_outcome(ticket)
            if out is None:
                continue   # histori belum tersedia, coba lagi siklus berikut
            if not out["filled"]:
                self.journal.update_signal(row["id"], user_decision="limit_cancelled", skip_reason="limit dibatalkan di MT5")
                self.notify(f"⌛ Limit {side} tidak terisi (dibatalkan di MT5)")
                continue
            sl, tp = out["sl"] or None, out["tp"] or None
            self.journal.log_trade_open(row["id"], out["position_id"], sym, d, out["volume"], out["price"], sl, tp,
                                        self.risk_amount(sym, out["volume"], abs(out["price"] - sl)) if sl else 0.0)
            self.journal.update_signal(row["id"], executed=1, user_decision="approved")
            f = f".{self.meta[sym]['digits']}f"
            self.notify(f"✅ LIMIT TERISI {side} @ {out['price']:{f}}\nLot {out['volume']} | "
                        f"SL {sl:{f}} | TP {tp:{f}}" if sl and tp else f"✅ LIMIT TERISI {side} @ {out['price']:{f}}")

    # ---------- perintah Telegram (hanya dari TELEGRAM_CHAT_ID) ----------
    def handle_message(self, msg: dict) -> None:
        chat = str((msg.get("chat") or {}).get("id", ""))
        if chat != str(self.s.telegram.chat_id):
            log.warning("Pesan dari chat tak dikenal %s diabaikan", chat)
            return
        text = (msg.get("text") or "").strip()
        if text in MENU:
            cmd = MENU[text]
        elif text.startswith("/"):
            cmd = text.split()[0][1:].split("@")[0].lower()
        else:
            cmd = "menu"
        {"status": self.cmd_status, "posisi": self.cmd_positions, "pnl": self.cmd_pnl, "sinyal": self.cmd_signals,
         "pause": self.cmd_pause, "resume": self.cmd_resume, "analisis": self.cmd_analysis,
         "lanjut": self.resume_after_losses, "cekposisi": self.cmd_position_review, "evaluasi": self.cmd_evaluation,
         "menu": self.cmd_menu, "start": self.cmd_menu}.get(cmd, self.cmd_help)()

    def menu_rows(self) -> list[list[str]]:
        """Saat dijeda tampilkan tombol Resume, selain itu Pause."""
        toggle = "▶️ Resume" if self.journal.get_state("paused") == "1" else "⏸️ Pause"
        return [["📟 Status", "📂 Posisi"], ["📈 P/L", "🔔 Sinyal"], ["📊 Analisis", "🩺 Posisi AI"],
                ["📋 Evaluasi", toggle], ["❓ Bantuan"]]

    def send_menu(self, text: str) -> None:
        self.notifier.send_keyboard(text, self.menu_rows())

    def cmd_menu(self) -> None:
        self.send_menu("👇 Pilih lewat tombol di bawah kolom chat")

    def cmd_help(self) -> None:
        self.send_menu("🤖 Tekan tombol di bawah, atau ketik perintah:\n"
                       + "\n".join(f"/{c} - {d}" for c, d in COMMANDS))

    def next_entry_close(self, now: datetime) -> str:
        """Close candle entry berikutnya yang masuk jam sesi (WIB)."""
        tf = TF_DELTA[self.s.strategy.entry_tf]
        t = pd.Timestamp(now).tz_convert(None) + pd.Timedelta(hours=self.offset)   # jam server
        t = t.floor(tf) + tf
        for _ in range(int(pd.Timedelta(days=8) / tf)):
            if in_session(t, self.offset, self.s.session, self.s.strategy.session_hours):
                wib = (t - pd.Timedelta(hours=self.offset)).tz_localize("UTC").tz_convert(WIB)
                return wib.strftime("%a %d/%m %H:%M WIB")
            t += tf
        return "-"

    def cmd_status(self) -> None:
        now = datetime.now(timezone.utc)
        acc = self.mt5.account()
        r = self.s.risk
        entry = ("⏸️ dijeda (tekan ▶️ Resume)" if self.journal.get_state("paused") == "1"
                 else "⛔ kill switch" if self.error_halt
                 else "🛑 circuit breaker" if self.dd.halted
                 else "🛑 batas rugi harian" if self.daily.halted(acc.equity) else "✅ aktif")
        mode = "DRY-RUN" if self.dry_run else "LIVE (akun DEMO)"
        self.notify(f"📟 Status | {mode}\nEquity {acc.equity:.2f} {acc.currency} | Balance {acc.balance:.2f}\n"
                    f"P/L hari ini (sejak 00:00 WIB, termasuk floating): {self.daily.pnl_pct(acc.equity):+.2%}\n"
                    f"Drawdown dari puncak {self.dd.peak:.2f}: {self.dd.drawdown(acc.equity):.1%} "
                    f"(batas {r.max_drawdown_from_peak:.0%})\n"
                    f"Posisi: {len(self.mt5.positions(self.s.magic))}/{r.max_open_positions}\n"
                    + self.open_risk_text(acc.equity)
                    + f"Entry baru: {entry}\nCandle entry berikut: {self.next_entry_close(now)}")

    def open_risk_text(self, equity: float) -> str:
        if not self.s.risk.max_open_risk:
            return ""
        used, problem = self.open_risk()
        return (f"Risiko terbuka (posisi + limit, bila semua SL kena): {used:.2f} = {used / equity:.2%} dari batas "
                f"{self.s.risk.max_open_risk * 100:g}%" + (f" ⚠️ {problem}" if problem else "") + "\n")

    def cmd_positions(self) -> None:
        positions = self.mt5.positions(self.s.magic)
        if not positions:
            self.notify("📭 Tidak ada posisi terbuka")
            return
        now = datetime.now(timezone.utc)
        for pos in positions:
            hours = (now - datetime.fromtimestamp(pos.time - self.offset * 3600, timezone.utc)).total_seconds() / 3600
            age = f"{hours / 24:.1f} hari" if hours >= 24 else f"{hours:.0f} jam"
            trend = "searah ✅" if self.trend_intact(pos) else "sinyal exit muncul ⚠️"
            net = pos.profit + getattr(pos, "swap", 0.0)
            self.notifier.send_buttons(
                f"{'BUY' if pos.type == 0 else 'SELL'} {pos.symbol} {pos.volume} lot #{pos.ticket}\n"
                f"Open {pos.price_open} | SL {pos.sl or '-'}\nProfit {net:+.2f} | umur {age} | tren {trend}",
                [[("❌ Tutup sekarang", f"pc:{pos.ticket}"), ("🔒 SL ke breakeven", f"be:{pos.ticket}")]]
                + ([[(f"🤖 Dynamic SL: {'OFF, nyalakan' if self.manage_state(pos.ticket).get('off') else 'ON, matikan'}",
                      f"ds:{pos.ticket}")]] if self.setup_of(pos.ticket) else []))

    def cmd_pnl(self) -> None:
        now = datetime.now(timezone.utc)
        day = now.astimezone(WIB).replace(hour=0, minute=0, second=0, microsecond=0)
        lines = []
        for label, start in (("Hari ini", day), ("Minggu ini", day - timedelta(days=day.weekday())),
                             ("Bulan ini", day.replace(day=1))):
            iso = start.astimezone(timezone.utc).isoformat(timespec="seconds")
            rows = [t for t in self.journal.trades_since(iso) if t["status"] == "closed" and t["close_time"] >= iso]
            st = trade_stats(rows)
            lines.append(f"{label}: {st['trades']} trade" + (
                f", winrate {st['winrate_%']}%, profit {st['profit']:+.2f}"
                + (f" ({st['total_R']:+.2f}R)" if st["total_R"] is not None else "") if st["trades"] else ""))
        floating = sum(p.profit + getattr(p, "swap", 0.0) for p in self.mt5.positions(self.s.magic))
        self.notify("📈 P/L (trade ditutup, hari WIB)\n" + "\n".join(lines) + f"\nFloating posisi terbuka: {floating:+.2f}")

    def cmd_signals(self) -> None:
        rows = self.journal.recent_signals(5)
        if not rows:
            self.notify("Belum ada sinyal")
            return
        lines = []
        for r in rows:
            when = datetime.fromisoformat(r["created_at"]).astimezone(WIB).strftime("%d/%m %H:%M")
            status = ("✅ dieksekusi" if r["executed"] else "⏳ menunggu konfirmasi" if r["user_decision"] == "pending"
                      else "⏳ limit menunggu harga" if r["user_decision"] == "limit"
                  else f"⏭️ {r['skip_reason'] or '-'}")
            ai = f" | AI {r['ai_decision']}" if r["ai_decision"] and r["ai_decision"] != "rule_only" else ""
            src = "📊 setup " if r["source"] == "ai_setup" else ""
            lines.append(f"{when} {src}{'BUY' if r['direction'] == 1 else 'SELL'} {r['symbol']}: {status}{ai}")
        self.notify("🔔 5 sinyal terakhir (WIB)\n" + "\n".join(lines))

    def loss_pause(self) -> bool:
        """True jika N trade nyata terakhir semuanya rugi dan pemilik belum menekan Lanjut sejak itu."""
        n = self.s.risk.stop_after_losses
        rows = self.journal.recent_closed(n) if n else []
        if not n or len(rows) < n or any((r["profit"] or 0) >= 0 for r in rows):
            return False
        return self.journal.get_state("loss_ack") != rows[0]["close_time"]

    def resume_after_losses(self) -> None:
        rows = self.journal.recent_closed(1)
        self.journal.set_state("loss_ack", rows[0]["close_time"] if rows else "")
        self.notify("▶️ Entry baru dan setup diizinkan lagi. Jeda aktif kembali bila rugi beruntun terulang.")

    def cmd_pause(self) -> None:
        self.journal.set_state("paused", "1")
        self.send_menu("⏸️ Entry baru dijeda. Posisi terbuka tetap dikelola (SL, TP, exit sinyal). "
                       "Tekan ▶️ Resume untuk lanjut.")

    def cmd_resume(self) -> None:
        self.journal.set_state("paused", "0")
        self.send_menu("▶️ Entry baru diizinkan lagi.")

    # ---------- 📊 Analisis harian oleh Claude ----------
    def analysis_cooldown_left(self) -> float:
        return ANALYSIS_COOLDOWN - (time.time() - float(self.journal.get_state("analysis_last_ts") or 0))

    def cmd_analysis(self, trigger: dict | None = None) -> bool:
        """Jalankan analisis. trigger = item pantauan yang baru kena (analisis otomatis, tanpa pesan cooldown).
        False bila tidak jalan karena AI nonaktif atau cooldown."""
        if self.llm is None:
            if trigger is None:
                self.notify("📊 Analisis butuh AI aktif (ai.enabled: true dan 9Router tersambung).")
            return False
        left = self.analysis_cooldown_left()
        if left > 0:
            if trigger is None:
                self.notify(f"⏳ Analisis terakhir baru {ANALYSIS_COOLDOWN - left:.0f} detik lalu. Coba lagi dalam "
                            f"{left:.0f} detik.")
            return False
        self.journal.set_state("analysis_last_ts", f"{time.time():.0f}")
        self.notify(f"⏳ Menganalisis {', '.join(self.meta)}... ±1-2 menit.")
        try:
            analysis = self.run_analysis(trigger)
        except Exception as e:
            log.warning("Analisis AI gagal: %s", e)
            self.notify(f"⚠️ Analisis gagal: {e}")
            return True
        now = datetime.now(timezone.utc).astimezone(WIB)
        used = getattr(self.llm, "last_model", None)
        backup = f" (model cadangan: {used})" if used and used != self.s.ai.model else ""
        watched = self.store_watch(analysis)
        opps = [format_opportunity(o, self.meta[o.symbol]["digits"]) for o in analysis.opportunities
                if o.symbol in self.meta]
        self.notify(f"📊 Analisis {now:%a %d/%m %H:%M} WIB{backup}\n\n{analysis.text}"
                    + ("\n\n🔍 Peluang tercatat (belum dieksekusi):\n" + "\n".join(opps) if opps else "")
                    + ("\n\n👀 Dipantau (analisis ulang otomatis saat pemicu kena):\n" + "\n".join(watched)
                       if watched else ""))
        self.offer_setups(analysis)
        return True

    def analysis_frames(self, bsym: str) -> dict[str, pd.DataFrame]:
        out = {}
        for tf, n in ANALYSIS_FRAMES.items():
            try:
                df = self.mt5.rates(bsym, tf, n)
            except MT5Error as e:   # broker tanpa histori M5/M15: analisis tetap jalan dengan frame yang ada
                log.warning("Frame %s %s tidak tersedia: %s", bsym, tf, e)
                continue
            if len(df):
                out[tf] = df
        return out

    def run_analysis(self, trigger: dict | None = None) -> Analysis:
        """Kumpulkan snapshot semua pair, minta analisis Claude. Raise jika AI gagal / jawaban tidak valid."""
        now = datetime.now(timezone.utc)
        acc = self.mt5.account()
        per_symbol, spreads, prices = {}, {}, {}
        for sym, meta in self.meta.items():
            bsym = self.s.broker_symbol(sym)
            per_symbol[sym] = self.analysis_frames(bsym)
            t = self.mt5.tick(bsym)
            spreads[sym] = (t.ask - t.bid) / meta["point"]
            prices[sym] = {"bid": t.bid, "ask": t.ask}
        positions = [{"symbol": p.symbol, "side": "BUY" if p.type == 0 else "SELL", "volume": p.volume,
                      "open": p.price_open, "sl": p.sl, "tp": p.tp, "profit": round(p.profit, 2)}
                     for p in self.mt5.positions(self.s.magic)]
        recent = [{"symbol": r["symbol"], "side": "BUY" if r["direction"] == 1 else "SELL",
                   "r": round(r["r_multiple"], 2) if r["r_multiple"] is not None else None,
                   "closed": r["close_time"]} for r in self.journal.recent_closed(5)]
        r, c = self.s.risk, self.s.confirm
        rules = {"initial_risk_by_tier_%": {"STRONG": round(self.tier_risk(STRONG_SCORE) * 100, 3),
                                            "MODERATE": round(self.tier_risk(0) * 100, 3)},
                 "max_daily_loss_%": r.max_daily_loss * 100, "today_pnl_%": round(100 * self.daily.pnl_pct(acc.equity), 2),
                 "stop_after_consecutive_losses": r.stop_after_losses,
                 "max_active_setups": c.max_active_setups, "active_setups_now": self.journal.active_setups(),
                 "min_net_reward_risk": MIN_RR,
                 "cost_per_symbol_in_price": {s: round(self.atr_and_cost(s)[1], self.meta[s]["digits"]) for s in self.meta},
                 "max_setups_per_day": c.max_setups_per_day, "setups_taken_today": self.setups_today(),
                 "paused_after_consecutive_losses": self.loss_pause(),
                 "forward_evidence_by_strategy": self.forward_evidence(),
                 f"opportunity_review_last_{OPPORTUNITY_REVIEW_DAYS}d": self.opportunity_review(),
                 "execution": "after owner confirms: price not yet at zone = LIMIT order at zone middle, "
                              f"waits valid_minutes; price at/through zone = market order if net RR still >= {MIN_RR:g}",
                 "news_block_for_setups": f"{self.s.news.setup_block_before_hours:g}h before to "
                                          f"{self.s.news.setup_block_after_hours:g}h after "
                                          f"{'/'.join(self.s.news.block_impacts)} events of the pair's currencies"}
        if trigger:
            rules["triggered_by"] = {k: v for k, v in trigger.items() if k not in ("id", "created")}
        ctx = build_analysis_context(per_symbol, self.s.strategy, self.meta, spreads, now, self.offset,
                                     self.calendar if self.calendar and self.calendar.available else None,
                                     {"equity": round(acc.equity, 2), "balance": round(acc.balance, 2),
                                      "currency": acc.currency}, positions, recent, rules, prices)
        return analyze_market(self.llm, ctx)

    def setup_checks(self, analysis: Analysis) -> list[tuple]:
        """[(setup, alasan_tidak_valid | None, atr_h1)] untuk setup yang pair-nya diperdagangkan bot."""
        out = []
        for st in analysis.setups:
            if st.symbol not in self.meta:
                continue
            atr_h1, cost = self.atr_and_cost(st.symbol)
            out.append((st, validate_setup(st, atr_h1, cost), atr_h1))
        return out

    def atr_and_cost(self, sym: str) -> tuple[float, float]:
        """(ATR H1 kasar = rata-rata range 14 bar, biaya spread + komisi round-turn dalam satuan harga)."""
        bsym, meta = self.s.broker_symbol(sym), self.meta[sym]
        bars = self.mt5.rates(bsym, "H1", 100)
        atr_h1 = float((bars["high"] - bars["low"]).tail(14).mean()) if len(bars) else 0.0
        t = self.mt5.tick(bsym)
        return atr_h1, (t.ask - t.bid) + self.s.backtest.commission_per_lot * meta["tick_size"] / meta["tick_value"]

    def setups_today(self) -> int:
        day = datetime.now(timezone.utc).astimezone(WIB).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.journal.setups_executed_since(day.astimezone(timezone.utc).isoformat(timespec="seconds"))

    def offer_setups(self, analysis: Analysis) -> None:
        """Tahap eksekusi: setup yang lolos batas keras, EV forward & confidence High/Medium ditawarkan lewat tombol
        konfirmasi yang sama dengan sinyal strategi; sisanya hanya diberitahukan. Semua setup dan peluang (WAIT /
        NO_TRADE) dicatat di jurnal peluang, lalu hasilnya dinilai dari candle M15 berikutnya."""
        recent = self.recent_opportunities()
        for st, why, atr_h1 in self.setup_checks(analysis):
            digits = self.meta[st.symbol]["digits"]
            text = format_setup(st, digits) + "\n" + self.ev_text(st.strategy)
            why = why or self.ev_block(st.strategy)
            sid = None
            again = find_duplicate(recent, st.symbol, st.direction, st.entry_low, st.entry_high,
                                   datetime.now(timezone.utc), decisions=("offered",))
            if again:   # ide yang sama sudah ditawarkan dan masih berlaku: jangan tawarkan / hitung ulang
                when = datetime.fromisoformat(next(r["created_at"] for r in recent if r["id"] == again))
                decision, why = "duplicate", f"sama dengan setup #{again} yang sudah ditawarkan"
                self.notify(f"ℹ️ Setup {st.side} {st.symbol} sama dengan yang sudah ditawarkan "
                            f"{when.astimezone(WIB):%H:%M} WIB (zona tumpang tindih, masih berlaku): tidak ditawarkan ulang.")
            elif why:
                decision = "rejected"
                self.notify(f"ℹ️ Setup tidak ditawarkan ({why}):\n{text}")
            elif st.confidence == "Low":
                decision, why = "rejected", "confidence Low"
                self.notify(f"ℹ️ Setup confidence Low, tidak ditawarkan; tunggu konfirmasi:\n{text}")
            else:
                why, sid = self.offer_setup(st, atr_h1, text)
                decision = "blocked" if why else "offered"
            self.record_setup(st, decision, why, sid)
        for o in analysis.opportunities:
            if o.symbol in self.meta:
                self.record_opportunity(o)

    def offer_setup(self, st, atr_h1: float, text: str) -> tuple[str | None, int | None]:
        """(alasan tidak ditawarkan | None, id sinyal bila tercatat)."""
        sym, digits = st.symbol, self.meta[st.symbol]["digits"]
        bsym, meta = self.s.broker_symbol(sym), self.meta[sym]
        if self.setups_today() >= self.s.confirm.max_setups_per_day:
            self.notify(f"🚫 Kuota {self.s.confirm.max_setups_per_day} setup/hari sudah terpakai, tidak ditawarkan:\n{text}")
            return f"kuota {self.s.confirm.max_setups_per_day} setup/hari", None
        acc = self.mt5.account()
        tick = self.mt5.tick(bsym)
        block = (self.active_setup_block()
                 or self.entry_block(sym, bsym, st.direction, acc.equity, (tick.ask - tick.bid) / meta["point"],
                                     source="ai_setup", new_risk=min(self.allowed_risk_options()))
                 or self.news_block(bsym, setup=True))
        if block:
            self.notify(f"🚫 Setup tidak ditawarkan ({block}):\n{text}")
            return block, None
        now = datetime.now(timezone.utc)
        bar_time = f"setup {now:%Y-%m-%dT%H:%M:%S} {st.entry_mid:.{digits}f}"   # unik per (symbol, bar_time)
        sid = self.journal.log_signal(sym, st.direction, bar_time, st.entry_mid, st.sl_distance, st.tp_distance,
                                      {"atr": atr_h1}, source="ai_setup", setup=st.model_dump())
        self.journal.update_signal(sid, ref_price=st.entry_mid, ai_decision="analysis", ai_model=self.model_used(),
                                   ai_confidence={"High": 0.8, "Medium": 0.5, "Low": 0.2}[st.confidence],
                                   ai_reasons=[st.reason] if st.reason else [], ai_risks=[st.invalid_if] if st.invalid_if else [])
        price_fmt = f".{digits}f"
        suggested = self.tier_risk(st.score_total)
        lot = self.lot_for(sym, acc.equity, suggested, st.sl_distance)
        if lot == 0:
            why = self.min_lot_reason(sym, acc.equity, st.sl_distance)
            self.journal.update_signal(sid, skip_reason=why)
            self.notify(f"🚫 Setup tidak ditawarkan ({why}):\n{text}")
            return why, sid
        sig = Signal(sym, st.direction, pd.Timestamp(now).tz_convert(None), st.entry_mid, atr_h1 or st.sl_distance,
                     st.sl_distance, st.tp_distance, {"atr": atr_h1})
        tp_txt = f"{st.take_profit_1:{price_fmt}}" + (
            f" (TP2 {st.take_profit_2:{price_fmt}} dikelola manual)" if st.take_profit_2 is not None else "")
        head = f"SETUP {st.side} {sym} | zona {st.entry_low:{price_fmt}}-{st.entry_high:{price_fmt}}"
        ok = self.ask_confirmation(sid, sym, sig, head, st.stop_loss, tp_txt, price_fmt, acc.equity, suggested, lot,
                                   text + "\nKlik: harga belum di zona = pasang order LIMIT di tengah zona; sudah lewat zona = market jika RR masih cukup.",
                                   timeout_minutes=min(self.s.confirm.timeout_minutes, st.valid_minutes),
                                   suggest_label=f"Risiko awal {st.grade}", max_risk=suggested)
        if not ok:
            self.journal.update_signal(sid, skip_reason="pesan konfirmasi Telegram gagal terkirim")
            self.journal.open_shadow(sid, st.entry_mid, st.stop_loss, st.take_profit_1)
            return "pesan konfirmasi Telegram gagal terkirim", sid
        return None, sid

    def tier_risk(self, score: int) -> float:
        """Risiko awal setup per tingkat skor (Strong / Moderate): opsi risk_options terbesar yang tidak
        melebihi risk_strong / risk_moderate; opsi di atasnya disembunyikan dari tombol."""
        c = self.s.confirm
        cap = c.risk_strong if score >= STRONG_SCORE else c.risk_moderate
        return max((r for r in c.risk_options if r <= cap + 1e-12), default=min(c.risk_options))

    def active_setup_block(self) -> str | None:
        n, cap = self.journal.active_setups(), self.s.confirm.max_active_setups
        return f"sudah ada {n} posisi/limit setup aktif (maks {cap})" if n >= cap else None

    # ---------- EV & catatan peluang (tahap deteksi) ----------
    def forward_evidence(self) -> dict:
        """Hasil forward setup 📊 per strategi (trade nyata, sudah bersih dari biaya): dasar EV terkalibrasi."""
        out: dict[str, list[float]] = {}
        for r in self.journal.closed_trades("ai_setup"):
            if r["r_multiple"] is None:
                continue
            strat = (json.loads(r["setup"]) if r["setup"] else {}).get("strategy") or "?"
            out.setdefault(strat, []).append(float(r["r_multiple"]))
        return {k: {"trades": len(v), "win_%": round(100 * sum(x > 0 for x in v) / len(v)),
                    "expectancy_R": round(sum(v) / len(v), 3),
                    "calibrated": len(v) >= EV_MIN_TRADES} for k, v in sorted(out.items())}

    def ev_text(self, strategy: str) -> str:
        ev = self.forward_evidence().get(strategy or "?")
        if ev and ev["calibrated"]:
            return f"EV forward strategi {strategy}: {ev['expectancy_R']:+.2f}R/trade ({ev['trades']} trade, win {ev['win_%']}%)"
        n = ev["trades"] if ev else 0
        return (f"EV: UNCALIBRATED (forward {n}/{EV_MIN_TRADES} trade strategi {strategy or '?'}; skor bukan peluang "
                "menang)")

    def ev_block(self, strategy: str) -> str | None:
        """Strategi yang EV forward-nya terbukti tidak positif (>= EV_MIN_TRADES trade) tidak ditawarkan lagi."""
        ev = self.forward_evidence().get(strategy or "?")
        if ev and ev["calibrated"] and ev["expectancy_R"] <= 0:
            return f"EV forward strategi {strategy or '?'} {ev['expectancy_R']:+.2f}R dari {ev['trades']} trade"
        return None

    def review_rows(self) -> list:
        since = datetime.now(timezone.utc) - timedelta(days=OPPORTUNITY_REVIEW_DAYS)
        return self.journal.opportunities_since(since.isoformat(timespec="seconds"))

    def opportunity_review(self) -> dict:
        """Statistik peluang & setup AI (catatan screener terpisah: screener_stats, tidak dikirim ke AI)."""
        return opportunity_stats([r for r in self.review_rows() if r["kind"] != "screener"])

    def cost_r(self, sym: str, risk_dist: float) -> float:
        return self.atr_and_cost(sym)[1] / risk_dist if risk_dist > 0 else 0.0

    def model_used(self) -> str | None:
        """Model AI yang benar-benar menjawab permintaan terakhir (utama atau cadangan)."""
        return getattr(self.llm, "last_model", None) or (self.s.ai.model if self.llm is not None else None)

    def recent_opportunities(self) -> list:
        """Peluang & setup AI 24 jam terakhir (tanpa catatan screener, yang punya deduplikasi sendiri)."""
        since = datetime.now(timezone.utc) - timedelta(days=1)
        return [r for r in self.journal.opportunities_since(since.isoformat(timespec="seconds"))
                if r["kind"] != "screener"]

    def log_idea(self, duplicate_of: int | None = None, **fields) -> None:
        """Catat satu baris peluang. Ide yang sama dengan baris yang masih berlaku (pair + arah + zona tumpang
        tindih) ditandai duplicate_of dan tidak disimulasikan ulang, supaya satu ide tidak terhitung berkali-kali."""
        if duplicate_of is None:
            duplicate_of = find_duplicate(self.recent_opportunities(), fields["symbol"], fields["direction"],
                                          fields.get("entry_low"), fields.get("entry_high"), datetime.now(timezone.utc))
        if duplicate_of:
            fields.update(duplicate_of=duplicate_of, outcome="duplicate")
        self.journal.log_opportunity(model=self.model_used(), **fields)

    def record_setup(self, st: Setup, decision: str, reason: str | None, sid: int | None) -> None:
        dup = int(reason.split("#")[1].split()[0]) if decision == "duplicate" else None
        self.log_idea(
            duplicate_of=dup, symbol=st.symbol, direction=st.direction, kind="setup", strategy=st.strategy,
            regime=st.regime, score=st.score_total / 10, tier=st.grade, decision=decision, reason=reason or "",
            missing=st.missing, activate_if=st.wait_for, entry_low=st.entry_low, entry_high=st.entry_high,
            stop=st.stop_loss, target=st.take_profit_1, target2=st.take_profit_2, rr=round(st.rr, 2),
            cost_r=round(self.cost_r(st.symbol, st.sl_distance), 3), valid_minutes=st.valid_minutes, signal_id=sid)

    def record_opportunity(self, o) -> None:
        mid = (o.entry_low + o.entry_high) / 2 if o.entry_low is not None else None
        risk = o.direction * (mid - o.invalid_level) if mid is not None and o.invalid_level is not None else 0.0
        self.log_idea(
            symbol=o.symbol, direction=o.direction, kind="opportunity", strategy=o.strategy, regime=o.regime,
            score=o.score, tier=o.tier, decision=o.status.lower(), reason=o.reason, missing=o.missing,
            activate_if=o.activate_if, entry_low=o.entry_low, entry_high=o.entry_high, stop=o.invalid_level,
            target=o.target, rr=round(o.rr, 2) if o.rr else None,
            cost_r=round(self.cost_r(o.symbol, risk), 3) if risk > 0 else None, valid_minutes=o.valid_minutes)

    def resolve_opportunities(self) -> None:
        """Sekali per candle M15 baru: nilai peluang yang masih terbuka dan shadow trade setup AI yang tidak
        dieksekusi (simulasi M15 dengan syarat zona tersentuh, bukan trade nyata)."""
        rows = self.journal.open_opportunities()
        shadows = [r for r in self.journal.open_shadows() if r["source"] == "ai_setup" and r["setup"]]
        if not rows and not shadows:
            return
        bars: dict[str, pd.DataFrame] = {}
        for sym in ({r["symbol"] for r in rows} | {r["symbol"] for r in shadows}) & set(self.meta):
            last = self.mt5.rates(self.s.broker_symbol(sym), "M15", 1)
            if last.empty or self.opp_bar.get(sym) == last["time"].iloc[-1]:
                continue
            self.opp_bar[sym] = last["time"].iloc[-1]
            bars[sym] = self.mt5.rates(self.s.broker_symbol(sym), "M15", 600)
        for r in rows:
            if r["symbol"] not in bars:
                continue
            if None in (r["entry_low"], r["entry_high"], r["stop"], r["target"]):
                self.journal.resolve_opportunity(r["id"], "untestable", None)
                continue
            res = simulate(bars[r["symbol"]], self.server_time(r["created_at"]), r["direction"], r["entry_low"],
                           r["entry_high"], r["stop"], r["target"], r["valid_minutes"] or 240, r["cost_r"] or 0.0,
                           target2=r["target2"])
            if res is not None:
                self.journal.resolve_opportunity(r["id"], res.outcome, res.r, res.tp2_outcome, res.r_tp2)
        for r in shadows:
            if r["symbol"] in bars:
                self.resolve_setup_shadow(r, bars[r["symbol"]])

    def server_time(self, iso_utc: str) -> pd.Timestamp:
        return pd.Timestamp(iso_utc).tz_convert(None) + pd.Timedelta(hours=self.offset)

    def resolve_setup_shadow(self, row, bars: pd.DataFrame) -> None:
        """Shadow setup AI (ditolak / tidak dijawab / diblok saat eksekusi): entry hanya bila zona tersentuh dalam
        valid_minutes, SL/TP berurutan di M15, ambiguous bila tidak bisa diketahui (audit 2026-11-16 H4)."""
        st = json.loads(row["setup"])
        d = row["direction"]
        cost_r = self.cost_r(row["symbol"], d * ((st["entry_low"] + st["entry_high"]) / 2 - st["stop_loss"]))
        res = simulate(bars, self.server_time(row["created_at"]), d, st["entry_low"], st["entry_high"],
                       st["stop_loss"], st["take_profit_1"], st.get("valid_minutes") or 120, cost_r)
        if res is not None:
            status = {"win": "tp", "loss": "sl", "untestable": "expired"}.get(res.outcome, res.outcome)
            self.journal.resolve_shadow(row["id"], status, res.r, method="m15_zone")

    # ---------- ringkasan tiap close candle (tanpa AI) ----------
    def maybe_candle_report(self, now: datetime) -> None:
        """Kirim ringkasan sekali per candle candle_report.timeframe yang baru close, di jam WIB yang diatur.
        Candle yang close lebih dari satu timeframe lalu (bot baru menyala, pasar tutup) dilewati."""
        c = self.s.candle_report
        if not c.enabled or not self.meta:
            return
        last = self.mt5.rates(self.s.broker_symbol(self.s.symbols[0].name), c.timeframe, 1)
        if last.empty:
            return
        bar = last["time"].iloc[-1]
        key, stamp = f"candle_report:{c.timeframe}", str(bar)
        if self.journal.get_state(key) == stamp:
            return
        self.journal.set_state(key, stamp)
        tf = TF_DELTA[c.timeframe]
        close_utc = (bar + tf - pd.Timedelta(hours=self.offset)).tz_localize("UTC")
        close_wib = close_utc.tz_convert(WIB)
        if pd.Timestamp(now) - close_utc > tf or not c.start_hour_wib <= close_wib.hour <= c.end_hour_wib:
            return
        self.notify(self.candle_report_text(now, c.timeframe, bar + tf, close_wib))

    def candle_report_text(self, now: datetime, tf: str, close_srv: pd.Timestamp, close_wib: pd.Timestamp) -> str:
        p = self.s.strategy
        lines = [f"🕐 Candle {tf} close {close_wib:%H:%M} WIB | sesi {session_info(now)['label']}"]
        for sym, meta in self.meta.items():
            digits = meta["digits"]
            frames = self.analysis_frames(self.s.broker_symbol(sym))
            bars = frames.get(tf) if tf in frames else self.mt5.rates(self.s.broker_symbol(sym), tf, 2)
            head = sym
            if bars is not None and len(bars):
                b = bars.iloc[-1]
                arrow = "▲" if b["close"] > b["open"] else "▼" if b["close"] < b["open"] else "■"
                head += (f" {b['close']:.{digits}f} | {tf} {arrow} {100 * (b['close'] / b['open'] - 1):+.2f}% "
                         f"(H {b['high']:.{digits}f} L {b['low']:.{digits}f})")
            lines.append(head)
            snap = intraday_snapshot(frames, digits)
            if snap:
                vwap_side = "di atas" if snap["close_vs_vwap_atr_M15"] > 0 else "di bawah"
                lines.append(f"  Regime {snap['regime']}{' · volatil' if snap['high_volatility'] else ''} | "
                             f"H4 {snap['h4_bias'].split()[0]}, H1 {snap['h1_bias']}, ADX H1 {snap['h1_adx']}")
                lines.append(f"  {vwap_side.capitalize()} VWAP {snap['session_vwap']:.{digits}f} "
                             f"({snap['close_vs_vwap_atr_M15']:+.1f} ATR M15) | zona "
                             f"{snap['dealing_range_H1_50bars']['zone'] or '-'}")
            if "H1" in frames and len(frames["H1"]) > p.ema_trend:
                h1 = tf_summary(frames["H1"], p, digits)
                pb = h1["bb_percent_b"]
                lines.append(f"  RSI H1 {h1['rsi']} | Bollinger H1 %B {pb if pb is not None else '-'}")
        # Status sinyal Ichimoku: candle H4 baru close bersamaan dengan candle ini?
        etf = TF_DELTA[p.entry_tf]
        if close_srv == close_srv.floor(etf):
            found = [sym for sym in self.meta if self.journal.signal_exists(sym, str(close_srv - etf))]
            lines.append(f"Sinyal Ichimoku {p.entry_tf}: " + (f"ada ({', '.join(found)}), lihat pesan sinyal"
                                                             if found else "tidak ada"))
        else:
            lines.append(f"Candle {p.entry_tf} (sinyal Ichimoku) berikut: {self.next_entry_close(now)}")
        positions = self.mt5.positions(self.s.magic)
        limits = len(self.journal.limit_orders())
        hours = self.s.ai.analysis_hours_wib
        auto = (f"Analisis AI otomatis {'jam ini' if close_wib.hour in hours else 'jam ' + ', '.join(map(str, hours))}"
                if hours and self.llm is not None else "setup AI: tekan 📊 Analisis")
        watching = len(self.watchlist())
        lines.append(f"Posisi terbuka: {len(positions)}" + (f" | order limit menunggu: {limits}" if limits else "")
                     + (f" | pantauan AI: {watching}" if watching else "") + f" | {auto}")
        return "\n".join(lines)

    def maybe_scheduled_analysis(self, now: datetime) -> None:
        wib = now.astimezone(WIB)
        if wib.weekday() >= 5 or wib.hour not in self.s.ai.analysis_hours_wib or self.llm is None:
            return
        key = f"analysis_auto:{wib:%Y-%m-%d:%H}"
        if self.journal.get_state(key):
            return
        self.journal.set_state(key, "1")
        self.cmd_analysis()

    # ---------- pantauan: analisis ulang saat skenario "tunggu" dari AI terjadi ----------
    def watchlist(self) -> list[dict]:
        try:
            return json.loads(self.journal.get_state("watchlist") or "[]")
        except ValueError:
            return []

    def store_watch(self, analysis: Analysis) -> list[str]:
        """Ganti pantauan lama dengan item dari analisis terbaru yang masuk akal di harga sekarang.
        Hasil: baris teks per item yang disimpan."""
        if self.s.ai.max_trigger_analyses_per_day <= 0:
            return []
        now = datetime.now(timezone.utc)
        items, lines = [], []
        for w in analysis.watch:
            if w.symbol not in self.meta:
                continue
            digits = self.meta[w.symbol]["digits"]
            t = self.mt5.tick(self.s.broker_symbol(w.symbol))
            why = watch_problem(w, (t.bid + t.ask) / 2)
            if why:
                log.info("Pantauan dilewati (%s): %s", why, format_watch(w, digits))
                continue
            items.append({**w.model_dump(), "id": f"{now:%H%M%S}-{len(items)}",
                          "created": now.isoformat(timespec="seconds"),
                          "expires": (now + timedelta(minutes=w.valid_minutes)).isoformat(timespec="seconds")})
            lines.append(f"- {format_watch(w, digits)} (sampai "
                         f"{(now + timedelta(minutes=w.valid_minutes)).astimezone(WIB):%H:%M} WIB)")
        self.journal.set_state("watchlist", json.dumps(items))
        return lines

    def watch_hours(self, wib: datetime) -> bool:
        hours = self.s.ai.analysis_hours_wib
        return bool(hours) and wib.weekday() < 5 and min(hours) <= wib.hour <= max(hours)

    def maybe_watch_triggers(self, now: datetime) -> None:
        """Cek tiap candle M15 yang baru close: pemicu kena = analisis ulang (sekali per item), pembatal atau
        kedaluwarsa = item dihapus. Ditahan bila entry baru toh sedang diblok atau batas harian habis."""
        items = self.watchlist()
        if not items or self.llm is None:
            return
        wib = now.astimezone(WIB)
        keep, fire = [], None   # fire = (item, Watch, bar, close_utc) pertama yang pemicunya kena; satu analisis per siklus
        for it in items:
            if datetime.fromisoformat(it["expires"]) <= now:
                log.info("Pantauan kedaluwarsa: %s %s %s", it["side"], it["symbol"], it["trigger"])
                continue
            keep.append(it)
        if self.watch_hours(wib):
            closed = {}   # simbol -> (candle M15 terakhir yang sudah close, waktu close UTC)
            for sym in {it["symbol"] for it in keep}:
                bars = self.mt5.rates(self.s.broker_symbol(sym), "M15", 1)
                if bars.empty:
                    continue
                bar = bars.iloc[-1]
                if self.watch_bar.get(sym) == bar["time"]:
                    continue
                self.watch_bar[sym] = bar["time"]
                close_utc = (pd.Timestamp(bar["time"]) + TF_DELTA["M15"] - pd.Timedelta(hours=self.offset)
                             ).tz_localize("UTC")
                closed[sym] = (bar, close_utc)
            still = []
            for it in keep:
                bar, close_utc = closed.get(it["symbol"], (None, None))
                # candle yang close sebelum pantauan dibuat sudah dilihat AI: tunggu candle berikutnya
                if bar is None or close_utc <= pd.Timestamp(it["created"]):
                    still.append(it)
                    continue
                w = Watch.model_validate({k: v for k, v in it.items() if k in Watch.model_fields})
                if w.invalidated(bar["close"]):
                    log.info("Pantauan batal (close %s): %s", bar["close"], format_watch(w, self.meta[w.symbol]["digits"]))
                    continue
                if fire is None and w.triggered(bar["high"], bar["low"], bar["close"]):
                    fire = (it, w, bar, close_utc)
                    continue
                still.append(it)
            keep = still
        if fire is not None:
            it, w, bar, close_utc = fire
            why = self.watch_block(w, wib)
            if why:   # tetap dipantau: dicek lagi di candle M15 berikut
                log.info("Pemicu kena tapi ditahan (%s): %s", why, format_watch(w, self.meta[w.symbol]["digits"]))
                keep.append(it)
            elif self.analysis_cooldown_left() > 0:   # analisis lain baru jalan: candle ini dicek ulang siklus berikut
                keep.append(it)
                self.watch_bar.pop(w.symbol, None)
            else:
                digits = self.meta[w.symbol]["digits"]
                bar_wib = close_utc.tz_convert(WIB)
                trigger = {**it, "fired": {"m15_close_wib": f"{bar_wib:%H:%M}",
                                           **{k: round(float(bar[k]), digits) for k in ("open", "high", "low", "close")}}}
                self.journal.set_state("watchlist", json.dumps(keep))
                self.notify(f"🎯 Pemicu kena: {format_watch(w, digits)}\nCandle M15 close {bar_wib:%H:%M} WIB: "
                            f"{bar['close']:.{digits}f}. Menganalisis ulang...")
                self.cmd_analysis(trigger=trigger)   # mengganti pantauan dengan hasil analisis baru
                self.count_trigger(wib)
                return
        self.journal.set_state("watchlist", json.dumps(keep))

    def count_trigger(self, wib: datetime) -> None:
        key = f"trigger_count:{wib:%Y-%m-%d}"
        self.journal.set_state(key, str(int(self.journal.get_state(key) or 0) + 1))

    def watch_block(self, w, wib: datetime) -> str | None:
        return self.auto_block(w.symbol, w.direction, wib)

    def auto_block(self, sym: str, direction: int, wib: datetime) -> str | None:
        """Alasan analisis otomatis di luar jadwal (pemicu pantauan / screener) tidak dijalankan sekarang."""
        cap = self.s.ai.max_trigger_analyses_per_day
        if int(self.journal.get_state(f"trigger_count:{wib:%Y-%m-%d}") or 0) >= cap:
            return f"batas {cap} analisis pemicu/hari"
        if self.setups_today() >= self.s.confirm.max_setups_per_day:
            return f"kuota {self.s.confirm.max_setups_per_day} setup/hari sudah terpakai"
        bsym, meta = self.s.broker_symbol(sym), self.meta[sym]
        tick = self.mt5.tick(bsym)
        return (self.entry_block(sym, bsym, direction, self.mt5.account().equity, (tick.ask - tick.bid) / meta["point"],
                                 source="ai_setup", new_risk=min(self.allowed_risk_options()))
                or self.news_block(bsym, setup=True))

    # ---------- screener M15: aturan A-E tanpa AI, AI dipanggil hanya bila ada kandidat ----------
    def screen_candidates(self, sym: str) -> tuple[pd.Series, list[dict]] | None:
        """Semua kandidat aturan A-E di candle M15 terakhir yang sudah close: (bar, [kandidat]). None bila data
        kurang. regime_ok = lolos filter regime hybrid (F); hanya itu yang boleh dikirim ke AI. entry / stop /
        target = level aturan riset (entry ~ close, SL di luar sl_ref + buffer ATR dengan jarak minimum ATR H1 dan
        biaya, TP 1,5R) untuk dicatat dan disimulasikan."""
        frames = self.analysis_frames(self.s.broker_symbol(sym))
        if any(tf not in frames or len(frames[tf]) < n for tf, n in SCREEN_MIN_BARS.items()):
            return None
        f = mtf_features({tf: frames[tf] for tf in SCREEN_MIN_BARS})
        cands = all_signals(f)
        last = len(f) - 1
        bar, digits = f.iloc[last], self.meta[sym]["digits"]
        rows = cands[cands["i"] == last]
        cost = self.atr_and_cost(sym)[1] if len(rows) else 0.0
        ip, pol = IntradayParams(), ExitPolicy()
        out = []
        for r in rows.itertuples():
            d, entry = int(r.dir), float(bar["close"])
            dist = d * (entry - (r.sl_ref - d * ip.sl_buffer_atr * float(bar["atr"])))
            dist = max(dist, ip.sl_min_atr_h1 * float(bar["h1_atr"]), cost / ip.max_cost_r)
            ok = math.isfinite(dist) and dist > 0
            out.append({"symbol": sym, "side": "BUY" if d == 1 else "SELL", "strategy": r.strat, "regime": r.regime,
                        "rule_stop_ref": round(float(r.sl_ref), digits),
                        "regime_ok": r.strat in REGIME_STRATEGIES[r.regime],
                        "entry": round(entry, digits) if ok else None,
                        "stop": round(entry - d * dist, digits) if ok else None,
                        "target": round(entry + d * pol.tp_r * dist, digits) if ok else None,
                        "cost_r": round(cost / dist, 3) if ok else None,
                        "sl_too_wide": bool(ok and dist > ip.sl_max_atr_h1 * float(bar["h1_atr"]))})
        return bar, out

    def maybe_screener(self, now: datetime) -> None:
        """Tiap candle M15 yang baru close, 24 jam Senin-Jumat: semua kandidat aturan A-E dicatat ke jurnal
        peluang (kind = screener; tanpa AI, tanpa order) beserta nasibnya. Di jam analysis_hours_wib dengan AI aktif,
        kandidat yang lolos regime dikirim ke AI seperti sebelumnya (konfirmasi pemilik tetap wajib): satu analisis
        per siklus, kandidat pair + arah yang sama tidak dikirim ulang dalam SCREEN_REPEAT_MINUTES."""
        if not self.s.ai.screener:
            return
        wib = now.astimezone(WIB)
        ai_on = self.llm is not None and self.watch_hours(wib)
        for sym in self.meta:
            last = self.mt5.rates(self.s.broker_symbol(sym), "M15", 1)
            if last.empty or self.screen_bar.get(sym) == last["time"].iloc[-1]:
                continue
            res = self.screen_candidates(sym)
            if res is None:
                self.screen_bar[sym] = last["time"].iloc[-1]
                continue
            bar, cands = res
            close_utc = (pd.Timestamp(bar["time"]) + TF_DELTA["M15"] - pd.Timedelta(hours=self.offset)).tz_localize("UTC")
            kept = [c for c in cands if c.get("regime_ok", True)]
            fate = {(c["strategy"], c["side"]): ("regime_filtered", f"strategi {c['strategy']} tidak dipakai di regime "
                                                                    f"{c['regime']}") for c in cands if c not in kept}
            hours = self.s.ai.analysis_hours_wib
            if not ai_on:
                why = (f"di luar jam AI ({min(hours)}:00-{max(hours)}:59 WIB, Senin-Jumat)" if hours and self.llm
                       else "AI nonaktif / analisis otomatis tidak dijadwalkan")
                fate.update({(c["strategy"], c["side"]): ("out_of_hours", why) for c in kept})
                self.log_screener(sym, bar, cands, fate, now)
                self.screen_bar[sym] = bar["time"]
                continue
            seen = float(self.journal.get_state("analysis_last_ts") or 0) >= close_utc.timestamp()
            fresh = [c for c in kept if not seen and now.timestamp() - float(
                self.journal.get_state(f"screen_last:{sym}:{c['side']}") or 0) >= SCREEN_REPEAT_MINUTES * 60]
            fate.update({(c["strategy"], c["side"]): ("repeat", "candle sudah dianalisis AI" if seen else
                                                      f"pair + arah sudah dikirim ke AI < {SCREEN_REPEAT_MINUTES} menit")
                         for c in kept if c not in fresh})
            if not fresh:
                self.log_screener(sym, bar, cands, fate, now)
                self.screen_bar[sym] = bar["time"]
                continue
            c = fresh[0]
            why = self.auto_block(sym, 1 if c["side"] == "BUY" else -1, wib)
            if why:
                log.info("Kandidat screener %s %s (%s) ditahan: %s", c["side"], sym, c["strategy"], why)
                fate.update({(o["strategy"], o["side"]): ("held", why) for o in fresh})
                self.log_screener(sym, bar, cands, fate, now)
                self.screen_bar[sym] = bar["time"]
                continue
            if self.analysis_cooldown_left() > 0:
                fate.update({(o["strategy"], o["side"]): ("cooldown", "analisis AI lain baru jalan (cooldown)")
                             for o in fresh})
                self.log_screener(sym, bar, cands, fate, now)
                return   # analisis lain baru jalan: candle ini dicek ulang siklus berikut
            self.screen_bar[sym] = bar["time"]
            fate.update({(o["strategy"], o["side"]): ("sent_to_ai", "") for o in fresh})
            self.log_screener(sym, bar, cands, fate, now)
            digits = self.meta[sym]["digits"]
            bar_wib = close_utc.tz_convert(WIB)
            others = "".join(f", {o['side']} {o['strategy']}" for o in kept if o is not c)
            self.notify(f"🔎 Screener M15 {bar_wib:%H:%M} WIB: kandidat {c['side']} {sym} strategi {c['strategy']} "
                        f"{STRATEGIES[c['strategy']]} (regime {c['regime']}{others}). Close "
                        f"{bar['close']:.{digits}f}. Minta AI menilai...")
            for o in kept:
                self.journal.set_state(f"screen_last:{sym}:{o['side']}", f"{now.timestamp():.0f}")
            ai_view = lambda o: {k: o[k] for k in SCREEN_AI_KEYS if k in o}   # noqa: E731  isi ke AI tetap sama
            trigger = {"source": "screener", **ai_view(c), "other_candidates": [ai_view(o) for o in kept if o is not c],
                       "fired": {"m15_close_wib": f"{bar_wib:%H:%M}",
                                 **{k: round(float(bar[k]), digits) for k in ("open", "high", "low", "close")}}}
            self.cmd_analysis(trigger=trigger)
            self.count_trigger(wib)
            return

    def log_screener(self, sym: str, bar: pd.Series, cands: list[dict], fate: dict, now: datetime) -> None:
        """Catat kandidat screener sekali per candle (kind = screener, tanpa AI). Candle yang sama dicek ulang
        (setelah cooldown): keputusan baris yang sudah ada diperbarui, tidak dicatat dua kali. Kandidat pair + arah +
        strategi sama dalam SCREEN_REPEAT_MINUTES ditautkan (duplicate_of) dan tidak disimulasikan ulang."""
        logged = self.screen_logged.get(sym)
        if logged and logged[0] == bar["time"]:
            for key, row_id in logged[1].items():
                if key in fate:
                    self.journal.update_opportunity(row_id, decision=fate[key][0], reason=fate[key][1])
            return
        ids = {}
        since = (now - timedelta(minutes=SCREEN_REPEAT_MINUTES)).isoformat(timespec="seconds")
        recent = [r for r in self.journal.opportunities_since(since) if r["kind"] == "screener" and not r["duplicate_of"]]
        for c in cands:
            key = (c["strategy"], c["side"])
            decision, reason = fate.get(key, ("unknown", ""))
            if c.get("sl_too_wide"):
                reason = (reason + "; " if reason else "") + "SL > 2 ATR H1 (aturan riset melewati sinyal ini)"
            d = 1 if c["side"] == "BUY" else -1
            dup = next((r["id"] for r in reversed(recent) if r["symbol"] == sym and r["direction"] == d
                        and r["strategy"] == c["strategy"]), None)
            entry, stop, target = c.get("entry"), c.get("stop"), c.get("target")
            rr = abs(target - entry) / abs(entry - stop) if None not in (entry, stop, target) and entry != stop else None
            ids[key] = self.journal.log_opportunity(
                symbol=sym, direction=d, kind="screener", strategy=c["strategy"], regime=c.get("regime", ""),
                decision=decision, reason=reason, entry_low=entry, entry_high=entry, stop=stop, target=target,
                rr=round(rr, 2) if rr else None, cost_r=c.get("cost_r"), valid_minutes=SCREEN_ENTRY_MINUTES,
                **({"duplicate_of": dup, "outcome": "duplicate"} if dup else {}))
        self.screen_logged[sym] = (bar["time"], ids)

    def position_action(self, cq: dict) -> None:
        """Tombol per posisi: pc (minta konfirmasi), pcy (tutup), pcn (batal), be (SL ke breakeven)."""
        kind, ticket, *rest = str(cq.get("data", "")).split(":") + [""]
        mid = (cq.get("message") or {}).get("message_id")
        pos = next((p for p in self.mt5.positions(self.s.magic) if str(p.ticket) == ticket), None)
        if pos is None:
            self.notifier.answer(cq["id"], "Posisi sudah tidak ada")
            self.notifier.clear_buttons(mid)
            return
        net = pos.profit + getattr(pos, "swap", 0.0)
        if kind == "pc":
            self.notifier.answer(cq["id"])
            self.notifier.send_buttons(
                f"⚠️ Yakin tutup {pos.symbol} #{pos.ticket} sekarang? Profit saat ini {net:+.2f}\n"
                f"Tombol berlaku {CLOSE_CONFIRM_SECONDS} detik.",
                [[("✅ Ya, tutup", f"pcy:{ticket}:{int(time.time())}"), ("↩️ Batal", f"pcn:{ticket}")]])
        elif kind == "pcn":
            self.notifier.answer(cq["id"], "Batal")
            self.notifier.clear_buttons(mid)
        elif kind == "pcy":
            self.notifier.clear_buttons(mid)
            if not rest[0].isdigit() or time.time() - int(rest[0]) > CLOSE_CONFIRM_SECONDS:
                self.notifier.answer(cq["id"], "Konfirmasi kedaluwarsa, ulangi dari 📂 Posisi")
            elif self.mt5.close_position(pos, "bot manual telegram"):
                self.notifier.answer(cq["id"], "Posisi ditutup")
                self.notify(f"🖐️ {pos.symbol} #{pos.ticket} ditutup manual dari Telegram (profit {net:+.2f})")
            else:
                self.notifier.answer(cq["id"], "Gagal menutup, cek log")
        elif kind == "pa":
            self.apply_advice(cq, pos, rest[0], rest[1] if len(rest) > 1 else "")
            self.notifier.clear_buttons(mid)
        elif kind == "ds":
            st = self.manage_state(pos.ticket)
            st["off"] = not st.get("off")
            self.save_manage_state(pos.ticket, st)
            self.notifier.answer(cq["id"], "Dynamic SL dimatikan" if st["off"] else "Dynamic SL dinyalakan")
            self.notify(f"🤖 {pos.symbol} #{pos.ticket}: dynamic SL {'DIMATIKAN, posisi dikelola manual' if st['off'] else 'AKTIF lagi'}")
        elif kind == "be":
            d = 1 if pos.type == 0 else -1
            t = self.mt5.tick(pos.symbol)
            if pos.sl and d * (pos.sl - pos.price_open) >= 0:
                self.notifier.answer(cq["id"], "SL sudah di breakeven atau lebih baik")
            elif d * ((t.bid if d == 1 else t.ask) - pos.price_open) <= 0:
                self.notifier.answer(cq["id"], "Posisi belum untung, SL tidak bisa ke breakeven")
            elif self.mt5.modify_sl(pos, pos.price_open):
                self.notifier.answer(cq["id"], "SL di breakeven")
                self.notify(f"🔒 {pos.symbol} #{pos.ticket}: SL digeser ke breakeven {pos.price_open} (dari Telegram)")
            else:
                self.notifier.answer(cq["id"], "Gagal ubah SL (terlalu dekat harga?), cek log")

    # ---------- peringatan: berita & profit 1R/2R ----------
    def send_alerts(self, now: datetime) -> None:
        positions = self.mt5.positions(self.s.magic)
        if not positions:
            return
        if self.calendar and self.calendar.available:
            hits: dict[tuple, set[str]] = {}
            for pos in positions:
                for e in self.calendar.for_pair(pos.symbol, now, now + timedelta(hours=NEWS_ALERT_HOURS), ["High"]):
                    hits.setdefault((e.time, e.currency, e.title), set()).add(pos.symbol)
            for (t, cur, title), syms in hits.items():
                key = f"news_alert:{t.isoformat()}:{cur}:{title}"
                if self.journal.get_state(key):
                    continue
                self.journal.set_state(key, "1")
                self.notify(f"📰 Berita high impact {cur}: {title} jam {t.astimezone(WIB):%H:%M} WIB "
                            f"({(t - now).total_seconds() / 60:.0f} menit lagi)\nPosisi terbuka: {', '.join(sorted(syms))}")
        trades = {r["ticket"]: r for r in self.journal.open_trades()}
        for pos in positions:
            row = trades.get(pos.ticket)
            if row is None or not row["risk_amount"]:
                continue
            net = pos.profit + getattr(pos, "swap", 0.0)
            r = net / row["risk_amount"]
            hit = 2 if r >= 2 else 1 if r >= 1 else 0
            key = f"r_alert:{pos.ticket}"
            if hit > int(self.journal.get_state(key) or 0):
                self.journal.set_state(key, str(hit))
                self.notify(f"🚀 {pos.symbol} #{pos.ticket} profit tembus {hit}R ({net:+.2f}, {r:.2f}R)")

    def review(self, sig: Signal, frames, meta, entry, spread_pts, acc, open_list) -> ReviewOutcome:
        if not (self.s.ai.enabled and self.llm):
            return ReviewOutcome(decision="rule_only", multiplier=1.0)
        positions = [{"symbol": p.symbol, "side": "BUY" if p.type == 0 else "SELL", "volume": p.volume,
                      "open": p.price_open, "sl": p.sl, "tp": p.tp, "profit": round(p.profit, 2)}
                     for p in self.mt5.positions(self.s.magic)]
        recent = [{"symbol": r["symbol"], "side": "BUY" if r["direction"] == 1 else "SELL",
                   "r": round(r["r_multiple"], 2) if r["r_multiple"] is not None else None,
                   "closed": r["close_time"]} for r in self.journal.recent_closed(10)]
        ctx = build_review_context(
            sig, frames, self.s.strategy, meta, entry, spread_pts,
            (datetime.now(timezone.utc)).hour,
            {"equity": round(acc.equity, 2), "balance": round(acc.balance, 2), "currency": acc.currency},
            positions, currency_exposure(open_list), self.daily.pnl_pct(acc.equity), recent,
            self.calendar.context(sig.symbol + self.s.symbol_suffix, datetime.now(timezone.utc))
            if self.calendar else None, offset_hours=self.offset)
        return review_signal(self.llm, ctx, self.s.ai.on_failure, self.review_prompt)

    @staticmethod
    def format_ai(o: ReviewOutcome) -> str:
        if o.decision == "rule_only":
            return "AI: nonaktif"
        if o.decision == "error":
            return f"AI error: {o.error}"
        lines = [f"AI: {o.decision.upper()} x{o.multiplier:.2f} (yakin {o.confidence or 0:.0%})"]
        lines += [f"+ {r}" for r in o.reasons]
        lines += [f"! {r}" for r in o.risks]
        return "\n".join(lines)

    # ---------- posisi ----------
    def open_list(self) -> list[tuple[str, int]]:
        """Posisi terbuka + order limit setup yang menunggu (dihitung untuk batas posisi & eksposur)."""
        return [(s, d) for s, d, _ in self.open_entries()]

    def find_ticket(self, bsym: str) -> int | None:
        mine = [p for p in self.mt5.positions(self.s.magic) if p.symbol == bsym]
        return max(mine, key=lambda p: p.time).ticket if mine else None

    def check_signal_exits(self, sym: str, bsym: str, f: pd.DataFrame) -> None:
        """exit_rule = signal: tutup posisi bila aturan exit strategi terpenuhi di bar yang baru close."""
        flags = exit_flags(f, self.s.strategy)
        if flags is None:
            return
        exit_long, exit_short = flags
        for pos in self.mt5.positions(self.s.magic):
            if pos.symbol != bsym:
                continue
            hit = exit_long[-1] if pos.type == 0 else exit_short[-1]
            if hit and self.mt5.close_position(pos, "bot exit sinyal"):
                self.notify(f"🔁 Exit sinyal {sym} #{pos.ticket}: aturan exit {self.s.strategy.entry_type} terpenuhi")

    # ---------- 🩺 saran Claude untuk posisi terbuka (dieksekusi hanya setelah pemilik klik) ----------
    def pending_limits(self) -> list[tuple]:
        """[(baris sinyal, dict setup, order MT5)] untuk order limit setup yang masih menunggu di MT5."""
        orders = {o.ticket: o for o in self.mt5.pending_orders(self.s.magic)}
        return [(r, json.loads(r["setup"]), orders[r["limit_ticket"]]) for r in self.journal.limit_orders()
                if r["limit_ticket"] in orders and r["setup"] and r["symbol"] in self.meta]

    def limit_item(self, row, setup: dict, order) -> dict:
        """Ringkasan satu order limit untuk konteks AI."""
        sym, d = row["symbol"], row["direction"]
        meta, digits = self.meta[sym], self.meta[sym]["digits"]
        price = (setup["entry_low"] + setup["entry_high"]) / 2
        sl, tp = setup["stop_loss"], setup["take_profit_1"]
        vol = getattr(order, "volume_current", None) or getattr(order, "volume_initial", None)
        t = self.mt5.tick(self.s.broker_symbol(sym))
        atr_h1, _ = self.atr_and_cost(sym)
        trigger = t.bid if d == -1 else t.ask      # SELL limit terisi oleh bid, BUY limit oleh ask
        return {
            "ticket": row["limit_ticket"], "symbol": sym, "side": "BUY" if d == 1 else "SELL",
            "limit_price": round(price, digits), "stop_loss": sl, "take_profit_1": tp,
            "take_profit_2": setup.get("take_profit_2"),
            "reward_risk": round(abs(tp - price) / abs(price - sl), 2) if price != sl else None,
            "volume": vol, "risk_money": round(self.risk_amount(sym, vol, abs(price - sl)), 2) if vol else None,
            "expires_wib": datetime.fromisoformat(row["limit_expires"]).astimezone(WIB).strftime("%H:%M")
            if row["limit_expires"] else None,
            "setup_reason": setup.get("reason"), "waited_for": setup.get("wait_for"),
            "invalid_if": setup.get("invalid_if"), "strategy": setup.get("strategy"),
            "price_now_trigger_side": round(trigger, digits),
            "distance_to_limit_atr_H1": round(d * (trigger - price) / atr_h1, 2) if atr_h1 else None,
        }

    def cmd_position_review(self) -> None:
        positions = self.mt5.positions(self.s.magic)
        limits = self.pending_limits()
        if not positions and not limits:
            self.notify("📭 Tidak ada posisi terbuka atau order limit untuk dicek")
            return
        if self.llm is None:
            self.notify("🩺 Cek posisi butuh AI aktif (ai.enabled: true dan 9Router tersambung).")
            return
        since = time.time() - float(self.journal.get_state("posreview_last_ts") or 0)
        if since < 60:
            self.notify(f"⏳ Cek posisi terakhir baru {since:.0f} detik lalu, tunggu sebentar.")
            return
        self.journal.set_state("posreview_last_ts", f"{time.time():.0f}")
        what = " dan ".join(x for x in (f"{len(positions)} posisi" if positions else "",
                                        f"{len(limits)} order limit" if limits else "") if x)
        self.notify(f"⏳ Meminta saran untuk {what}... ±1 menit.")
        now = datetime.now(timezone.utc)
        acc = self.mt5.account()
        per_symbol, spreads, prices, items = {}, {}, {}, []
        by_ticket = {}
        for pos in positions:
            sym = next((s for s in self.meta if self.s.broker_symbol(s) == pos.symbol), None)
            if sym is None:
                continue
            meta, d = self.meta[sym], (1 if pos.type == 0 else -1)
            t = self.mt5.tick(pos.symbol)
            if sym not in per_symbol:
                per_symbol[sym] = self.analysis_frames(pos.symbol)
                spreads[sym], prices[sym] = (t.ask - t.bid) / meta["point"], {"bid": t.bid, "ask": t.ask}
            found = self.setup_of(pos.ticket)
            row = found[0] if found else next((r for r in self.journal.open_trades() if r["ticket"] == pos.ticket), None)
            r_dist = abs(row["entry"] - row["sl"]) if row is not None and row["sl"] else 0.0
            px = t.bid if d == 1 else t.ask
            by_ticket[pos.ticket] = (pos, sym, d, px)
            items.append({
                "ticket": pos.ticket, "symbol": sym, "side": "BUY" if d == 1 else "SELL", "volume": pos.volume,
                "open": pos.price_open, "stop_loss": pos.sl or None, "take_profit": pos.tp or None,
                "floating_profit": round(pos.profit + getattr(pos, "swap", 0.0), 2),
                "r_now": round(d * (px - pos.price_open) / r_dist, 2) if r_dist else None,
                "initial_stop": row["sl"] if row is not None else None,
                "setup_targets": {k: found[1].get(k) for k in ("take_profit_1", "take_profit_2", "invalid_if")} if found else None,
                "dynamic_sl": {"auto": bool(found) and not self.manage_state(pos.ticket).get("off"),
                               "stage": self.manage_state(pos.ticket).get("stage"),
                               "partial_done": bool(self.manage_state(pos.ticket).get("partial"))},
            })
        by_limit, limit_items = {}, []
        for row, setup, order in limits:
            sym = row["symbol"]
            if sym not in per_symbol:
                bsym, meta = self.s.broker_symbol(sym), self.meta[sym]
                t = self.mt5.tick(bsym)
                per_symbol[sym] = self.analysis_frames(bsym)
                spreads[sym], prices[sym] = (t.ask - t.bid) / meta["point"], {"bid": t.bid, "ask": t.ask}
            by_limit[row["limit_ticket"]] = (row, setup)
            limit_items.append(self.limit_item(row, setup, order))
        ctx = build_analysis_context(per_symbol, self.s.strategy, self.meta, spreads, now, self.offset,
                                     self.calendar if self.calendar and self.calendar.available else None,
                                     {"equity": round(acc.equity, 2), "currency": acc.currency}, items, [], {}, prices,
                                     pending_orders=limit_items)
        try:
            review = review_positions(self.llm, ctx)
        except Exception as e:
            log.warning("Cek posisi AI gagal: %s", e)
            self.notify(f"⚠️ Cek posisi gagal: {e}")
            return
        if review.text:
            self.notify(f"🩺 Posisi AI\n\n{review.text}")
        for adv in review.advice:
            if adv.ticket not in by_ticket:
                continue
            pos, sym, d, px = by_ticket[adv.ticket]
            f = f".{self.meta[sym]['digits']}f"
            head = (f"{adv.action} | {'BUY' if d == 1 else 'SELL'} {sym} #{adv.ticket}\n{adv.reason}"
                    + (f"\nJika berbalik: {adv.reverse_scenario}" if adv.reverse_scenario else ""))
            button, problem = None, None
            if adv.action == "MOVE_SL":
                problem = self.sl_problem(pos, sym, d, px, adv.new_sl)
                button = (f"✅ Terapkan SL {adv.new_sl:{f}}", f"pa:{adv.ticket}:sl:{adv.new_sl:{f}}") if not problem else None
            elif adv.action == "PARTIAL_CLOSE":
                vol = self.partial_volume(pos, sym, adv.close_frac or 0.0)
                problem = None if vol else "lot terlalu kecil untuk ditutup sebagian"
                button = (f"✅ Tutup {vol:g} lot", f"pa:{adv.ticket}:pc:{vol:g}") if vol else None
            elif adv.action == "CLOSE":
                button = ("❌ Tutup posisi", f"pc:{adv.ticket}")
            if button:
                self.notifier.send_buttons(f"🩺 {head}", [[button]])
            else:
                self.notify(f"🩺 {head}" + (f"\n(tidak bisa diterapkan: {problem})" if problem else ""))
        ts = int(time.time())
        for la in review.limit_advice:
            if la.ticket not in by_limit:
                continue
            row, setup = by_limit[la.ticket]
            sym, d = row["symbol"], row["direction"]
            f = f".{self.meta[sym]['digits']}f"
            old = (setup["entry_low"] + setup["entry_high"]) / 2
            head = f"{la.action} | LIMIT {'BUY' if d == 1 else 'SELL'} {sym} @ {old:{f}} #{la.ticket}\n{la.reason}"
            button, problem = None, None
            if la.action == "CANCEL":
                button = ("❌ Batalkan limit", f"la:{la.ticket}:cx:{ts}")
            elif la.action == "MOVE":
                if None in (la.new_price, la.new_sl, la.new_tp):
                    problem = "harga entry / SL / TP baru tidak lengkap"
                else:
                    problem = self.limit_move_problem(row, setup, la.new_price, la.new_sl, la.new_tp)
                if not problem:
                    head += (f"\nUsulan: entry {old:{f}} → {la.new_price:{f}} | SL {setup['stop_loss']:{f}} → "
                             f"{la.new_sl:{f}} | TP {setup['take_profit_1']:{f}} → {la.new_tp:{f}} "
                             f"| RR {abs(la.new_tp - la.new_price) / abs(la.new_price - la.new_sl):.1f}")
                    button = ("✅ Pindahkan limit",
                              f"la:{la.ticket}:mv:{la.new_price:{f}}:{la.new_sl:{f}}:{la.new_tp:{f}}:{ts}")
            if button:
                self.notifier.send_buttons(f"🩺 {head}\nTombol berlaku {LIMIT_ADVICE_SECONDS // 60} menit.", [[button]])
            else:
                self.notify(f"🩺 {head}" + (f"\n(tidak bisa diterapkan: {problem})" if problem else ""))

    # ---------- saran AI untuk order limit yang menunggu ----------
    def limit_move_problem(self, row, setup: dict, price: float, sl: float, tp: float) -> str | None:
        """Alasan limit tidak boleh dipindah ke (price, sl, tp), atau None. Batas keras sama dengan setup baru
        (sisi SL/TP, RR >= 1.5, jarak SL vs ATR, biaya), ditambah: limit harus tetap entry pullback (SELL di atas
        bid, BUY di bawah ask) dan tidak saat ada berita."""
        sym, d = row["symbol"], row["direction"]
        bsym, meta = self.s.broker_symbol(sym), self.meta[sym]
        t = self.mt5.tick(bsym)
        trigger = t.bid if d == -1 else t.ask
        if d * (trigger - price) <= (meta.get("stops_level", 0) + 2) * meta["point"]:
            return (f"harga limit {price:.{meta['digits']}f} sudah dilewati / terlalu dekat harga sekarang "
                    f"{trigger:.{meta['digits']}f}")
        moved = Setup(**{**setup, "entry_low": price, "entry_high": price, "stop_loss": sl, "take_profit_1": tp,
                         "take_profit_2": None})
        atr_h1, cost = self.atr_and_cost(sym)
        return validate_setup(moved, atr_h1, cost, check_score=False) or self.news_block(bsym, setup=True)

    def limit_action(self, cq: dict) -> None:
        """Tombol saran limit: la:<ticket>:cx:<ts> (batalkan) | la:<ticket>:mv:<harga>:<sl>:<tp>:<ts> (pindahkan)."""
        parts = str(cq.get("data", "")).split(":")
        mid = (cq.get("message") or {}).get("message_id")
        try:
            ticket, kind, ts = int(parts[1]), parts[2], int(parts[-1])
        except (IndexError, ValueError):
            self.notifier.answer(cq["id"], "Tombol tidak valid")
            return
        found = next(((r, s) for r, s, _ in self.pending_limits() if r["limit_ticket"] == ticket), None)
        if found is None:
            self.notifier.answer(cq["id"], "Order limit sudah tidak ada (terisi / dibatalkan)")
            self.notifier.clear_buttons(mid)
            return
        if time.time() - ts > LIMIT_ADVICE_SECONDS:
            self.notifier.answer(cq["id"], "Saran sudah kedaluwarsa, minta 🩺 Posisi AI lagi")
            self.notifier.clear_buttons(mid)
            return
        row, setup = found
        side = f"{'BUY' if row['direction'] == 1 else 'SELL'} {row['symbol']}"
        if kind == "cx":
            if self.mt5.cancel_order(ticket):
                self.journal.update_signal(row["id"], user_decision="limit_cancelled",
                                           skip_reason="limit: dibatalkan atas saran AI, disetujui Anda")
                self.notifier.answer(cq["id"], "Limit dibatalkan")
                self.notify(f"🗑️ Limit {side} #{ticket} dibatalkan (saran AI, disetujui Anda)")
            else:
                self.notifier.answer(cq["id"], "Gagal membatalkan, cek MT5")
        elif kind == "mv" and len(parts) == 7:
            self.notifier.answer(cq["id"], "Memindahkan limit...")
            self.move_limit(row, setup, ticket, *(float(x) for x in parts[3:6]))
        else:
            self.notifier.answer(cq["id"], "Tombol tidak valid")
            return
        self.notifier.clear_buttons(mid)

    def move_limit(self, row, setup: dict, ticket: int, price: float, sl: float, tp: float) -> None:
        """Ganti order limit: cek ulang batas keras, batalkan yang lama, pasang yang baru dengan lot dihitung ulang
        dari risiko pilihan pemilik (risiko uang tidak pernah naik). Kedaluwarsa tetap seperti semula."""
        sym, d = row["symbol"], row["direction"]
        bsym, f = self.s.broker_symbol(sym), f".{self.meta[sym]['digits']}f"
        side = f"{'BUY' if d == 1 else 'SELL'} {sym}"
        problem = self.limit_move_problem(row, setup, price, sl, tp)
        acc = self.mt5.account()
        risk = row["user_risk"] or self.s.risk.risk_per_trade
        sl_dist = d * (price - sl)
        lot = 0.0 if problem else self.lot_for(sym, acc.equity, risk, sl_dist)
        if not problem and lot == 0:
            problem = self.min_lot_reason(sym, acc.equity, sl_dist)
        if problem:
            self.notify(f"⏭️ Limit {side} #{ticket} tidak dipindah: {problem}")
            return
        if not self.mt5.cancel_order(ticket):
            self.notify(f"⚠️ Limit {side} #{ticket} gagal dibatalkan, tidak dipindah. Cek MT5.")
            return
        try:
            res = self.mt5.limit_order(bsym, d, lot, price, sl, tp, self.s.magic, "bot user limit",
                                       expires=datetime.fromisoformat(row["limit_expires"]), offset_hours=self.offset)
        except MT5Error as e:
            self.journal.update_signal(row["id"], user_decision="limit_cancelled", skip_reason=f"limit: pindah gagal ({e})")
            self.notify(f"⚠️ Limit lama {side} #{ticket} sudah dibatalkan, tetapi limit baru gagal: {e}")
            return
        tp2 = setup.get("take_profit_2")
        new_setup = {**setup, "entry_low": price, "entry_high": price, "stop_loss": sl, "take_profit_1": tp,
                     "take_profit_2": tp2 if tp2 is not None and d * (tp2 - tp) > 0 else None}
        self.journal.update_signal(row["id"], limit_ticket=res.order, ref_price=price, sl_distance=sl_dist,
                                   tp_distance=d * (tp - price), setup=json.dumps(new_setup, ensure_ascii=False))
        self.notify(f"🔁 LIMIT {side} dipindah (saran AI, disetujui Anda)\n"
                    f"Entry {price:{f}} | SL {sl:{f}} | TP {tp:{f}} | RR {abs(tp - price) / sl_dist:.1f}\n"
                    f"Lot {lot} | Risiko {self.risk_amount(sym, lot, sl_dist):.2f} {acc.currency} | "
                    + LIMIT_EXPIRY_TEXT[self.mt5.expiry_mode(bsym)].format(
                        t=f"{datetime.fromisoformat(row['limit_expires']).astimezone(WIB):%H:%M}"))

    def sl_problem(self, pos, sym: str, d: int, px: float, new_sl: float | None) -> str | None:
        """Alasan SL baru tidak boleh dipasang: SL hanya boleh mendekat ke harga dan tetap di sisi yang benar."""
        meta = self.meta[sym]
        if new_sl is None:
            return "level SL tidak diberikan"
        if pos.sl and d * (new_sl - pos.sl) <= 0:
            return "SL baru menjauhi harga (memperlebar risiko)"
        if d * (px - new_sl) <= (meta.get("stops_level", 0) + 2) * meta["point"]:
            return "SL baru melewati / terlalu dekat harga sekarang"
        return None

    def partial_volume(self, pos, sym: str, frac: float) -> float:
        """Lot yang ditutup untuk frac (0.25-0.75) dari posisi; 0 jika tidak bisa dibagi."""
        meta = self.meta[sym]
        if not 0.25 <= frac <= 0.75:
            return 0.0
        step = meta["volume_step"]
        vol = round(math.floor(pos.volume * frac / step + 1e-9) * step, 8)
        return vol if vol >= meta["volume_min"] and pos.volume - vol >= meta["volume_min"] - 1e-9 else 0.0

    def apply_advice(self, cq: dict, pos, what: str, value: str) -> None:
        sym = next((s for s in self.meta if self.s.broker_symbol(s) == pos.symbol), None)
        d = 1 if pos.type == 0 else -1
        try:
            num = float(value)
        except ValueError:
            num = 0.0
        if sym is None or what not in ("sl", "pc") or num <= 0:
            self.notifier.answer(cq["id"], "Saran tidak valid")
            return
        t = self.mt5.tick(pos.symbol)
        if what == "sl":
            problem = self.sl_problem(pos, sym, d, t.bid if d == 1 else t.ask, num)   # harga bisa sudah berubah
            if problem:
                self.notifier.answer(cq["id"], f"Tidak diterapkan: {problem}")
            elif self.mt5.modify_sl(pos, num):
                self.notifier.answer(cq["id"], "SL digeser")
                self.notify(f"🔒 {sym} #{pos.ticket}: SL → {num:.{self.meta[sym]['digits']}f} (saran AI, disetujui Anda)")
            else:
                self.notifier.answer(cq["id"], "Gagal ubah SL, cek log")
        elif num < pos.volume and self.mt5.close_partial(pos, num, "bot partial AI"):
            self.notifier.answer(cq["id"], "Sebagian posisi ditutup")
            self.notify(f"🎯 {sym} #{pos.ticket}: {num:g} lot ditutup (saran AI, disetujui Anda)")
        else:
            self.notifier.answer(cq["id"], "Gagal menutup sebagian, cek log")

    # ---------- 📋 evaluasi performa setup ----------
    def what_if(self, row) -> dict | None:
        """Simulasi kasar di bar M5: R jika TP1 dibiarkan penuh, dan R dengan dynamic SL + parsial. None = data kurang
        atau simulasi belum selesai. Hasil disimpan supaya tidak dihitung ulang."""
        cached = self.journal.get_state(f"wi:{row['ticket']}")
        if cached:
            return json.loads(cached)
        if not row["setup"] or not row["sl"] or row["symbol"] not in self.meta:
            return None
        sym, d, setup = row["symbol"], row["direction"], json.loads(row["setup"])
        meta, cfg = self.meta[sym], self.s.manage_for(sym)
        df = self.mt5.rates(self.s.broker_symbol(sym), "M5", 3000)
        start = pd.Timestamp(row["open_time"]).tz_convert(None) + pd.Timedelta(hours=self.offset)
        df = df[df["time"] > start].reset_index(drop=True) if len(df) else df
        entry, sl, r_dist = row["entry"], row["sl"], abs(row["entry"] - row["sl"])
        if len(df) < 3 or r_dist <= 0:
            return None
        o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        sp = df["spread"].to_numpy(float) * meta["point"]
        tp1, tp2 = setup["take_profit_1"], setup.get("take_profit_2")
        a_atr = float(atr_series(df["high"], df["low"], df["close"], 14).iloc[-1]) * 1.7   # ~ATR M15 dari M5
        full = simulate_exit(o, h, l, sp, 0, d, entry, sl, tp1, close=c)
        dyn = simulate_managed(o, h, l, sp, c, 0, d, entry, sl,
                               cfg.partial_at_r * r_dist if cfg.partial_at_r else abs(tp1 - entry), cfg.partial_frac,
                               tp2, self.s.backtest.commission_per_lot * meta["tick_size"] / meta["tick_value"],
                               cfg.trail_atr_mult * a_atr if cfg.trail == "atr" else 0.0,
                               9 if cfg.trail == "swing" else 0, be_trigger=cfg.be_at_r * r_dist)
        if full is None or dyn is None:
            return None
        out = {"tp1_R": round(d * (full[1] - entry) / r_dist, 2), "dynamic_R": round(d * (dyn[1] - entry) / r_dist, 2),
               "actual_R": round(row["r_multiple"] or 0.0, 2)}
        self.journal.set_state(f"wi:{row['ticket']}", json.dumps(out))
        return out

    def evaluation_text(self) -> str:
        rows = self.journal.closed_trades("ai_setup")
        sims = [w for w in (self.what_if(r) for r in rows[-40:]) if w]
        what_if = {"n": len(sims), **{k: sum(w[k] for w in sims) for k in ("actual_R", "tp1_R", "dynamic_R")}} if sims else None
        text = format_evaluation("Evaluasi setup 📊 Analisis", intraday_stats(rows), what_if)
        rule = self.journal.closed_trades("rule")
        if rule:
            st = intraday_stats(rule)
            text += (f"\n\nSinyal strategi (Ichimoku): {st['trades']} trade, win rate {st['winrate_%']}%, "
                     f"net {st['net']:+.2f}")
        shadow = self.journal.shadow_mode_signals()
        for sym in sorted({r["symbol"] for r in shadow}):
            rows = [r for r in shadow if r["symbol"] == sym]
            done = [r["shadow_r"] for r in rows if r["shadow_r"] is not None]
            text += (f"\n\n🧪 {self.s.strategy.entry_type} {sym} mode shadow: {len(rows)} sinyal, {len(done)} selesai"
                     + (f", total {sum(done):+.2f}R, win {sum(x > 0 for x in done)}/{len(done)}" if done else ""))
        ev = self.forward_evidence()
        if ev:
            text += "\n\nEV forward per strategi: " + "; ".join(
                f"{k} {v['trades']} trade {v['expectancy_R']:+.2f}R" + ("" if v["calibrated"] else " (UNCALIBRATED)")
                for k, v in ev.items())
        return (text + "\n\n" + format_opportunity_stats(self.opportunity_review(), OPPORTUNITY_REVIEW_DAYS)
                + "\n\n" + format_screener_stats(screener_stats(self.review_rows()), OPPORTUNITY_REVIEW_DAYS))

    def cmd_evaluation(self) -> None:
        self.notify(self.evaluation_text())

    # ---------- dynamic SL & partial TP untuk posisi setup 📊 (source = ai_setup) ----------
    def manage_state(self, ticket: int) -> dict:
        return json.loads(self.journal.get_state(f"mg:{ticket}") or "{}")

    def save_manage_state(self, ticket: int, st: dict) -> None:
        self.journal.set_state(f"mg:{ticket}", json.dumps(st))

    def setup_of(self, ticket: int) -> tuple | None:
        """(baris trade, dict setup) jika posisi ini berasal dari setup 📊 Analisis, selain itu None."""
        row = next((r for r in self.journal.open_trades() if r["ticket"] == ticket), None)
        sig = self.journal.get_signal(row["signal_id"]) if row is not None and row["signal_id"] else None
        if sig is None or sig["source"] != "ai_setup" or not sig["setup"]:
            return None
        return row, json.loads(sig["setup"])

    def manage_setup_positions(self, now: datetime) -> None:
        """Tahap dynamic SL: breakeven + biaya -> kunci profit -> partial di TP1 -> trailing sisa posisi.
        Satu aksi per posisi per siklus (data posisi dibaca ulang dari MT5 di siklus berikutnya)."""
        for pos in self.mt5.positions(self.s.magic):
            found = self.setup_of(pos.ticket)
            if found:
                self.manage_setup_position(pos, found[0], found[1], now)

    def trail_frame(self, bsym: str, cfg) -> pd.DataFrame:
        return self.mt5.rates(bsym, cfg.trail_tf, 200)

    def manage_setup_position(self, pos, row, setup: dict, now: datetime) -> None:
        sym = row["symbol"]
        cfg, st = self.s.manage_for(sym), self.manage_state(pos.ticket)
        r_dist = abs(row["entry"] - row["sl"]) if row["sl"] else 0.0   # R = jarak SL awal (tidak pernah berubah)
        if not cfg.enabled or st.get("off") or r_dist <= 0:
            return
        meta, d = self.meta[sym], (1 if pos.type == 0 else -1)
        f = f".{meta['digits']}f"
        t = self.mt5.tick(pos.symbol)
        px = t.bid if d == 1 else t.ask            # harga keluar posisi ini
        entry, tp1, tp2 = pos.price_open, setup["take_profit_1"], setup.get("take_profit_2")
        r_now = d * (px - entry) / r_dist
        # Biaya dalam satuan harga: komisi round-turn per lot + setengah spread sebagai cadangan slippage
        cost = self.s.backtest.commission_per_lot * meta["tick_size"] / meta["tick_value"] + (t.ask - t.bid) / 2
        splittable = cfg.partial_frac > 0 and pos.volume >= 2 * meta["volume_min"] - 1e-9

        # TP server pindah ke TP2: bagian pertama ditutup bot di TP1, sisanya dibiarkan jalan
        if splittable and not st.get("partial") and not st.get("tp_moved"):
            new_tp = tp2 if tp2 and d * (tp2 - tp1) > 0 else 0.0
            if self.mt5.modify_sltp(pos, pos.sl, new_tp):
                st["tp_moved"] = True
                self.save_manage_state(pos.ticket, st)
            return
        part_px = entry + d * cfg.partial_at_r * r_dist if cfg.partial_at_r else tp1
        part_px = part_px if d * (tp1 - part_px) > 0 else tp1          # tidak pernah melewati TP1
        if cfg.partial_at_usd and pos.volume > 0:
            # Target uang: jarak harga yang membuat profit floating posisi ini = partial_at_usd; pakai yang lebih dekat
            usd_px = entry + d * cfg.partial_at_usd * meta["tick_size"] / (meta["tick_value"] * pos.volume)
            part_px = usd_px if d * (part_px - usd_px) > 0 else part_px
        if st.get("tp_moved") and not st.get("partial") and d * (px - part_px) >= 0:
            step = meta["volume_step"]
            vol = round(max(math.floor(pos.volume * cfg.partial_frac / step + 1e-9) * step, meta["volume_min"]), 8)
            if self.mt5.close_partial(pos, vol, "bot partial TP1"):
                brk = market_structure(self.trail_frame(pos.symbol, cfg), meta["digits"]).get("last_break") or {}
                st.update(partial=True, brk=brk.get("time"))
                st.setdefault("events", []).append([now.isoformat(timespec="seconds"), "partial", round(part_px, meta["digits"])])
                self.save_manage_state(pos.ticket, st)
                self.notify(f"🎯 {sym} #{pos.ticket}: target parsial {part_px:{f}} ({r_now:+.1f}R) tercapai, {vol:g} lot ditutup. Sisa "
                            f"{pos.volume - vol:g} lot dibiarkan jalan dengan SL terkunci"
                            + (f", target TP2 {tp2:{f}}" if tp2 else ""))
            return

        cands = []   # (SL baru, label); dipilih yang paling menguntungkan posisi
        if st.get("partial") or (cfg.be_at_r and r_now >= cfg.be_at_r):
            cands.append((entry + d * cost, "breakeven + biaya"))
        if cfg.lock_at_r and r_now >= cfg.lock_at_r:
            cands.append((entry + d * cfg.lock_r * r_dist, f"kunci {cfg.lock_r:g}R"))
        if st.get("partial") and cfg.trail != "none":
            df = self.trail_frame(pos.symbol, cfg)
            if len(df) >= 30:
                a = float(atr_series(df["high"], df["low"], df["close"], 14).iloc[-1])
                ms = market_structure(df, meta["digits"])
                brk = ms.get("last_break") or {}
                against = brk.get("direction") == ("down" if d == 1 else "up")
                if cfg.close_on_choch and brk.get("type") == "CHoCH" and against and brk.get("time") != st.get("brk"):
                    if self.mt5.close_position(pos, "bot exit CHoCH"):
                        self.notify(f"🔁 {sym} #{pos.ticket}: struktur {cfg.trail_tf} berbalik (CHoCH {brk.get('level')}), "
                                    f"sisa posisi ditutup di {r_now:+.1f}R")
                    return
                if cfg.trail == "atr":
                    cands.append((px - d * cfg.trail_atr_mult * a, f"trailing {cfg.trail_atr_mult:g} ATR {cfg.trail_tf}"))
                else:
                    pivot = ms.get("last_pivot_low" if d == 1 else "last_pivot_high")
                    if pivot is not None:
                        cands.append((pivot - d * 0.1 * a, f"trailing swing {cfg.trail_tf}"))
        if not cands:
            return
        new_sl, label = max(cands, key=lambda c: d * c[0])
        gap = (meta.get("stops_level", 0) + 2) * meta["point"]
        # Invarian: SL hanya boleh mendekat ke harga (minimal 0.1R lebih baik, hindari modify tiap siklus)
        better = not pos.sl or d * (new_sl - pos.sl) >= 0.1 * r_dist
        if better and d * (px - new_sl) > gap and self.mt5.modify_sl(pos, new_sl):
            st.setdefault("events", []).append([now.isoformat(timespec="seconds"), label, round(new_sl, meta["digits"])])
            st["stage"] = label
            self.save_manage_state(pos.ticket, st)
            self.notify(f"🔒 {sym} #{pos.ticket}: SL → {new_sl:{f}} ({label}), profit sekarang {r_now:+.1f}R")

    def manage_breakeven(self) -> None:
        be_r = self.s.strategy.breakeven_at_r
        if not be_r:
            return
        for pos in self.mt5.positions(self.s.magic):
            if not pos.sl:
                continue
            t = self.mt5.tick(pos.symbol)
            if pos.type == 0:
                risk = pos.price_open - pos.sl
                hit = risk > 0 and t.bid - pos.price_open >= be_r * risk
            else:
                risk = pos.sl - pos.price_open
                hit = risk > 0 and pos.price_open - t.ask >= be_r * risk
            if hit and self.mt5.modify_sl(pos, pos.price_open):
                self.notify(f"🔒 {pos.symbol} #{pos.ticket}: SL digeser ke breakeven {pos.price_open}")

    def trend_intact(self, pos) -> bool:
        """Tren masih searah posisi: aturan exit strategi belum terpenuhi di bar terakhir (butuh exit_rule: signal)."""
        p = self.s.strategy
        frames = self.frames(pos.symbol)
        f = compute_features(frames[p.entry_tf], frames[p.trend_tf], p, self.offset)
        flags = None if f.empty else exit_flags(f, p)
        if flags is None:
            return False
        return not (flags[0][-1] if pos.type == 0 else flags[1][-1])

    def trail_profit(self, pos) -> None:
        """Kunci trail_lock_fraction dari gerak profit saat ini lewat SL di server (hanya mengetatkan)."""
        t = self.mt5.tick(pos.symbol)
        d = 1 if pos.type == 0 else -1
        move = (t.bid - pos.price_open) if d == 1 else (pos.price_open - t.ask)
        new_sl = pos.price_open + d * move * self.s.risk.trail_lock_fraction
        # Geser hanya jika naik >= 10% gerak profit: hindari modify order tiap 20 detik
        if pos.sl and d * (new_sl - pos.sl) <= 0.1 * move:
            return
        first_lock = not pos.sl or d * (pos.sl - pos.price_open) <= 0
        if self.mt5.modify_sl(pos, new_sl) and first_lock:
            self.notify(f"📈 {pos.symbol} #{pos.ticket}: profit lewat ${self.s.risk.take_profit_usd:g} dan tren masih "
                        f"searah, posisi dibiarkan jalan. SL dikunci di {new_sl:.6g} "
                        f"({self.s.risk.trail_lock_fraction:.0%} dari gerak profit, ikut naik)")

    def manage_take_profit_and_hold(self, now: datetime) -> None:
        """Profit >= take_profit_usd: tutup, kecuali tren masih searah (trail_lock_fraction > 0) maka SL dikunci
        dan profit dibiarkan jalan. Juga tutup paksa setelah max_hold_days. Notif CLOSE dari sync_closed_trades."""
        r = self.s.risk
        if not (r.take_profit_usd or r.max_hold_days):
            return
        for pos in self.mt5.positions(self.s.magic):
            net = pos.profit + pos.swap
            opened = datetime.fromtimestamp(pos.time - self.offset * 3600, timezone.utc)  # pos.time = jam server
            age_days = (now - opened).total_seconds() / 86400
            if r.max_hold_days and age_days >= r.max_hold_days:
                why = f"batas {r.max_hold_days:g} hari"
            elif r.take_profit_usd and net >= r.take_profit_usd:
                if r.trail_lock_fraction and self.trend_intact(pos):
                    self.trail_profit(pos)
                    continue
                why = f"TP ${r.take_profit_usd:g}"
            else:
                continue
            if self.mt5.close_position(pos, f"bot {why}"):
                self.notify(f"🎯 Tutup {pos.symbol} #{pos.ticket}: {why} (profit {net:+.2f})")

    def sync_closed_trades(self) -> None:
        open_tickets = {p.ticket for p in self.mt5.positions(self.s.magic)}
        for row in self.journal.open_trades():
            if row["ticket"] in open_tickets:
                continue
            res = self.mt5.closed_position_result(row["ticket"])
            if res is None:
                continue
            tr = self.journal.close_trade(row["ticket"], res["close_price"], res["close_time"].isoformat(),
                                          res["profit"])
            icon = "💰" if tr["profit"] > 0 else "🔻"
            r_txt = f"{tr['r_multiple']:+.2f}R" if tr["r_multiple"] is not None else ""
            self.notify(f"{icon} CLOSE {tr['symbol']} #{tr['ticket']} @ {tr['close_price']}\n"
                        f"Profit {tr['profit']:+.2f} ({r_txt})")
            n_setup = len(self.journal.closed_trades("ai_setup"))
            if n_setup and n_setup % 20 == 0 and self.journal.get_state("eval_at") != str(n_setup):
                self.journal.set_state("eval_at", str(n_setup))   # evaluasi otomatis tiap 20 setup tertutup
                self.cmd_evaluation()
            if self.loss_pause() and self.journal.get_state("loss_prompt") != tr["close_time"]:
                self.journal.set_state("loss_prompt", tr["close_time"])
                self.notifier.send_buttons(
                    f"⛔ {self.s.risk.stop_after_losses} rugi beruntun. Entry baru dan setup dihentikan dulu.\n"
                    "Evaluasi: apakah entry terlalu cepat, SL terlalu dekat, atau melawan tren? "
                    "Tekan tombol di bawah kalau sudah siap lanjut.",
                    [[("▶️ Lanjut (sudah evaluasi)", "go:1")]])

    # ---------- shadow trade ----------
    def resolve_shadows(self) -> None:
        p = self.s.strategy
        features: dict[str, pd.DataFrame] = {}
        for row in self.journal.open_shadows():
            sym = row["symbol"]
            meta = self.meta.get(sym)
            if meta is None or row["source"] == "ai_setup":   # setup AI: resolve_setup_shadow (M15 + zona)
                continue
            if sym not in features:
                frames = self.frames(self.s.broker_symbol(sym))
                features[sym] = compute_features(frames[p.entry_tf], frames[p.trend_tf], p, self.offset)
            f = features[sym]
            bar_time = pd.Timestamp(row["bar_time"])
            if f.empty:
                continue
            if f["time"].iloc[0] > bar_time:
                self.journal.resolve_shadow(row["id"], "expired", None)
                continue
            mask = (f["time"] > bar_time).to_numpy()
            after = f[mask].reset_index(drop=True)
            if after.empty:
                continue
            positive = f["spread"][f["spread"] > 0]
            fill = positive.median() if len(positive) else self.s.backtest.default_spread_points
            spread_pts = after["spread"].where(after["spread"] > 0, fill)
            flags = exit_flags(f, p)
            exit_flag = None if flags is None else flags[0 if row["direction"] == 1 else 1][mask]
            res = simulate_exit(after["open"].to_numpy(float), after["high"].to_numpy(float),
                                after["low"].to_numpy(float), (spread_pts * meta["point"]).to_numpy(float),
                                0, row["direction"], row["shadow_entry"], row["shadow_sl"], row["shadow_tp"],
                                p.breakeven_at_r * row["sl_distance"], close=after["close"].to_numpy(float),
                                exit_flag=exit_flag)
            if res is None:
                continue
            _, price, reason = res
            r = row["direction"] * (price - row["shadow_entry"]) / row["sl_distance"]
            self.journal.resolve_shadow(row["id"], reason, round(r, 3))

    # ---------- laporan harian ----------
    def maybe_weekly_review(self, now: datetime) -> None:
        a = self.s.ai
        if now.weekday() != a.weekly_review_weekday or now.hour < a.weekly_review_hour_utc:
            return
        key = now.date().isoformat()
        if self.journal.get_state("last_weekly") == key:   # tersimpan: tidak dikirim ulang saat restart
            return
        self.journal.set_state("last_weekly", key)
        self.notify(f"📅 Laporan mingguan s/d {key}\n\n{self.weekly_text(now)}")

    def weekly_text(self, now: datetime) -> str:
        equity = self.mt5.account().equity
        dd_info = {"peak": round(self.dd.peak, 2), "drawdown_%": round(100 * self.dd.drawdown(equity), 2),
                   "circuit_breaker_active": self.dd.halted,
                   "losing_streak_now": current_losing_streak(self.journal)}
        st = weekly_stats(self.journal, (now - timedelta(days=7)).isoformat(timespec="seconds"), dd_info)
        if self.llm:
            try:
                return weekly_report(self.llm, st)
            except Exception as e:
                log.warning("Laporan mingguan AI gagal: %s", e)
        return format_weekly_fallback(st)

    def maybe_daily_review(self, now: datetime) -> None:
        if now.hour < self.s.ai.daily_review_hour_utc or self.last_review_date == now.date():
            return
        self.last_review_date = now.date()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        acc = self.mt5.account()
        account = {"equity": round(acc.equity, 2), "balance": round(acc.balance, 2), "currency": acc.currency,
                   "today_pnl_%": round(100 * self.daily.pnl_pct(acc.equity), 2)}
        signals = [{k: r[k] for k in ("symbol", "direction", "bar_time", "ai_decision", "ai_multiplier",
                                      "ai_reasons", "skip_reason", "executed")}
                   for r in self.journal.signals_since(start)]
        trades = [dict(r) for r in self.journal.trades_since(start)]
        results = intraday_stats([r for r in self.journal.closed_trades() if (r["close_time"] or "") >= start])

        text = None
        if self.llm:
            try:
                markets = {sym: market_summary(self.frames(self.s.broker_symbol(sym)), self.s.strategy,
                                               self.meta[sym]["digits"]) for sym in self.meta}
                currencies = {c for sym in self.meta for c in (sym[:3], sym[3:6])}
                calendar = (self.calendar.upcoming_all(now, 48, currencies)
                            if self.calendar and self.calendar.available else None)
                text = daily_report(self.llm, now.date().isoformat(), account, signals, trades, markets,
                                    self.journal.ai_vs_rules(), calendar, results)
            except Exception as e:
                log.warning("Laporan harian AI gagal: %s", e)
        if text is None:
            text = (f"Equity {account['equity']} {account['currency']} ({account['today_pnl_%']:+.2f}% hari ini)\n"
                    f"Sinyal: {len(signals)} | Trade ditutup: {results['trades']}")
            if results["trades"]:
                pf = results["profit_factor"] if results["profit_factor"] is not None else "-"
                text += (f" | Win rate {results['winrate_%']}%\n"
                         f"Gross profit {results['gross_profit']:+.2f} | gross loss {results['gross_loss']:+.2f} | "
                         f"net {results['net']:+.2f}\nProfit factor {pf} | expectancy {results['expectancy']:+.2f}")
        self.notify(f"📊 Laporan harian {now.date().isoformat()}\n\n{text}")
