"""Peluang yang terdeteksi analisis AI (dieksekusi atau tidak) dinilai dari candle M15 sesudahnya, untuk menjawab:
apakah filter membuang peluang yang sebenarnya menang (peluang terlewat), dan berapa setup yang ditawarkan ternyata
kalah (false positive). Simulasi: entry di tengah zona hanya bila candle benar-benar menyentuh zona dalam
valid_minutes; SL/TP dinilai berurutan per candle M15; bila urutannya tidak bisa diketahui dari OHLC (SL dan TP di
candle yang sama, atau TP di candle pengisian) hasilnya AMBIGUOUS, tidak ditebak. Biaya (spread + komisi) dalam R."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

HOLD_HOURS = 24           # setelah terisi, posisi simulasi ditutup di close bila belum kena SL/TP dalam 24 jam
EV_MIN_TRADES = 20        # trade forward minimal sebelum EV satu strategi dianggap terkalibrasi
RESOLVED = ("win", "loss", "timeout")


@dataclass
class SimResult:
    outcome: str                    # win | loss | timeout | not_reached | ambiguous | untestable
    r: float | None = None          # R bersih bila posisi penuh keluar di TP1 / SL / close timeout
    tp2_outcome: str | None = None  # bila TP1 tercapai dan ada TP2: win | loss | timeout | ambiguous (SL awal tetap)
    r_tp2: float | None = None


def simulate(bars: pd.DataFrame, start: pd.Timestamp, direction: int, entry_low: float, entry_high: float,
             stop: float, target: float, valid_minutes: int, cost_r: float = 0.0, target2: float | None = None,
             hold_hours: float = HOLD_HOURS) -> SimResult | None:
    """Hasil dari candle M15 `bars` (kolom time/high/low/close, waktu server) yang dibuka setelah `start`.
    None = belum selesai (tunggu candle berikut). Zona harus tersentuh sebelum start + valid_minutes."""
    mid = (entry_low + entry_high) / 2
    risk, reward = direction * (mid - stop), direction * (target - mid)
    if risk <= 0 or reward <= 0:
        return SimResult("untestable")
    if bars.empty or bars["time"].iloc[0] > start:
        return SimResult("untestable")   # histori tidak menjangkau waktu peluang
    if target2 is not None and direction * (target2 - target) <= 0:
        target2 = None
    after = bars[bars["time"] >= start]
    deadline = start + pd.Timedelta(minutes=valid_minutes)

    def hits(b, level_tp: float) -> tuple[bool, bool]:
        sl = b.low <= stop if direction == 1 else b.high >= stop
        tp = b.high >= level_tp if direction == 1 else b.low <= level_tp
        return sl, tp

    filled_at, first = None, None
    for b in after.itertuples():
        if filled_at is None:
            if b.time >= deadline:
                return SimResult("not_reached")
            if b.high < entry_low or b.low > entry_high:
                continue
            filled_at = b.time
            sl, tp = hits(b, target)
            if tp:   # TP di candle pengisian: tidak diketahui apakah TP tersentuh sebelum atau sesudah entry
                return SimResult("ambiguous")
            if sl:   # harga datang dari sisi entry, menembus zona lalu SL: urutannya pasti
                return SimResult("loss", round(-1 - cost_r, 3))
            continue
        timeout = b.time >= filled_at + pd.Timedelta(hours=hold_hours)
        if first is None:
            sl, tp = hits(b, target)
            if sl and tp:
                return SimResult("ambiguous")
            if sl:
                return SimResult("loss", round(-1 - cost_r, 3))
            if tp:
                first = SimResult("win", round(reward / risk - cost_r, 3))
                if target2 is None:
                    return first
                continue
            if timeout:
                return SimResult("timeout", round(direction * (b.close - mid) / risk - cost_r, 3))
            continue
        # TP1 sudah tercapai: lanjutkan seluruh posisi ke TP2 dengan SL awal
        sl, tp = hits(b, target2)
        if sl and tp:
            first.tp2_outcome = "ambiguous"
        elif sl:
            first.tp2_outcome, first.r_tp2 = "loss", round(-1 - cost_r, 3)
        elif tp:
            first.tp2_outcome = "win"
            first.r_tp2 = round(direction * (target2 - mid) / risk - cost_r, 3)
        elif timeout:
            first.tp2_outcome = "timeout"
            first.r_tp2 = round(direction * (b.close - mid) / risk - cost_r, 3)
        else:
            continue
        return first
    return None


def zones_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    return lo1 <= hi2 and lo2 <= hi1


def find_duplicate(rows, symbol: str, direction: int, entry_low: float | None, entry_high: float | None,
                   now: datetime, decisions: tuple[str, ...] | None = None) -> int | None:
    """Id baris jurnal peluang yang merupakan ide yang sama: pair + arah sama, zona tumpang tindih, dan masih
    dalam valid_minutes-nya. decisions = hanya baris dengan keputusan itu (mis. ("offered",) untuk penawaran
    ulang). None bila tidak ada / zona tidak diketahui."""
    if entry_low is None or entry_high is None:
        return None
    for r in reversed(rows):
        if r["symbol"] != symbol or r["direction"] != direction or r["duplicate_of"] or r["entry_low"] is None:
            continue
        if decisions is not None and r["decision"] not in decisions:
            continue
        created = datetime.fromisoformat(r["created_at"])
        if now - created > timedelta(minutes=r["valid_minutes"] or 240):
            continue
        if zones_overlap(entry_low, entry_high, r["entry_low"], r["entry_high"]):
            return r["id"]
    return None


REASON_KEYS = [   # (pola di alasan, kategori) untuk ringkasan "kenapa tidak trade"
    ("Weak", "skor Weak"), ("RR", "RR bersih kurang"), ("biaya", "biaya terlalu besar"), ("jarak SL", "jarak SL vs ATR"),
    ("zona entry", "zona terlalu lebar"), ("berita", "blok berita"), ("setup aktif", "sudah ada setup aktif"),
    ("Kuota", "kuota harian"), ("kuota", "kuota harian"), ("rugi harian", "batas rugi harian"),
    ("rugi beruntun", "jeda rugi beruntun"), ("EV forward", "EV forward negatif"), ("confidence Low", "confidence Low"),
    ("spread", "spread lebar"), ("dijeda", "entry dijeda"), ("circuit breaker", "circuit breaker"),
    ("sisi yang salah", "level tidak valid"), ("modal terlalu kecil", "lot minimum"), ("tick basi", "data harga basi"),
    ("feed terputus", "data harga basi"), ("pasar tutup", "pasar tutup"), ("instance bot lain", "bot lain aktif"),
    ("sudah ada posisi", "posisi lain di pair ini"), ("eksposur", "eksposur mata uang"),
]

# Tahap corong keputusan per ide (urut dari paling jauh ke eksekusi); ide = baris pertama + duplikatnya
STAGE_ORDER = ["executed", "limit_pending", "awaiting_owner", "limit_not_filled", "exec_failed", "exec_blocked",
               "owner_declined", "owner_timeout", "offered", "risk_blocked", "gate_rejected", "ai_low_quality",
               "ai_wait"]
STAGE_LABEL = {"executed": "dieksekusi", "limit_pending": "limit menunggu", "awaiting_owner": "menunggu Anda",
               "limit_not_filled": "limit tidak terisi", "exec_failed": "gagal eksekusi",
               "exec_blocked": "diblok saat eksekusi", "owner_declined": "ditolak Anda", "owner_timeout": "tidak dijawab",
               "offered": "ditawarkan", "risk_blocked": "diblok manajemen risiko", "gate_rejected": "ditolak gerbang kode",
               "ai_low_quality": "skor Weak / confidence Low", "ai_wait": "AI WAIT / NO TRADE"}


def reason_key(reason: str) -> str:
    for pat, key in REASON_KEYS:
        if pat in (reason or ""):
            return key
    return re.sub(r"[\d.:%/+-]+", "", reason or "lainnya").strip()[:40] or "lainnya"


def _get(r, key: str):
    try:
        return r[key]
    except (KeyError, IndexError):
        return None


def stage(r) -> str:
    """Tahap terakhir yang dicapai satu baris peluang (penolakan AI, gerbang kode, risiko, pemilik, eksekusi)."""
    d = r["decision"]
    if d in ("wait", "no_trade"):
        return "ai_wait"
    if d == "rejected":
        return "ai_low_quality" if reason_key(r["reason"]) in ("skor Weak", "confidence Low") else "gate_rejected"
    if d == "blocked":
        return "risk_blocked"
    if _get(r, "executed"):
        return "executed"
    return {"declined": "owner_declined", "expired": "owner_timeout", "blocked": "exec_blocked",
            "failed": "exec_failed", "limit_cancelled": "limit_not_filled", "limit": "limit_pending",
            "pending": "awaiting_owner"}.get(_get(r, "user_decision"), "offered")


def ideas(rows) -> list[list]:
    """Kelompokkan baris per ide: baris pertama + baris yang duplicate_of-nya menunjuk ke sana."""
    groups: dict[int, list] = {}
    for r in rows:
        groups.setdefault(_get(r, "duplicate_of") or r["id"], []).append(r)
    return list(groups.values())


def opportunity_stats(rows) -> dict:
    """Ringkasan jurnal peluang per IDE (pengulangan ide yang sama dihitung sekali): corong keputusan, hasil
    simulasi (peluang terlewat, rugi terhindar, false positive, ambiguous), TP2, per strategi dan per model."""
    if not rows:
        return {"detected": 0}
    groups = ideas(rows)

    def best(g):   # tahap terjauh yang dicapai ide ini; hasil simulasi dari baris pertamanya
        return min(g, key=lambda r: STAGE_ORDER.index(stage(r)))

    items = [(best(g), g[0]) for g in groups]
    resolved = [(b, f) for b, f in items if f["outcome"] in RESOLVED]
    not_taken = [(b, f) for b, f in resolved if stage(b) != "executed"]
    offered = ("executed", "limit_pending", "awaiting_owner", "limit_not_filled", "exec_failed", "exec_blocked",
               "owner_declined", "owner_timeout", "offered")

    def avg_r(rs) -> float | None:
        vals = [float(f["outcome_r"]) for _, f in rs if f["outcome_r"] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    funnel = Counter(stage(b) for b, _ in items)
    missed = Counter(stage(b) for b, f in not_taken if f["outcome"] == "win")
    inactivity = Counter(reason_key(b["reason"]) for b, _ in items if stage(b) in ("risk_blocked", "gate_rejected",
                                                                                     "ai_low_quality"))
    inactivity.update("AI " + b["decision"].upper() for b, _ in items if stage(b) == "ai_wait")
    by_strategy: dict[str, list] = {}
    for b, f in resolved:
        by_strategy.setdefault(f["strategy"] or "?", []).append((b, f))
    tp1_wins = [f for _, f in resolved if f["outcome"] == "win" and _get(f, "outcome_tp2")]
    return {
        "detected": len(rows),
        "ideas": len(groups),
        "repeats": len(rows) - len(groups),
        "by_tier": dict(Counter(f["tier"] or "?" for _, f in items)),
        "funnel": {k: funnel[k] for k in STAGE_ORDER if funnel[k]},
        "executable_offered": sum(1 for b, _ in items if stage(b) in offered),
        "rejected_by_gates": sum(1 for b, _ in items if stage(b) in ("risk_blocked", "gate_rejected", "ai_low_quality")),
        "wait_or_no_trade": funnel["ai_wait"],
        "executed": funnel["executed"],
        "simulated_resolved": len(resolved),
        "not_reached": sum(1 for _, f in items if f["outcome"] == "not_reached"),
        "ambiguous": sum(1 for _, f in items if f["outcome"] == "ambiguous"),
        "missed_winners": sum(missed.values()),
        "missed_winners_by_stage": {k: missed[k] for k in STAGE_ORDER if missed[k]},
        "avoided_losers": sum(1 for _, f in not_taken if f["outcome"] == "loss"),
        "false_positives": sum(1 for b, f in resolved if stage(b) in offered and f["outcome"] == "loss"),
        "sim_avg_R_offered": avg_r([(b, f) for b, f in resolved if stage(b) in offered]),
        "sim_avg_R_filtered": avg_r([(b, f) for b, f in resolved if stage(b) not in offered]),
        "tp2_after_tp1": {"n": len(tp1_wins), "reached": sum(f["outcome_tp2"] == "win" for f in tp1_wins)},
        "sim_by_strategy": {k: {"n": len(v), "win_%": round(100 * sum(f["outcome"] == "win" for _, f in v) / len(v)),
                                "avg_R": avg_r(v)} for k, v in sorted(by_strategy.items())},
        "by_model": dict(Counter(_get(f, "model") or "?" for _, f in items)),
        "inactivity_reasons": dict(inactivity.most_common(6)),
    }


SCREEN_FATE_LABEL = {"out_of_hours": "di luar jam AI", "regime_filtered": "ditolak regime", "repeat": "pengulangan",
                     "held": "ditahan (risiko / berita / kuota)", "cooldown": "cooldown AI", "sent_to_ai": "dikirim ke AI"}


def screener_stats(rows) -> dict:
    """Kandidat screener aturan A-E (catat-saja, tanpa AI): seberapa banyak peluang aturan sebenarnya tersedia,
    per pair / strategi / regime / sesi, berapa yang harganya tersentuh, kena TP 1,5R atau SL, dan nasibnya di jalur
    AI. Kandidat yang sama dalam 60 menit dihitung sekali."""
    from bot.ai.context import session_info   # impor di sini: context membawa banyak dependensi

    rows = [r for r in rows if r["kind"] == "screener"]
    if not rows:
        return {"candidates": 0}
    firsts = [r for r in rows if not _get(r, "duplicate_of")]

    def summary(rs) -> dict:
        res = [r for r in rs if r["outcome"] in RESOLVED]
        vals = [float(r["outcome_r"]) for r in res if r["outcome_r"] is not None]
        return {"n": len(rs), "touched": sum(r["outcome"] in (*RESOLVED, "ambiguous") for r in rs),
                "tp": sum(r["outcome"] == "win" for r in rs), "sl": sum(r["outcome"] == "loss" for r in rs),
                "ambiguous": sum(r["outcome"] == "ambiguous" for r in rs),
                "never_active": sum(r["outcome"] == "not_reached" for r in rs), "open": sum(r["outcome"] == "open" for r in rs),
                "avg_R": round(sum(vals) / len(vals), 3) if vals else None}

    def group(key) -> dict:
        out: dict[str, list] = {}
        for r in firsts:
            out.setdefault(key(r), []).append(r)
        return {k: summary(v) for k, v in sorted(out.items())}

    return {
        "candidates": len(firsts),
        "repeats": len(rows) - len(firsts),
        "all": summary(firsts),
        "by_pair": group(lambda r: r["symbol"]),
        "by_strategy": group(lambda r: r["strategy"] or "?"),
        "by_regime": group(lambda r: r["regime"] or "?"),
        "by_session": group(lambda r: session_info(datetime.fromisoformat(r["created_at"]))["label"]),
        "by_fate": group(lambda r: r["decision"] or "?"),
    }


def format_screener_stats(st: dict, days: int) -> str:
    if not st.get("candidates"):
        return f"🛰️ Screener 24/5 {days} hari terakhir: belum ada kandidat tercatat."

    def line(label: str, groups: dict, names: dict | None = None) -> str:
        return f"{label}: " + "; ".join(
            f"{(names or {}).get(k, k)} {v['n']} (TP {v['tp']}/SL {v['sl']}"
            + (f", {v['avg_R']:+.2f}R" if v["avg_R"] is not None else "") + ")" for k, v in groups.items())

    a = st["all"]
    repeats = f", {st['repeats']} pengulangan tidak dihitung" if st["repeats"] else ""
    lines = [f"🛰️ Screener 24/5 (catat-saja, tanpa AI) {days} hari terakhir: {st['candidates']} kandidat{repeats}",
             f"Harga tersentuh {a['touched']} | TP 1,5R {a['tp']} | SL {a['sl']} | ambiguous {a['ambiguous']} | "
             f"tidak pernah aktif {a['never_active']} | belum selesai {a['open']}"
             + (f" | rata-rata {a['avg_R']:+.2f}R" if a["avg_R"] is not None else ""),
             line("Per pair", st["by_pair"]), line("Per strategi", st["by_strategy"]),
             line("Per regime", st["by_regime"]), line("Per sesi", st["by_session"]),
             line("Nasib", st["by_fate"], SCREEN_FATE_LABEL)]
    return "\n".join(lines)


def format_opportunity_stats(st: dict, days: int) -> str:
    if not st.get("detected"):
        return f"🔍 Peluang {days} hari terakhir: belum ada yang tercatat."
    tiers = ", ".join(f"{k} {v}" for k, v in st["by_tier"].items())
    repeats = f", {st['repeats']} pengulangan ide yang sama tidak dihitung" if st["repeats"] else ""
    lines = [f"🔍 Peluang {days} hari terakhir: {st['ideas']} ide ({tiers}){repeats}",
             "Corong: " + "; ".join(f"{STAGE_LABEL[k]} {v}" for k, v in st["funnel"].items())]
    if st["simulated_resolved"] or st["ambiguous"]:
        lines.append(f"Simulasi M15 ({st['simulated_resolved']} selesai, {st['not_reached']} zona tidak tersentuh, "
                     f"{st['ambiguous']} ambiguous): peluang terlewat yang menang {st['missed_winners']}, rugi yang "
                     f"terhindar {st['avoided_losers']}, false positive {st['false_positives']}")
        if st["missed_winners_by_stage"]:
            lines.append("Peluang menang hilang di tahap: " + "; ".join(
                f"{STAGE_LABEL[k]} {v}" for k, v in st["missed_winners_by_stage"].items()))
        fmt = lambda v: f"{v:+.2f}R" if v is not None else "-"   # noqa: E731
        lines.append(f"Rata-rata simulasi: ditawarkan {fmt(st['sim_avg_R_offered'])} vs disaring "
                     f"{fmt(st['sim_avg_R_filtered'])} (disaring lebih bagus = filter terlalu ketat)")
        if st["tp2_after_tp1"]["n"]:
            lines.append(f"TP2 setelah TP1 (SL awal): {st['tp2_after_tp1']['reached']}/{st['tp2_after_tp1']['n']}")
    if st["inactivity_reasons"]:
        lines.append("Alasan tidak trade: " + "; ".join(f"{k} {v}x" for k, v in st["inactivity_reasons"].items()))
    if set(st.get("by_model", {})) - {"?"}:
        lines.append("Model: " + "; ".join(f"{k} {v}" for k, v in st["by_model"].items()))
    return "\n".join(lines)
