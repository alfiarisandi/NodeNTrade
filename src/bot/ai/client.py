"""Client Claude lewat 9Router (endpoint format Anthropic /v1/messages)."""
from __future__ import annotations

import logging
from typing import Protocol

import anthropic

from bot.config import AICfg


log = logging.getLogger(__name__)


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

        self.last_model = cfg.model   # model yang benar-benar menjawab permintaan terakhir

    def complete(self, system: str, user: str) -> str:
        """Coba model utama; jika gagal (limit, kuota, error provider) lanjut ke ai.fallback_models berurutan."""
        models = [self.cfg.model] + [m for m in self.cfg.fallback_models if m and m != self.cfg.model]
        first_error = None
        for i, model in enumerate(models):
            try:
                text = self._complete(model, system, user, self.cfg.max_tokens if i == 0 else
                                      max(self.cfg.max_tokens, self.cfg.fallback_max_tokens))
            except LLMError as e:
                first_error = first_error or e
                if i + 1 < len(models):
                    log.warning("Model %s gagal (%s), mencoba cadangan %s", model, str(e)[:160], models[i + 1])
                continue
            self.last_model = model
            return text
        raise LLMError(f"{first_error}" + (f" (semua {len(models)} model gagal)" if len(models) > 1 else ""))

    def _complete(self, model: str, system: str, user: str, max_tokens: int | None = None) -> str:
        try:
            resp = self.client.messages.create(
                model=model,
                max_tokens=max_tokens or self.cfg.max_tokens,
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
