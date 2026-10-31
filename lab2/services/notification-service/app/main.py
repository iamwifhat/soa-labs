"""Сервіс нагадувань і сповіщень: споживач подій календаря та участі."""
import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from soa_common.db import create_pool
from soa_common.discovery import ConsulRegistry
from soa_common.messaging import MessageBus
from soa_common.settings import configure_logging, load_settings
from soa_common.web import UserId, gateway_tags, setup_http

from . import store
from .store import local_time

settings = load_settings("notification-service")
configure_logging(settings)
log = logging.getLogger("notification")
SCHEMA = (Path(__file__).parent / "schema.sql").read_text()
DISPATCH_INTERVAL_SECONDS = 2.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool(settings.database_url, SCHEMA)
    app.state.bus = MessageBus(settings.rabbitmq_url, settings.service_name, settings.instance_id)
    await app.state.bus.connect()
    await app.state.bus.subscribe(
        "notification.domain-events",
        [
            "calendar.event.created", "calendar.event.updated", "calendar.event.cancelled",
            "participation.invitation.created", "participation.invitation.responded",
            "participation.invitation.revoked",
        ],
        on_domain_event,
    )
    dispatcher = asyncio.create_task(dispatch_loop())
    registry = ConsulRegistry(settings.consul_url)
    await registry.register(
        settings.service_name, settings.port, gateway_tags(settings, "notification"),
        {"instance": settings.instance_id, "lab": "2"},
    )
    yield
    await registry.deregister()
    await registry.close()
    dispatcher.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await dispatcher
    await app.state.bus.close()
    await app.state.pool.close()


app = FastAPI(
    title="Notification Service",
    description="Контекст «Нагадування та сповіщення»: нагадування, листи й налаштування каналів.",
    version="2.0.0",
    root_path=settings.gateway_prefix,
    lifespan=lifespan,
)
setup_http(app, settings)


# ---------- Споживач подій (хореографія) ----------

async def on_domain_event(envelope: dict, _message) -> None:
    handlers = {
        "EventCreated.v1": _event_created,
        "EventUpdated.v1": _event_updated,
        "EventCancelled.v1": _event_cancelled,
        "InvitationCreated.v1": _invitation_created,
        "InvitationResponded.v1": _invitation_responded,
        "InvitationRevoked.v1": _invitation_revoked,
    }
    handler = handlers.get(envelope["type"])
    if handler is None:
        log.info("Пропущено невідомий тип %s", envelope["type"])
        return
    async with app.state.pool.acquire() as connection, connection.transaction():
        await handler(connection, envelope["data"])
    log.info("Оброблено %s %s", envelope["type"], envelope["aggregateId"])


async def _event_created(connection, data: dict) -> None:
    await store.schedule_reminder(
        connection, uuid.UUID(data["eventId"]), data["organizerId"], data["title"],
        datetime.fromisoformat(data["startAt"]), data["timeZone"], data["remindBeforeMinutes"], data["version"],
    )


async def _event_updated(connection, data: dict) -> None:
    if "time" not in data.get("changes", []):
        return
    event_id = uuid.UUID(data["eventId"])
    await store.move_reminders(
        connection, event_id, data["organizerId"], data["title"],
        datetime.fromisoformat(data["startAt"]), data["timeZone"], data["version"],
    )
    when = local_time(datetime.fromisoformat(data["startAt"]), data["timeZone"])
    for user_id in data["inviteeUserIds"]:
        await store.enqueue(
            connection, user_id, event_id, "EventRescheduled",
            f"Подію «{data['title']}» перенесено на {when}; підтвердьте участь повторно",
            f"rescheduled:{event_id}:v{data['version']}:{user_id}",
        )


async def _event_cancelled(connection, data: dict) -> None:
    await store.cancel_reminders(connection, uuid.UUID(data["eventId"]))


async def _invitation_created(connection, data: dict) -> None:
    event = data["event"]
    await store.enqueue(
        connection, data["inviteeId"], uuid.UUID(data["eventId"]), "Invitation",
        f"Запрошення на «{event['title']}» {local_time(datetime.fromisoformat(event['startAt']), event['timeZone'])}",
        f"invitation:{data['invitationId']}",
    )


async def _invitation_responded(connection, data: dict) -> None:
    event = data["event"]
    event_id = uuid.UUID(data["eventId"])
    if data["status"] == "Accepted":
        await store.schedule_reminder(
            connection, event_id, data["inviteeId"], event["title"], datetime.fromisoformat(event["startAt"]),
            event["timeZone"], event["remindBeforeMinutes"], event["version"],
        )
    else:
        await store.cancel_reminders(connection, event_id, data["inviteeId"])
    await store.enqueue(
        connection, data["organizerId"], event_id, "Response",
        f"{data['inviteeId']}: {data['status']} для «{event['title']}»",
        f"response:{data['invitationId']}:v{data['version']}",
    )


async def _invitation_revoked(connection, data: dict) -> None:
    event_id = uuid.UUID(data["eventId"])
    await store.cancel_reminders(connection, event_id, data["inviteeId"])
    await store.enqueue(
        connection, data["inviteeId"], event_id, "EventCancelled",
        f"Подію «{data['event']['title']}» скасовано",
        f"revoked:{data['invitationId']}",
    )


# ---------- Планувальник і доставка ----------

async def dispatch_loop() -> None:
    while True:
        try:
            async with app.state.pool.acquire() as connection:
                fired = await store.fire_due_reminders(connection)
                delivered = await store.deliver_queued(connection, settings.instance_id)
            if fired:
                log.info("Спрацювало нагадувань: %s", fired)
            for row in delivered:
                # Поштовий провайдер імітовано журналом; реальний SMTP-адаптер підключається окремо.
                log.info("EMAIL [%s] -> %s: %s (%s)", row["kind"], row["user_id"], row["subject"], row["status"])
        except Exception:  # noqa: BLE001
            log.exception("Помилка планувальника")
        await asyncio.sleep(DISPATCH_INTERVAL_SECONDS)


# ---------- Публічний API ----------

class Preferences(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    email_enabled: bool = True


@app.get("/notifications", tags=["notifications"], summary="Сповіщення користувача та стан доставки")
async def list_notifications(user_id: UserId) -> list[dict]:
    async with app.state.pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT kind, subject, status, created_at, sent_at, sent_by FROM notifications
             WHERE user_id = $1 ORDER BY created_at DESC LIMIT 50
            """,
            user_id,
        )
    return [
        {
            "kind": row["kind"], "subject": row["subject"], "status": row["status"],
            "createdAt": row["created_at"].isoformat(),
            "sentAt": row["sent_at"].isoformat() if row["sent_at"] else None,
            "sentBy": row["sent_by"],
        }
        for row in rows
    ]


@app.put("/users/me/preferences", response_model=Preferences, tags=["preferences"])
async def put_preferences(body: Preferences, user_id: UserId) -> Preferences:
    async with app.state.pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO preferences (user_id, email_enabled) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET email_enabled = EXCLUDED.email_enabled, updated_at = now()
            """,
            user_id, body.email_enabled,
        )
    return body


@app.get("/users/me/preferences", response_model=Preferences, tags=["preferences"])
async def get_preferences(user_id: UserId) -> Preferences:
    async with app.state.pool.acquire() as connection:
        enabled = await connection.fetchval("SELECT email_enabled FROM preferences WHERE user_id = $1", user_id)
    return Preferences(email_enabled=True if enabled is None else enabled)


# ---------- Внутрішній API ----------

@app.post("/internal/events/{event_id}/reschedule", tags=["internal"], summary="Крок оркестрації: перенести нагадування")
async def reschedule_step(event_id: uuid.UUID, snapshot: dict) -> dict:
    async with app.state.pool.acquire() as connection, connection.transaction():
        moved, waiting = await store.move_reminders(
            connection, event_id, snapshot["organizerId"], snapshot["title"],
            datetime.fromisoformat(snapshot["startAt"]), snapshot["timeZone"], snapshot["version"],
        )
    log.info("Крок перенесення %s v%s: перенесено %s, очікують підтвердження %s", event_id, snapshot["version"], moved, waiting)
    return {"eventId": str(event_id), "eventVersion": snapshot["version"], "moved": moved, "awaitingReconfirmation": waiting}


@app.get("/internal/events/{event_id}/reminders", tags=["internal"])
async def event_reminders(event_id: uuid.UUID) -> list[dict]:
    async with app.state.pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT user_id, remind_at, status, event_version FROM reminders WHERE event_id = $1 ORDER BY user_id",
            event_id,
        )
    return [
        {"userId": row["user_id"], "remindAt": row["remind_at"].isoformat(), "status": row["status"],
         "eventVersion": row["event_version"]}
        for row in rows
    ]


@app.post("/diagnostics/chain", tags=["diagnostics"])
async def diagnostics_chain(request: Request, payload: dict) -> dict:
    appended = f"notification[{settings.instance_id}]"
    payload["message"] = f"{payload['message']} -> {appended}"
    payload["hops"].append({
        "service": settings.service_name,
        "instance": settings.instance_id,
        "protocol": f"HTTP/{request.scope.get('http_version', '1.1')} REST",
        "receivedFrom": "participation-service (адресу знайдено в Consul, клієнтський round robin)",
        "appended": appended,
    })
    return payload
