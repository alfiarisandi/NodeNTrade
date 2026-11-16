"""Peluang yang terdeteksi analisis AI (dieksekusi atau tidak) dinilai dari candle M15 sesudahnya, untuk menjawab:
apakah filter membuang peluang yang sebenarnya menang (peluang terlewat), dan berapa setup yang ditawarkan ternyata
kalah (false positive). Simulasi kasar: entry di tengah zona saat candle pertama menyentuh zona; SL dan TP tersentuh
di candle yang sama dihitung rugi (konservatif); biaya (spread + komisi) dikurangkan dalam R."""
from __future__ import annotations

import re
from collections import Counter

import pandas as pd

HOLD_HOURS = 24           # setelah terisi, posisi simulasi ditutup di close bila belum kena SL/TP dalam 24 jam
EV_MIN_TRADES = 20        # trade forward minimal sebelum EV satu strategi dianggap terkalibrasi


def simulate(bars: pd.DataFrame, start: pd.Timestamp, direction: int, entry_low: float, entry_high: float,
             stop: float, target: float, valid_minutes: int, cost_r: float = 0.0) -> tuple[str, float | None] | None:
    """Hasil (outcome, R bersih) dari candle M15 `bars` (kolom time/high/low/close, waktu server) yang dibuka setelah
    `start`. outcome: win | loss | timeout | not_reached | untestable. None = belum selesai (tunggu candle berikut)."""
    mid = (entry_low + entry_high) / 2
    risk, reward = direction * (mid - stop), direction * (target - mid)
    if risk <= 0 or reward <= 0:
        return "untestable", None
    if bars.empty or bars["time"].iloc[0] > start:
        return "untestable", None   # histori tidak menjangkau waktu peluang
    after = bars[bars["time"] >= start]
    deadline = start + pd.Timedelta(minutes=valid_minutes)
    filled_at = None
    for b in after.itertuples():
        if filled_at is None:
            if b.time >= deadline:
                return "not_reached", None
            if b.high < entry_low or b.low > entry_high:
                continue
            filled_at = b.time
            # candle pengisian: hanya SL yang dihitung (urutan sentuhan di dalam candle tidak diketahui)
            if (b.low <= stop) if direction == 1 else (b.high >= stop):
                return "loss", round(-1 - cost_r, 3)
            continue
        hit_sl = b.low <= stop if direction == 1 else b.high >= stop
        hit_tp = b.high >= target if direction == 1 else b.low <= target
        if hit_sl:
            return "loss", round(-1 - cost_r, 3)
        if hit_tp:
            return "win", round(reward / risk - cost_r, 3)
        if b.time >= filled_at + pd.Timedelta(hours=HOLD_HOURS):
            return "timeout", round(direction * (b.close - mid) / risk - cost_r, 3)
    return None


REASON_KEYS = [   # (pola di alasan, kategori) untuk ringkasan "kenapa tidak trade"
    ("Weak", "skor Weak"), ("RR", "RR bersih kurang"), ("biaya", "biaya terlalu besar"), ("jarak SL", "jarak SL vs ATR"),
    ("zona entry", "zona terlalu lebar"), ("berita", "blok berita"), ("setup aktif", "sudah ada setup aktif"),
    ("Kuota", "kuota harian"), ("kuota", "kuota harian"), ("rugi harian", "batas rugi harian"),
    ("rugi beruntun", "jeda rugi beruntun"), ("EV forward", "EV forward negatif"), ("confidence Low", "confidence Low"),
    ("spread", "spread lebar"), ("dijeda", "entry dijeda"), ("circuit breaker", "circuit breaker"),
    ("sisi yang salah", "level tidak valid"), ("modal terlalu kecil", "lot minimum"),
]


def reason_key(reason: str) -> str:
    for pat, key in REASON_KEYS:
        if pat in (reason or ""):
            return key
    return re.sub(r"[\d.:%/+-]+", "", reason or "lainnya").strip()[:40] or "lainnya"


def opportunity_stats(rows) -> dict:
    """Ringkasan baris jurnal peluang (kolom kind, tier, decision, reason, executed, outcome, outcome_r, strategy)."""
    n = len(rows)
    if n == 0:
        return {"detected": 0}
    resolved = [r for r in rows if r["outcome"] in ("win", "loss", "timeout")]
    offered = [r for r in rows if r["decision"] == "offered"]
    not_taken = [r for r in resolved if not r["executed"]]

    def avg_r(rs) -> float | None:
        vals = [float(r["outcome_r"]) for r in rs if r["outcome_r"] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    inactivity = Counter(reason_key(r["reason"]) for r in rows if r["decision"] in ("rejected", "blocked"))
    inactivity.update(f"AI {r['decision'].upper()}" for r in rows if r["decision"] in ("wait", "no_trade"))
    by_strategy: dict[str, list] = {}
    for r in resolved:
        by_strategy.setdefault(r["strategy"] or "?", []).append(r)
    return {
        "detected": n,
        "by_tier": dict(Counter(r["tier"] or "?" for r in rows)),
        "executable_offered": len(offered),
        "rejected_by_gates": sum(1 for r in rows if r["decision"] in ("rejected", "blocked")),
        "wait_or_no_trade": sum(1 for r in rows if r["decision"] in ("wait", "no_trade")),
        "executed": sum(1 for r in rows if r["executed"]),
        "simulated_resolved": len(resolved),
        "not_reached": sum(1 for r in rows if r["outcome"] == "not_reached"),
        "missed_winners": sum(1 for r in not_taken if r["outcome"] == "win"),
        "avoided_losers": sum(1 for r in not_taken if r["outcome"] == "loss"),
        "false_positives": sum(1 for r in resolved if r["decision"] == "offered" and r["outcome"] == "loss"),
        "sim_avg_R_offered": avg_r([r for r in resolved if r["decision"] == "offered"]),
        "sim_avg_R_filtered": avg_r([r for r in resolved if r["decision"] != "offered"]),
        "sim_by_strategy": {k: {"n": len(v), "win_%": round(100 * sum(r["outcome"] == "win" for r in v) / len(v)),
                                "avg_R": avg_r(v)} for k, v in sorted(by_strategy.items())},
        "inactivity_reasons": dict(inactivity.most_common(6)),
    }


def format_opportunity_stats(st: dict, days: int) -> str:
    if not st.get("detected"):
        return f"🔍 Peluang {days} hari terakhir: belum ada yang tercatat."
    tiers = ", ".join(f"{k} {v}" for k, v in st["by_tier"].items())
    lines = [f"🔍 Peluang {days} hari terakhir: {st['detected']} terdeteksi ({tiers})",
             f"Ditawarkan {st['executable_offered']} | ditolak gerbang {st['rejected_by_gates']} | WAIT/NO TRADE "
             f"{st['wait_or_no_trade']} | dieksekusi {st['executed']}"]
    if st["simulated_resolved"]:
        lines.append(f"Simulasi M15 ({st['simulated_resolved']} selesai, {st['not_reached']} tidak tersentuh): "
                     f"peluang terlewat yang menang {st['missed_winners']}, rugi yang terhindar {st['avoided_losers']}, "
                     f"false positive {st['false_positives']}")
        fmt = lambda v: f"{v:+.2f}R" if v is not None else "-"   # noqa: E731
        lines.append(f"Rata-rata simulasi: ditawarkan {fmt(st['sim_avg_R_offered'])} vs disaring "
                     f"{fmt(st['sim_avg_R_filtered'])} (disaring lebih bagus = filter terlalu ketat)")
    if st["inactivity_reasons"]:
        lines.append("Alasan tidak trade: " + "; ".join(f"{k} {v}x" for k, v in st["inactivity_reasons"].items()))
    return "\n".join(lines)
