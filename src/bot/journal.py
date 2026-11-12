"""Jurnal SQLite: sinyal (termasuk keputusan AI & shadow trade), trade nyata, dan snapshot equity."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Tambah kolom baru ke database lama tanpa menghapus data."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(signals)")}
        for col, typ in [("ref_price", "REAL"), ("user_decision", "TEXT"), ("user_risk", "REAL"),
                         ("confirm_msg_id", "INTEGER"), ("confirm_expires", "TEXT")]:
            if col not in have:
                self.db.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")

    def get_signal(self, signal_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()

    def pending_confirmations(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM signals WHERE user_decision='pending'").fetchall()

    # ---------- sinyal ----------
    def signal_exists(self, symbol: str, bar_time: str) -> bool:
        cur = self.db.execute("SELECT 1 FROM signals WHERE symbol=? AND bar_time=?", (symbol, bar_time))
        return cur.fetchone() is not None

    def log_signal(self, symbol: str, direction: int, bar_time: str, entry_ref: float,
                   sl_distance: float, tp_distance: float, snapshot: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO signals (created_at, symbol, direction, bar_time, entry_ref, sl_distance, tp_distance, snapshot)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (_now(), symbol, direction, bar_time, entry_ref, sl_distance, tp_distance, json.dumps(snapshot)))
        self.db.commit()
        return cur.lastrowid

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

    def resolve_shadow(self, signal_id: int, status: str, r: float | None) -> None:
        self.update_signal(signal_id, shadow_status=status, shadow_r=r)

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

    def trades_since(self, iso_ts: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM trades WHERE open_time >= ? OR close_time >= ? ORDER BY id", (iso_ts, iso_ts)).fetchall()

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
        rows = self.db.execute("""
            SELECT s.ai_decision, s.user_decision, s.executed, s.shadow_r, t.r_multiple
            FROM signals s LEFT JOIN trades t ON t.signal_id = s.id
            WHERE s.shadow_status NOT IN ('none', 'open', 'expired') OR t.status = 'closed'
        """).fetchall()
        groups: dict[str, list[float]] = {}
        for r in rows:
            val = r["r_multiple"] if r["r_multiple"] is not None else r["shadow_r"]
            key = {"reject": "ditolak_ai", "news_block": "diblok_berita"}.get(r["ai_decision"], "diloloskan")
            groups.setdefault(key, []).append(val)
            if r["user_decision"] in ("approved", "declined", "expired"):
                groups.setdefault(f"user_{r['user_decision']}", []).append(val)
        allr = [v for k, vs in groups.items() if not k.startswith("user_") for v in vs]
        summary = {k: {"n": len(v), "total_R": round(sum(v), 2), "avg_R": round(sum(v) / len(v), 3)}
                   for k, v in groups.items()}
        if allr:
            summary["semua_sinyal_aturan_saja"] = {"n": len(allr), "total_R": round(sum(allr), 2),
                                                  "avg_R": round(sum(allr) / len(allr), 3)}
        return summary

    def export_csv(self, out_dir: str | Path) -> list[Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for table in ("signals", "trades", "equity"):
            cur = self.db.execute(f"SELECT * FROM {table}")
            path = out_dir / f"{table}.csv"
            with path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow([d[0] for d in cur.description])
                w.writerows(cur.fetchall())
            paths.append(path)
        return paths
