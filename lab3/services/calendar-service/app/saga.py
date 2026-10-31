"""Оркестрована SAGA: координатор у сервісі календаря веде журнал кроків і запускає компенсації.

Сценарії:
* CreateEvent — подія (Pending) -> запрошення в participation-service -> підтвердження події;
* RescheduleEvent — новий час -> оновлення запрошень -> підтвердження перенесення.
Збій учасника фіксується двома шляхами: повідомлення потрапляє в DLQ або
за дедлайн не надходить відповідь. В обох випадках координатор компенсує власну
транзакцію і повідомляє учасників через outbox.
"""
import asyncio
import logging
import uuid
from datetime import datetime

import asyncpg

from soa_common.messaging import new_envelope
from soa_common.outbox import accept_once, add_to_outbox

from . import store
from .domain import Event, TimeSlot

log = logging.getLogger("saga")

CREATE = "CreateEvent"
RESCHEDULE = "RescheduleEvent"
STARTED, COMPLETED, COMPENSATED = "Started", "Completed", "Compensated"
FORWARD_MESSAGES = ("EventCreated.v1", "EventRescheduled.v1")


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class SagaCoordinator:
    def __init__(self, pool: asyncpg.Pool, producer: str, timeout_seconds: float) -> None:
        self._pool = pool
        self._producer = producer
        self.timeout_seconds = timeout_seconds

    def _envelope(self, type_: str, event: Event, correlation_id: str, extra: dict | None = None) -> dict:
        return new_envelope(
            type_, event.id, event.version, {**event.snapshot(), **(extra or {})}, self._producer, correlation_id
        )

    async def _log(self, connection: asyncpg.Connection, saga_id: uuid.UUID, entry: str) -> None:
        await connection.execute("INSERT INTO saga_log (saga_id, entry) VALUES ($1, $2)", saga_id, entry)
        log.info("SAGA %s: %s", saga_id, entry)

    async def _insert(self, connection, saga_id, saga_type, event_id, correlation_id, compensation=None) -> None:
        await connection.execute(
            """
            INSERT INTO sagas (id, saga_type, event_id, status, correlation_id, compensation, deadline_at)
            VALUES ($1, $2, $3, 'Started', $4, $5, now() + make_interval(secs => $6))
            """,
            saga_id, saga_type, event_id, correlation_id, compensation or {}, self.timeout_seconds,
        )

    # ---------- Початок SAGA (у транзакції запиту користувача) ----------

    async def start_creation(self, connection: asyncpg.Connection, event: Event, correlation_id: str) -> uuid.UUID:
        saga_id = uuid.uuid4()
        event.lock(saga_id)
        await store.insert_event(connection, event)
        await self._insert(connection, saga_id, CREATE, event.id, correlation_id)
        await add_to_outbox(
            connection, self._envelope("EventCreated.v1", event, correlation_id), "calendar.event.created"
        )
        await self._log(connection, saga_id, "Крок 1 (calendar): подію збережено зі статусом Pending; "
                                             "EventCreated.v1 записано в outbox у тій самій транзакції")
        return saga_id

    async def start_reschedule(
        self, connection: asyncpg.Connection, event: Event, previous_version: int, previous_slot: TimeSlot,
        changes: list[str], correlation_id: str,
    ) -> uuid.UUID:
        saga_id = uuid.uuid4()
        event.lock(saga_id)
        await store.update_event(connection, event, previous_version)
        compensation = {
            "startAt": previous_slot.start_at.isoformat(),
            "endAt": previous_slot.end_at.isoformat(),
            "timeZone": previous_slot.time_zone,
            "failedVersion": event.version,
        }
        await self._insert(connection, saga_id, RESCHEDULE, event.id, correlation_id, compensation)
        await add_to_outbox(
            connection, self._envelope("EventRescheduled.v1", event, correlation_id, {"changes": changes}),
            "calendar.event.rescheduled",
        )
        await self._log(connection, saga_id, f"Крок 1 (calendar): новий час збережено як версію {event.version}, "
                                             "попередній час — у даних компенсації; EventRescheduled.v1 записано в outbox")
        return saga_id

    # ---------- Відповідь учасника ----------

    async def on_reply(self, envelope: dict, _message) -> None:
        saga_id = uuid.UUID(envelope["data"]["sagaId"])
        async with self._pool.acquire() as connection, connection.transaction():
            if not await accept_once(connection, envelope):
                log.info("Дублікат відповіді %s пропущено (inbox)", envelope["messageId"])
                return
            saga = await connection.fetchrow("SELECT * FROM sagas WHERE id = $1 FOR UPDATE", saga_id)
            if saga is None:
                log.warning("Відповідь для невідомої SAGA %s", saga_id)
                return
            if saga["status"] != STARTED:
                await self._log(connection, saga_id, f"Запізнілу відповідь {envelope['type']} проігноровано: "
                                                     f"SAGA вже у стані {saga['status']}")
                return
            event = await store.get_event(connection, saga["event_id"], for_update=True)
            previous_version = event.version
            if saga["saga_type"] == CREATE:
                event.confirm()
                type_, routing_key = "EventConfirmed.v1", "calendar.event.confirmed"
            else:
                event.complete_reschedule()
                type_, routing_key = "EventRescheduleConfirmed.v1", "calendar.event.reschedule-confirmed"
            await store.update_event(connection, event, previous_version)
            await connection.execute("UPDATE sagas SET status = $2, finished_at = now() WHERE id = $1", saga_id, COMPLETED)
            await add_to_outbox(
                connection, self._envelope(type_, event, saga["correlation_id"], {"sagaId": str(saga_id)}), routing_key
            )
            await self._log(connection, saga_id, f"Крок 2 (participation): отримано {envelope['type']} "
                                                 f"від {envelope['producer']}")
            await self._log(connection, saga_id, f"SAGA завершено: подія v{event.version} {event.status}; "
                                                 f"{type_} записано в outbox")

    # ---------- Виявлення збою ----------

    async def on_dead_letter(self, envelope: dict, message) -> None:
        headers = message.headers or {}
        reason = _text(headers.get("x-first-death-reason", "unknown"))
        queue = _text(headers.get("x-first-death-queue", "?"))
        if envelope["type"] not in FORWARD_MESSAGES or not envelope["data"].get("sagaId"):
            # Компенсаційні повідомлення повторюються до успіху; якщо вони в DLQ, потрібне ручне втручання.
            log.error("У DLQ %s потрапило %s (%s); потрібне ручне втручання", queue, envelope["type"], reason)
            return
        await self.compensate(
            uuid.UUID(envelope["data"]["sagaId"]),
            f"повідомлення {envelope['type']} потрапило в DLQ черги {queue} (причина: {reason})",
        )

    async def compensate(self, saga_id: uuid.UUID, reason: str) -> bool:
        async with self._pool.acquire() as connection, connection.transaction():
            saga = await connection.fetchrow("SELECT * FROM sagas WHERE id = $1 FOR UPDATE", saga_id)
            if saga is None or saga["status"] != STARTED:
                log.info("SAGA %s уже завершено, компенсація не потрібна", saga_id)
                return False
            await self._compensate_locked(connection, saga, reason)
            return True

    async def _compensate_locked(self, connection: asyncpg.Connection, saga: asyncpg.Record, reason: str) -> None:
        event = await store.get_event(connection, saga["event_id"], for_update=True)
        previous_version = event.version
        extra = {"sagaId": str(saga["id"])}
        if saga["saga_type"] == CREATE:
            event.compensate_creation()
            envelope = self._envelope("EventCancelled.v1", event, saga["correlation_id"],
                                      {**extra, "reason": "SagaCompensation"})
            routing_key = "calendar.event.cancelled"
        else:
            data = saga["compensation"]
            event.compensate_reschedule(TimeSlot(
                datetime.fromisoformat(data["startAt"]), datetime.fromisoformat(data["endAt"]), data["timeZone"]
            ))
            envelope = self._envelope("EventRescheduleReverted.v1", event, saga["correlation_id"],
                                      {**extra, "revertedVersion": data["failedVersion"]})
            routing_key = "calendar.event.reschedule-reverted"
        await store.update_event(connection, event, previous_version)
        await connection.execute(
            "UPDATE sagas SET status = $2, failure_reason = $3, finished_at = now() WHERE id = $1",
            saga["id"], COMPENSATED, reason,
        )
        await add_to_outbox(connection, envelope, routing_key)
        await self._log(connection, saga["id"], f"Збій: {reason}")
        await self._log(connection, saga["id"], f"Компенсація: подія v{event.version} {event.status}, "
                                                f"{envelope['type']} записано в outbox для учасників")

    async def expire_loop(self) -> None:
        """Таймаут SAGA: SKIP LOCKED дозволяє кільком екземплярам перевіряти дедлайни без подвійної компенсації."""
        while True:
            try:
                async with self._pool.acquire() as connection, connection.transaction():
                    expired = await connection.fetch(
                        """
                        SELECT * FROM sagas WHERE status = 'Started' AND deadline_at < now()
                         ORDER BY deadline_at LIMIT 10 FOR UPDATE SKIP LOCKED
                        """
                    )
                    for saga in expired:
                        await self._compensate_locked(
                            connection, saga,
                            f"таймаут {self.timeout_seconds:g} с: відповідь учасника не надійшла",
                        )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Помилка перевірки дедлайнів SAGA")
            await asyncio.sleep(1)

    async def describe(self, connection: asyncpg.Connection, saga_id: uuid.UUID) -> dict | None:
        saga = await connection.fetchrow("SELECT * FROM sagas WHERE id = $1", saga_id)
        if saga is None:
            return None
        entries = await connection.fetch(
            "SELECT created_at, entry FROM saga_log WHERE saga_id = $1 ORDER BY id", saga_id
        )
        return {
            "sagaId": str(saga["id"]),
            "type": saga["saga_type"],
            "eventId": str(saga["event_id"]),
            "status": saga["status"],
            "failureReason": saga["failure_reason"],
            "startedAt": saga["started_at"].isoformat(),
            "deadlineAt": saga["deadline_at"].isoformat(),
            "finishedAt": saga["finished_at"].isoformat() if saga["finished_at"] else None,
            "log": [{"at": row["created_at"].isoformat(), "entry": row["entry"]} for row in entries],
        }
