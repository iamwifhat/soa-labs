#!/usr/bin/env python3
"""Завдання 2: LLM-хост працює з системою лише через MCP-сервер (без знання REST API).

    docker compose run --rm agent-cli python scripts/mcp_host_demo.py | tee docs/test-output/mcp_host_demo.txt
1) Streamable HTTP: хост бере промпт-шаблон schedule_meeting із сервера і виконує його інструментами сервера.
2) stdio: той самий сервер як підпроцес — порядок денний за шаблоном weekly_agenda.
3) Скасування: сервер просить підтвердження (elicitation), людина відмовляє — подія лишається.
"""
import asyncio
import sys
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, "/srv")
from calendar_ai import config  # noqa: E402
from calendar_ai.console import StepPrinter, print_result  # noqa: E402
from calendar_ai.llm import ChatModel  # noqa: E402
from calendar_ai.mcp_host import ask, connect  # noqa: E402

KYIV = ZoneInfo("Europe/Kyiv")
USER = f"artem.mcp.{uuid.uuid4().hex[:4]}@pm.me"
URL = f"{config.GATEWAY_URL}/mcp"
CHECKS = []


def check(name, condition):
    CHECKS.append(bool(condition))
    print(f"  [{'OK' if condition else 'FAIL'}] {name}")


async def events(calendar_id):
    async with httpx.AsyncClient(base_url=config.GATEWAY_URL, headers={"X-User-Id": USER}, timeout=20) as http:
        return (await http.get("/api/calendar/events", params={"calendarId": calendar_id})).json()


async def main():
    model, limits = ChatModel(config.llm_config()), config.limits()
    printer = StepPrinter()
    async with httpx.AsyncClient(base_url=config.GATEWAY_URL, headers={"X-User-Id": USER}, timeout=20) as http:
        calendar = (await http.post("/api/calendar/calendars", json={"name": "Навчання"})).json()
    day = datetime.now(KYIV) + timedelta(days=2)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    print(f"Модель {model.label}; користувач {USER}; MCP {URL}")

    print("\n=== 1. Streamable HTTP: промпт-шаблон schedule_meeting від сервера -> інструменти сервера ===")
    async with connect(URL, USER, auto_confirm=False) as client:
        prompt = await client.get_prompt("schedule_meeting", {
            "title": "Захист ЛР4", "participants": "ihor@pm.me", "start": f"{day:%d.%m.%Y} 14:00",
            "duration_minutes": "45"})
    text = prompt.messages[0].content.text
    print(f"  prompts/get schedule_meeting -> {len(text)} символів: «{text.splitlines()[0]}»")
    result = await ask(text, USER, url=URL, model=model, limits=limits, printer=printer)
    print_result(result)
    created = [e for e in await events(calendar["id"]) if e["title"] == "Захист ЛР4"]
    check("подію створено через MCP-інструмент schedule_event і SAGA завершено",
          created and created[0]["status"] == "Scheduled"
          and any(c.tool == "schedule_event" and c.ok for c in result.tool_calls))

    print("\n=== 2. stdio: сервер як підпроцес хоста, шаблон weekly_agenda ===")
    async with connect(None, USER, auto_confirm=False) as client:
        agenda = (await client.get_prompt("weekly_agenda", {"calendar_name": "Навчання", "days": "7"}))
    text = agenda.messages[0].content.text
    result = await ask(text, USER, url=None, model=model, limits=limits, printer=printer)
    print_result(result)
    check("через stdio хост отримав події і склав порядок денний",
          result.stop_reason == "completed" and "Захист ЛР4" in result.answer)

    print("\n=== 3. Деструктивний інструмент: elicitation, людина відмовляє ===")
    result = await ask(f"Скасуй подію «Захист ЛР4» у календарі «Навчання».", USER, url=URL, model=model,
                       limits=limits, auto_confirm=False, printer=printer)
    print_result(result)
    after = [e for e in await events(calendar["id"]) if e["title"] == "Захист ЛР4"]
    check("сервер запитав підтвердження, після відмови подія Scheduled",
          any(c.tool == "cancel_event" for c in result.tool_calls) and after[0]["status"] == "Scheduled")

    await model.close()
    print(f"\nПідсумок: {sum(CHECKS)} з {len(CHECKS)} перевірок пройдено")


if __name__ == "__main__":
    asyncio.run(main())
