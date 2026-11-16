"""Клієнт OpenAI-сумісного Chat Completions API з обліком токенів і повагою до лімітів провайдера."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, InternalServerError, RateLimitError

from .config import LLMConfig
from .guardrails import TokenBudget

log = logging.getLogger("llm")

RETRY_IN = re.compile(r"retry in ([\d.]+)\s*s", re.IGNORECASE)
MAX_ATTEMPTS = 4
MAX_WAIT_SECONDS = 90.0


class ChatModel:
    def __init__(self, config: LLMConfig) -> None:
        if not config.api_key:
            raise RuntimeError("LLM_API_KEY не задано: див. .env.example")
        self.config = config
        # Повтори робимо самі: при 429 провайдер каже, скільки чекати, а миттєвий повтор лише марнує квоту.
        self._client = AsyncOpenAI(base_url=config.base_url, api_key=config.api_key, timeout=90.0, max_retries=0)
        rpm = float(os.getenv("LLM_MAX_RPM", "0"))
        self._min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    @property
    def label(self) -> str:
        effort = self.config.reasoning_effort or "default"
        return f"{self.config.model} (reasoning_effort={effort})"

    async def _throttle(self) -> None:
        """Клієнтське обмеження частоти (LLM_MAX_RPM): не перевищувати ліміт тарифу провайдера."""
        async with self._lock:
            wait = self._last_call + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    async def complete(self, messages: list[dict], tools: list[dict] | None, budget: TokenBudget,
                       tool_choice: str = "auto") -> tuple[Any, dict]:
        """Один виклик моделі. Бюджет перевіряється до виклику, фактичне usage враховується після."""
        estimate = budget.reserve(messages, tools)
        params: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": budget.max_output,   # guardrail: стеля відповіді (разом із токенами міркувань)
        }
        if tools:
            params["tools"] = tools
            params["tool_choice"] = tool_choice
        if self.config.reasoning_effort:
            params["reasoning_effort"] = self.config.reasoning_effort
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._throttle()
            try:
                response = await self._client.chat.completions.create(**params)
                break
            except (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError) as exc:
                hint = RETRY_IN.search(str(exc))
                delay = float(hint.group(1)) + 1 if hint else 2.0 ** attempt
                if attempt == MAX_ATTEMPTS or delay > MAX_WAIT_SECONDS:
                    raise
                log.warning("LLM: %s, повтор %s через %.0f с", exc.__class__.__name__, attempt, delay)
                await asyncio.sleep(delay)
        step = budget.record(response.usage, estimate)
        choice = response.choices[0]
        log.info("LLM виклик %s: %s, finish=%s", step["call"], step, choice.finish_reason)
        return choice, step

    async def close(self) -> None:
        await self._client.close()
