from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

from bot.config import NewsCfg
from bot.news import Calendar, pair_currencies

NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)  # Jumat 09:00 UTC

FEED = [
    # NFP 13:30 UTC (08:30 ET) -> 4.5 jam lagi
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-11-06T08:30:00-05:00",
     "impact": "High", "forecast": "65K", "previous": "29K"},
    # BoE kemarin 12:00 UTC -> sudah lewat 21 jam
    {"title": "Official Bank Rate", "country": "GBP", "date": "2026-11-05T07:00:00-05:00",
     "impact": "High", "forecast": "4.00%", "previous": "3.75%"},
    # Event JPY 2 jam lagi, tapi Medium
    {"title": "Household Spending", "country": "JPY", "date": "2026-11-06T06:00:00-05:00",
     "impact": "Medium", "forecast": "", "previous": ""},
    {"title": "German Industrial Production", "country": "EUR", "date": "bukan-tanggal", "impact": "High"},
]


class FakeResp:
    def __init__(self, data, ok=True):
        self.data, self.ok = data, ok

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError("429 Too Many Requests")

    def json(self):
        return self.data


def make(tmp_path, data=FEED, ok=True, **cfg):
    calls = []

    def fetch(url, **kw):
        calls.append(url)
        return FakeResp(data, ok)

    cal = Calendar(NewsCfg(**cfg), tmp_path / "cal.json", fetch)
    cal.calls = calls
    return cal


def test_parse_and_skip_bad_dates(tmp_path):
    cal = make(tmp_path)
    assert cal.refresh(NOW)
    assert len(cal.events) == 3
    assert cal.events[0].time.tzinfo == timezone.utc


def test_blocks_before_high_impact_for_pair_currencies(tmp_path):
    cal = make(tmp_path)
    cal.refresh(NOW)
    # NFP 4.5 jam lagi: di luar jendela 4 jam
    assert cal.blocking_event("EURUSD.m", NOW) is None
    # 30 menit kemudian NFP tinggal 4 jam -> diblok, juga untuk emas (digerakkan USD)
    later = NOW + timedelta(minutes=31)
    assert cal.blocking_event("EURUSD.m", later).title == "Non-Farm Employment Change"
    assert cal.blocking_event("XAUUSD.m", later) is not None
    # EURGBP tidak punya USD; event Medium JPY tidak memblok
    assert cal.blocking_event("EURGBP", later) is None
    assert cal.blocking_event("EURJPY", later) is None


def test_blocks_shortly_after_event(tmp_path):
    cal = make(tmp_path)
    cal.refresh(NOW)
    nfp = datetime(2026, 11, 6, 13, 30, tzinfo=timezone.utc)
    assert cal.blocking_event("USDJPY", nfp + timedelta(minutes=50)) is not None
    assert cal.blocking_event("USDJPY", nfp + timedelta(minutes=70)) is None


def test_context_for_claude(tmp_path):
    cal = make(tmp_path)
    cal.refresh(NOW)
    ctx = cal.context("GBPUSD", NOW)
    assert ctx["available"]
    assert [e["event"] for e in ctx["upcoming_high_medium"]] == ["Non-Farm Employment Change"]
    assert ctx["upcoming_high_medium"][0]["hours_from_now"] == 4.5
    assert ctx["high_impact_last_24h"][0]["event"] == "Official Bank Rate"


def test_refresh_respects_interval_and_uses_cache(tmp_path):
    cal = make(tmp_path)
    cal.refresh(NOW)
    assert not cal.refresh(NOW + timedelta(minutes=30))
    assert len(cal.calls) == 1
    # instance baru (bot restart) langsung punya data dari cache tanpa fetch
    fresh = make(tmp_path, data=[], ok=False)
    assert fresh.available and len(fresh.events) == 3


def test_fetch_failure_without_cache_disables_filter(tmp_path):
    cal = make(tmp_path, ok=False)
    assert not cal.refresh(NOW)
    assert not cal.available
    assert cal.blocking_event("EURUSD", NOW) is None
    assert cal.context("EURUSD", NOW)["available"] is False
    # tidak mencoba ulang di setiap siklus bot
    assert not cal.refresh(NOW + timedelta(minutes=1))
    assert len(cal.calls) == 1


def test_disabled_never_blocks(tmp_path):
    cal = make(tmp_path, enabled=False)
    cal.refresh(NOW)
    assert cal.blocking_event("EURUSD", NOW + timedelta(minutes=31)) is None


def test_pair_currencies():
    assert pair_currencies("XAUUSD.m") == {"USD", "ALL"}
    assert pair_currencies("EURJPY") == {"EUR", "JPY", "ALL"}
