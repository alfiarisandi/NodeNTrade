"""Client Claude lewat 9Router (endpoint format Anthropic /v1/messages)."""
from __future__ import annotations

import logging
import time
from typing import Protocol

import anthropic

from bot.config import AICfg


log = logging.getLogger(__name__)
RATE_LIMIT_COOLDOWN = 600   # detik: model yang kena 429 dicoba paling akhir selama ini


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
            max_retries=0,   # retry = model cadangan; retry SDK pada timeout menggandakan waktu tunggu (16/11: ±6 menit)
        )

        self.last_model = cfg.model   # model yang benar-benar menjawab permintaan terakhir
        self.rate_limited: dict[str, float] = {}   # model -> waktu (monotonic) rate limit berakhir

    def complete(self, system: str, user: str) -> str:
        """Coba model utama; jika gagal (limit, kuota, error provider) lanjut ke ai.fallback_models berurutan."""
        models = [self.cfg.model] + [m for m in self.cfg.fallback_models if m and m != self.cfg.model]
        # Model yang baru kena rate limit dicoba paling akhir (bukan dibuang): hemat waktu tanpa kehilangan cadangan
        now = time.monotonic()
        cooled = [m for m in models if self.rate_limited.get(m, 0) > now]
        order = [m for m in models if m not in cooled] + cooled
        first_error = None
        for i, model in enumerate(order):
            try:
                text = self._complete(model, system, user, self.cfg.max_tokens if model == self.cfg.model else
                                      max(self.cfg.max_tokens, self.cfg.fallback_max_tokens))
            except LLMError as e:
                first_error = first_error or e
                if "429" in str(e) or "rate_limit" in str(e):
                    self.rate_limited[model] = time.monotonic() + RATE_LIMIT_COOLDOWN
                if i + 1 < len(order):
                    log.warning("Model %s gagal (%s), mencoba cadangan %s", model, str(e)[:160], order[i + 1])
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
