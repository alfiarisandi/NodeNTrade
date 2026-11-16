"""Jurnal SQLite: sinyal (termasuk keputusan AI & shadow trade), trade nyata, dan snapshot equity."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction INTEGER NOT NULL,
    bar_time TEXT NOT NULL,
    entry_ref REAL,
    sl_distance REAL,
    tp_distance REAL,
    snapshot TEXT,
    ai_decision TEXT,
    ai_multiplier REAL,
    ai_confidence REAL,
    ai_reasons TEXT,
    ai_risks TEXT,
    ai_error TEXT,
    executed INTEGER DEFAULT 0,
    skip_reason TEXT,
    shadow_status TEXT DEFAULT 'none',   -- none | open | tp | sl | be | expired
    shadow_entry REAL,
    shadow_sl REAL,
    shadow_tp REAL,
    shadow_r REAL,
    UNIQUE(symbol, bar_time)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    ticket INTEGER UNIQUE,
    symbol TEXT NOT NULL,
    direction INTEGER NOT NULL,
    volume REAL,
    entry REAL,
    sl REAL,
    tp REAL,
    risk_amount REAL,
    open_time TEXT,
    close_time TEXT,
    close_price REAL,
    profit REAL,
    r_multiple REAL,
    status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT NOT NULL,
    balance REAL,
    equity REAL
);
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- setup | opportunity (AI) | screener (aturan A-E, catat-saja)
    strategy TEXT,
    regime TEXT,
    score REAL,                      -- 0-10
    tier TEXT,                       -- Strong | Moderate | Weak
    decision TEXT,                   -- offered | rejected | blocked | wait | no_trade
    reason TEXT,
    missing TEXT,                    -- JSON list konfirmasi yang belum ada
    activate_if TEXT,
    entry_low REAL,
    entry_high REAL,
    stop REAL,
    target REAL,
    rr REAL,
    cost_r REAL,
    valid_minutes INTEGER,
    signal_id INTEGER,               -- setup yang ditawarkan (signals.id)
    outcome TEXT DEFAULT 'open',     -- open | win | loss | timeout | not_reached | ambiguous | untestable | duplicate
    outcome_r REAL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Tambah kolom baru ke database lama tanpa menghapus data."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(signals)")}
        for col, typ in [("ref_price", "REAL"), ("user_decision", "TEXT"), ("user_risk", "REAL"),
                         ("confirm_msg_id", "INTEGER"), ("confirm_expires", "TEXT"),
                         ("source", "TEXT DEFAULT 'rule'"),   # rule = sinyal strategi | ai_setup = setup 📊 Analisis
                         ("setup", "TEXT"),                   # JSON setup Claude (zona, TP1/TP2, timeframe, alasan)
                         ("limit_ticket", "INTEGER"),         # order limit pending di MT5 (user_decision = 'limit')
                         ("limit_expires", "TEXT"),
                         ("ai_model", "TEXT"),                # model AI yang menjawab (review / analisis)
                         ("shadow_method", "TEXT")]:          # m15_zone = setup dinilai dari M15 + syarat zona;
            if col not in have:                               # NULL di setup lama = metode H4 lama (bias, audit H4)
                self.db.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(opportunities)")}
        for col, typ in [("model", "TEXT"),                  # model AI yang menghasilkan peluang / setup
                         ("duplicate_of", "INTEGER"),        # ide yang sama dengan baris ini (tidak dihitung ulang)
                         ("target2", "REAL"),
                         ("outcome_tp2", "TEXT"),            # TP1 tercapai lalu: win | loss | timeout | ambiguous
                         ("outcome_r_tp2", "REAL"),
                         ("time_wib", "TEXT"),               # waktu tercatat dalam WIB (created_at tetap UTC)
                         ("score_parts", "TEXT"),            # JSON komponen skor setup AI (data kalibrasi, audit H9)
                         ("snapshot_ref", "TEXT")]:          # file snapshot konteks + jawaban AI (relatif ke jurnal)
            if col not in have:
                self.db.execute(f"ALTER TABLE opportunities ADD COLUMN {col} {typ}")

    def get_signal(self, signal_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()

    def pending_confirmations(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM signals WHERE user_decision='pending'").fetchall()

    def limit_orders(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM signals WHERE user_decision='limit'").fetchall()

    # ---------- sinyal ----------
    def signal_exists(self, symbol: str, bar_time: str) -> bool:
        cur = self.db.execute("SELECT 1 FROM signals WHERE symbol=? AND bar_time=?", (symbol, bar_time))
        return cur.fetchone() is not None

    def log_signal(self, symbol: str, direction: int, bar_time: str, entry_ref: float,
                   sl_distance: float, tp_distance: float, snapshot: dict, source: str = "rule",
                   setup: dict | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO signals (created_at, symbol, direction, bar_time, entry_ref, sl_distance, tp_distance, snapshot,"
            " source, setup) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_now(), symbol, direction, bar_time, entry_ref, sl_distance, tp_distance, json.dumps(snapshot), source,
             json.dumps(setup, ensure_ascii=False) if setup is not None else None))
        self.db.commit()
        return cur.lastrowid

    def active_setups(self) -> int:
        """Posisi terbuka + order limit menunggu yang berasal dari setup 📊 Analisis."""
        return self.db.execute(
            "SELECT (SELECT COUNT(*) FROM trades t JOIN signals s ON s.id = t.signal_id"
            "        WHERE t.status='open' AND s.source='ai_setup')"
            " + (SELECT COUNT(*) FROM signals WHERE source='ai_setup' AND user_decision='limit')").fetchone()[0]

    def setups_executed_since(self, iso_ts: str) -> int:
        """Jumlah setup 📊 Analisis yang benar-benar terisi sejak iso_ts (kuota harian; limit menunggu tidak dihitung)."""
        return self.db.execute("SELECT COUNT(*) FROM signals WHERE source='ai_setup' AND created_at >= ?"
                               " AND executed=1",
                               (iso_ts,)).fetchone()[0]

    def update_signal(self, signal_id: int, **fields) -> None:
        for k in ("ai_reasons", "ai_risks"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE signals SET {cols} WHERE id=?", (*fields.values(), signal_id))
        self.db.commit()

    def open_shadow(self, signal_id: int, entry: float, sl: float, tp: float) -> None:
        self.update_signal(signal_id, shadow_status="open", shadow_entry=entry, shadow_sl=sl, shadow_tp=tp)

    def open_shadows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM signals WHERE shadow_status='open'").fetchall()

    def resolve_shadow(self, signal_id: int, status: str, r: float | None, method: str | None = None) -> None:
        self.update_signal(signal_id, shadow_status=status, shadow_r=r,
                           **({"shadow_method": method} if method else {}))

    def recent_signals(self, n: int = 5) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (n,)).fetchall()

    def signals_since(self, iso_ts: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM signals WHERE created_at >= ? ORDER BY id", (iso_ts,)).fetchall()

    # ---------- trade ----------
    def log_trade_open(self, signal_id: int, ticket: int, symbol: str, direction: int, volume: float,
                       entry: float, sl: float, tp: float, risk_amount: float) -> None:
        self.db.execute(
            "INSERT INTO trades (signal_id, ticket, symbol, direction, volume, entry, sl, tp, risk_amount, open_time)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (signal_id, ticket, symbol, direction, volume, entry, sl, tp, risk_amount, _now()))
        self.db.commit()

    def open_trades(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades WHERE status='open'").fetchall()

    def close_trade(self, ticket: int, close_price: float, close_time: str, profit: float) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM trades WHERE ticket=?", (ticket,)).fetchone()
        r = profit / row["risk_amount"] if row["risk_amount"] else None
        self.db.execute(
            "UPDATE trades SET status='closed', close_price=?, close_time=?, profit=?, r_multiple=? WHERE ticket=?",
            (close_price, close_time, profit, r, ticket))
        self.db.commit()
        return self.db.execute("SELECT * FROM trades WHERE ticket=?", (ticket,)).fetchone()

    def recent_closed(self, n: int = 10) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY close_time DESC LIMIT ?", (n,)).fetchall()

    def closed_trades(self, source: str | None = None) -> list[sqlite3.Row]:
        """Trade tertutup (urut waktu tutup) + sumber sinyalnya; source = 'ai_setup' | 'rule' | None (semua)."""
        rows = self.db.execute(
            "SELECT t.*, COALESCE(s.source, 'rule') AS source, s.setup AS setup FROM trades t"
            " LEFT JOIN signals s ON s.id = t.signal_id WHERE t.status='closed' ORDER BY t.close_time").fetchall()
        return [r for r in rows if source is None or r["source"] == source]

    def trades_since(self, iso_ts: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM trades WHERE open_time >= ? OR close_time >= ? ORDER BY id", (iso_ts, iso_ts)).fetchall()

    # ---------- peluang terdeteksi (tahap deteksi analisis AI) ----------
    def log_opportunity(self, **fields) -> int:
        for k in ("missing", "score_parts"):
            if isinstance(fields.get(k), (list, dict)):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        fields.setdefault("created_at", _now())
        fields.setdefault("time_wib", (datetime.fromisoformat(fields["created_at"]) + timedelta(hours=7))
                          .strftime("%Y-%m-%d %H:%M"))
        cols = ", ".join(fields)
        cur = self.db.execute(f"INSERT INTO opportunities ({cols}) VALUES ({', '.join('?' * len(fields))})",
                              tuple(fields.values()))
        self.db.commit()
        return cur.lastrowid

    def update_opportunity(self, opp_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE opportunities SET {cols} WHERE id=?", (*fields.values(), opp_id))
        self.db.commit()

    def open_opportunities(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM opportunities WHERE outcome='open' ORDER BY id").fetchall()

    def resolve_opportunity(self, opp_id: int, outcome: str, r: float | None, outcome_tp2: str | None = None,
                            r_tp2: float | None = None) -> None:
        self.db.execute("UPDATE opportunities SET outcome=?, outcome_r=?, outcome_tp2=?, outcome_r_tp2=? WHERE id=?",
                        (outcome, r, outcome_tp2, r_tp2, opp_id))
        self.db.commit()

    def opportunities_since(self, iso_ts: str) -> list[sqlite3.Row]:
        """Peluang sejak iso_ts + nasib setupnya di tombol konfirmasi (user_decision) dan apakah benar-benar terisi."""
        return self.db.execute(
            "SELECT o.*, COALESCE(s.executed, 0) AS executed, s.user_decision AS user_decision FROM opportunities o"
            " LEFT JOIN signals s ON s.id = o.signal_id WHERE o.created_at >= ? ORDER BY o.id", (iso_ts,)).fetchall()

    # ---------- state (circuit breaker, laporan terakhir, dll) ----------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    # ---------- equity ----------
    def equity_since(self, iso_ts: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM equity WHERE ts >= ? ORDER BY ts", (iso_ts,)).fetchall()

    def log_equity(self, balance: float, equity: float) -> None:
        self.db.execute("INSERT INTO equity VALUES (?,?,?)", (_now(), balance, equity))
        self.db.commit()

    # ---------- evaluasi ----------
    def ai_vs_rules(self) -> dict:
        """Bandingkan hasil (dalam R) sinyal yang diloloskan vs ditolak AI.
        Sinyal dry-run/ditolak dinilai dari shadow trade; trade nyata dari r_multiple."""
        # Shadow setup AI dengan metode lama (bar H4, tanpa syarat zona, shadow_method NULL) tidak dihitung: hasilnya
        # bias (audit 2026-11-16 H4). Datanya tetap disimpan, tidak ditulis ulang. Status tanpa R (not_reached,
        # ambiguous, untestable) juga tidak dihitung.
        rows = self.db.execute("""
            SELECT s.ai_decision, s.user_decision, s.executed, s.shadow_r, s.source, t.r_multiple
            FROM signals s LEFT JOIN trades t ON t.signal_id = s.id
            WHERE t.status = 'closed' OR (s.shadow_r IS NOT NULL
                  AND s.shadow_status NOT IN ('none', 'open', 'expired')
                  AND NOT (s.source = 'ai_setup' AND s.shadow_method IS NULL))
        """).fetchall()
        groups: dict[str, list[float]] = {}
        for r in rows:
            val = r["r_multiple"] if r["r_multiple"] is not None else r["shadow_r"]
            if r["source"] == "ai_setup":   # setup Claude dinilai terpisah dari sinyal strategi
                groups.setdefault("setup_ai_semua", []).append(val)
                if r["user_decision"] in ("approved", "declined", "expired", "blocked"):
                    groups.setdefault(f"setup_ai_user_{r['user_decision']}", []).append(val)
                continue
            key = {"reject": "ditolak_ai", "news_block": "diblok_berita",
                   "shadow_mode": "mode_shadow"}.get(r["ai_decision"], "diloloskan")
            groups.setdefault(key, []).append(val)
            if r["user_decision"] in ("approved", "declined", "expired"):
                groups.setdefault(f"user_{r['user_decision']}", []).append(val)
        allr = [v for k, vs in groups.items() if not k.startswith(("user_", "setup_ai")) for v in vs]
        summary = {k: {"n": len(v), "total_R": round(sum(v), 2), "avg_R": round(sum(v) / len(v), 3)}
                   for k, v in groups.items()}
        if allr:
            summary["semua_sinyal_aturan_saja"] = {"n": len(allr), "total_R": round(sum(allr), 2),
                                                  "avg_R": round(sum(allr) / len(allr), 3)}
        return summary

    def ai_dataset(self) -> list[sqlite3.Row]:
        """Data kalibrasi skor AI (audit H9 opsi B): tiap setup / peluang AI (bukan pengulangan) dengan komponen skor,
        model, snapshot, keputusan, nasib di tombol, hasil simulasi M15 dan R trade nyata bila dieksekusi."""
        return self.db.execute("""
            SELECT o.id, o.time_wib, o.symbol, o.direction, o.kind, o.strategy, o.regime, o.score, o.tier,
                   o.score_parts, o.model, o.snapshot_ref, o.decision, o.reason, o.missing, o.entry_low, o.entry_high,
                   o.stop, o.target, o.rr, o.cost_r, o.outcome AS sim_outcome, o.outcome_r AS sim_r,
                   o.outcome_tp2 AS sim_tp2, s.user_decision, COALESCE(s.executed, 0) AS executed,
                   t.status AS trade_status, t.r_multiple AS actual_r, t.profit AS actual_profit
            FROM opportunities o
            LEFT JOIN signals s ON s.id = o.signal_id
            LEFT JOIN trades t ON t.signal_id = o.signal_id
            WHERE o.kind IN ('setup', 'opportunity') AND o.duplicate_of IS NULL
            ORDER BY o.id""").fetchall()

    def shadow_mode_signals(self) -> list[sqlite3.Row]:
        """Sinyal strategi aturan di pair mode shadow (symbols[].rule_mode): dicatat + shadow trade, tanpa order."""
        return self.db.execute("SELECT * FROM signals WHERE ai_decision='shadow_mode' ORDER BY id").fetchall()

    def export_csv(self, out_dir: str | Path) -> list[Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for table in ("signals", "trades", "equity", "opportunities"):
            cur = self.db.execute(f"SELECT * FROM {table}")
            path = out_dir / f"{table}.csv"
            with path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow([d[0] for d in cur.description])
                w.writerows(cur.fetchall())
            paths.append(path)
        rows = self.ai_dataset()   # data kalibrasi skor AI (audit H9): satu baris per ide, hasil simulasi + nyata
        path = out_dir / "ai_dataset.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(rows[0].keys() if rows else ["id"])
            w.writerows(tuple(r) for r in rows)
        paths.append(path)
        return paths
