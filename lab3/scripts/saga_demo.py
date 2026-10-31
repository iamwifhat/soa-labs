#!/usr/bin/env python3
"""Перевірка ЛР3: Transactional Outbox/Inbox і SAGA з компенсацією по DLQ та таймауту.

Запуск з каталогу lab3 після `docker compose up -d --build`:
    python3 scripts/saga_demo.py
Сценарій керує режимом збою учасника через Consul KV і зупиняє RabbitMQ для перевірки outbox.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8080")
CONSUL = "http://localhost:8500"
RABBIT = "http://localhost:15672"
RABBIT_AUTH = "Basic " + base64.b64encode(b"calendar:calendar").decode()
CHECKS: list[tuple[str, bool]] = []


def http(method, url, user=None, body=None, headers=None, raw=None):
    request = urllib.request.Request(url, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    if user:
        request.add_header("X-User-Id", user)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(request, data, timeout=20) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def api(method, path, user=None, body=None, headers=None):
    return http(method, GATEWAY + path, user, body, headers)


def check(name, condition):
    CHECKS.append((name, condition))
    print(f"  [{'OK' if condition else 'FAIL'}] {name}")


def wait_for(description, probe, timeout=20.0, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(interval)
    raise SystemExit(f"Не дочекалися: {description}")


def title(text):
    print(f"\n=== {text} ===")


def set_fault(mode):
    urllib.request.urlopen(urllib.request.Request(
        f"{CONSUL}/v1/kv/config/participation-service/fault-mode", data=mode.encode(), method="PUT"), timeout=5)
    print(f"  $ consul kv put config/participation-service/fault-mode {mode}")
    time.sleep(2.5)  # кеш конфігурації в сервісі — 2 с


def compose(*args):
    subprocess.run(["docker", "compose", *args], check=True, capture_output=True)


def saga(saga_id):
    return api("GET", f"/api/calendar/sagas/{saga_id}")[1]


def finished_saga(saga_id, timeout=20.0):
    return wait_for(f"завершення SAGA {saga_id}",
                    lambda: (lambda s: s if s["status"] != "Started" else None)(saga(saga_id)), timeout)


def print_log(state):
    started = datetime.fromisoformat(state["startedAt"])
    for item in state["log"]:
        offset = (datetime.fromisoformat(item["at"]) - started).total_seconds()
        print(f"    +{offset:5.1f} с  {item['entry']}")


def invitations(event_id, organizer):
    return api("GET", f"/api/participation/invitations?eventId={event_id}", organizer)[1]


def settled_invitation(invitation_id, user):
    """Чекає, поки учасник застосує підтвердження SAGA (знімок події знову Scheduled)."""
    return wait_for("підтвердження в participation", lambda: (
        lambda i: i if i["event"]["status"] == "Scheduled" else None
    )(api("GET", f"/api/participation/invitations/{invitation_id}", user)[1]))


def participation_logs(marker):
    out = subprocess.run(["docker", "compose", "logs", "--no-log-prefix", "participation-service"],
                         check=True, capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if marker in line]


def main():
    suffix = uuid.uuid4().hex[:6]
    alice, bob, carol = f"alice-{suffix}", f"bob-{suffix}", f"carol-{suffix}"
    set_fault("none")
    _, calendar = api("POST", "/api/calendar/calendars", alice, {"name": "Робочий"})
    base = (datetime.now(timezone.utc) + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)

    def new_event(name, hours, invitees):
        start = base + timedelta(hours=hours)
        return api("POST", "/api/calendar/events", alice, {
            "calendarId": calendar["id"], "title": name, "startAt": start.isoformat(),
            "endAt": (start + timedelta(hours=1)).isoformat(), "timeZone": "Europe/Kyiv",
            "inviteeUserIds": invitees, "remindBeforeMinutes": 15})

    # ------------------------------------------------------------------
    title("1. SAGA CreateEvent: успішне виконання")
    status, created = new_event("Планування спринту", 0, [bob, carol])
    event_id, saga_id = created["event"]["eventId"], created["sagaId"]
    print(f"  POST /events -> HTTP {status}, подія {created['event']['status']} v{created['event']['version']}")
    check("подію прийнято (202) у стані Pending", status == 202 and created["event"]["status"] == "Pending")
    state = finished_saga(saga_id)
    print(f"  SAGA {state['type']}: {state['status']}")
    print_log(state)
    _, event = api("GET", f"/api/calendar/events/{event_id}")
    print(f"  подія: {event['status']} v{event['version']}")
    check("SAGA завершено, подія Scheduled v2", state["status"] == "Completed" and event["status"] == "Scheduled")
    box = wait_for("публікація з outbox", lambda: (
        lambda b: b if all(m["publishedAt"] for m in b["messages"]) else None
    )(api("GET", f"/api/calendar/diagnostics/outbox?aggregateId={event_id}")[1]))
    for message in box["messages"]:
        print(f"  outbox #{message['outboxId']}: {message['type']:<18} опубліковано {message['publishedAt']}")
    check("обидва повідомлення пройшли через outbox", all(m["publishedAt"] for m in box["messages"]))
    mails = wait_for("лист-запрошення", lambda: [n for n in api("GET", "/api/notification/notifications", bob)[1]
                                                  if n["kind"] == "Invitation" and n["status"] == "Sent"])
    print(f"  лист для {bob}: {mails[0]['subject']} [{mails[0]['status']}]")
    check("лист надіслано лише після EventConfirmed", bool(mails))

    # ------------------------------------------------------------------
    title("2. Transactional Inbox: повторна доставка того самого повідомлення")
    original = next(m for m in box["messages"] if m["type"] == "EventCreated.v1")["envelope"]
    before = len(invitations(event_id, alice))
    status, routed = http("POST", f"{RABBIT}/api/exchanges/%2F/calendar.events/publish", headers={"Authorization": RABBIT_AUTH},
                          body={"routing_key": "calendar.event.created", "payload": json.dumps(original, ensure_ascii=False),
                                "payload_encoding": "string",
                                "properties": {"message_id": original["messageId"], "type": original["type"],
                                               "content_type": "application/json", "delivery_mode": 2}})
    print(f"  повторно опубліковано EventCreated.v1 {original['messageId']} -> routed={routed['routed']}")
    duplicate = wait_for("запис про дублікат", lambda: participation_logs(f"Дублікат EventCreated.v1 {original['messageId']}"))
    print(f"  participation: {duplicate[-1].split('] ', 1)[1]}")
    after = len(invitations(event_id, alice))
    check("дублікат відсіяно inbox, запрошень не додалося", before == after == 2)

    # ------------------------------------------------------------------
    title("3. SAGA RescheduleEvent: успішне перенесення")
    invite = next(i for i in invitations(event_id, alice) if i["inviteeId"] == bob)
    _, answer = api("POST", f"/api/participation/invitations/{invite['invitationId']}/respond", bob,
                    {"decision": "Accepted", "eventVersion": event["version"]})
    print(f"  {bob} відповів {answer['status']} на версію {answer['event']['version']}")
    moved = base + timedelta(hours=3)
    status, result = api("PATCH", f"/api/calendar/events/{event_id}", alice,
                         {"startAt": moved.isoformat(), "endAt": (moved + timedelta(hours=1)).isoformat()},
                         {"If-Match": f'"{event["version"]}"'})
    print(f"  PATCH -> HTTP {status}, нова версія {result['event']['version']}")
    state = finished_saga(result["sagaId"])
    print_log(state)
    mine = settled_invitation(invite["invitationId"], bob)
    print(f"  запрошення {bob}: {mine['status']} для v{mine['event']['version']} ({mine['event']['status']})")
    check("перенесення завершено, відповідь скинуто до Pending",
          state["status"] == "Completed" and mine["status"] == "Pending")

    # ------------------------------------------------------------------
    title("4. SAGA RescheduleEvent: збій учасника -> DLQ -> компенсація")
    _, event = api("GET", f"/api/calendar/events/{event_id}")
    status, answer = api("POST", f"/api/participation/invitations/{invite['invitationId']}/respond", bob,
                         {"decision": "Accepted", "eventVersion": event["version"]})
    print(f"  {bob} знову прийняв запрошення для v{event['version']} ({event['startAt']}) -> HTTP {status} {answer['status']}")
    set_fault("error")
    failed_start = base + timedelta(hours=6)
    status, result = api("PATCH", f"/api/calendar/events/{event_id}", alice,
                         {"startAt": failed_start.isoformat(), "endAt": (failed_start + timedelta(hours=1)).isoformat()},
                         {"If-Match": f'"{event["version"]}"'})
    print(f"  PATCH -> HTTP {status}, версія {result['event']['version']}, новий час {result['event']['startAt']}")
    state = finished_saga(result["sagaId"])
    set_fault("none")
    print_log(state)
    for line in participation_logs("спроба")[-4:]:
        print(f"  participation: {line.split('] ', 1)[1][:150]}")
    _, restored = api("GET", f"/api/calendar/events/{event_id}")
    mine = wait_for("відновлення запрошення", lambda: (lambda i: i if i["event"]["version"] == restored["version"] else None)(
        settled_invitation(invite["invitationId"], bob)))
    print(f"  подія: v{restored['version']} {restored['startAt']}; запрошення {bob}: {mine['status']} v{mine['event']['version']}")
    check("час події відновлено, відповідь Accepted збережено",
          state["status"] == "Compensated" and restored["startAt"] == event["startAt"] and mine["status"] == "Accepted")

    # ------------------------------------------------------------------
    title("5. SAGA CreateEvent: збій учасника -> DLQ -> компенсація")
    set_fault("error")
    status, created = new_event("Демо збою", 8, [bob, carol])
    state = finished_saga(created["sagaId"])
    set_fault("none")
    print_log(state)
    _, failed_event = api("GET", f"/api/calendar/events/{created['event']['eventId']}")
    left = invitations(created["event"]["eventId"], alice)
    print(f"  подія: {failed_event['status']} v{failed_event['version']}; запрошень у participation: {len(left)}")
    check("подію скасовано компенсацією, запрошень немає",
          failed_event["status"] == "Cancelled" and state["status"] == "Compensated" and not left)

    # ------------------------------------------------------------------
    title("6. Transactional Outbox: RabbitMQ недоступний під час створення події")
    print("  $ docker compose stop rabbitmq")
    compose("stop", "rabbitmq")
    status, created = new_event("Подія під час аварії брокера", 10, [carol])
    _, box = api("GET", "/api/calendar/diagnostics/outbox")
    print(f"  POST /events -> HTTP {status}; outbox: неопубліковано {box['pending']}")
    check("сервіс приймає зміну без брокера, повідомлення чекає в outbox", status == 202 and box["pending"] >= 1)
    print("  $ docker compose start rabbitmq")
    compose("start", "rabbitmq")
    restarted = time.monotonic()
    state = finished_saga(created["sagaId"], timeout=60)
    print(f"  SAGA {state['status']} через {time.monotonic() - restarted:.1f} с після запуску брокера")
    print_log(state)
    box = wait_for("порожній outbox", lambda: (lambda b: b if b["pending"] == 0 else None)(
        api("GET", "/api/calendar/diagnostics/outbox")[1]))
    print(f"  outbox: неопубліковано {box['pending']}, опубліковано {box['published']}")
    check("після відновлення брокера SAGA завершилася", state["status"] == "Completed" and box["pending"] == 0)

    # ------------------------------------------------------------------
    title("7. SAGA CreateEvent: учасник не відповів до дедлайну -> компенсація за таймаутом")
    set_fault("delay")
    status, created = new_event("Демо таймауту", 12, [bob])
    time.sleep(1)
    status, error = api("POST", f"/api/calendar/events/{created['event']['eventId']}/cancel", alice)
    print(f"  скасування під час SAGA -> HTTP {status}: {error['message']}")
    check("семантичне блокування відхиляє зміну під час SAGA (409)", status == 409)
    set_fault("none")
    state = finished_saga(created["sagaId"], timeout=45)
    print(f"  SAGA {state['status']}: {state['failureReason']}")
    left = wait_for("відкликання запізнілих запрошень", lambda: (
        lambda items: items if items and all(i["status"] == "Revoked" for i in items) else None
    )(invitations(created["event"]["eventId"], alice)), timeout=40, interval=1)
    state = wait_for("запис про запізнілу відповідь", lambda: (
        lambda s: s if any("Запізнілу" in item["entry"] for item in s["log"]) else None
    )(saga(created["sagaId"])))
    print_log(state)
    print(f"  запрошення, створені із запізненням: {[i['status'] for i in left]}")
    check("запізнілу відповідь проігноровано, запрошення відкликано",
          any("Запізнілу" in item["entry"] for item in state["log"]) and state["status"] == "Compensated")

    failed = [name for name, ok in CHECKS if not ok]
    print(f"\nПеревірок: {len(CHECKS)}, успішних: {len(CHECKS) - len(failed)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
