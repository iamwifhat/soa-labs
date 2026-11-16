"""Завдання 2: MCP-хост — LLM-застосунок, що користується інструментами MCP-сервера через MCP-клієнт.

    python -m calendar_ai.mcp_host --url http://gateway/mcp --user artem@pm.me "Запит"
    python -m calendar_ai.mcp_host --stdio --user artem@pm.me "Запит"     # сервер як підпроцес

Хост не знає REST API: список інструментів і їхні схеми він отримує від сервера (tools/list), а цикл агента
з guardrails той самий, що й у завданні 1.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from typing import Any

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp_types import ElicitResult, TextContent

from . import config
from .agent import run_agent
from .console import StepPrinter, print_result
from .guardrails import ToolPolicy, untrusted_tool_message
from .llm import ChatModel
from .prompts import system_prompt
from .tools import ToolCallRecord, _llm_schema


class McpToolRunner:
    """Адаптер MCP -> function calling: схеми з tools/list, виклики через tools/call."""

    def __init__(self, client: Client, tools: list, policy: ToolPolicy, result_max_chars: int) -> None:
        self.client, self.tools, self.policy = client, {tool.name: tool for tool in tools}, policy
        self.result_max_chars = result_max_chars
        self.trace: list[ToolCallRecord] = []

    def schemas(self) -> list[dict]:
        return [{"type": "function", "function": {
            "name": tool.name, "description": tool.description or tool.title or tool.name,
            "parameters": _llm_schema(tool.input_schema)}} for tool in self.tools.values()]

    async def call(self, name: str, raw_arguments: str) -> str:
        started = time.perf_counter()
        tool = self.tools.get(name)
        try:
            args = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            args = None
        if tool is None or args is None:
            return self._error(name, raw_arguments, "invalid-call", "невідомий інструмент або аргументи не JSON", started)
        destructive = bool(tool.annotations and tool.annotations.destructive_hint)
        try:
            # Анотації сервера — лише підказка, тому політика хоста перевіряє наміри користувача сама.
            self.policy.authorize(name, destructive, args)
        except Exception as exc:  # noqa: BLE001 - ToolDenied
            return self._error(name, args, "denied", str(exc), started)
        result = await self.client.call_tool(name, args)
        text = "\n".join(block.text for block in result.content if isinstance(block, TextContent))
        if result.is_error:
            return self._error(name, args, "tool-error", text, started)
        payload = result.structured_content if result.structured_content is not None else text
        content, findings = untrusted_tool_message(name, payload, self.result_max_chars)
        self.trace.append(ToolCallRecord(name, args, True, "ok", int((time.perf_counter() - started) * 1000),
                                         findings, payload))
        return content

    def _error(self, name: str, args: Any, outcome: str, message: str, started: float) -> str:
        self.trace.append(ToolCallRecord(name, args, False, outcome, int((time.perf_counter() - started) * 1000)))
        return json.dumps({"tool": name, "ok": False, "error": outcome, "message": message[:500]}, ensure_ascii=False)


def elicitation_handler(auto: bool):
    """Elicitation від сервера (підтвердження скасування) показується людині, а не моделі."""
    async def handle(_context, params) -> ElicitResult:
        print(f"    ? MCP-сервер просить підтвердження: {params.message}")
        if auto or (sys.stdin.isatty() and input("      [y/N] ").strip().lower() in ("y", "yes", "так")):
            return ElicitResult(action="accept", content={"confirm": True})
        print("      відмовлено")
        return ElicitResult(action="decline")
    return handle


@contextlib.asynccontextmanager
async def connect(url: str | None, user: str, auto_confirm: bool):
    if url:
        http = httpx2.AsyncClient(headers={"X-User-Id": user}, timeout=httpx2.Timeout(30, read=300))
        transport = streamable_http_client(url, http_client=http)
        server: Any = transport
    else:
        http = None
        server = StdioServerParameters(command=sys.executable, args=["-m", "calendar_ai.mcp_server", "stdio"],
                                       env={**os.environ, "CALENDAR_USER_ID": user, "CONSUL_REGISTER": "0"})
    try:
        async with Client(server, elicitation_callback=elicitation_handler(auto_confirm)) as client:
            yield client
    finally:
        if http is not None:
            await http.aclose()


async def ask(prompt: str, user: str, *, url: str | None, model: ChatModel, limits: config.Limits,
              auto_confirm: bool = False, printer: StepPrinter | None = None):
    async with connect(url, user, auto_confirm) as client:
        tools = (await client.list_tools()).tools
        if printer:
            print(f"  MCP: сервер {client.server_info.name} {client.server_info.version}, інструментів {len(tools)}")
        policy = ToolPolicy(user_request=prompt, confirm=lambda *_: True)  # остаточне підтвердження — elicitation
        runner = McpToolRunner(client, tools, policy, limits.tool_result_max_chars)
        return await run_agent(model, system_prompt(user, model.config.is_reasoning), prompt, runner, limits,
                               on_step=printer, denied=policy.denied)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt")
    parser.add_argument("--user", default="artem@pm.me")
    parser.add_argument("--url", default=os.getenv("MCP_URL", f"{config.GATEWAY_URL}/mcp"))
    parser.add_argument("--stdio", action="store_true", help="запустити сервер підпроцесом (транспорт stdio)")
    parser.add_argument("--yes", action="store_true", help="підтверджувати elicitation автоматично")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    model, limits = ChatModel(config.llm_config()), config.limits()
    transport = "stdio" if args.stdio else f"Streamable HTTP {args.url}"
    print(f"Користувач {args.user}; модель {model.label}; MCP {transport}")
    print(f"> {args.prompt}")

    async def run() -> None:
        try:
            print_result(await ask(args.prompt, args.user, url=None if args.stdio else args.url, model=model,
                                   limits=limits, auto_confirm=args.yes, printer=StepPrinter()))
        finally:
            await model.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
