"""Transactional Outbox і Inbox: надійна публікація та одноразова обробка повідомлень."""
import asyncio
import logging
import uuid
from typing import Any

import asyncpg

from .messaging import MessageBus

log = logging.getLogger("outbox")

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id            bigserial PRIMARY KEY,
    message_id    uuid NOT NULL UNIQUE,
    message_type  text NOT NULL,
    routing_key   text NOT NULL,
    payload       jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    published_at  timestamptz
);
CREATE INDEX IF NOT EXISTS outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;

CREATE TABLE IF NOT EXISTS inbox (
    message_id    uuid PRIMARY KEY,
    message_type  text NOT NULL,
    received_at   timestamptz NOT NULL DEFAULT now()
);
"""

RELAY_LOCK_ID = 7002


async def add_to_outbox(connection: asyncpg.Connection, envelope: dict[str, Any], routing_key: str) -> None:
    """Викликається в тій самій транзакції, що й зміна агрегату: обидва записи або фіксуються, або ні."""
    await connection.execute(
        "INSERT INTO outbox (message_id, message_type, routing_key, payload) VALUES ($1, $2, $3, $4)",
        uuid.UUID(envelope["messageId"]), envelope["type"], routing_key, envelope,
    )


async def accept_once(connection: asyncpg.Connection, envelope: dict[str, Any]) -> bool:
    """Transactional Inbox: повторно доставлене повідомлення з тим самим messageId не обробляється."""
    inserted = await connection.fetchval(
        "INSERT INTO inbox (message_id, message_type) VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING true",
        uuid.UUID(envelope["messageId"]), envelope["type"],
    )
    return bool(inserted)


class OutboxRelay:
    """Фоновий ретранслятор: читає неопубліковані записи й передає їх брокеру.

    У кожному екземплярі сервісу працює свій ретранслятор, але advisory lock
    дозволяє публікувати лише одному з них одночасно, тому порядок повідомлень
    зберігається. Збій між публікацією та позначкою published_at дає повтор
    (доставка «щонайменше один раз»), який відсіює Inbox споживача.
    """

    def __init__(self, pool: asyncpg.Pool, bus: MessageBus, batch_size: int = 20, idle_delay: float = 0.3) -> None:
        self._pool = pool
        self._bus = bus
        self._batch_size = batch_size
        self._idle_delay = idle_delay

    async def run(self) -> None:
        while True:
            try:
                published = await self.publish_batch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - брокер недоступний: записи залишаються в outbox
                log.warning("Публікацію з outbox відкладено: %s", exc)
                published = 0
                await asyncio.sleep(2)
            if not published:
                await asyncio.sleep(self._idle_delay)

    async def publish_batch(self) -> int:
        async with self._pool.acquire() as connection, connection.transaction():
            if not await connection.fetchval("SELECT pg_try_advisory_xact_lock($1)", RELAY_LOCK_ID):
                return 0
            rows = await connection.fetch(
                "SELECT id, routing_key, payload FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT $1",
                self._batch_size,
            )
            for row in rows:
                await self._bus.publish(row["payload"], row["routing_key"])
                await connection.execute("UPDATE outbox SET published_at = now() WHERE id = $1", row["id"])
            return len(rows)


async def outbox_stats(connection: asyncpg.Connection) -> dict[str, Any]:
    row = await connection.fetchrow(
        """
        SELECT count(*) FILTER (WHERE published_at IS NULL) AS pending,
               count(*) FILTER (WHERE published_at IS NOT NULL) AS published,
               max(published_at) AS last_published_at
          FROM outbox
        """
    )
    return {
        "pending": row["pending"],
        "published": row["published"],
        "lastPublishedAt": row["last_published_at"].isoformat() if row["last_published_at"] else None,
    }
