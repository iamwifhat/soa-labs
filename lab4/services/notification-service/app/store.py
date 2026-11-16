"""Сховище нагадувань і сповіщень."""
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg


def local_time(moment: datetime, time_zone: str) -> str:
    return moment.astimezone(ZoneInfo(time_zone)).strftime("%d.%m.%Y %H:%M")


async def schedule_reminder(
    connection: asyncpg.Connection, event_id: uuid.UUID, user_id: str, title: str, start_at: datetime,
    time_zone: str, remind_before_minutes: int, event_version: int,
) -> None:
    """Одне нагадування на пару «подія — користувач»; повторне повідомлення лише оновлює його."""
    await connection.execute(
        """
        INSERT INTO reminders (id, event_id, user_id, event_title, event_start_at, event_time_zone,
                               remind_before_minutes, remind_at, event_version, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'Scheduled')
        ON CONFLICT (event_id, user_id) DO UPDATE
           SET event_title = EXCLUDED.event_title, event_start_at = EXCLUDED.event_start_at,
               event_time_zone = EXCLUDED.event_time_zone, remind_at = EXCLUDED.remind_at,
               event_version = EXCLUDED.event_version, status = 'Scheduled', sent_at = NULL
         WHERE reminders.event_version <= EXCLUDED.event_version
        """,
        uuid.uuid4(), event_id, user_id, title, start_at, time_zone, remind_before_minutes,
        start_at - timedelta(minutes=remind_before_minutes), event_version,
    )


async def cancel_reminders(connection: asyncpg.Connection, event_id: uuid.UUID, user_id: str | None = None) -> int:
    result = await connection.execute(
        """
        UPDATE reminders SET status = 'Cancelled'
         WHERE event_id = $1 AND status = 'Scheduled' AND ($2::text IS NULL OR user_id = $2)
        """,
        event_id, user_id,
    )
    return int(result.split()[-1])


async def move_reminders(
    connection: asyncpg.Connection, event_id: uuid.UUID, organizer_id: str, title: str, start_at: datetime,
    time_zone: str, event_version: int,
) -> tuple[int, int]:
    """Нагадування організатора переноситься; нагадування запрошених чекають повторного підтвердження."""
    rows = await connection.fetch(
        """
        UPDATE reminders
           SET event_title = $3, event_start_at = $4::timestamptz, event_time_zone = $5, event_version = $6,
               remind_at = $4::timestamptz - make_interval(mins => remind_before_minutes), sent_at = NULL,
               status = CASE WHEN user_id = $2 THEN 'Scheduled' ELSE 'Cancelled' END
         WHERE event_id = $1 AND event_version < $6
        RETURNING status
        """,
        event_id, organizer_id, title, start_at, time_zone, event_version,
    )
    moved = sum(1 for row in rows if row["status"] == "Scheduled")
    return moved, len(rows) - moved


async def enqueue(
    connection: asyncpg.Connection, user_id: str, event_id: uuid.UUID | None, kind: str, subject: str, dedup_key: str,
) -> bool:
    """Ключ дедуплікації не дає повторному повідомленню створити другий лист."""
    inserted = await connection.fetchval(
        """
        INSERT INTO notifications (id, user_id, event_id, kind, subject, status, dedup_key)
        VALUES ($1, $2, $3, $4, $5, 'Queued', $6)
        ON CONFLICT (dedup_key) DO NOTHING
        RETURNING id
        """,
        uuid.uuid4(), user_id, event_id, kind, subject, dedup_key,
    )
    return inserted is not None


async def fire_due_reminders(connection: asyncpg.Connection) -> int:
    """SKIP LOCKED: кілька екземплярів планувальника не беруть одне нагадування двічі."""
    async with connection.transaction():
        due = await connection.fetch(
            """
            WITH due AS (
                SELECT id FROM reminders
                 WHERE status = 'Scheduled' AND remind_at <= now()
                 ORDER BY remind_at LIMIT 20
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE reminders r SET status = 'Sent', sent_at = now()
              FROM due WHERE r.id = due.id
            RETURNING r.*
            """
        )
        for row in due:
            await enqueue(
                connection, row["user_id"], row["event_id"], "Reminder",
                f"Нагадування: «{row['event_title']}» о {local_time(row['event_start_at'], row['event_time_zone'])}",
                f"reminder:{row['id']}:v{row['event_version']}",
            )
    return len(due)


async def deliver_queued(connection: asyncpg.Connection, instance: str) -> list[asyncpg.Record]:
    return await connection.fetch(
        """
        WITH batch AS (
            SELECT id FROM notifications
             WHERE status = 'Queued'
             ORDER BY created_at LIMIT 20
             FOR UPDATE SKIP LOCKED
        )
        UPDATE notifications n
           SET status = CASE
                   WHEN COALESCE((SELECT email_enabled FROM preferences p WHERE p.user_id = n.user_id), true)
                   THEN 'Sent' ELSE 'Skipped' END,
               sent_at = now(), sent_by = $1
          FROM batch WHERE n.id = batch.id
        RETURNING n.user_id, n.kind, n.subject, n.status
        """,
        instance,
    )
