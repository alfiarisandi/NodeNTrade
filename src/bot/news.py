"""Kalender ekonomi (feed JSON ForexFactory minggu berjalan): filter entry di sekitar berita & konteks untuk Claude.

Feed ini tidak resmi dan hanya berisi jadwal (tanpa angka aktual). Jika gagal diambil, bot tetap jalan
dengan data cache terakhir, atau tanpa kalender sama sekali (ditandai available = False).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests

from bot.config import NewsCfg
from bot.risk import split_currencies

log = logging.getLogger(__name__)

# Emas tidak punya event sendiri di kalender; yang menggerakkannya adalah berita USD
CURRENCY_ALIASES = {"XAU": [], "XAG": []}


@dataclass
class Event:
    time: datetime          # UTC
    currency: str
    title: str
    impact: str             # High | Medium | Low | Holiday
    forecast: str = ""
    previous: str = ""

    def brief(self, now: datetime) -> dict:
        return {"currency": self.currency, "event": self.title, "impact": self.impact,
                "time_utc": self.time.strftime("%a %d %b %H:%M"),
                "hours_from_now": round((self.time - now).total_seconds() / 3600, 1),
                "forecast": self.forecast or None, "previous": self.previous or None}


def parse_events(raw: list[dict]) -> list[Event]:
    out = []
    for e in raw:
        try:
            t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        out.append(Event(t, e.get("country", ""), e.get("title", ""), e.get("impact", ""),
                         e.get("forecast", ""), e.get("previous", "")))
    return sorted(out, key=lambda ev: ev.time)


def pair_currencies(symbol: str) -> set[str]:
    cur: set[str] = set()
    for c in split_currencies(symbol):
        cur.update(CURRENCY_ALIASES.get(c, [c]))
    return cur | {"ALL"}


class Calendar:
    def __init__(self, cfg: NewsCfg, cache_path: Path, fetch: Callable[..., requests.Response] = requests.get):
        self.cfg = cfg
        self.cache_path = cache_path
        self.fetch = fetch
        self.events: list[Event] = []
        self.fetched_at: datetime | None = None
        self.available = False
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.events = parse_events(data["events"])
            self.fetched_at = datetime.fromisoformat(data["fetched_at"])
            self.available = bool(self.events)
        except (OSError, KeyError, ValueError):
            pass

    def refresh(self, now: datetime, force: bool = False) -> bool:
        """Ambil ulang feed bila sudah lewat refresh_minutes. Return True jika data baru berhasil diambil."""
        if not force and self.fetched_at and now - self.fetched_at < timedelta(minutes=self.cfg.refresh_minutes):
            return False
        try:
            r = self.fetch(self.cfg.url, timeout=20, headers={"User-Agent": "trading-bot/1.0"})
            r.raise_for_status()
            raw = r.json()
            events = parse_events(raw)
            if not events:
                raise ValueError("feed kosong")
        except Exception as e:  # jaringan, rate limit, format berubah
            log.warning("Kalender ekonomi gagal diambil: %s", e)
            # Coba lagi nanti, tapi jangan setiap siklus (feed membatasi jumlah request)
            self.fetched_at = now - timedelta(minutes=self.cfg.refresh_minutes) + timedelta(minutes=15)
            self.available = bool(self.events) and self._covers(now)
            return False
        self.events, self.fetched_at, self.available = events, now, True
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"fetched_at": now.isoformat(), "events": raw}), encoding="utf-8")
        except OSError as e:
            log.warning("Cache kalender tidak tersimpan: %s", e)
        return True

    def _covers(self, now: datetime) -> bool:
        """Data cache masih relevan jika minggu di feed belum lewat."""
        return bool(self.events) and self.events[-1].time >= now - timedelta(days=1)

    def for_pair(self, symbol: str, start: datetime, end: datetime, impacts: list[str]) -> list[Event]:
        cur = pair_currencies(symbol)
        return [e for e in self.events if e.currency in cur and e.impact in impacts and start <= e.time <= end]

    def blocking_event(self, symbol: str, now: datetime) -> Event | None:
        """Event high impact yang terlalu dekat untuk membuka posisi baru di pair ini."""
        if not (self.cfg.enabled and self.available):
            return None
        hits = self.for_pair(symbol, now - timedelta(hours=self.cfg.block_after_hours),
                             now + timedelta(hours=self.cfg.block_before_hours), self.cfg.block_impacts)
        return hits[0] if hits else None

    def context(self, symbol: str, now: datetime) -> dict:
        """Ringkasan kalender untuk prompt Claude."""
        if not self.available:
            return {"available": False, "note": "economic calendar unavailable; event risk unknown"}
        ahead = self.for_pair(symbol, now, now + timedelta(days=self.cfg.context_days), ["High", "Medium"])
        past = self.for_pair(symbol, now - timedelta(hours=24), now, ["High"])
        return {
            "available": True,
            "source": "ForexFactory weekly feed (schedule only, no actual results); covers this week only",
            "upcoming_high_medium": [e.brief(now) for e in ahead[:15]],
            "high_impact_last_24h": [e.brief(now) for e in past],
        }

    def upcoming_all(self, now: datetime, hours: float, currencies: set[str]) -> list[dict]:
        end = now + timedelta(hours=hours)
        return [e.brief(now) for e in self.events
                if e.impact == "High" and e.currency in currencies | {"ALL"} and now <= e.time <= end]
