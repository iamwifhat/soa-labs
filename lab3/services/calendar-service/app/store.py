"""Репозиторії календарів і подій (працюють лише з коренями агрегатів)."""
import uuid

import asyncpg

from soa_common.web import NotFoundError, PreconditionFailedError

from .domain import Calendar, Event, EventTitle, TimeSlot


def _event(row: asyncpg.Record) -> Event:
    return Event(
        id=row["id"],
        calendar_id=row["calendar_id"],
        organizer_id=row["organizer_id"],
        title=EventTitle(row["title"]),
        slot=TimeSlot(row["start_at"], row["end_at"], row["time_zone"]),
        invitee_ids=list(row["invitee_ids"]),
        remind_before_minutes=row["remind_before_minutes"],
        status=row["status"],
        version=row["version"],
        saga_id=row["saga_id"],
    )


async def insert_calendar(connection: asyncpg.Connection, calendar: Calendar) -> None:
    await connection.execute(
        "INSERT INTO calendars (id, owner_id, name, time_zone) VALUES ($1, $2, $3, $4)",
        calendar.id, calendar.owner_id, calendar.name, calendar.time_zone,
    )


async def get_calendar(connection: asyncpg.Connection, calendar_id: uuid.UUID) -> Calendar:
    row = await connection.fetchrow("SELECT * FROM calendars WHERE id = $1", calendar_id)
    if row is None:
        raise NotFoundError(f"Календар {calendar_id} не знайдено")
    return Calendar(row["id"], row["owner_id"], row["name"], row["time_zone"])


async def insert_event(connection: asyncpg.Connection, event: Event) -> None:
    await connection.execute(
        """
        INSERT INTO events (id, calendar_id, organizer_id, title, start_at, end_at, time_zone,
                            invitee_ids, remind_before_minutes, status, version, saga_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """,
        event.id, event.calendar_id, event.organizer_id, event.title.value,
        event.slot.start_at, event.slot.end_at, event.slot.time_zone,
        event.invitee_ids, event.remind_before_minutes, event.status, event.version, event.saga_id,
    )


async def get_event(connection: asyncpg.Connection, event_id: uuid.UUID, for_update: bool = False) -> Event:
    query = "SELECT * FROM events WHERE id = $1" + (" FOR UPDATE" if for_update else "")
    row = await connection.fetchrow(query, event_id)
    if row is None:
        raise NotFoundError(f"Подію {event_id} не знайдено")
    return _event(row)


async def list_events(connection: asyncpg.Connection, calendar_id: uuid.UUID) -> list[Event]:
    rows = await connection.fetch("SELECT * FROM events WHERE calendar_id = $1 ORDER BY start_at", calendar_id)
    return [_event(row) for row in rows]


async def update_event(connection: asyncpg.Connection, event: Event, previous_version: int) -> None:
    """Оптимістичне блокування: запис вдається лише якщо версію ніхто не змінив після читання."""
    result = await connection.execute(
        """
        UPDATE events
           SET title = $3, start_at = $4, end_at = $5, time_zone = $6, status = $7,
               version = $8, saga_id = $9, updated_at = now()
         WHERE id = $1 AND version = $2
        """,
        event.id, previous_version, event.title.value, event.slot.start_at, event.slot.end_at,
        event.slot.time_zone, event.status, event.version, event.saga_id,
    )
    if result == "UPDATE 0":
        raise PreconditionFailedError("Подію змінено паралельним запитом; прочитайте її повторно")
