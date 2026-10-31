"""Сервіс участі: запрошення та рішення адресатів."""
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, Query, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from soa_common.db import create_pool
from soa_common.discovery import ConsulRegistry
from soa_common.http_client import ServiceClient, ServiceUnavailableError
from soa_common.messaging import MessageBus, new_envelope
from soa_common.settings import configure_logging, load_settings
from soa_common.web import ForbiddenError, UserId, current_correlation_id, gateway_tags, setup_http

from . import store
from .domain import EventSnapshot, Invitation

settings = load_settings("participation-service")
configure_logging(settings)
log = logging.getLogger("participation")
SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool(settings.database_url, SCHEMA)
    app.state.bus = MessageBus(settings.rabbitmq_url, settings.service_name, settings.instance_id)
    await app.state.bus.connect()
    await app.state.bus.subscribe(
        "participation.calendar-events",
        ["calendar.event.created", "calendar.event.updated", "calendar.event.cancelled"],
        on_calendar_event,
    )
    registry = ConsulRegistry(settings.consul_url)
    app.state.notification = ServiceClient(registry, "notification-service")
    await registry.register(
        settings.service_name, settings.port, gateway_tags(settings, "participation"),
        {"instance": settings.instance_id, "lab": "2"},
    )
    yield
    await registry.deregister()
    await app.state.notification.close()
    await registry.close()
    await app.state.bus.close()
    await app.state.pool.close()


app = FastAPI(
    title="Participation Service",
    description="Контекст «Участь у подіях»: запрошення та відповіді адресатів.",
    version="2.0.0",
    root_path=settings.gateway_prefix,
    lifespan=lifespan,
)
setup_http(app, settings)


async def _publish(type_: str, routing_key: str, invitation: Invitation, correlation_id: str) -> None:
    envelope = new_envelope(
        type_, invitation.id, invitation.version, invitation.to_dict(), settings.service_name, correlation_id
    )
    try:
        await app.state.bus.publish(envelope, routing_key)
    except Exception:  # noqa: BLE001
        log.exception("Не вдалося опублікувати %s для запрошення %s", type_, invitation.id)


# ---------- Споживач подій календаря (хореографія) ----------

async def on_calendar_event(envelope: dict, _message) -> None:
    handlers = {
        "EventCreated.v1": _event_created,
        "EventUpdated.v1": _event_updated,
        "EventCancelled.v1": _event_cancelled,
    }
    handler = handlers.get(envelope["type"])
    if handler is None:
        log.info("Пропущено невідомий тип %s", envelope["type"])
        return
    await handler(envelope)


async def _event_created(envelope: dict) -> None:
    data = envelope["data"]
    event_id = uuid.UUID(data["eventId"])
    snapshot = EventSnapshot.from_data(data)
    created = []
    async with app.state.pool.acquire() as connection, connection.transaction():
        for invitee_id in data["inviteeUserIds"]:
            invitation = Invitation.issue(event_id, data["organizerId"], invitee_id, snapshot)
            if await store.insert_if_absent(connection, invitation):
                created.append(invitation)
    log.info("EventCreated %s: створено запрошень %s з %s", event_id, len(created), len(data["inviteeUserIds"]))
    for invitation in created:
        await _publish("InvitationCreated.v1", "participation.invitation.created", invitation, envelope["correlationId"])


async def _event_updated(envelope: dict) -> None:
    data = envelope["data"]
    snapshot = EventSnapshot.from_data(data)
    time_changed = "time" in data.get("changes", [])
    async with app.state.pool.acquire() as connection, connection.transaction():
        invitations = await store.for_event(connection, uuid.UUID(data["eventId"]), for_update=True)
        changed = 0
        for invitation in invitations:
            applied = invitation.apply_reschedule(snapshot) if time_changed else invitation.refresh(snapshot)
            if applied:
                await store.save(connection, invitation)
                changed += 1
    # Якщо координатор уже застосував цю версію синхронно, повідомлення нічого не змінює.
    log.info("EventUpdated %s v%s: оновлено запрошень %s", data["eventId"], snapshot.version, changed)


async def _event_cancelled(envelope: dict) -> None:
    event_id = uuid.UUID(envelope["data"]["eventId"])
    revoked = []
    async with app.state.pool.acquire() as connection, connection.transaction():
        for invitation in await store.for_event(connection, event_id, for_update=True):
            if invitation.revoke():
                await store.save(connection, invitation)
                revoked.append(invitation)
    log.info("EventCancelled %s: відкликано запрошень %s", event_id, len(revoked))
    for invitation in revoked:
        await _publish("InvitationRevoked.v1", "participation.invitation.revoked", invitation, envelope["correlationId"])


# ---------- Публічний API ----------

class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ResponseIn(CamelModel):
    decision: str
    event_version: int


@app.get("/invitations", tags=["invitations"], summary="Запрошення користувача або всі запрошення події для організатора")
async def list_invitations(
    user_id: UserId, event_id: Annotated[uuid.UUID | None, Query(alias="eventId")] = None
) -> list[dict]:
    async with app.state.pool.acquire() as connection:
        if event_id is None:
            return [item.to_dict() for item in await store.for_invitee(connection, user_id)]
        invitations = await store.for_event(connection, event_id)
    visible = [item for item in invitations if user_id in (item.organizer_id, item.invitee_id)]
    return [item.to_dict() for item in visible]


@app.get("/invitations/{invitation_id}", tags=["invitations"])
async def get_invitation(invitation_id: uuid.UUID, user_id: UserId) -> dict:
    async with app.state.pool.acquire() as connection:
        invitation = await store.get(connection, invitation_id)
    if user_id not in (invitation.organizer_id, invitation.invitee_id):
        raise ForbiddenError("Запрошення доступне лише організатору та адресату")
    return invitation.to_dict()


@app.post("/invitations/{invitation_id}/respond", tags=["invitations"], summary="Прийняти або відхилити запрошення")
async def respond(
    invitation_id: uuid.UUID,
    body: ResponseIn,
    user_id: UserId,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    async with app.state.pool.acquire() as connection, connection.transaction():
        if idempotency_key:
            stored = await connection.fetchrow(
                "SELECT user_id, response FROM idempotency_keys WHERE key = $1", idempotency_key
            )
            if stored and stored["user_id"] == user_id:
                return {**stored["response"], "replayed": True}
        invitation = await store.get(connection, invitation_id, for_update=True)
        invitation.respond(user_id, body.decision, body.event_version, datetime.now(timezone.utc))
        await store.save(connection, invitation)
        result = invitation.to_dict()
        if idempotency_key:
            await connection.execute(
                "INSERT INTO idempotency_keys (key, user_id, response) VALUES ($1, $2, $3)",
                idempotency_key, user_id, result,
            )
    await _publish("InvitationResponded.v1", "participation.invitation.responded", invitation, current_correlation_id())
    return result


# ---------- Внутрішній API для координатора та композиції ----------

@app.post("/internal/events/{event_id}/reschedule", tags=["internal"], summary="Крок оркестрації: скинути відповіді")
async def reschedule_step(event_id: uuid.UUID, snapshot_data: dict) -> dict:
    snapshot = EventSnapshot.from_data(snapshot_data)
    updated = reset = 0
    async with app.state.pool.acquire() as connection, connection.transaction():
        for invitation in await store.for_event(connection, event_id, for_update=True):
            previous_status = invitation.status
            if invitation.apply_reschedule(snapshot):
                await store.save(connection, invitation)
                updated += 1
                reset += previous_status != invitation.status
    log.info("Крок перенесення %s v%s: оновлено %s, скинуто відповідей %s", event_id, snapshot.version, updated, reset)
    return {"eventId": str(event_id), "eventVersion": snapshot.version, "updated": updated, "resetResponses": reset}


@app.get("/internal/events/{event_id}/invitations", tags=["internal"])
async def event_invitations(event_id: uuid.UUID) -> list[dict]:
    async with app.state.pool.acquire() as connection:
        invitations = await store.for_event(connection, event_id)
    return [
        {"inviteeId": item.invitee_id, "status": item.status, "eventVersion": item.snapshot.version}
        for item in invitations
    ]


@app.post("/diagnostics/chain", tags=["diagnostics"])
async def diagnostics_chain(request: Request, payload: dict) -> dict:
    appended = f"participation[{settings.instance_id}]"
    payload["message"] = f"{payload['message']} -> {appended}"
    payload["hops"].append({
        "service": settings.service_name,
        "instance": settings.instance_id,
        "protocol": f"HTTP/{request.scope.get('http_version', '1.1')} REST",
        "receivedFrom": "calendar-service (адресу знайдено в Consul, клієнтський round robin)",
        "appended": appended,
    })
    try:
        reply = await app.state.notification.request("POST", "/diagnostics/chain", json=payload)
        return reply.json()
    except ServiceUnavailableError as exc:
        payload["error"] = str(exc)
        return payload
