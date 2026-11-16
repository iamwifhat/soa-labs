#!/usr/bin/env python3
"""Завдання 3: продюсер і споживач ЛР3 як AI-агенти, інтегровані через A2A.

    docker compose run --rm agent-cli python scripts/a2a_demo.py | tee docs/test-output/a2a_demo.txt
Організатор (artem@pm.me) просить агента-продюсера запланувати зустріч о 10:00. Асистент учасниці
(olena@pm.me, агент-споживач) отримує EventConfirmed.v1 з RabbitMQ, за її політикою (не раніше 12:00)
просить продюсера через A2A перенести зустріч, а після EventRescheduleConfirmed.v1 приймає запрошення.
"""
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, "/srv")
from calendar_ai import config  # noqa: E402
from calendar_ai.a2a_common import RemoteAgent, task_result  # noqa: E402

GATEWAY = config.GATEWAY_URL
PRODUCER, CONSUMER = f"{GATEWAY}/a2a/producer", f"{GATEWAY}/a2a/consumer"
ORGANIZER_KEY, ASSISTANT_KEY, OLENA_KEY = "dev-organizer-key", "dev-olena-assistant-key", "dev-olena-console-key"
ARTEM, OLENA = "artem@pm.me", "olena@pm.me"
KYIV = ZoneInfo("Europe/Kyiv")
RUN = uuid.uuid4().hex[:4]
CHECKS: list[tuple[str, bool]] = []


def check(name, condition):
    CHECKS.append((name, bool(condition)))
    print(f"  [{'OK' if condition else 'FAIL'}] {name}")


def title(text):
    print(f"\n=== {text} ===")


def local(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(KYIV).strftime("%d.%m %H:%M")


async def rest(method, path, user, **kwargs):
    async with httpx.AsyncClient(base_url=GATEWAY, timeout=30, headers={"X-User-Id": user}) as http:
        response = await http.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()


def next_weekday() -> datetime:
    day = datetime.now(KYIV) + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


async def main():
    title("1. Виявлення агентів: Consul (де) і Agent Card (що вміє)")
    async with httpx.AsyncClient(base_url=config.CONSUL_URL, timeout=5) as consul:
        catalog = (await consul.get("/v1/catalog/services")).json()
        for name in catalog:
            entries = (await consul.get(f"/v1/catalog/service/{name}")).json()
            meta = entries[0]["ServiceMeta"] if entries else {}
            if meta.get("protocol") in ("a2a", "mcp"):
                print(f"  Consul: {name:15} {entries[0]['ServiceAddress']}:{entries[0]['ServicePort']} "
                      f"protocol={meta['protocol']} {meta.get('card') or meta.get('path')}")
    cards = {}
    for base, key in ((PRODUCER, ORGANIZER_KEY), (CONSUMER, OLENA_KEY)):
        async with RemoteAgent(base, key) as agent:
            card = agent.card
            cards[base] = card
            print(f"  {card.name} v{card.version}: {card.supported_interfaces[0].protocol_binding} "
                  f"{card.supported_interfaces[0].url}; схема безпеки: "
                  f"{card.security_schemes['agentKey'].api_key_security_scheme.name}")
            for skill in card.skills:
                print(f"    skill {skill.id}: {skill.description[:95]}…")
    check("продюсер оголошує навички schedule_event і reschedule_event",
          {s.id for s in cards[PRODUCER].skills} == {"schedule_event", "reschedule_event"})
    check("адреса інтерфейсу в картці — та, через яку звернулися (шлюз)",
          cards[PRODUCER].supported_interfaces[0].url == f"{PRODUCER}/")

    title("2. Автентифікація і авторизація агентів")
    async with httpx.AsyncClient(timeout=10) as raw:
        response = await raw.post(f"{PRODUCER}/", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage"})
    print(f"  JSON-RPC без X-Agent-Key -> HTTP {response.status_code} {response.json()}")
    check("виклик без ключа агента відхилено (401)", response.status_code == 401)
    async with RemoteAgent(PRODUCER, ASSISTANT_KEY) as producer:
        task = task_result(await producer.send(text="Скасуй усі зустрічі Артема"))
    print(f"  olena-assistant, вільний текст -> {task['state']}: {task['status']}")
    check("асистент учасниці не може давати продюсеру довільних доручень", task["state"] == "TASK_STATE_REJECTED")

    calendars = await rest("GET", "/api/calendar/calendars", ARTEM)
    calendar = next((c for c in calendars if c["name"] == "Навчання"), None) or \
        await rest("POST", "/api/calendar/calendars", ARTEM, json={"name": "Навчання"})
    day = next_weekday()
    private_start = day.replace(hour=16, minute=0, second=0, microsecond=0)
    private = await rest("POST", "/api/calendar/events", ARTEM, json={
        "calendarId": calendar["id"], "title": f"Особиста зустріч {RUN}", "startAt": private_start.isoformat(),
        "endAt": (private_start + timedelta(hours=1)).isoformat(), "timeZone": "Europe/Kyiv",
        "inviteeUserIds": ["ihor@pm.me"]})
    async with RemoteAgent(PRODUCER, ASSISTANT_KEY) as producer:
        task = task_result(await producer.send(data={
            "skill": "reschedule_event", "eventId": private["event"]["eventId"],
            "proposedStartAt": (private_start + timedelta(hours=1)).isoformat(),
            "proposedEndAt": (private_start + timedelta(hours=2)).isoformat(), "reason": "хочу прийти"}))
    print(f"  olena-assistant просить перенести чужу зустріч -> {task['state']}: {task['status']}")
    check("перенести можна лише подію, на яку запрошено (перевірка в коді, до LLM)",
          task["state"] == "TASK_STATE_REJECTED")

    title("3. Організатор -> агент-продюсер: навичка schedule_event")
    meeting = f"Консультація з ЛР4 (A2A {RUN})"
    request = (f"Заплануй «{meeting}» на {day:%d.%m.%Y} з 10:00 до 11:00 у календарі «Навчання» "
               f"і запроси {OLENA}.")
    print(f"  > {request}")
    started = time.perf_counter()
    async with RemoteAgent(PRODUCER, ORGANIZER_KEY) as producer:
        task = task_result(await producer.send(text=request))
    if not task["artifacts"]:
        check(f"навичка виконана (стан {task['state']}: {task['status']})", False)
        raise SystemExit(1)
    data = task["artifacts"][0]["data"][0]
    print(f"  задача {task['taskId'][:8]}: {task['state']} за {time.perf_counter() - started:.1f} с, "
          f"рішення {data['decision']}, токенів {int(data['tokens'])}")
    print(f"  інструменти: {', '.join(c['tool'] + '=' + c['outcome'] for c in data['toolCalls'])}")
    created = data["events"][0] if data["events"] else None
    if created:
        print(f"  подія {created['event']['eventId']}: {created['event']['status']} "
              f"{local(created['event']['startAt'])}, SAGA {created['saga']['status']}")
        print(f"  повідомлення з outbox: {', '.join(m['type'] for m in created['producedMessages'])}")
    print(f"  відповідь агента: {data['answer'].strip()[:200]}")
    check("навичка завершилась і створила подію", task["state"] == "TASK_STATE_COMPLETED" and created)
    check("продюсер видав EventCreated.v1 і EventConfirmed.v1",
          created and {"EventCreated.v1", "EventConfirmed.v1"} <= {m["type"] for m in created["producedMessages"]})
    event_id = created["event"]["eventId"]

    title("4. Агент-споживач: EventConfirmed.v1 -> рішення за політикою -> A2A reschedule_event -> відповідь")
    final = None
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        invitations = await rest("GET", "/api/participation/invitations", OLENA, params={"eventId": event_id})
        if invitations and invitations[0]["status"] != "Pending":
            final = invitations[0]
            break
        await asyncio.sleep(2)
    async with RemoteAgent(CONSUMER, OLENA_KEY) as consumer:
        log = task_result(await consumer.send(data={"eventId": event_id}))
    decisions = log["artifacts"][0]["data"][0]["decisions"]
    for item in decisions:
        print(f"  [{item['message']}] v{int(item['version'])} {item['localStart']}: {item['action']} — {item['reason']}")
        if item.get("producer"):
            p = item["producer"]
            print(f"      A2A -> {p['agent']}: {p['taskState']}, рішення продюсера {p['decision']}: "
                  f"{p['answer'].strip()[:160]}")
            for ev in p.get("events", []):
                print(f"      продюсер: подія {local(ev['event']['startAt'])} v{int(ev['event']['version'])}, "
                      f"SAGA {ev['saga']['status'] if ev['saga'] else '—'}, повідомлення: "
                      f"{', '.join(m['type'] for m in ev['producedMessages'])}")
    event = await rest("GET", f"/api/calendar/events/{event_id}", ARTEM)
    print(f"  підсумок: подія {local(event['startAt'])}–{local(event['endAt'])[-5:]} {event['status']} "
          f"v{event['version']}; запрошення {OLENA}: {final['status'] if final else 'Pending'}")
    check("споживач попросив перенести (раніше 12:00 — проти політики)",
          decisions and decisions[0]["action"] == "propose_new_time")
    check("продюсер переніс подію на 12:00 і видав EventRescheduled.v1",
          datetime.fromisoformat(event["startAt"]).astimezone(KYIV).hour == 12
          and any(m["type"] == "EventRescheduled.v1" for d in decisions for ev in d.get("producer", {}).get("events", [])
                  for m in ev["producedMessages"]))
    check("після EventRescheduleConfirmed.v1 споживач прийняв запрошення", final and final["status"] == "Accepted")

    title("5. Наслідки в системі ЛР3: повідомлення і сповіщення")
    messages = await rest("GET", "/api/calendar/diagnostics/outbox", ARTEM, params={"aggregateId": event_id})
    print(f"  outbox calendar-service для події: {', '.join(m['type'] for m in messages['messages'])}")
    await asyncio.sleep(4)
    notes = await rest("GET", "/api/notification/notifications", ARTEM)
    mine = [n for n in notes if meeting in n["subject"]]
    for note in mine[:3]:
        print(f"  сповіщення {ARTEM}: [{note['kind']}] {note['subject']} ({note['status']})")
    check("організатор отримав сповіщення про відповідь учасниці", any(n["kind"] == "Response" for n in mine))

    ok = sum(1 for _, passed in CHECKS if passed)
    print(f"\nПідсумок: {ok} з {len(CHECKS)} перевірок пройдено")


if __name__ == "__main__":
    asyncio.run(main())
