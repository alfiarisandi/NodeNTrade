"""Laporan harian dari Claude untuk Telegram."""
from __future__ import annotations

import json

from bot.ai.client import LLM
from bot.ai.prompts import DAILY_SYSTEM, WEEKLY_SYSTEM


def daily_report(llm: LLM, date_str: str, account: dict, signals: list[dict], trades: list[dict],
                 markets: dict[str, dict], ai_vs_rules: dict, calendar: list[dict] | None = None,
                 results: dict | None = None) -> str:
    payload = {
        "date_utc": date_str,
        "account": account,
        "results_today_closed_trades": results or {"trades": 0},
        "signals_today": signals,
        "trades_today": trades,
        "market_by_symbol": markets,
        "ai_filter_track_record_so_far_R": ai_vs_rules,
        "high_impact_events_next_48h": calendar if calendar is not None else "calendar unavailable",
    }
    return llm.complete(DAILY_SYSTEM, json.dumps(payload, ensure_ascii=False, default=str))


def weekly_report(llm: LLM, stats: dict) -> str:
    return llm.complete(WEEKLY_SYSTEM, json.dumps(stats, ensure_ascii=False, default=str))
