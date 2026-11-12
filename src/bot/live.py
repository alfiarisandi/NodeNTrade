"""Loop utama bot live (akun demo): sinyal -> cek risiko -> review AI -> order -> jurnal & notifikasi."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.ai.client import LLM
from bot.ai.context import build_review_context, market_summary
from bot.ai.daily_review import daily_report, weekly_report
from bot.ai.prompts import review_system
from bot.ai.reviewer import ReviewOutcome, review_signal
from bot.config import Settings
from bot.journal import Journal
from bot.mt5_client import MT5Client, MT5Error
from bot.news import Calendar
from bot.notifier import Notifier
from bot.performance import current_losing_streak, format_weekly_fallback, trade_stats, weekly_stats
from bot.risk import DailyGuard, DrawdownGuard, check_new_trade, currency_exposure, lot_with_min
from bot.strategy import (DAY_LEVEL_TYPES, TF_DELTA, Signal, compute_features, exit_flags, in_session, signal_at,
                          simulate_exit)

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
            ("menu", "Tampilkan tombol menu"),
            ("help", "Daftar perintah")]
# Tombol menu permanen di bawah chat: label -> perintah (bot menerima label sebagai teks biasa)
MENU = {"📟 Status": "status", "📂 Posisi": "posisi", "📈 P/L": "pnl", "🔔 Sinyal": "sinyal",
        "⏸️ Pause": "pause", "▶️ Resume": "resume", "❓ Bantuan": "help"}
NEWS_ALERT_HOURS = 1.0
CLOSE_CONFIRM_SECONDS = 120   # tombol "Ya, tutup" kedaluwarsa: cegah klik tak sengaja di pesan lama


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

    def step(self) -> None:
        now = datetime.now(timezone.utc)
        acc = self.mt5.account()
        if self.daily.update(now, acc.equity):
            log.info("Hari baru %s, equity awal %.2f", self.daily.day, acc.equity)
        self.update_drawdown(acc.equity)
        if self.last_equity_hour != (now.date(), now.hour):
            self.last_equity_hour = (now.date(), now.hour)
            self.journal.log_equity(acc.balance, acc.equity)

        if self.calendar:
            self.calendar.refresh(now)
        self.manage_breakeven()
        self.manage_take_profit_and_hold(now)
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
        self.maybe_daily_review(now)
        self.maybe_weekly_review(now)

    # ---------- sinyal ----------
    def frames(self, bsym: str) -> dict[str, pd.DataFrame]:
        p = self.s.strategy
        counts = {"H1": 600, "H4": 600, "D1": 400, p.entry_tf: 600, p.trend_tf: 600}
        return {tf: self.mt5.rates(bsym, tf, n) for tf, n in counts.items()}

    def process_symbol(self, sym: str) -> None:
        p, bsym, meta = self.s.strategy, self.s.broker_symbol(sym), self.meta[sym]
        frames = self.frames(bsym)
        f = compute_features(frames[p.entry_tf], frames[p.trend_tf], p)
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

        block = self.entry_block(sym, bsym, sig.direction, acc.equity, spread_pts)
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
                                   ai_risks=outcome.risks, ai_error=outcome.error)

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
    def entry_block(self, sym: str, bsym: str, direction: int, equity: float, spread_pts: float) -> str | None:
        if self.error_halt:
            return "kill switch aktif (error berulang)"
        if self.journal.get_state("paused") == "1":
            return "entry dijeda (tekan ▶️ Resume atau kirim /resume untuk lanjut)"
        if self.daily.halted(equity):
            return f"batas rugi harian {self.s.risk.max_daily_loss:.0%} tercapai"
        if self.dd.halted:
            return (f"circuit breaker aktif: equity turun {self.dd.drawdown(equity):.1%} dari puncak "
                    f"{self.dd.peak:.2f} (batas {self.s.risk.max_drawdown_from_peak:.0%}), reset manual di Telegram")
        if spread_pts > self.s.symbol_cfg(sym).max_spread_points:
            return f"spread {spread_pts:.0f} point terlalu lebar"
        return check_new_trade(bsym, direction, self.open_list(),
                               self.s.risk.max_open_positions, self.s.risk.max_currency_exposure)

    def news_block(self, bsym: str) -> str | None:
        news = self.calendar.blocking_event(bsym, datetime.now(timezone.utc)) if self.calendar else None
        if news is None:
            return None
        hours = (news.time - datetime.now(timezone.utc)).total_seconds() / 3600
        when = f"{hours:.1f} jam lagi" if hours >= 0 else f"{-hours:.1f} jam lalu"
        return f"berita {news.impact} {news.currency}: {news.title} ({when})"

    def lot_for(self, sym: str, equity: float, risk: float, sl_distance: float) -> float:
        """Lot untuk risiko `risk` x equity; akun kecil memakai lot minimum bila risikonya <= min_lot_max_risk."""
        return lot_with_min(equity, risk, max(risk, self.s.risk.min_lot_max_risk), sl_distance, self.meta[sym])

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
                         equity: float, suggested: float, lot: float, ai_text: str) -> bool:
        c = self.s.confirm
        rows = []
        streak = self.tilt_streak()
        seen = set()
        for r in self.allowed_risk_options():
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
                f"{tilt}Saran AI: {suggested * 100:g}% → lot {lot} "
                f"(rugi di SL ${self.risk_amount(sym, lot, sig.sl_distance):.2f})\n"
                f"Pilih dalam {c.timeout_minutes} menit, lewat dari itu otomatis batal:")
        buttons = [rows[i:i + 2] for i in range(0, len(rows), 2)] + [[("❌ Tidak", f"c:{sid}:no")]]
        mid = self.notifier.send_buttons(text, buttons)
        if mid is None:
            return False
        expires = datetime.now(timezone.utc) + timedelta(minutes=c.timeout_minutes)
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
            if cq.get("data") == "dd:reset":
                self.reset_drawdown(cq)
                continue
            if str(cq.get("data", "")).split(":")[0] in ("pc", "pcy", "pcn", "be"):
                self.position_action(cq)
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

        # Kondisi bisa berubah selama menunggu jawaban: cek ulang semuanya
        streak = self.tilt_streak()
        block = (self.entry_block(sym, bsym, d, acc.equity, spread_pts) or self.news_block(bsym)
                 or (f"cek tilt: {streak} rugi beruntun, maksimal {self.s.risk.risk_per_trade * 100:g}%"
                     if not any(abs(risk - r) < 1e-9 for r in self.allowed_risk_options()) else None)
                 or (f"harga sudah bergerak {moved:.2f} ATR dari harga sinyal"
                     if moved > self.s.confirm.max_price_move_atr else None))
        if block:
            self.finish_confirmation(row, "blocked", f"⏭️ Tidak dieksekusi: {block}")
            return
        try:
            lot, fill = self.open_trade(row["id"], sym, d, row["sl_distance"], row["tp_distance"],
                                        acc.equity, risk, "bot user")
        except MT5Error as e:
            self.finish_confirmation(row, "failed", f"⚠️ Order gagal: {e}")
            return
        self.journal.update_signal(row["id"], user_decision="approved", user_risk=risk)
        self.notifier.clear_buttons(row["confirm_msg_id"])
        tp = f"{fill + d * row['tp_distance']:.{meta['digits']}f}" if row["tp_distance"] else "- (exit sinyal)"
        self.notify(f"✅ OPEN {'BUY' if d == 1 else 'SELL'} {sym} @ {fill:.{meta['digits']}f} (pilihan Anda)\n"
                    f"Lot {lot} | SL {fill - d * row['sl_distance']:.{meta['digits']}f} | TP {tp}\n"
                    f"Risiko {acc.equity * risk:.2f} {acc.currency} ({risk * 100:g}%)")

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
         "pause": self.cmd_pause, "resume": self.cmd_resume, "menu": self.cmd_menu,
         "start": self.cmd_menu}.get(cmd, self.cmd_help)()

    def menu_rows(self) -> list[list[str]]:
        """Saat dijeda tampilkan tombol Resume, selain itu Pause."""
        toggle = "▶️ Resume" if self.journal.get_state("paused") == "1" else "⏸️ Pause"
        return [["📟 Status", "📂 Posisi"], ["📈 P/L", "🔔 Sinyal"], [toggle, "❓ Bantuan"]]

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
                    f"P/L hari ini (sejak 07:00 WIB / restart bot): {self.daily.pnl_pct(acc.equity):+.2%}\n"
                    f"Drawdown dari puncak {self.dd.peak:.2f}: {self.dd.drawdown(acc.equity):.1%} "
                    f"(batas {r.max_drawdown_from_peak:.0%})\n"
                    f"Posisi: {len(self.mt5.positions(self.s.magic))}/{r.max_open_positions}\n"
                    f"Entry baru: {entry}\nCandle entry berikut: {self.next_entry_close(now)}")

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
                [[("❌ Tutup sekarang", f"pc:{pos.ticket}"), ("🔒 SL ke breakeven", f"be:{pos.ticket}")]])

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
                      else f"⏭️ {r['skip_reason'] or '-'}")
            ai = f" | AI {r['ai_decision']}" if r["ai_decision"] and r["ai_decision"] != "rule_only" else ""
            lines.append(f"{when} {'BUY' if r['direction'] == 1 else 'SELL'} {r['symbol']}: {status}{ai}")
        self.notify("🔔 5 sinyal terakhir (WIB)\n" + "\n".join(lines))

    def cmd_pause(self) -> None:
        self.journal.set_state("paused", "1")
        self.send_menu("⏸️ Entry baru dijeda. Posisi terbuka tetap dikelola (SL, TP, exit sinyal). "
                       "Tekan ▶️ Resume untuk lanjut.")

    def cmd_resume(self) -> None:
        self.journal.set_state("paused", "0")
        self.send_menu("▶️ Entry baru diizinkan lagi.")

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
            if self.calendar else None)
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
        return [(p.symbol, 1 if p.type == 0 else -1) for p in self.mt5.positions(self.s.magic)]

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
        f = compute_features(frames[p.entry_tf], frames[p.trend_tf], p)
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

    # ---------- shadow trade ----------
    def resolve_shadows(self) -> None:
        p = self.s.strategy
        features: dict[str, pd.DataFrame] = {}
        for row in self.journal.open_shadows():
            sym = row["symbol"]
            meta = self.meta.get(sym)
            if meta is None:
                continue
            if sym not in features:
                frames = self.frames(self.s.broker_symbol(sym))
                features[sym] = compute_features(frames[p.entry_tf], frames[p.trend_tf], p)
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

        text = None
        if self.llm:
            try:
                markets = {sym: market_summary(self.frames(self.s.broker_symbol(sym)), self.s.strategy,
                                               self.meta[sym]["digits"]) for sym in self.meta}
                currencies = {c for sym in self.meta for c in (sym[:3], sym[3:6])}
                calendar = (self.calendar.upcoming_all(now, 48, currencies)
                            if self.calendar and self.calendar.available else None)
                text = daily_report(self.llm, now.date().isoformat(), account, signals, trades, markets,
                                    self.journal.ai_vs_rules(), calendar)
            except Exception as e:
                log.warning("Laporan harian AI gagal: %s", e)
        if text is None:
            closed = [t for t in trades if t["status"] == "closed"]
            text = (f"Equity {account['equity']} {account['currency']} ({account['today_pnl_%']:+.2f}% hari ini)\n"
                    f"Sinyal: {len(signals)} | Trade ditutup: {len(closed)} | "
                    f"Profit: {sum(t['profit'] or 0 for t in closed):+.2f}")
        self.notify(f"📊 Laporan harian {now.date().isoformat()}\n\n{text}")
