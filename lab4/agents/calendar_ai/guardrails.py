"""Guardrails для агентів: бюджет токенів, захист від prompt injection і політика викликів інструментів."""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("guardrails")


# ---------- Рівень 1: витрати токенів ----------

class BudgetExceeded(Exception):
    pass


@dataclass
class TokenBudget:
    """Бюджет токенів однієї задачі агента.

    Перед викликом моделі оцінюємо розмір запиту і резервуємо max_output: якщо разом із вже витраченим
    це більше за ліміт, виклик не робиться. Після виклику враховуємо фактичне usage з відповіді API.
    """

    limit: int
    max_output: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    steps: list[dict] = field(default_factory=list)

    @property
    def used(self) -> int:
        # total_tokens, а не prompt + completion: Gemini не включає токени міркувань у completion_tokens.
        return self.total_tokens

    @staticmethod
    def estimate(messages: list[dict], tools: list[dict] | None) -> int:
        # Груба оцінка без токенізатора конкретної моделі: ~3 символи JSON на токен (кирилиця «дорожча»).
        size = len(json.dumps(messages, ensure_ascii=False, default=str))
        size += len(json.dumps(tools or [], ensure_ascii=False))
        return size // 3

    def reserve(self, messages: list[dict], tools: list[dict] | None) -> int:
        estimate = self.estimate(messages, tools)
        if self.used + estimate + self.max_output > self.limit:
            raise BudgetExceeded(
                f"бюджет {self.limit} токенів: витрачено {self.used}, наступний виклик потребує "
                f"~{estimate} + {self.max_output} на відповідь"
            )
        return estimate

    def record(self, usage: Any, estimate: int) -> dict:
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        total = getattr(usage, "total_tokens", 0) or prompt + completion
        details = getattr(usage, "completion_tokens_details", None)
        # OpenAI повідомляє reasoning_tokens (вони входять у completion); Gemini — лише через різницю в total.
        reasoning = (getattr(details, "reasoning_tokens", 0) or 0) if details else 0
        reasoning = reasoning or max(0, total - prompt - completion)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.reasoning_tokens += reasoning
        self.total_tokens += total
        self.calls += 1
        step = {"call": self.calls, "estimate": estimate, "prompt": prompt, "completion": completion,
                "reasoning": reasoning, "total": self.used}
        self.steps.append(step)
        return step

    def summary(self) -> str:
        reasoning = f", міркування {self.reasoning_tokens}" if self.reasoning_tokens else ""
        return (f"викликів моделі {self.calls}; токени: запит {self.prompt_tokens}, відповідь "
                f"{self.completion_tokens}{reasoning}; разом {self.used} з {self.limit}")


def clip(text: str, limit: int) -> str:
    """Обрізає відповідь інструмента, щоб великий список не з'їв бюджет контексту."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [обрізано guardrail: {len(text) - limit} символів]"


# ---------- Рівень 3: prompt injection ----------

INJECTION_PATTERNS: dict[str, re.Pattern] = {name: re.compile(pattern, re.IGNORECASE | re.UNICODE) for name, pattern in {
    "ignore-instructions": r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instruction|prompt|rule|guideline|context)s?\b",
    "ignore-instructions-uk": r"(ігнору|знехту|забудь|забуд|відкин|не\s+зважа)\w*.{0,40}(інструкці|вказівк|правил|промпт|обмежен)",
    "ignore-instructions-ru": r"(игнорир|забудь|забуд|отбрось)\w*.{0,40}(инструкц|указани|правил|промпт)",
    "role-override": r"\b(you\s+are\s+now|from\s+now\s+on\s+you|act\s+as\s+(an?\s+)?(admin|system|developer))\b|"
                     r"(ти\s+тепер|відтепер\s+ти|ты\s+теперь)",
    "fake-system": r"(^|\n|\s)(system|assistant|developer)\s*:|\bsystem\s+(prompt|message|override)\b|"
                   r"системн\w*\s+(промпт|повідомлен|інструкці)|нов\w*\s+інструкці|new\s+instructions?\s*:",
    "chat-markup": r"<\|?\s*(im_start|im_end|system|endoftext)\s*\|?>|\[/?(INST|SYS)\]|</?(tool_output|system)>",
    "exfiltration": r"\b(reveal|print|show|send|leak)\b.{0,30}\b(system\s+prompt|instructions|api[\s_-]?key|token|password)s?\b|"
                    r"(покажи|виведи|надішли)\w*.{0,30}(системн\w*\s+промпт|ключ|токен|парол)",
}.items()}

INVISIBLE = {"Cf", "Cc", "Co"}  # керівні, приватні й невидимі символи (zero-width, bidi-override)


def normalize(text: str) -> str:
    """Прибирає невидимі символи, якими маскують інструкції, і нормалізує Unicode (NFKC)."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in INVISIBLE)


def find_injection(text: str) -> list[str]:
    clean = normalize(text)
    return [name for name, pattern in INJECTION_PATTERNS.items() if pattern.search(clean)]


REDACTED = "[вилучено guardrail: текст схожий на інструкції для моделі]"


def sanitize(value: Any, path: str = "$", findings: list[dict] | None = None) -> tuple[Any, list[dict]]:
    """Обходить відповідь API: рядки з ознаками prompt injection замінюються маркером.

    Дані інструментів (назви подій, імена) пишуть інші користувачі, тому вони — недовірений вхід
    (indirect prompt injection). Модель отримує факт, що поле вилучено, а не саму «інструкцію».
    """
    findings = [] if findings is None else findings
    if isinstance(value, str):
        hits = find_injection(value)
        if hits:
            findings.append({"path": path, "patterns": hits})
            return REDACTED, findings
        return normalize(value), findings
    if isinstance(value, dict):
        return {key: sanitize(item, f"{path}.{key}", findings)[0] for key, item in value.items()}, findings
    if isinstance(value, list):
        return [sanitize(item, f"{path}[{index}]", findings)[0] for index, item in enumerate(value)], findings
    return value, findings


def untrusted_tool_message(tool: str, payload: Any, limit: int) -> tuple[str, list[dict]]:
    """Spotlighting: результат інструмента явно позначено як дані, а не інструкції."""
    clean, findings = sanitize(payload)
    envelope = {"tool": tool, "trust": "untrusted-data", "data": clean}
    if findings:
        envelope["guardrail"] = {"injectionSuspected": findings}
    return clip(json.dumps(envelope, ensure_ascii=False, default=str), limit), findings


class InputRejected(Exception):
    pass


def check_user_input(text: str, max_chars: int) -> str:
    """Пряма prompt injection і завеликі запити відсікаються до виклику моделі (0 токенів)."""
    if len(text) > max_chars:
        raise InputRejected(f"запит довший за {max_chars} символів")
    hits = find_injection(text)
    if hits:
        raise InputRejected(f"запит схожий на prompt injection ({', '.join(hits)})")
    return normalize(text)


# ---------- Політика інструментів ----------

class ToolDenied(Exception):
    pass


DESTRUCTIVE_INTENT = re.compile(r"(скасу|відмін|видал|анулю|cancel|delete|remove)", re.IGNORECASE)


@dataclass
class ToolPolicy:
    """Деструктивну дію дозволено, лише якщо її явно просив користувач і він підтвердив виклик.

    Перевірка намірів дивиться на запит користувача, а не на контекст моделі: інструкція, підкинута через
    дані інструмента, не може «попросити» скасування від імені користувача.
    """

    user_request: str
    confirm: Callable[[str, dict], bool] | None = None
    denied: list[dict] = field(default_factory=list)

    def authorize(self, name: str, destructive: bool, args: dict) -> None:
        if not destructive:
            return
        if not DESTRUCTIVE_INTENT.search(self.user_request):
            self._deny(name, args, "користувач не просив деструктивної дії")
        if self.confirm is None or not self.confirm(name, args):
            self._deny(name, args, "користувач не підтвердив дію")

    def _deny(self, name: str, args: dict, reason: str) -> None:
        self.denied.append({"tool": name, "args": args, "reason": reason})
        log.warning("Відхилено виклик %s: %s", name, reason)
        raise ToolDenied(reason)
