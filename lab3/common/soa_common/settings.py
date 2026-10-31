import logging
import os
import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    service_name: str
    port: int
    consul_url: str
    database_url: str
    rabbitmq_url: str
    gateway_prefix: str
    instance_id: str


def load_settings(default_name: str) -> Settings:
    """Налаштування читаються із середовища контейнера (12-factor)."""
    return Settings(
        service_name=os.getenv("SERVICE_NAME", default_name),
        port=int(os.getenv("SERVICE_PORT", "8000")),
        consul_url=os.getenv("CONSUL_URL", "http://consul:8500"),
        database_url=os.environ["DATABASE_URL"],
        rabbitmq_url=os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/"),
        gateway_prefix=os.getenv("GATEWAY_PREFIX", ""),
        instance_id=socket.gethostname(),
    )


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format=f"%(asctime)s %(levelname)s {settings.service_name}@{settings.instance_id} [%(name)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aio_pika").setLevel(logging.WARNING)
    logging.getLogger("aiormq").setLevel(logging.WARNING)
