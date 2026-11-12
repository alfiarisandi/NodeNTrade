"""Client Claude lewat 9Router (endpoint format Anthropic /v1/messages)."""
from __future__ import annotations

from typing import Protocol

import anthropic

from bot.config import AICfg


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class ClaudeLLM:
    def __init__(self, cfg: AICfg):
        missing = [k for k in ("base_url", "api_key", "model") if not getattr(cfg, k)]
        if missing:
            raise LLMError(f"Konfigurasi AI belum lengkap di .env: {', '.join(missing)}")
        self.cfg = cfg
        self.client = anthropic.Anthropic(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            timeout=cfg.timeout_seconds,
            max_retries=1,
        )

    def complete(self, system: str, user: str) -> str:
        try:
            resp = self.client.messages.create(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APITimeoutError as e:
            raise LLMError(f"timeout: {e}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"tidak bisa konek ke 9Router ({self.cfg.base_url}): {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"HTTP {e.status_code}: {str(e)[:300]}") from e

        if resp.stop_reason == "refusal":
            raise LLMError("model menolak menjawab (refusal)")
        if resp.stop_reason == "max_tokens":
            raise LLMError("jawaban terpotong (max_tokens)")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise LLMError("jawaban kosong")
        return text

    def list_models(self) -> list[str]:
        return [m.id for m in self.client.models.list()]
