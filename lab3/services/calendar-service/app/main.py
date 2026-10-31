"""Сервіс календаря (ЛР3): події, Transactional Outbox і координатор SAGA."""
import asyncio
import contextlib
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from soa_common import outbox
from soa_common.db import create_pool
from soa_common.discovery import ConsulRegistry
from soa_common.http_client import ServiceClient, ServiceUnavailableError
from soa_common.messaging import MessageBus, new_envelope
from soa_common.settings import configure_logging, load_settings
from soa_common.web import DomainError, NotFoundError, UserId, current_correlation_id, gateway_tags, setup_http

from . import store
from .domain import Calendar, Event, EventTitle, TimeSlot
from .saga import SagaCoordinator

settings = load_settings("calendar-service")
configure_logging(settings)
log = logging.getLogger("calendar")
SCHEMA = (Path(__file__).parent / "schema.sql").read_text() + outbox.SCHEMA
SAGA_TIMEOUT_SECONDS = float(os.getenv("SAGA_TIMEOUT_SECONDS", "30"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool(settings.database_url, SCHEMA)
    app.state.bus = MessageBus(settings.rabbitmq_url, settings.service_name, settings.instance_id)
    await app.state.bus.connect()
    app.state.saga = SagaCoordinator(
        app.state.pool, settings.service_name, SAGA_TIMEOUT_SECONDS
    )
    # Відповіді учасників SAGA та «мертві» повідомлення, які учасник не зміг обробити.
    await app.state.bus.subscribe(
        "calendar.saga-replies",
        ["participation.saga.invitations-created", "participation.saga.invitations-rescheduled"],
        app.state.saga.on_reply,
    )
    await app.state.bus.consume_dead_letters("participation.calendar-events", app.state.saga.on_dead_letter)
    tasks = [
        asyncio.create_task(outbox.OutboxRelay(app.state.pool, app.state.bus).run()),
        asyncio.create_task(app.state.saga.expire_loop()),
    ]
    registry = ConsulRegistry(settings.consul_url)
    app.state.participation = ServiceClient(registry, "participation-service")
    app.state.notification = ServiceClient(registry, "notification-service")
    await registry.register(
        settings.service_name, settings.port, gateway_tags(settings, "calendar"),
        {"instance": settings.instance_id, "lab": "3"},
    )
    yield
    await registry.deregister()
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await app.state.participation.close()
    await app.state.notification.close()
    await registry.close()
    await app.state.bus.close()
    await app.state.pool.close()


app = FastAPI(
    title="Calendar Service",
    description="Контекст «Планування календаря»: події, Transactional Outbox і координатор SAGA.",
    version="3.0.0",
    root_path=settings.gateway_prefix,
    lifespan=lifespan,
)
setup_http(app, settings)


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CalendarIn(CamelModel):
    name: str
    time_zone: str = "Europe/Kyiv"


class CalendarOut(CamelModel):
    id: uuid.UUID
    owner_id: str
    name: str
    time_zone: str


class EventIn(CamelModel):
    calendar_id: uuid.UUID
    title: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    time_zone: str
    invitee_user_ids: list[str] = Field(default_factory=list)
    remind_before_minutes: int = 15


class EventPatch(CamelModel):
    title: str | None = None
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    time_zone: str | None = None


def _calendar_out(calendar: Calendar) -> CalendarOut:
    return CalendarOut(id=calendar.id, owner_id=calendar.owner_id, name=calendar.name, time_zone=calendar.time_zone)


def _version(if_match: str) -> int:
    try:
        return int(if_match.strip().removeprefix("W/").strip('"'))
    except ValueError as exc:
        raise DomainError("Заголовок If-Match має містити версію події, наприклад \"3\"") from exc


def _saga_link(saga_id: uuid.UUID) -> str:
    return f"{settings.gateway_prefix}/sagas/{saga_id}"


@app.post("/calendars", status_code=201, response_model=CalendarOut, tags=["calendars"])
async def create_calendar(body: CalendarIn, user_id: UserId) -> CalendarOut:
    calendar = Calendar.create(user_id, body.name, body.time_zone)
    async with app.state.pool.acquire() as connection:
        await store.insert_calendar(connection, calendar)
    return _calendar_out(calendar)


@app.get("/calendars/{calendar_id}", response_model=CalendarOut, tags=["calendars"])
async def get_calendar(calendar_id: uuid.UUID) -> CalendarOut:
    async with app.state.pool.acquire() as connection:
        return _calendar_out(await store.get_calendar(connection, calendar_id))


@app.post("/events", status_code=202, tags=["events"], summary="Створення події: старт SAGA CreateEvent")
async def create_event(body: EventIn, user_id: UserId, response: Response) -> dict:
    async with app.state.pool.acquire() as connection, connection.transaction():
        calendar = await store.get_calendar(connection, body.calendar_id)
        event = Event.create(
            calendar, user_id, EventTitle(body.title),
            TimeSlot(body.start_at, body.end_at, body.time_zone),
            body.invitee_user_ids, body.remind_before_minutes,
        )
        saga_id = await app.state.saga.start_creation(connection, event, current_correlation_id())
    # 202: подію прийнято, але вона стане Scheduled лише після успіху всіх кроків SAGA.
    response.headers["ETag"] = f'"{event.version}"'
    response.headers["Location"] = f"{settings.gateway_prefix}/events/{event.id}"
    return {"event": event.snapshot(), "sagaId": str(saga_id), "saga": _saga_link(saga_id)}


@app.get("/events", tags=["events"])
async def list_events(calendar_id: Annotated[uuid.UUID, Query(alias="calendarId")]) -> list[dict]:
    async with app.state.pool.acquire() as connection:
        return [event.snapshot() for event in await store.list_events(connection, calendar_id)]


@app.get("/events/{event_id}", tags=["events"])
async def get_event(event_id: uuid.UUID, response: Response) -> dict:
    async with app.state.pool.acquire() as connection:
        event = await store.get_event(connection, event_id)
    response.headers["ETag"] = f'"{event.version}"'
    return event.snapshot()


@app.patch("/events/{event_id}", tags=["events"], summary="Зміна події; перенесення запускає SAGA RescheduleEvent")
async def patch_event(
    event_id: uuid.UUID,
    body: EventPatch,
    user_id: UserId,
    if_match: Annotated[str, Header(alias="If-Match")],
    response: Response,
) -> dict:
    saga_id = None
    async with app.state.pool.acquire() as connection, connection.transaction():
        event = await store.get_event(connection, event_id, for_update=True)
        previous_version, previous_slot = event.version, event.slot
        slot = None
        if body.start_at or body.end_at or body.time_zone:
            slot = TimeSlot(
                body.start_at or event.slot.start_at,
                body.end_at or event.slot.end_at,
                body.time_zone or event.slot.time_zone,
            )
        title = EventTitle(body.title) if body.title is not None else None
        changes = event.change(user_id, _version(if_match), title=title, slot=slot)
        if "time" in changes:
            saga_id = await app.state.saga.start_reschedule(
                connection, event, previous_version, previous_slot, changes, current_correlation_id()
            )
        else:
            # Зміна лише назви не зачіпає інших сервісів атомарно: достатньо повідомлення через outbox.
            await store.update_event(connection, event, previous_version)
            await outbox.add_to_outbox(connection, new_envelope(
                "EventUpdated.v1", event.id, event.version, {**event.snapshot(), "changes": changes},
                settings.service_name, current_correlation_id(),
            ), "calendar.event.updated")
    response.headers["ETag"] = f'"{event.version}"'
    if saga_id:
        response.status_code = 202
        return {"event": event.snapshot(), "changes": changes, "sagaId": str(saga_id), "saga": _saga_link(saga_id)}
    return {"event": event.snapshot(), "changes": changes}


@app.post("/events/{event_id}/cancel", tags=["events"], summary="Скасування організатором (повідомлення через outbox)")
async def cancel_event(
    event_id: uuid.UUID,
    user_id: UserId,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict:
    async with app.state.pool.acquire() as connection, connection.transaction():
        event = await store.get_event(connection, event_id, for_update=True)
        previous_version = event.version
        event.cancel(user_id, _version(if_match) if if_match else event.version)
        await store.update_event(connection, event, previous_version)
        await outbox.add_to_outbox(connection, new_envelope(
            "EventCancelled.v1", event.id, event.version, {**event.snapshot(), "reason": "OrganizerCancelled"},
            settings.service_name, current_correlation_id(),
        ), "calendar.event.cancelled")
    response.headers["ETag"] = f'"{event.version}"'
    return event.snapshot()


@app.get("/sagas/{saga_id}", tags=["saga"], summary="Стан SAGA та її журнал (Saga log)")
async def get_saga(saga_id: uuid.UUID) -> dict:
    async with app.state.pool.acquire() as connection:
        saga = await app.state.saga.describe(connection, saga_id)
    if saga is None:
        raise NotFoundError(f"SAGA {saga_id} не знайдено")
    return saga


@app.get("/events/{event_id}/overview", tags=["events"], summary="Композиція даних трьох сервісів (синхронно)")
async def event_overview(event_id: uuid.UUID) -> dict:
    async with app.state.pool.acquire() as connection:
        event = await store.get_event(connection, event_id)
    overview = {"event": event.snapshot()}
    for key, client, path in (
        ("invitations", app.state.participation, f"/internal/events/{event_id}/invitations"),
        ("reminders", app.state.notification, f"/internal/events/{event_id}/reminders"),
    ):
        try:
            reply = await client.request("GET", path)
            overview[key] = {"servedBy": reply.headers.get("X-Served-By"), "items": reply.json()}
        except ServiceUnavailableError as exc:
            overview[key] = {"error": str(exc), "circuitBreaker": client.breaker.state}
    return overview


@app.get("/diagnostics/outbox", tags=["diagnostics"], summary="Стан outbox і повідомлення агрегату")
async def diagnostics_outbox(aggregate_id: Annotated[uuid.UUID | None, Query(alias="aggregateId")] = None) -> dict:
    async with app.state.pool.acquire() as connection:
        result = await outbox.outbox_stats(connection)
        if aggregate_id:
            rows = await connection.fetch(
                """
                SELECT id, message_type, routing_key, payload, created_at, published_at FROM outbox
                 WHERE payload->>'aggregateId' = $1 ORDER BY id
                """,
                str(aggregate_id),
            )
            result["messages"] = [
                {
                    "outboxId": row["id"], "type": row["message_type"], "routingKey": row["routing_key"],
                    "createdAt": row["created_at"].isoformat(),
                    "publishedAt": row["published_at"].isoformat() if row["published_at"] else None,
                    "envelope": row["payload"],
                }
                for row in rows
            ]
    return result


@app.get("/diagnostics/chain", tags=["diagnostics"], summary="Синхронний ланцюжок: кожен сервіс дописує свій фрагмент")
async def diagnostics_chain(request: Request, message: str = "ping") -> dict:
    started = time.perf_counter()
    hop = {
        "service": settings.service_name,
        "instance": settings.instance_id,
        "protocol": f"HTTP/{request.scope.get('http_version', '1.1')} REST",
        "receivedFrom": f"api-gateway@{request.headers['x-forwarded-server']}"
        if "x-forwarded-server" in request.headers else "client",
        "appended": f"calendar[{settings.instance_id}]",
    }
    payload = {"message": f"{message} -> {hop['appended']}", "hops": [hop]}
    try:
        reply = await app.state.participation.request("POST", "/diagnostics/chain", json=payload)
        payload = reply.json()
    except ServiceUnavailableError as exc:
        payload["error"] = str(exc)
    payload["totalMs"] = round((time.perf_counter() - started) * 1000, 1)
    return payload
