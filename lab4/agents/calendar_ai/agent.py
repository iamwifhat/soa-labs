"""Цикл агента з function calling: модель обирає інструменти, програма їх виконує, доки задачу не завершено."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from .config import Limits
from .guardrails import BudgetExceeded, InputRejected, TokenBudget, check_user_input
from .llm import ChatModel

log = logging.getLogger("agent")


@dataclass
class AgentResult:
    answer: str
    stop_reason: str              # completed | budget | max_steps | max_tool_calls | input_rejected | length
    budget: TokenBudget
    tool_calls: list[Any] = field(default_factory=list)
    denied: list[dict] = field(default_factory=list)


class ToolRunner(Protocol):
    """Виконавець інструментів: ToolBox (REST-клієнт) або McpToolRunner (інструменти MCP-сервера)."""

    trace: list[Any]

    def schemas(self) -> list[dict]: ...

    async def call(self, name: str, raw_arguments: str) -> str: ...


async def run_agent(model: ChatModel, system: str, user_text: str, tools: ToolRunner, limits: Limits,
                    on_step: Callable[[str, Any], Awaitable[None] | None] | None = None,
                    denied: list[dict] | None = None) -> AgentResult:
    budget = TokenBudget(limits.session_token_budget, limits.max_output_tokens)

    def result(answer: str, reason: str) -> AgentResult:
        return AgentResult(answer, reason, budget, list(tools.trace), list(denied or []))

    async def emit(kind: str, payload: Any) -> None:
        if on_step:
            maybe = on_step(kind, payload)
            if maybe is not None:
                await maybe

    try:
        user_text = check_user_input(user_text, limits.user_input_max_chars)
    except InputRejected as exc:
        await emit("rejected", str(exc))
        return result(f"Запит відхилено guardrail: {exc}.", "input_rejected")

    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user_text}]
    schemas = tools.schemas()
    calls = 0
    for _ in range(limits.max_steps):
        try:
            choice, usage = await model.complete(messages, schemas, budget)
        except BudgetExceeded as exc:
            await emit("budget", str(exc))
            return result(f"Зупинено guardrail: {exc}.", "budget")
        message = choice.message
        # model_dump зберігає й нестандартні поля провайдера (напр. thought signature у Gemini).
        messages.append(message.model_dump(exclude_none=True))
        await emit("llm", {"usage": usage, "tool_calls": [
            {"name": call.function.name, "arguments": call.function.arguments} for call in message.tool_calls or []
        ], "content": message.content})
        if not message.tool_calls:
            reason = "length" if choice.finish_reason == "length" else "completed"
            return result(message.content or "", reason)
        for call in message.tool_calls:
            calls += 1
            if calls > limits.max_tool_calls:
                content = json.dumps({"ok": False, "error": "tool_call_limit",
                                      "message": f"перевищено ліміт {limits.max_tool_calls} викликів"})
            else:
                content = await tools.call(call.function.name, call.function.arguments)
            await emit("tool", {"name": call.function.name, "result": content})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
        if calls > limits.max_tool_calls:
            return result(f"Зупинено guardrail: перевищено ліміт {limits.max_tool_calls} викликів інструментів.",
                          "max_tool_calls")
    # Ліміт кроків: просимо підсумок без інструментів, якщо бюджет дозволяє.
    try:
        choice, usage = await model.complete(messages + [{"role": "user", "content":
            "Ліміт кроків вичерпано. Підсумуй, що вже зроблено і що лишилося, без нових викликів."}],
            schemas, budget, tool_choice="none")
        await emit("llm", {"usage": usage, "tool_calls": [], "content": choice.message.content})
        return result(choice.message.content or "", "max_steps")
    except BudgetExceeded as exc:
        return result(f"Зупинено guardrail: ліміт кроків і {exc}.", "max_steps")
