"""Сервіс календаря: календарі, події, координатор перенесення."""
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from soa_common.db import create_pool
from soa_common.discovery import ConsulRegistry
from soa_common.http_client import ServiceClient, ServiceUnavailableError
from soa_common.messaging import MessageBus, new_envelope
from soa_common.settings import configure_logging, load_settings
from soa_common.web import DomainError, UserId, current_correlation_id, gateway_tags, setup_http

from . import store
from .domain import Calendar, Event, EventTitle, TimeSlot
from .orchestrator import RescheduleOrchestrator

settings = load_settings("calendar-service")
configure_logging(settings)
log = logging.getLogger("calendar")
SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool(settings.database_url, SCHEMA)
    app.state.bus = MessageBus(settings.rabbitmq_url, settings.service_name, settings.instance_id)
    await app.state.bus.connect()
    registry = ConsulRegistry(settings.consul_url)
    app.state.participation = ServiceClient(registry, "participation-service")
    app.state.notification = ServiceClient(registry, "notification-service")
    app.state.orchestrator = RescheduleOrchestrator(
        app.state.participation, app.state.notification, f"{settings.service_name}@{settings.instance_id}"
    )
    await registry.register(
        settings.service_name, settings.port, gateway_tags(settings, "calendar"),
        {"instance": settings.instance_id, "lab": "2"},
    )
    yield
    await registry.deregister()
    await app.state.participation.close()
    await app.state.notification.close()
    await registry.close()
    await app.state.bus.close()
    await app.state.pool.close()


app = FastAPI(
    title="Calendar Service",
    description="Контекст «Планування календаря»: календарі, події та координатор перенесення.",
    version="2.0.0",
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


async def _publish(type_: str, routing_key: str, event: Event, extra: dict | None = None) -> None:
    """ЛР2: повідомлення публікується після коміту транзакції.

    Якщо брокер недоступний саме зараз, подію вже збережено, а повідомлення
    втрачено (проблема подвійного запису). Її розв'язує Transactional Outbox у ЛР3.
    """
    envelope = new_envelope(
        type_, event.id, event.version, {**event.snapshot(), **(extra or {})},
        settings.service_name, current_correlation_id(),
    )
    try:
        await app.state.bus.publish(envelope, routing_key)
    except Exception:  # noqa: BLE001
        log.exception("Не вдалося опублікувати %s для події %s", type_, event.id)


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


@app.post("/events", status_code=201, tags=["events"], summary="Створення події (далі — хореографія через RabbitMQ)")
async def create_event(body: EventIn, user_id: UserId, response: Response) -> dict:
    async with app.state.pool.acquire() as connection:
        calendar = await store.get_calendar(connection, body.calendar_id)
        event = Event.create(
            calendar, user_id, EventTitle(body.title),
            TimeSlot(body.start_at, body.end_at, body.time_zone),
            body.invitee_user_ids, body.remind_before_minutes,
        )
        await store.insert_event(connection, event)
    await _publish("EventCreated.v1", "calendar.event.created", event)
    response.headers["ETag"] = f'"{event.version}"'
    response.headers["Location"] = f"{settings.gateway_prefix}/events/{event.id}"
    return event.snapshot()


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


@app.patch("/events/{event_id}", tags=["events"], summary="Зміна події; перенесення виконує координатор (оркестрація)")
async def patch_event(
    event_id: uuid.UUID,
    body: EventPatch,
    user_id: UserId,
    if_match: Annotated[str, Header(alias="If-Match")],
    response: Response,
) -> dict:
    async with app.state.pool.acquire() as connection:
        event = await store.get_event(connection, event_id)
        previous_version = event.version
        slot = None
        if body.start_at or body.end_at or body.time_zone:
            slot = TimeSlot(
                body.start_at or event.slot.start_at,
                body.end_at or event.slot.end_at,
                body.time_zone or event.slot.time_zone,
            )
        title = EventTitle(body.title) if body.title is not None else None
        changes = event.change(user_id, _version(if_match), title=title, slot=slot)
        await store.update_event(connection, event, previous_version)

    result = {"event": event.snapshot(), "changes": changes}
    if "time" in changes:
        result["orchestration"] = await app.state.orchestrator.run(event.snapshot())
    await _publish("EventUpdated.v1", "calendar.event.updated", event, {"changes": changes})
    response.headers["ETag"] = f'"{event.version}"'
    return result


@app.post("/events/{event_id}/cancel", tags=["events"], summary="Скасування (далі — хореографія через RabbitMQ)")
async def cancel_event(
    event_id: uuid.UUID,
    user_id: UserId,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict:
    async with app.state.pool.acquire() as connection:
        event = await store.get_event(connection, event_id)
        previous_version = event.version
        event.cancel(user_id, _version(if_match) if if_match else event.version)
        await store.update_event(connection, event, previous_version)
    await _publish("EventCancelled.v1", "calendar.event.cancelled", event)
    response.headers["ETag"] = f'"{event.version}"'
    return event.snapshot()


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
            # Часткова відповідь замість помилки всього запиту (деградація при відкритому breaker).
            overview[key] = {"error": str(exc), "circuitBreaker": client.breaker.state}
    return overview


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
