"""Пул з'єднань PostgreSQL і початкова схема власної БД сервісу."""
import asyncio
import json
import logging

import asyncpg

log = logging.getLogger("db")

SCHEMA_LOCK_ID = 7001


async def _init_connection(connection: asyncpg.Connection) -> None:
    await connection.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str, schema_sql: str) -> asyncpg.Pool:
    for attempt in range(1, 31):
        try:
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10, init=_init_connection)
            break
        except (OSError, asyncpg.PostgresError) as exc:
            log.warning("PostgreSQL недоступний (спроба %s): %s", attempt, exc)
            await asyncio.sleep(2)
    else:
        raise RuntimeError("Не вдалося підключитися до PostgreSQL")
    async with pool.acquire() as connection:
        async with connection.transaction():
            # Кілька екземплярів стартують одночасно; блокування не дає їм створювати схему паралельно.
            await connection.execute("SELECT pg_advisory_xact_lock($1)", SCHEMA_LOCK_ID)
            await connection.execute(schema_sql)
    return pool
