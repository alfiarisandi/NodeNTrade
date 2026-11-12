"""Claude sebagai manajer risiko: menilai kandidat sinyal, hanya boleh mengurangi risiko."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from bot.ai.client import LLM
from bot.ai.prompts import REVIEW_SYSTEM

log = logging.getLogger(__name__)

MIN_MULT, MAX_REDUCE, MAX_MULT, DEFAULT_REDUCE = 0.25, 0.75, 1.0, 0.5


class Decision(BaseModel):
    decision: Literal["approve", "reduce", "reject"]
    size_multiplier: float = 1.0
    confidence: float = 0.5
    reasons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


@dataclass
class ReviewOutcome:
    decision: str                 # approve | reduce | reject | error
    multiplier: float             # 0 = jangan trade
    confidence: float | None = None
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    error: str | None = None
    raw: str = ""


def parse_decision(text: str) -> Decision:
    """Ambil objek JSON dari jawaban (toleran terhadap code fence / teks tambahan)."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("tidak ada JSON di jawaban")
    return Decision.model_validate(json.loads(m.group(0)))


def enforce_limits(d: Decision) -> float:
    """Batas keras: AI tidak bisa menaikkan risiko di atas ukuran normal (1.0)."""
    if d.decision == "reject":
        return 0.0
    if d.decision == "approve":
        return MAX_MULT
    if d.size_multiplier >= MAX_MULT:  # "reduce" tanpa angka yang masuk akal
        return DEFAULT_REDUCE
    return min(max(d.size_multiplier, MIN_MULT), MAX_REDUCE)


def review_signal(llm: LLM, context_json: str, on_failure: str = "skip",
                  system: str = REVIEW_SYSTEM) -> ReviewOutcome:
    raw = ""
    try:
        raw = llm.complete(system, f"Candidate trade and context (JSON):\n{context_json}")
        d = parse_decision(raw)
    except (ValueError, ValidationError, json.JSONDecodeError) as e:
        return _failed(f"jawaban AI tidak valid: {e}", raw, on_failure)
    except Exception as e:  # LLMError, timeout, koneksi, dll
        return _failed(str(e), raw, on_failure)

    return ReviewOutcome(
        decision=d.decision,
        multiplier=enforce_limits(d),
        confidence=min(max(d.confidence, 0.0), 1.0),
        reasons=d.reasons[:3],
        risks=d.risks[:3],
        raw=raw,
    )


def _failed(error: str, raw: str, on_failure: str) -> ReviewOutcome:
    log.warning("Review AI gagal: %s", error)
    mult = 1.0 if on_failure == "rule_only" else 0.0
    return ReviewOutcome(decision="error", multiplier=mult, error=error, raw=raw)
