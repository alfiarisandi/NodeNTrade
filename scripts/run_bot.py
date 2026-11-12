"""CLI utama bot.

  python scripts/run_bot.py --check              cek koneksi MT5, mode demo, simbol & spread
  python scripts/run_bot.py --check-llm          cek koneksi Claude lewat 9Router
  python scripts/run_bot.py --test-review EURUSD minta Claude me-review sinyal contoh (tanpa order)
  python scripts/run_bot.py --dry-run            jalankan bot tanpa mengirim order
  python scripts/run_bot.py --live               jalankan bot dengan order (hanya akun DEMO)
  python scripts/run_bot.py --export             ekspor jurnal ke CSV + bandingkan AI vs aturan
  python scripts/run_bot.py --news               kalender ekonomi minggu ini & pair yang sedang diblok
  python scripts/run_bot.py --weekly-report      tampilkan laporan mingguan sekarang (tanpa kirim Telegram)
  python scripts/run_bot.py --analysis           analisis XAUUSD & EURUSD oleh Claude (sama dengan tombol 📊, tanpa Telegram)
  python scripts/run_bot.py --reset-dd           reset circuit breaker drawdown (puncak = equity terbaru)

  Profil lain: --config config/settings.small.yaml (atau BOT_CONFIG=... di .env), mis. akun $100.
"""
import argparse
import json
import sys

import numpy as np
from _common import setup_logging

from bot.ai.client import ClaudeLLM, LLMError
from bot.config import load_settings
from bot.journal import Journal
from bot.mt5_client import MT5Client, MT5Error
from bot.notifier import Notifier
from bot.risk import lot_with_min


def check_mt5(s) -> None:
    mt5c = MT5Client(s.mt5)
    mt5c.connect()
    try:
        acc = mt5c.account()
        demo = mt5c.is_demo()
        print(f"Akun    : {acc.login} ({acc.name}) @ {acc.server} - {acc.company}")
        print(f"Tipe    : {'DEMO ✅' if demo else 'REAL ⛔ (bot akan menolak jalan)'}")
        print(f"Saldo   : {acc.balance:.2f} {acc.currency} | Equity {acc.equity:.2f} | Leverage 1:{acc.leverage}")
        print(f"Algo Trading: {'ON ✅' if mt5c.trading_enabled() else 'OFF ⛔ (klik tombol Algo Trading / Ctrl+E di MT5)'}")
        first = s.broker_symbol(s.symbols[0].name)
        offset = mt5c.server_utc_offset(first)
        print(f"Offset server: {'GMT%+d' % offset if offset is not None else 'tidak terdeteksi (pasar tutup?)'}"
              f"{' (config: %s)' % s.session.server_utc_offset_hours if s.session.server_utc_offset_hours is not None else ''}")
        cap = max(s.risk.risk_per_trade, s.risk.min_lot_max_risk)
        print(f"Risiko  : {s.risk.risk_per_trade:.1%}/trade"
              f"{f', lot minimum boleh sampai {cap:.0%}' if s.risk.min_lot_max_risk else ''}")
        print(f"\n{'Simbol':<10}{'Status':<8}{'Spread':>8}{'Maks':>7}{'TickVal':>10}{'MinLot':>8}{'Lot':>7}"
              f"{'ModalMin':>10}  SL")
        for sym in s.symbols:
            bsym = s.broker_symbol(sym.name)
            try:
                meta = mt5c.symbol_meta(bsym)
            except MT5Error:
                alt = mt5c.find_symbols(sym.name)
                print(f"{bsym:<10}TIDAK ADA  alternatif di broker: {', '.join(alt[:6]) or '-'}")
                continue
            t = mt5c.tick(bsym)
            spread = (t.ask - t.bid) / meta["point"]
            bars = mt5c.rates(bsym, s.strategy.entry_tf, 100)
            tr = np.maximum(bars["high"] - bars["low"], (bars["high"] - bars["close"].shift()).abs()).tail(14).mean()
            sl = s.strategy.sl_atr_mult * tr
            lot = lot_with_min(acc.equity, s.risk.risk_per_trade, cap, sl, meta)
            # Modal minimum: lot minimum di SL ini masih <= batas risiko
            min_cap = sl / meta["tick_size"] * meta["tick_value"] * meta["volume_min"] / cap
            flag = "OK" if spread <= sym.max_spread_points else "LEBAR"
            print(f"{bsym:<10}{flag:<8}{spread:>8.0f}{sym.max_spread_points:>7.0f}{meta['tick_value']:>10.4f}"
                  f"{meta['volume_min']:>8}{lot:>7}{min_cap:>10.0f}  {sl:.{meta['digits']}f} "
                  f"({s.strategy.sl_atr_mult:g} ATR {s.strategy.entry_tf})")
    finally:
        mt5c.shutdown()


def check_llm(s) -> None:
    llm = ClaudeLLM(s.ai)
    print(f"9Router : {s.ai.base_url}\nModel   : {s.ai.model}")
    try:
        models = llm.list_models()
        print(f"Model tersedia ({len(models)}): {', '.join(models[:15])}{' ...' if len(models) > 15 else ''}")
    except Exception as e:
        print(f"(Daftar model tidak bisa diambil lewat format Anthropic: {str(e)[:150]})")
    reply = llm.complete("You are a connectivity check. Reply with only a JSON object.",
                         'Reply exactly: {"ok": true, "model": "<your model name>"}')
    print(f"Respons : {reply}")
    print("✅ Claude terhubung lewat 9Router (format /v1/messages)")


def test_review(s, symbol: str) -> None:
    """Sinyal contoh dari bar terakhir searah tren H4, lalu direview Claude. Tidak ada order."""
    from datetime import datetime, timezone

    from bot.ai.context import build_review_context
    from bot.ai.prompts import review_system
    from bot.ai.reviewer import review_signal
    from bot.strategy import Signal, compute_features

    mt5c = MT5Client(s.mt5)
    mt5c.connect()
    try:
        bsym = s.broker_symbol(symbol)
        meta = mt5c.symbol_meta(bsym)
        cal = make_calendar(s)
        if cal:
            cal.refresh(datetime.now(timezone.utc))
        offset = mt5c.server_utc_offset(bsym)
        offset = 2 if offset is None else offset
        frames = {tf: mt5c.rates(bsym, tf, n) for tf, n in
                  {"H1": 600, "H4": 600, "D1": 400, s.strategy.entry_tf: 600, s.strategy.trend_tf: 600}.items()}
        f = compute_features(frames[s.strategy.entry_tf], frames[s.strategy.trend_tf], s.strategy, offset)
        row = f.iloc[-1]
        d = 1 if row["trend"] >= 0 else -1
        sig = Signal(symbol, d, row["time"], float(row["close"]), float(row["atr"]),
                     s.strategy.sl_atr_mult * row["atr"], s.strategy.tp_atr_mult * row["atr"])
        t = mt5c.tick(bsym)
        acc = mt5c.account()
        ctx = build_review_context(sig, frames, s.strategy, meta, t.ask if d == 1 else t.bid,
                                   (t.ask - t.bid) / meta["point"], datetime.now(timezone.utc).hour,
                                   {"equity": acc.equity, "currency": acc.currency}, [], {}, 0.0, [],
                                   cal.context(bsym, datetime.now(timezone.utc)) if cal else None,
                                   offset_hours=offset)
        print(f"Sinyal contoh: {sig.side} {symbol} (konteks {len(ctx)} karakter)")
        out = review_signal(ClaudeLLM(s.ai), ctx, s.ai.on_failure,
                            review_system(s.strategy, s.risk.risk_per_trade))
        print(json.dumps({"decision": out.decision, "multiplier": out.multiplier, "confidence": out.confidence,
                          "reasons": out.reasons, "risks": out.risks, "error": out.error},
                         ensure_ascii=False, indent=2))
    finally:
        mt5c.shutdown()


def export(s) -> None:
    j = Journal(s.path(s.journal_path))
    for p in j.export_csv(s.path(s.reports_dir) / "journal"):
        print(f"Ekspor: {p}")
    print("AI vs aturan (dalam R):")
    print(json.dumps(j.ai_vs_rules(), indent=2, ensure_ascii=False))


def make_calendar(s):
    from bot.news import Calendar

    return Calendar(s.news, s.path(s.news.cache_path)) if s.news.enabled else None


def show_news(s) -> None:
    from datetime import datetime, timedelta, timezone

    cal = make_calendar(s)
    if cal is None:
        print("Filter berita nonaktif (news.enabled: false)")
        return
    now = datetime.now(timezone.utc)
    fresh = cal.refresh(now, force=True)
    print(f"Kalender: {'diperbarui' if fresh else 'pakai cache'} | tersedia: {cal.available} | {len(cal.events)} event")
    print(f"Aturan blok: event {'/'.join(s.news.block_impacts)} {s.news.block_before_hours:g} jam sebelum "
          f"s/d {s.news.block_after_hours:g} jam sesudah\n")
    for sym in s.symbols:
        bsym = s.broker_symbol(sym.name)
        ev = cal.blocking_event(bsym, now)
        status = f"DIBLOK ({ev.currency} {ev.title})" if ev else "boleh entry"
        print(f"{sym.name:7} {status}")
        for e in cal.for_pair(bsym, now, now + timedelta(days=s.news.context_days), ["High"]):
            wib = e.time + timedelta(hours=7)
            print(f"          {wib:%a %d %b %H:%M} WIB  {e.currency}  {e.title}")


def reset_dd(s) -> None:
    j = Journal(s.path(s.journal_path))
    was = j.get_state("dd_halted") == "1"
    j.set_state("dd_halted", "0")
    j.set_state("dd_peak", "0")   # bot memakai equity berikutnya sebagai puncak baru
    print("Circuit breaker di-reset." if was else "Circuit breaker memang tidak aktif; puncak di-reset.")


def weekly_report_now(s) -> None:
    from datetime import datetime, timedelta, timezone

    from bot.ai.daily_review import weekly_report
    from bot.performance import current_losing_streak, format_weekly_fallback, weekly_stats

    j = Journal(s.path(s.journal_path))
    now = datetime.now(timezone.utc)
    eq = j.equity_since((now - timedelta(days=7)).isoformat(timespec="seconds"))
    # Puncak dari state bot; jika belum ada (bot belum pernah menyimpannya), dari snapshot equity jurnal
    peak = float(j.get_state("dd_peak") or 0) or max((r["equity"] for r in j.equity_since("")), default=0)
    last = eq[-1]["equity"] if eq else None
    dd_info = {"peak": peak, "drawdown_%": round(100 * max(0, 1 - last / peak), 2) if peak and last else 0,
               "circuit_breaker_active": j.get_state("dd_halted") == "1",
               "losing_streak_now": current_losing_streak(j)}
    st = weekly_stats(j, (now - timedelta(days=7)).isoformat(timespec="seconds"), dd_info)
    print(json.dumps(st, indent=2, ensure_ascii=False, default=str))
    try:
        print("\n--- Laporan Claude ---\n" + weekly_report(ClaudeLLM(s.ai), st))
    except Exception as e:
        print(f"\n(Claude tidak tersedia: {e})\n" + format_weekly_fallback(st))


def analysis_now(s) -> None:
    """Analisis harian Claude dari data MT5 live, dicetak ke layar (tidak ada order, tidak ada Telegram)."""
    from bot.ai.analysis import format_setup
    from bot.config import TelegramCfg
    from bot.live import LiveBot

    mt5c = MT5Client(s.mt5)
    mt5c.connect()
    try:
        bot = LiveBot(s, mt5c, Journal(s.path(s.journal_path)), Notifier(TelegramCfg()), ClaudeLLM(s.ai), True,
                      make_calendar(s))
        for sym in s.symbols:
            bot.meta[sym.name] = mt5c.symbol_meta(s.broker_symbol(sym.name))
        offset = s.session.server_utc_offset_hours
        if offset is None:
            offset = mt5c.server_utc_offset(s.broker_symbol(s.symbols[0].name))
        bot.offset = bot.offset if offset is None else offset
        if bot.calendar:
            bot.calendar.refresh(__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        print("Meminta analisis Claude (±1-2 menit)...")
        a = bot.run_analysis()
        print("\n--- Analisis ---\n" + a.text)
        print(f"\n--- Setup terstruktur ({len(a.setups)}) ---")
        for st, why, _ in bot.setup_checks(a):
            print(f"{'✅ layak ditawarkan' if why is None else '⛔ ' + why}\n{format_setup(st, bot.meta[st.symbol]['digits'])}\n")
    finally:
        mt5c.shutdown()


def run(s, dry_run: bool) -> None:
    from bot.live import LiveBot

    llm = None
    if s.ai.enabled:
        try:
            llm = ClaudeLLM(s.ai)
        except LLMError as e:
            if s.ai.on_failure == "skip":
                sys.exit(f"AI aktif tapi tidak bisa dipakai: {e}")
            print(f"AI tidak tersedia ({e}), lanjut mode aturan saja")
    bot = LiveBot(s, MT5Client(s.mt5), Journal(s.path(s.journal_path)), Notifier(s.telegram), llm, dry_run,
                  make_calendar(s))
    bot.run()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true")
    g.add_argument("--check-llm", action="store_true")
    g.add_argument("--test-review", metavar="SYMBOL")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--live", action="store_true")
    g.add_argument("--export", action="store_true")
    g.add_argument("--news", action="store_true")
    g.add_argument("--weekly-report", action="store_true")
    g.add_argument("--analysis", action="store_true")
    g.add_argument("--reset-dd", action="store_true")
    ap.add_argument("--config", help="file settings (default: BOT_CONFIG di .env, atau config/settings.yaml)")
    args = ap.parse_args()

    s = load_settings(args.config)
    try:
        dispatch(args, s)
    except (MT5Error, LLMError) as e:
        sys.exit(f"❌ {e}")


def dispatch(args, s) -> None:
    if args.check:
        setup_logging()
        check_mt5(s)
    elif args.check_llm:
        setup_logging()
        check_llm(s)
    elif args.test_review:
        setup_logging()
        test_review(s, args.test_review.upper())
    elif args.weekly_report:
        weekly_report_now(s)
    elif args.analysis:
        setup_logging()
        analysis_now(s)
    elif args.reset_dd:
        reset_dd(s)
    elif args.news:
        show_news(s)
    elif args.export:
        export(s)
    else:
        setup_logging("data/bot.log")
        dry = True if args.dry_run else False if args.live else s.mode == "dry_run"
        run(s, dry)


if __name__ == "__main__":
    main()
