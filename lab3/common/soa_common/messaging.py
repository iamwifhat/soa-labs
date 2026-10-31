"""Асинхронний обмін інтеграційними подіями через RabbitMQ."""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

log = logging.getLogger("messaging")

EVENTS_EXCHANGE = "calendar.events"
DEAD_LETTER_EXCHANGE = "calendar.dlx"
RETRY_DELAY_SECONDS = 1.0

Handler = Callable[[dict[str, Any], AbstractIncomingMessage], Awaitable[None]]


def new_envelope(
    type_: str,
    aggregate_id: Any,
    aggregate_version: int,
    data: dict[str, Any],
    producer: str,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Контракт інтеграційної події з ЛР1: тип, ідентифікатор, версія агрегату, час і дані."""
    return {
        "type": type_,
        "messageId": str(uuid.uuid4()),
        "aggregateId": str(aggregate_id),
        "aggregateVersion": aggregate_version,
        "occurredAt": datetime.now(timezone.utc).isoformat(),
        "correlationId": correlation_id or str(uuid.uuid4()),
        "producer": producer,
        "data": data,
    }


class MessageBus:
    """З'єднання з RabbitMQ під наглядом: після розриву шина сама перепідключається
    і повторно оголошує обмінники, черги та всіх споживачів."""

    def __init__(self, url: str, service_name: str, instance_id: str) -> None:
        self._url = url
        self._service_name = service_name
        self._instance_id = instance_id
        self._connection: aio_pika.abc.AbstractConnection | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None
        self._subscriptions: list[Callable[[], Awaitable[None]]] = []
        self._reconnect_task: asyncio.Task | None = None
        self._closing = False

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and not self._connection.is_closed

    async def connect(self) -> None:
        for attempt in range(1, 31):
            try:
                await self._open()
                return
            except (aio_pika.exceptions.AMQPConnectionError, OSError) as exc:
                log.warning("RabbitMQ недоступний (спроба %s): %s", attempt, exc)
                await asyncio.sleep(2)
        raise RuntimeError("Не вдалося підключитися до RabbitMQ")

    async def _open(self) -> None:
        connection = await aio_pika.connect(
            self._url, client_properties={"connection_name": f"{self._service_name}@{self._instance_id}"}
        )
        channel = await connection.channel(publisher_confirms=True)
        self._exchange = await channel.declare_exchange(EVENTS_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
        await channel.declare_exchange(DEAD_LETTER_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
        self._connection = connection
        for setup in self._subscriptions:
            await setup()
        connection.close_callbacks.add(self._on_close)

    def _on_close(self, *_args) -> None:
        if self._closing or (self._reconnect_task and not self._reconnect_task.done()):
            return
        log.warning("З'єднання з RabbitMQ втрачено; перепідключення")
        self._reconnect_task = asyncio.get_running_loop().create_task(self._reconnect())

    async def _reconnect(self) -> None:
        while not self._closing:
            await asyncio.sleep(2)
            try:
                await self._open()
            except Exception as exc:  # noqa: BLE001 - брокер ще не готовий
                log.warning("Перепідключення до RabbitMQ не вдалося: %s", exc)
                continue
            log.info("З'єднання з RabbitMQ відновлено, споживачів відновлено: %s", len(self._subscriptions))
            return

    async def publish(self, envelope: dict[str, Any], routing_key: str) -> None:
        """Публікація з підтвердженням брокера (publisher confirms)."""
        message = aio_pika.Message(
            body=json.dumps(envelope, ensure_ascii=False).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=envelope["messageId"],
            correlation_id=envelope.get("correlationId"),
            type=envelope["type"],
            app_id=self._service_name,
            timestamp=datetime.now(timezone.utc),
        )
        await self._exchange.publish(message, routing_key=routing_key)
        log.info("Опубліковано %s %s -> %s", envelope["type"], envelope["messageId"], routing_key)

    async def _declare_queue(self, channel, queue_name: str, delivery_limit: int):
        dead_letters = await channel.declare_queue(
            f"{queue_name}.dlq", durable=True, arguments={"x-queue-type": "quorum"}
        )
        await dead_letters.bind(DEAD_LETTER_EXCHANGE, routing_key=queue_name)
        queue = await channel.declare_queue(
            queue_name,
            durable=True,
            arguments={
                "x-queue-type": "quorum",
                "x-delivery-limit": delivery_limit,
                "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
                "x-dead-letter-routing-key": queue_name,
            },
        )
        return queue, dead_letters

    @staticmethod
    def _consumer(handler: Handler, limit: int):
        async def on_message(message: AbstractIncomingMessage) -> None:
            attempt = int((message.headers or {}).get("x-delivery-count", 0)) + 1
            try:
                envelope = json.loads(message.body)
            except json.JSONDecodeError:
                log.error("Некоректне повідомлення %s відправлено в DLQ", message.message_id)
                await message.reject(requeue=False)
                return
            try:
                await handler(envelope, message)
            except Exception as exc:  # noqa: BLE001 - будь-яка помилка означає повторну доставку
                log.warning(
                    "Помилка обробки %s %s, спроба %s з %s: %s",
                    envelope.get("type"), envelope.get("messageId"), attempt, limit, exc,
                )
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                await message.nack(requeue=True)
            else:
                await message.ack()

        return on_message

    async def subscribe(
        self,
        queue_name: str,
        routing_keys: list[str],
        handler: Handler,
        *,
        prefetch: int = 1,
        delivery_limit: int = 3,
    ) -> None:
        """Черга споживача з повторною доставкою та чергою невдалих повідомлень (DLQ).

        Після delivery_limit невдалих повторів quorum-черга сама перекладає
        повідомлення в обмінник calendar.dlx, звідки воно потрапляє в <черга>.dlq.
        """
        async def setup() -> None:
            channel = await self._connection.channel()
            await channel.set_qos(prefetch_count=prefetch)
            queue, _ = await self._declare_queue(channel, queue_name, delivery_limit)
            for key in routing_keys:
                await queue.bind(EVENTS_EXCHANGE, routing_key=key)
            await queue.consume(self._consumer(handler, delivery_limit + 1))
            log.info("Підписка на %s: %s", queue_name, ", ".join(routing_keys))

        await setup()
        self._subscriptions.append(setup)

    async def consume_dead_letters(self, queue_name: str, handler: Handler, *, delivery_limit: int = 3) -> None:
        """Підписка на DLQ чужої черги: так продюсер дізнається, що споживач не впорався."""
        async def setup() -> None:
            channel = await self._connection.channel()
            await channel.set_qos(prefetch_count=1)
            _, dead_letters = await self._declare_queue(channel, queue_name, delivery_limit)
            await dead_letters.consume(self._consumer(handler, delivery_limit + 1))
            log.info("Підписка на DLQ %s.dlq", queue_name)

        await setup()
        self._subscriptions.append(setup)

    async def close(self) -> None:
        self._closing = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self._connection and not self._connection.is_closed:
            await self._connection.close()
