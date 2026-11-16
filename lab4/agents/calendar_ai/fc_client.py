"""Завдання 1: LLM-клієнт, який через function calling користується REST-клієнтом календаря.

    python -m calendar_ai.fc_client --user artem@pm.me "Створи подію ..."
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import config
from .agent import AgentResult, run_agent
from .console import StepPrinter, args_text, print_result
from .guardrails import ToolPolicy
from .llm import ChatModel
from .prompts import system_prompt
from .rest_client import CalendarApi
from .tools import ToolBox


def console_confirm(auto_yes: bool):
    """Людина в контурі: деструктивний виклик виконується лише після підтвердження в терміналі."""
    def confirm(name: str, args: dict) -> bool:
        if auto_yes:
            print(f"    ? підтвердження {name}({args_text(args)}): так (--yes)")
            return True
        if not sys.stdin.isatty():
            print(f"    ? підтвердження {name}: немає термінала, відмовлено")
            return False
        return input(f"    ? Виконати {name}({args_text(args)})? [y/N] ").strip().lower() in ("y", "yes", "т", "так")
    return confirm


async def ask(prompt: str, user_id: str, *, model: ChatModel, limits: config.Limits, auto_yes: bool = False,
              printer: StepPrinter | None = None) -> AgentResult:
    policy = ToolPolicy(user_request=prompt, confirm=console_confirm(auto_yes))
    async with CalendarApi(config.GATEWAY_URL, user_id) as api:
        tools = ToolBox(api, policy, result_max_chars=limits.tool_result_max_chars)
        system = system_prompt(user_id, model.config.is_reasoning)
        return await run_agent(model, system, prompt, tools, limits, on_step=printer, denied=policy.denied)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt")
    parser.add_argument("--user", default="artem@pm.me", help="від чийого імені діє агент (X-User-Id)")
    parser.add_argument("--yes", action="store_true", help="підтверджувати деструктивні дії автоматично")
    parser.add_argument("--budget", type=int, help="бюджет токенів сесії")
    parser.add_argument("--max-steps", type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    limits = config.limits()
    if args.budget or args.max_steps:
        limits = config.Limits(**{**limits.__dict__, **({"session_token_budget": args.budget} if args.budget else {}),
                                  **({"max_steps": args.max_steps} if args.max_steps else {})})
    model = ChatModel(config.llm_config())
    print(f"Користувач {args.user}; модель {model.label}; бюджет {limits.session_token_budget} токенів")
    print(f"> {args.prompt}")

    async def run() -> None:
        try:
            print_result(await ask(args.prompt, args.user, model=model, limits=limits, auto_yes=args.yes,
                                   printer=StepPrinter()))
        finally:
            await model.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
