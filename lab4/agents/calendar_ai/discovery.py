"""Реєстрація агентів у Consul і пошук інших агентів (той самий ConsulRegistry, що й у сервісів ЛР2–ЛР3)."""
from __future__ import annotations

import contextlib
import logging
import os

from soa_common.discovery import ConsulRegistry

from . import config

log = logging.getLogger("discovery")


@contextlib.asynccontextmanager
async def register_in_consul(name: str, port: int, tags: list[str], meta: dict[str, str]):
    """Самореєстрація на час життя процесу; CONSUL_REGISTER=0 вимикає її для локального запуску."""
    if os.getenv("CONSUL_REGISTER", "1") == "0":
        yield None
        return
    registry = ConsulRegistry(config.CONSUL_URL)
    await registry.register(name, port, tags, {**meta, "instance": os.getenv("HOSTNAME", "")})
    try:
        yield registry
    finally:
        await registry.deregister()
        await registry.close()


def gateway_route(router: str, prefix: str) -> list[str]:
    """Маршрут Traefik із каталогу Consul: зовнішній префікс зрізається, агент бачить шляхи від «/»."""
    return [
        "traefik.enable=true",
        f"traefik.http.routers.{router}.rule=PathPrefix(`{prefix}`)",
        f"traefik.http.routers.{router}.entrypoints=web",
        f"traefik.http.routers.{router}.middlewares={router}-strip",
        f"traefik.http.middlewares.{router}-strip.stripprefix.prefixes={prefix}",
    ]
