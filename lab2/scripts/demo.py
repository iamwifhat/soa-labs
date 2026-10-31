#!/usr/bin/env python3
"""Наскрізна перевірка ЛР2 через API-шлюз: синхронна взаємодія, хореографія, оркестрація.

Запуск: python3 scripts/demo.py  (потрібен лише стандартний Python 3.10+)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8080")
CHECKS: list[tuple[str, bool]] = []


def call(method: str, path: str, user: str | None = None, body: dict | None = None, headers: dict | None = None):
    request = urllib.request.Request(GATEWAY + path, method=method)
    request.add_header("Content-Type", "application/json")
    if user:
        request.add_header("X-User-Id", user)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(request, data, timeout=15) as response:
            return response.status, response.headers, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, error.headers, json.loads(error.read() or b"null")


def check(name: str, condition: bool) -> None:
    CHECKS.append((name, condition))
    print(f"  [{'OK' if condition else 'FAIL'}] {name}")


def wait_for(description: str, probe, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(0.5)
    raise SystemExit(f"Не дочекалися: {description}")


def title(text: str) -> None:
    print(f"\n=== {text} ===")


def main() -> None:
    suffix = uuid.uuid4().hex[:6]
    alice, bob, carol = f"alice-{suffix}", f"bob-{suffix}", f"carol-{suffix}"

    title("1. Стан сервісів через шлюз")
    for service in ("calendar", "participation", "notification"):
        status, headers, body = call("GET", f"/api/{service}/health")
        print(f"  {service:<13} HTTP {status} {body['status']}  X-Served-By: {headers.get('X-Served-By')}")
        check(f"{service}-service доступний через шлюз", status == 200)

    title("2. Синхронний ланцюжок calendar -> participation -> notification")
    _, _, chain = call("GET", "/api/calendar/diagnostics/chain?message=hello")
    print(f"  message: {chain['message']}")
    for hop in chain["hops"]:
        print(f"  - {hop['service']:<22} {hop['protocol']:<14} від: {hop['receivedFrom']}")
    check("кожен сервіс дописав свій фрагмент", chain["message"].count("->") == 3)

    title("3. Хореографія: створення події з двома запрошеними")
    _, _, calendar = call("POST", "/api/calendar/calendars", alice, {"name": "Робочий", "timeZone": "Europe/Kyiv"})
    start = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(second=0, microsecond=0)
    status, headers, event = call("POST", "/api/calendar/events", alice, {
        "calendarId": calendar["id"], "title": "Рев'ю архітектури",
        "startAt": start.isoformat(), "endAt": (start + timedelta(hours=1)).isoformat(),
        "timeZone": "Europe/Kyiv", "inviteeUserIds": [bob, carol], "remindBeforeMinutes": 30,
    })
    print(f"  POST /events -> HTTP {status}, ETag {headers.get('ETag')}, eventId {event['eventId']}")
    check("подію створено (201) з версією 1", status == 201 and event["version"] == 1)

    bob_invites = wait_for("запрошення для bob", lambda: call("GET", "/api/participation/invitations", bob)[2])
    print(f"  participation: запрошення {bob} -> {bob_invites[0]['status']} (версія події {bob_invites[0]['event']['version']})")
    check("сервіс участі створив запрошення з повідомлення EventCreated", bob_invites[0]["status"] == "Pending")
    notes = wait_for("лист-запрошення", lambda: [n for n in call("GET", "/api/notification/notifications", bob)[2]
                                                   if n["kind"] == "Invitation" and n["status"] == "Sent"])
    print(f"  notification: {notes[0]['kind']} -> {bob}: {notes[0]['subject']} [{notes[0]['status']}]")
    check("сервіс сповіщень надіслав лист з повідомлення InvitationCreated", bool(notes))
    reminder = wait_for("нагадування організатору", lambda: [n for n in call("GET", "/api/notification/notifications", alice)[2]
                                                         if n["kind"] == "Reminder"])
    print(f"  notification: {reminder[0]['kind']} -> {alice}: {reminder[0]['subject']}")

    title("4. Відповіді на запрошення (ідемпотентність команди)")
    invitation_id = bob_invites[0]["invitationId"]
    key = str(uuid.uuid4())
    answer = {"decision": "Accepted", "eventVersion": 1}
    _, _, first = call("POST", f"/api/participation/invitations/{invitation_id}/respond", bob, answer, {"Idempotency-Key": key})
    _, _, repeat = call("POST", f"/api/participation/invitations/{invitation_id}/respond", bob, answer, {"Idempotency-Key": key})
    print(f"  bob: {first['status']}; повтор з тим самим ключем -> replayed={repeat.get('replayed')}")
    check("повтор команди повертає збережений результат", repeat.get("replayed") is True)
    carol_invite = wait_for("запрошення carol", lambda: call("GET", "/api/participation/invitations", carol)[2])[0]
    call("POST", f"/api/participation/invitations/{carol_invite['invitationId']}/respond", carol,
         {"decision": "Declined", "eventVersion": 1})
    wait_for("нагадування для bob", lambda: any(
        r["userId"] == bob for r in call("GET", f"/api/calendar/events/{event['eventId']}/overview")[2]["reminders"]["items"]))

    title("5. Композиція даних трьох сервісів (синхронно)")
    _, _, overview = call("GET", f"/api/calendar/events/{event['eventId']}/overview")
    for item in overview["invitations"]["items"]:
        print(f"  запрошення {item['inviteeId']:<14} {item['status']:<9} v{item['eventVersion']}")
    for item in overview["reminders"]["items"]:
        print(f"  нагадування {item['userId']:<13} {item['status']:<9} {item['remindAt']}")
    check("overview зібрано з participation і notification",
          "items" in overview["invitations"] and "items" in overview["reminders"])

    title("6. Оркестрація: перенесення події координатором")
    new_start = start + timedelta(days=1)
    status, headers, result = call("PATCH", f"/api/calendar/events/{event['eventId']}", alice,
                                   {"startAt": new_start.isoformat(), "endAt": (new_start + timedelta(hours=1)).isoformat()},
                                   {"If-Match": '"1"'})
    orchestration = result["orchestration"]
    print(f"  PATCH -> HTTP {status}, нова версія {result['event']['version']}, координатор {orchestration['coordinator']}")
    for step in orchestration["steps"]:
        detail = step.get("result") or step.get("error")
        print(f"  крок {step['step']:<18} {step['status']:<5} {step.get('instance')}  {json.dumps(detail, ensure_ascii=False)}")
    check("усі кроки оркестрації виконано", orchestration["status"] == "COMPLETED")
    _, _, bob_after = call("GET", f"/api/participation/invitations/{invitation_id}", bob)
    check("відповідь bob скинуто до Pending для нової версії", bob_after["status"] == "Pending" and bob_after["event"]["version"] == 2)

    title("7. Захист від застарілих змін")
    status, _, error = call("PATCH", f"/api/calendar/events/{event['eventId']}", alice, {"title": "Нова назва"}, {"If-Match": '"1"'})
    print(f"  PATCH з If-Match \"1\" -> HTTP {status}: {error['message']}")
    check("застаріла версія відхилена (412)", status == 412)
    status, _, error = call("POST", f"/api/participation/invitations/{invitation_id}/respond", bob,
                            {"decision": "Accepted", "eventVersion": 1})
    print(f"  відповідь bob на версію 1 -> HTTP {status}: {error['message']}")
    check("відповідь на застарілу версію відхилена (409)", status == 409)
    status, _, error = call("POST", "/api/calendar/events", alice, {
        "calendarId": calendar["id"], "title": "Помилка", "startAt": new_start.isoformat(),
        "endAt": (new_start - timedelta(hours=1)).isoformat(), "timeZone": "Europe/Kyiv"})
    print(f"  подія із завершенням до початку -> HTTP {status}: {error['message']}")
    check("порушення інваріанту часу відхилено (422)", status == 422)

    title("8. Хореографія: скасування події")
    status, _, cancelled = call("POST", f"/api/calendar/events/{event['eventId']}/cancel", alice)
    print(f"  POST /cancel -> HTTP {status}, статус {cancelled['status']}, версія {cancelled['version']}")
    revoked = wait_for("відкликання запрошень", lambda: (
        lambda items: items if items and all(i["status"] == "Revoked" for i in items) else None
    )(call("GET", f"/api/participation/invitations?eventId={event['eventId']}", alice)[2]))
    print(f"  participation: відкликано {len(revoked)} запрошення")
    notice = wait_for("лист про скасування", lambda: [n for n in call("GET", "/api/notification/notifications", carol)[2]
                                                     if n["kind"] == "EventCancelled"])
    print(f"  notification: {notice[0]['kind']} -> {carol}: {notice[0]['subject']}")
    check("запрошення відкликано, адресатів сповіщено", len(revoked) == 2 and bool(notice))

    failed = [name for name, ok in CHECKS if not ok]
    print(f"\nПеревірок: {len(CHECKS)}, успішних: {len(CHECKS) - len(failed)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
