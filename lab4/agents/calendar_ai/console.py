"""Вивід кроків агента в термінал: запит, виклики моделі з usage, інструменти та результат."""
from __future__ import annotations

import json
import sys
from typing import Any

from .agent import AgentResult


def short(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def args_text(arguments: Any) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return short(arguments, 160)
    return short(json.dumps(arguments, ensure_ascii=False), 200)


class StepPrinter:
    def __init__(self, stream=sys.stdout) -> None:
        self.stream = stream

    def __call__(self, kind: str, payload: Any) -> None:
        out = self.stream
        if kind == "llm":
            u = payload["usage"]
            reasoning = f", міркування {u['reasoning']}" if u["reasoning"] else ""
            print(f"  LLM #{u['call']}: запит {u['prompt']} (оцінка {u['estimate']}), відповідь {u['completion']}"
                  f"{reasoning}; накопичено {u['total']}", file=out)
            for call in payload["tool_calls"]:
                print(f"    -> {call['name']}({args_text(call['arguments'])})", file=out)
        elif kind == "tool":
            data = json.loads(payload["result"]) if payload["result"].startswith("{") else {}
            if data.get("ok") is False:
                print(f"    <- {payload['name']}: ПОМИЛКА {data.get('error')}: {short(data.get('message', ''), 160)}",
                      file=out)
            else:
                guard = data.get("guardrail")
                note = f"  [guardrail: вилучено {len(guard['injectionSuspected'])} поле(я)]" if guard else ""
                print(f"    <- {payload['name']}: {short(json.dumps(data.get('data'), ensure_ascii=False), 150)}{note}",
                      file=out)
        elif kind in ("budget", "rejected"):
            print(f"  GUARDRAIL: {payload}", file=out)
        out.flush()


def print_result(result: AgentResult, stream=sys.stdout) -> None:
    print(f"  Відповідь ({result.stop_reason}): {result.answer.strip()}", file=stream)
    for item in result.denied:
        print(f"  Політика відхилила {item['tool']}: {item['reason']}", file=stream)
    print(f"  Підсумок: {result.budget.summary()}; викликів інструментів {len(result.tool_calls)}", file=stream)
