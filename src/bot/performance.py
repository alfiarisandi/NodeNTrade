"""Statistik performa dari jurnal: losing streak (cek tilt) dan ringkasan mingguan."""
from __future__ import annotations

import json
from collections import Counter

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
