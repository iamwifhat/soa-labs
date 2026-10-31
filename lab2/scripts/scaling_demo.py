#!/usr/bin/env python3
"""Горизонтальне масштабування, балансування та стійкість до відмов (ЛР2).

Запуск з каталогу lab2:
    python3 scripts/scaling_demo.py all
або окремими етапами: up, lb, consumers, failover, down, breaker.
"""
import collections
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

GATEWAY = "http://localhost:8080"
CONSUL = "http://localhost:8500"
REPLICAS = 3


def http(method: str, url: str, user: str | None = None, body: dict | None = None):
    request = urllib.request.Request(url, method=method, headers={"Content-Type": "application/json"})
    if user:
        request.add_header("X-User-Id", user)
    data = json.dumps(body).encode() if body is not None else None
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, data, timeout=20) as response:
            payload = json.loads(response.read() or b"null")
            return response.status, response.headers, payload, time.perf_counter() - started
    except urllib.error.HTTPError as error:
        return error.code, error.headers, json.loads(error.read() or b"null"), time.perf_counter() - started


def compose(*args: str) -> str:
    return subprocess.run(["docker", "compose", *args], check=True, capture_output=True, text=True).stdout


def passing(service: str) -> list[dict]:
    _, _, entries, _ = http("GET", f"{CONSUL}/v1/health/service/{service}?passing=true")
    return entries


def wait_instances(service: str, count: int, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(passing(service)) == count:
            return
        time.sleep(1)
    raise SystemExit(f"{service}: не дочекалися {count} справних екземплярів")


def title(text: str) -> None:
    print(f"\n=== {text} ===")


def stage_up() -> None:
    title(f"Масштабування: calendar-service і participation-service до {REPLICAS} екземплярів")
    print(f"$ docker compose up -d --scale calendar-service={REPLICAS} --scale participation-service={REPLICAS}")
    compose("up", "-d", "--no-recreate", "--scale", f"calendar-service={REPLICAS}",
            "--scale", f"participation-service={REPLICAS}")
    for service in ("calendar-service", "participation-service", "notification-service"):
        wait_instances(service, REPLICAS if service != "notification-service" else 1)
        print(f"Consul: {service}")
        for entry in passing(service):
            svc = entry["Service"]
            checks = ", ".join(check["Status"] for check in entry["Checks"])
            print(f"  {svc['ID']:<36} {svc['Address']}:{svc['Port']}  health: {checks}")
    time.sleep(4)  # Traefik перечитує каталог Consul кожні 3 с


def stage_lb() -> None:
    title("Серверне балансування: Traefik розподіляє 30 запитів між екземплярами calendar-service")
    served = collections.Counter(http("GET", f"{GATEWAY}/api/calendar/health")[1]["X-Served-By"] for _ in range(30))
    for instance, count in sorted(served.items()):
        print(f"  {instance:<36} {count} запитів")

    title("Клієнтське балансування: 18 ланцюжків, calendar-service обирає participation-service з Consul")
    pairs = collections.Counter()
    for _ in range(18):
        hops = http("GET", f"{GATEWAY}/api/calendar/diagnostics/chain?message=lb")[2]["hops"]
        pairs[(hops[0]["instance"], hops[1]["instance"])] += 1
    for (calendar, participation), count in sorted(pairs.items()):
        print(f"  calendar[{calendar}] -> participation[{participation}]: {count}")
    targets = collections.Counter()
    for (_, participation), count in pairs.items():
        targets[participation] += count
    print("  Разом на екземпляри participation-service: "
          + ", ".join(f"{instance}={count}" for instance, count in sorted(targets.items())))


def stage_consumers() -> None:
    title("Конкуруючі споживачі: 9 повідомлень EventCreated з однієї черги")
    owner = f"load-{uuid.uuid4().hex[:6]}"
    calendar = http("POST", f"{GATEWAY}/api/calendar/calendars", owner, {"name": "Навантаження"})[2]
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2)
    event_ids = []
    for index in range(9):
        event = http("POST", f"{GATEWAY}/api/calendar/events", owner, {
            "calendarId": calendar["id"], "title": f"Зустріч {index + 1}",
            "startAt": (start + timedelta(hours=index)).isoformat(),
            "endAt": (start + timedelta(hours=index, minutes=30)).isoformat(),
            "timeZone": "Europe/Kyiv", "inviteeUserIds": [f"guest-{index}"],
        })[2]
        event_ids.append(event["eventId"])
    time.sleep(4)
    logs = compose("logs", "--no-log-prefix", "participation-service")
    handled = collections.Counter()
    for line in logs.splitlines():
        if "EventCreated" in line and any(event_id in line for event_id in event_ids):
            handled[line.split()[3]] += 1
    for instance, count in sorted(handled.items()):
        print(f"  {instance:<36} обробив {count}")
    print(f"  Усього оброблено: {sum(handled.values())} з {len(event_ids)}")


def stage_failover() -> None:
    title("Відмова екземпляра: аварійна зупинка одного participation-service")
    containers = compose("ps", "-q", "participation-service").split()
    victim = containers[-1]
    hostname = subprocess.run(["docker", "inspect", "-f", "{{.Config.Hostname}}", victim],
                              check=True, capture_output=True, text=True).stdout.strip()
    print(f"$ docker kill {hostname}")
    subprocess.run(["docker", "kill", victim], check=True, capture_output=True)
    for attempt in range(1, 9):
        status, _, payload, elapsed = http("GET", f"{GATEWAY}/api/calendar/diagnostics/chain?message=failover")
        target = payload["hops"][1]["instance"] if len(payload.get("hops", [])) > 1 else payload.get("error")
        healthy = len(passing("participation-service"))
        print(f"  запит {attempt}: HTTP {status}, participation[{target}], {elapsed * 1000:.0f} мс, у Consul справних: {healthy}")
        time.sleep(1.5)
    print(f"$ docker start {hostname}")
    subprocess.run(["docker", "start", victim], check=True, capture_output=True)
    wait_instances("participation-service", REPLICAS)
    print(f"  Екземпляр повернувся до каталогу: справних {len(passing('participation-service'))}")


def stage_down() -> None:
    title("Зменшення кількості екземплярів до одного (зняття з реєстрації під час зупинки)")
    print("$ docker compose up -d --scale calendar-service=1 --scale participation-service=1")
    compose("up", "-d", "--no-recreate", "--scale", "calendar-service=1", "--scale", "participation-service=1")
    for service in ("calendar-service", "participation-service"):
        wait_instances(service, 1)
        print(f"  {service}: справних екземплярів {len(passing(service))}")
    time.sleep(4)


def stage_breaker() -> None:
    title("Circuit Breaker: недоступний notification-service")
    owner = f"cb-{uuid.uuid4().hex[:6]}"
    calendar = http("POST", f"{GATEWAY}/api/calendar/calendars", owner, {"name": "CB"})[2]
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)
    event = http("POST", f"{GATEWAY}/api/calendar/events", owner, {
        "calendarId": calendar["id"], "title": "Перевірка breaker", "startAt": start.isoformat(),
        "endAt": (start + timedelta(hours=1)).isoformat(), "timeZone": "Europe/Kyiv"})[2]
    print("$ docker compose stop notification-service")
    compose("stop", "notification-service")
    time.sleep(1)

    def probe(label: str) -> None:
        _, _, overview, elapsed = http("GET", f"{GATEWAY}/api/calendar/events/{event['eventId']}/overview")
        reminders = overview["reminders"]
        state = reminders.get("circuitBreaker", "CLOSED")
        detail = "дані отримано" if "items" in reminders else reminders["error"]
        print(f"  {label}: {elapsed * 1000:5.0f} мс, breaker={state:<9} {detail}")

    for index in range(1, 6):
        probe(f"запит {index}")
    print("$ docker compose start notification-service")
    compose("start", "notification-service")
    wait_instances("notification-service", 1)
    time.sleep(10)
    probe("через 10 с")
    probe("наступний")


STAGES = {"up": stage_up, "lb": stage_lb, "consumers": stage_consumers, "failover": stage_failover,
          "down": stage_down, "breaker": stage_breaker}

if __name__ == "__main__":
    selected = sys.argv[1:] or ["all"]
    for name in (list(STAGES) if selected == ["all"] else selected):
        STAGES[name]()
