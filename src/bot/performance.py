"""Statistik performa dari jurnal: losing streak (cek tilt) dan ringkasan mingguan."""
from __future__ import annotations

import json
from collections import Counter

from datetime import datetime

from bot.ai.context import session_info
from bot.journal import Journal


def _r(row) -> float | None:
    return row["r_multiple"] if row["r_multiple"] is not None else None


def current_losing_streak(journal: Journal, lookback: int = 20) -> int:
    """Jumlah trade nyata terakhir yang rugi berturut-turut (dari yang paling baru)."""
    streak = 0
    for row in journal.recent_closed(lookback):
        loss = (row["profit"] or 0) < 0
        if not loss:
            break
        streak += 1
    return streak


def trade_stats(rows) -> dict:
    n = len(rows)
    if n == 0:
        return {"trades": 0}
    rs = [_r(r) for r in rows if _r(r) is not None]
    profit = sum(r["profit"] or 0 for r in rows)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    streak = best = 0
    for r in sorted(rows, key=lambda x: x["close_time"] or ""):
        streak = streak + 1 if (r["profit"] or 0) < 0 else 0
        best = max(best, streak)
    return {
        "trades": n,
        "winrate_%": round(100 * sum(1 for r in rows if (r["profit"] or 0) > 0) / n, 1),
        "profit": round(profit, 2),
        "total_R": round(sum(rs), 2) if rs else None,
        "expectancy_R": round(sum(rs) / len(rs), 3) if rs else None,
        "avg_win_R": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_R": round(sum(losses) / len(losses), 2) if losses else None,
        "max_losing_streak": best,
    }


def weekly_stats(journal: Journal, since_iso: str, dd_info: dict) -> dict:
    """Ringkasan periode untuk laporan mingguan (angka dihitung kode, bukan oleh AI)."""
    signals = journal.signals_since(since_iso)
    closed = [r for r in journal.trades_since(since_iso) if r["status"] == "closed"]
    shadows = [s for s in signals if s["shadow_status"] not in ("none", "open", "expired", None)]
    equity = journal.equity_since(since_iso)
    return {
        "period_start_utc": since_iso,
        "signals": {
            "total": len(signals),
            "by_source": dict(Counter(s["source"] or "rule" for s in signals)),
            "ai_decision": dict(Counter(s["ai_decision"] or "dilewati_aturan_risiko" for s in signals)),
            "user_decision": dict(Counter(s["user_decision"] for s in signals if s["user_decision"])),
            "skip_reasons": dict(Counter((s["skip_reason"] or "").split(":")[0] for s in signals
                                         if s["skip_reason"])),
        },
        "real_trades_closed": trade_stats(closed),
        "open_trades": len(journal.open_trades()),
        "shadow_trades_resolved": {
            "n": len(shadows),
            "total_R": round(sum(s["shadow_r"] or 0 for s in shadows), 2),
            "by_reason": {k: round(sum(s["shadow_r"] or 0 for s in shadows if (s["skip_reason"] or "").startswith(k)), 2)
                          for k in {(s["skip_reason"] or "").split(":")[0] for s in shadows}},
        },
        "equity": {
            "start": equity[0]["equity"] if equity else None,
            "end": equity[-1]["equity"] if equity else None,
            **dd_info,
        },
        "ai_vs_rules_all_time_R": journal.ai_vs_rules(),
        "ai_reasons_sample": [json.loads(s["ai_reasons"]) for s in signals if s["ai_reasons"]][:5],
    }


def format_weekly_fallback(st: dict) -> str:
    """Laporan mingguan tanpa AI (dipakai jika Claude tidak tersedia)."""
    t, sig, eq = st["real_trades_closed"], st["signals"], st["equity"]
    lines = [f"Sinyal: {sig['total']} | keputusan AI: {sig['ai_decision'] or '-'}",
             f"Keputusan Anda: {sig['user_decision'] or '-'}",
             f"Trade ditutup: {t['trades']}"]
    if t["trades"]:
        lines.append(f"Winrate {t['winrate_%']}% | Profit {t['profit']:+.2f} | Expectancy {t['expectancy_R']}R")
    if eq["start"] is not None:
        lines.append(f"Equity {eq['start']:.2f} -> {eq['end']:.2f} | DD dari puncak {eq.get('drawdown_%', 0)}%")
    lines.append(f"Shadow trade selesai: {st['shadow_trades_resolved']['n']} "
                 f"({st['shadow_trades_resolved']['total_R']:+}R)")
    return "\n".join(lines)


def _streaks(profits: list[float]) -> tuple[int, int]:
    best_w = best_l = w = l = 0
    for p in profits:
        w, l = (w + 1, 0) if p > 0 else (0, l + 1)
        best_w, best_l = max(best_w, w), max(best_l, l)
    return best_w, best_l


def intraday_stats(rows) -> dict:
    """Statistik trade tertutup (urut waktu tutup): win rate, rata-rata menang/kalah, profit factor, expectancy
    (= win rate x rata-rata menang - loss rate x rata-rata kalah, profit sudah bersih dari komisi/swap),
    drawdown maksimum, beruntun, lama tahan, dan rincian per pair & per sesi."""
    n = len(rows)
    if n == 0:
        return {"trades": 0}
    profits = [float(r["profit"] or 0) for r in rows]
    wins, losses = [p for p in profits if p > 0], [p for p in profits if p <= 0]
    rs = [float(r["r_multiple"]) for r in rows if r["r_multiple"] is not None]
    wr = len(wins) / n
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = -sum(losses) / len(losses) if losses else 0.0
    peak = cum = dd = 0.0
    for p in profits:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    hold = [(datetime.fromisoformat(r["close_time"]) - datetime.fromisoformat(r["open_time"])).total_seconds() / 60
            for r in rows if r["open_time"] and r["close_time"]]
    best_w, best_l = _streaks(profits)

    def group(key) -> dict:
        out: dict[str, list[float]] = {}
        for r, p in zip(rows, profits):
            out.setdefault(key(r), []).append(p)
        return {k: {"trades": len(v), "winrate_%": round(100 * sum(1 for p in v if p > 0) / len(v), 1),
                    "net": round(sum(v), 2)} for k, v in out.items()}

    return {
        "trades": n,
        "winrate_%": round(100 * wr, 1),
        "avg_win": round(avg_w, 2),
        "avg_loss": round(avg_l, 2),
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "expectancy": round(wr * avg_w - (1 - wr) * avg_l, 2),
        "expectancy_R": round(sum(rs) / len(rs), 3) if rs else None,
        "total_R": round(sum(rs), 2) if rs else None,
        "net": round(sum(profits), 2),
        "max_drawdown": round(dd, 2),
        "max_win_streak": best_w,
        "max_loss_streak": best_l,
        "avg_hold_minutes": round(sum(hold) / len(hold)) if hold else None,
        "by_symbol": group(lambda r: r["symbol"]),
        "by_session": group(lambda r: session_info(datetime.fromisoformat(r["open_time"]))["label"]
                            if r["open_time"] else "?"),
    }


def format_evaluation(title: str, st: dict, what_if: dict | None = None) -> str:
    if not st.get("trades"):
        return f"📋 {title}\nBelum ada trade tertutup."
    pf = st["profit_factor"] if st["profit_factor"] is not None else "-"
    lines = [f"📋 {title}: {st['trades']} trade tertutup",
             f"Win rate {st['winrate_%']}% | Profit factor {pf} | Net {st['net']:+.2f}",
             f"Rata-rata menang +{st['avg_win']:.2f} | rata-rata kalah -{st['avg_loss']:.2f}",
             f"Expectancy per trade {st['expectancy']:+.2f}"
             + (f" ({st['expectancy_R']:+.2f}R, total {st['total_R']:+.2f}R)" if st["expectancy_R"] is not None else ""),
             f"Drawdown maks {st['max_drawdown']:.2f} | beruntun menang {st['max_win_streak']}, kalah {st['max_loss_streak']}"
             + (f" | tahan rata-rata {st['avg_hold_minutes']} menit" if st["avg_hold_minutes"] is not None else "")]
    for label, key in (("Per pair", "by_symbol"), ("Per sesi", "by_session")):
        lines.append(f"{label}: " + "; ".join(f"{k} {v['trades']}x, win {v['winrate_%']}%, net {v['net']:+.2f}"
                                               for k, v in st[key].items()))
    if what_if and what_if.get("n"):
        lines.append(f"Andai-andai ({what_if['n']} trade, simulasi bar M5, kasar): hasil nyata {what_if['actual_R']:+.2f}R | "
                     f"TP1 penuh tanpa disentuh {what_if['tp1_R']:+.2f}R | dynamic SL + parsial {what_if['dynamic_R']:+.2f}R")
    if st["trades"] < 30:
        lines.append(f"⚠️ Sampel baru {st['trades']} trade: belum bermakna secara statistik. Jangan ubah parameter dulu; "
                     "evaluasi ulang setelah 20-30 trade.")
    return "\n".join(lines)
