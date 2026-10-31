"""Сервіс участі (ЛР3): учасник SAGA з Transactional Inbox/Outbox."""
import asyncio
import contextlib
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import asyncpg
from fastapi import FastAPI, Header, Query, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from soa_common import outbox
from soa_common.config import ConsulConfig
from soa_common.db import create_pool
from soa_common.discovery import ConsulRegistry
from soa_common.http_client import ServiceClient, ServiceUnavailableError
from soa_common.messaging import MessageBus, new_envelope
from soa_common.settings import configure_logging, load_settings
from soa_common.web import ForbiddenError, UserId, current_correlation_id, gateway_tags, setup_http

from . import store
from .domain import EVENT_PENDING, EVENT_RESCHEDULING, EVENT_SCHEDULED, EventSnapshot, Invitation

settings = load_settings("participation-service")
configure_logging(settings)
log = logging.getLogger("participation")
SCHEMA = (Path(__file__).parent / "schema.sql").read_text() + outbox.SCHEMA
SLOW_CONSUMER_SECONDS = float(os.getenv("SLOW_CONSUMER_SECONDS", "45"))
# Прямі кроки SAGA; компенсаційні повідомлення збою не імітують, бо мають завершуватися завжди.
FORWARD_STEPS = ("EventCreated.v1", "EventRescheduled.v1")


class SimulatedWriteError(RuntimeError):
    pass


class FaultInjector:
    """Керована відмова для перевірки SAGA: режим задається ключем Consul KV config/participation-service/fault-mode.

    none  — звичайна робота;
    error — запис запрошень завершується винятком, транзакція відкочується, повідомлення повторюється і йде в DLQ;
    delay — споживач відповідає пізніше за дедлайн SAGA.
    """

    def __init__(self, config: ConsulConfig) -> None:
        self._config = config

    async def mode(self) -> str:
        return await self._config.get("fault-mode", "none")

    async def before_step(self, envelope: dict) -> None:
        if await self.mode() == "delay":
            log.warning("fault-mode=delay: обробку %s відкладено на %s с", envelope["type"], SLOW_CONSUMER_SECONDS)
            await asyncio.sleep(SLOW_CONSUMER_SECONDS)

    async def after_write(self, envelope: dict) -> None:
        if await self.mode() == "error":
            raise SimulatedWriteError(f"імітований збій запису під час {envelope['type']} (fault-mode=error)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool(settings.database_url, SCHEMA)
    app.state.bus = MessageBus(settings.rabbitmq_url, settings.service_name, settings.instance_id)
    await app.state.bus.connect()
    app.state.config = ConsulConfig(settings.consul_url, settings.service_name)
    app.state.faults = FaultInjector(app.state.config)
    await app.state.bus.subscribe(
        "participation.calendar-events",
        [
            "calendar.event.created", "calendar.event.confirmed", "calendar.event.rescheduled",
            "calendar.event.reschedule-confirmed", "calendar.event.reschedule-reverted",
            "calendar.event.updated", "calendar.event.cancelled",
        ],
        on_calendar_event,
    )
    relay = asyncio.create_task(outbox.OutboxRelay(app.state.pool, app.state.bus).run())
    registry = ConsulRegistry(settings.consul_url)
    app.state.notification = ServiceClient(registry, "notification-service")
    await registry.register(
        settings.service_name, settings.port, gateway_tags(settings, "participation"),
        {"instance": settings.instance_id, "lab": "3"},
    )
    yield
    await registry.deregister()
    relay.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await relay
    await app.state.notification.close()
    await registry.close()
    await app.state.config.close()
    await app.state.bus.close()
    await app.state.pool.close()


app = FastAPI(
    title="Participation Service",
    description="Контекст «Участь у подіях»: запрошення, відповіді адресатів і кроки SAGA.",
    version="3.0.0",
    root_path=settings.gateway_prefix,
    lifespan=lifespan,
)
setup_http(app, settings)


def _envelope(type_: str, aggregate_id, version: int, data: dict, correlation_id: str) -> dict:
    return new_envelope(type_, aggregate_id, version, data, settings.service_name, correlation_id)


# ---------- Споживач подій календаря ----------

async def on_calendar_event(envelope: dict, _message) -> None:
    handler = HANDLERS.get(envelope["type"])
    if handler is None:
        log.info("Пропущено невідомий тип %s", envelope["type"])
        return
    forward = envelope["type"] in FORWARD_STEPS
    if forward:
        await app.state.faults.before_step(envelope)
    async with app.state.pool.acquire() as connection, connection.transaction():
        # Inbox у тій самій транзакції: або оброблено і записано messageId, або нічого.
        if not await outbox.accept_once(connection, envelope):
            log.info("Дублікат %s %s пропущено (inbox)", envelope["type"], envelope["messageId"])
            return
        await handler(connection, envelope)
        if forward:
            await app.state.faults.after_write(envelope)


async def _event_created(connection: asyncpg.Connection, envelope: dict) -> None:
    data = envelope["data"]
    event_id = uuid.UUID(data["eventId"])
    snapshot = EventSnapshot.from_data(data, EVENT_PENDING)
    created = []
    for invitee_id in data["inviteeUserIds"]:
        invitation = Invitation.issue(event_id, data["organizerId"], invitee_id, snapshot)
        if await store.insert_if_absent(connection, invitation):
            created.append(str(invitation.id))
    await outbox.add_to_outbox(connection, _envelope(
        "InvitationsCreated.v1", event_id, snapshot.version,
        {"sagaId": data["sagaId"], "eventId": str(event_id), "invitationIds": created},
        envelope["correlationId"],
    ), "participation.saga.invitations-created")
    log.info("EventCreated %s: створено запрошень %s; відповідь SAGA записано в outbox", event_id, len(created))


async def _event_confirmed(connection: asyncpg.Connection, envelope: dict) -> None:
    data = envelope["data"]
    changed = 0
    for invitation in await store.for_event(connection, uuid.UUID(data["eventId"]), for_update=True):
        if invitation.confirm_event(int(data["version"])):
            await store.save(connection, invitation)
            changed += 1
    log.info("%s %s v%s: активовано запрошень %s", envelope["type"], data["eventId"], data["version"], changed)


async def _event_rescheduled(connection: asyncpg.Connection, envelope: dict) -> None:
    data = envelope["data"]
    event_id = uuid.UUID(data["eventId"])
    snapshot = EventSnapshot.from_data(data, EVENT_RESCHEDULING)
    updated = reset = 0
    for invitation in await store.for_event(connection, event_id, for_update=True):
        before = invitation.status
        if invitation.apply_reschedule(snapshot):
            await store.save(connection, invitation)
            updated += 1
            reset += before != invitation.status
    await outbox.add_to_outbox(connection, _envelope(
        "InvitationsRescheduled.v1", event_id, snapshot.version,
        {"sagaId": data["sagaId"], "eventId": str(event_id), "eventVersion": snapshot.version,
         "updated": updated, "resetResponses": reset},
        envelope["correlationId"],
    ), "participation.saga.invitations-rescheduled")
    log.info("EventRescheduled %s v%s: оновлено %s, скинуто відповідей %s", event_id, snapshot.version, updated, reset)


async def _reschedule_reverted(connection: asyncpg.Connection, envelope: dict) -> None:
    data = envelope["data"]
    restored = EventSnapshot.from_data(data, EVENT_SCHEDULED)
    changed = 0
    for invitation in await store.for_event(connection, uuid.UUID(data["eventId"]), for_update=True):
        if invitation.revert_reschedule(int(data["revertedVersion"]), restored):
            await store.save(connection, invitation)
            changed += 1
    log.info("Компенсація перенесення %s: відновлено запрошень %s (v%s)", data["eventId"], changed, restored.version)


async def _event_updated(connection: asyncpg.Connection, envelope: dict) -> None:
    data = envelope["data"]
    snapshot = EventSnapshot.from_data(data, EVENT_SCHEDULED)
    for invitation in await store.for_event(connection, uuid.UUID(data["eventId"]), for_update=True):
        if invitation.refresh(snapshot):
            await store.save(connection, invitation)


async def _event_cancelled(connection: asyncpg.Connection, envelope: dict) -> None:
    data = envelope["data"]
    event_id = uuid.UUID(data["eventId"])
    revoked = silent = 0
    for invitation in await store.for_event(connection, event_id, for_update=True):
        announced = invitation.announced
        if not invitation.revoke():
            continue
        await store.save(connection, invitation)
        if announced:
            await outbox.add_to_outbox(connection, _envelope(
                "InvitationRevoked.v1", invitation.id, invitation.version, invitation.to_dict(),
                envelope["correlationId"],
            ), "participation.invitation.revoked")
            revoked += 1
        else:
            silent += 1  # про запрошення до непідтвердженої події адресат ще не знав
    log.info("EventCancelled %s (%s): відкликано %s, без сповіщення %s",
             event_id, data.get("reason"), revoked, silent)


HANDLERS = {
    "EventCreated.v1": _event_created,
    "EventConfirmed.v1": _event_confirmed,
    "EventRescheduled.v1": _event_rescheduled,
    "EventRescheduleConfirmed.v1": _event_confirmed,
    "EventRescheduleReverted.v1": _reschedule_reverted,
    "EventUpdated.v1": _event_updated,
    "EventCancelled.v1": _event_cancelled,
}


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
        await outbox.add_to_outbox(connection, _envelope(
            "InvitationResponded.v1", invitation.id, invitation.version, result, current_correlation_id()
        ), "participation.invitation.responded")
    return result


# ---------- Внутрішній API ----------

@app.get("/internal/events/{event_id}/invitations", tags=["internal"])
async def event_invitations(event_id: uuid.UUID) -> list[dict]:
    async with app.state.pool.acquire() as connection:
        invitations = await store.for_event(connection, event_id)
    return [
        {"inviteeId": item.invitee_id, "status": item.status, "eventVersion": item.snapshot.version,
         "eventStatus": item.snapshot.status}
        for item in invitations
    ]


@app.get("/diagnostics/inbox", tags=["diagnostics"], summary="Кількість оброблених повідомлень і стан outbox")
async def diagnostics_inbox() -> dict:
    async with app.state.pool.acquire() as connection:
        processed = await connection.fetchval("SELECT count(*) FROM inbox")
        return {"inboxProcessed": processed, "outbox": await outbox.outbox_stats(connection),
                "faultMode": await app.state.faults.mode()}


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
