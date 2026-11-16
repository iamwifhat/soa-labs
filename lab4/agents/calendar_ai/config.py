"""Налаштування з оточення (12-factor): адреса шлюзу, модель LLM і межі guardrails."""
import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class LLMConfig:
    """Будь-який OpenAI-сумісний Chat Completions API: Gemini, OpenAI, Mistral, Ollama, vLLM."""

    base_url: str
    api_key: str
    model: str
    reasoning_effort: str | None
    temperature: float

    @property
    def is_reasoning(self) -> bool:
        return self.reasoning_effort not in (None, "", "none")


@dataclass(frozen=True)
class Limits:
    """Guardrails витрат: межі на виклик, на сесію і на розмір даних, що потрапляють у контекст моделі."""

    max_output_tokens: int        # max_tokens одного виклику моделі
    session_token_budget: int     # сумарний бюджет токенів однієї задачі агента
    max_steps: int                # кількість звернень до моделі в циклі агента
    max_tool_calls: int           # загальна кількість викликів інструментів
    tool_result_max_chars: int    # обрізання відповіді інструмента
    user_input_max_chars: int     # довжина запиту користувача


def llm_config() -> LLMConfig:
    return LLMConfig(
        base_url=os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        api_key=os.getenv("LLM_API_KEY", ""),
        model=os.getenv("LLM_MODEL", "gemini-2.5-flash"),
        reasoning_effort=os.getenv("LLM_REASONING_EFFORT") or None,
        temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
    )


def limits() -> Limits:
    return Limits(
        max_output_tokens=_int("LLM_MAX_OUTPUT_TOKENS", 2048),
        session_token_budget=_int("LLM_SESSION_TOKEN_BUDGET", 40000),
        max_steps=_int("AGENT_MAX_STEPS", 8),
        max_tool_calls=_int("AGENT_MAX_TOOL_CALLS", 12),
        tool_result_max_chars=_int("TOOL_RESULT_MAX_CHARS", 2500),
        user_input_max_chars=_int("USER_INPUT_MAX_CHARS", 1500),
    )


GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:10088")
CONSUL_URL = os.getenv("CONSUL_URL", "http://localhost:10500")
