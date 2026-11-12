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
