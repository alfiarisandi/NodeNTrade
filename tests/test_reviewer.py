import json

from bot.ai.client import LLMError
from bot.ai.reviewer import review_signal


class FakeLLM:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc = reply, exc

    def complete(self, system, user):
        if self.exc:
            raise self.exc
        return self.reply


def _reply(**kw):
    base = {"decision": "approve", "size_multiplier": 1.0, "confidence": 0.7, "reasons": ["a"], "risks": []}
    base.update(kw)
    return json.dumps(base)


def test_approve_full_size():
    out = review_signal(FakeLLM(_reply()), "{}")
    assert (out.decision, out.multiplier) == ("approve", 1.0)


def test_multiplier_above_one_is_capped():
    assert review_signal(FakeLLM(_reply(size_multiplier=3)), "{}").multiplier == 1.0


def test_reduce_bounds():
    assert review_signal(FakeLLM(_reply(decision="reduce", size_multiplier=0.5)), "{}").multiplier == 0.5
    assert review_signal(FakeLLM(_reply(decision="reduce", size_multiplier=0.05)), "{}").multiplier == 0.25
    assert review_signal(FakeLLM(_reply(decision="reduce", size_multiplier=0.9)), "{}").multiplier == 0.75
    assert review_signal(FakeLLM(_reply(decision="reduce", size_multiplier=1.0)), "{}").multiplier == 0.5


def test_reject_is_zero():
    assert review_signal(FakeLLM(_reply(decision="reject", size_multiplier=1)), "{}").multiplier == 0


def test_json_inside_code_fence():
    out = review_signal(FakeLLM("Here you go:\n```json\n" + _reply(decision="reduce", size_multiplier=0.5) + "\n```"), "{}")
    assert out.decision == "reduce"


def test_broken_json_skips():
    out = review_signal(FakeLLM('{"decision": "approve", '), "{}")
    assert (out.decision, out.multiplier) == ("error", 0.0)


def test_unknown_decision_skips():
    assert review_signal(FakeLLM(_reply(decision="double_down")), "{}").multiplier == 0.0


def test_timeout_skips_by_default():
    out = review_signal(FakeLLM(exc=LLMError("timeout")), "{}")
    assert (out.decision, out.multiplier) == ("error", 0.0)
    assert "timeout" in out.error


def test_rule_only_fallback():
    assert review_signal(FakeLLM(exc=LLMError("down")), "{}", on_failure="rule_only").multiplier == 1.0


def test_review_prompt_covers_structure_levels_and_timeframes():
    from bot.ai.prompts import review_system
    from bot.config import StrategyCfg

    s = review_system(StrategyCfg(entry_type="ichimoku", entry_tf="H4", trend_tf="D1", tp_atr_mult=0), 0.005)
    assert "D1 (trend), H4 (entry) and H1" in s and "Counter-trend against the D1 trend" in s
    for key in ("key_levels", "structure", "CHoCH", "candles_H4", "session", "DXY", "exits on its own signal"):
        assert key in s
    s2 = review_system(StrategyCfg(entry_tf="H1", trend_tf="D1", tp_r=2.0), 0.01)
    assert "D1 (trend), H1 (entry)" in s2 and "and H1" not in s2 and "take profit are fixed" in s2
