"""Реєстрація екземплярів і пошук сервісів через Consul."""
import asyncio
import logging
import socket
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("discovery")

CACHE_TTL_SECONDS = 3.0


@dataclass(frozen=True)
class Instance:
    id: str
    address: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://{self.address}:{self.port}"


class ConsulRegistry:
    def __init__(self, consul_url: str) -> None:
        self._http = httpx.AsyncClient(base_url=consul_url, timeout=5.0)
        self._service_id: str | None = None
        self._cache: dict[str, tuple[float, list[Instance]]] = {}

    async def register(self, name: str, port: int, tags: list[str], meta: dict[str, str]) -> str:
        """Самореєстрація: екземпляр повідомляє Consul свою адресу та перевірку стану."""
        hostname = socket.gethostname()
        address = socket.gethostbyname(hostname)
        service_id = f"{name}-{hostname}"
        body = {
            "ID": service_id,
            "Name": name,
            "Address": address,
            "Port": port,
            "Tags": tags,
            "Meta": meta,
            "Check": {
                "HTTP": f"http://{address}:{port}/health",
                "Interval": "5s",
                "Timeout": "2s",
                "DeregisterCriticalServiceAfter": "30s",
            },
        }
        for attempt in range(1, 31):
            try:
                response = await self._http.put("/v1/agent/service/register", json=body)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("Consul недоступний (спроба %s): %s", attempt, exc)
                await asyncio.sleep(2)
                continue
            self._service_id = service_id
            log.info("Зареєстровано %s за адресою %s:%s", service_id, address, port)
            return service_id
        raise RuntimeError("Не вдалося зареєструватися в Consul")

    async def deregister(self) -> None:
        if not self._service_id:
            return
        try:
            await self._http.put(f"/v1/agent/service/deregister/{self._service_id}")
            log.info("Знято з реєстрації %s", self._service_id)
        except httpx.HTTPError as exc:
            log.warning("Не вдалося зняти реєстрацію: %s", exc)

    async def healthy_instances(self, name: str) -> list[Instance]:
        """Повертає лише екземпляри, у яких перевірка стану проходить успішно."""
        cached = self._cache.get(name)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
        response = await self._http.get(f"/v1/health/service/{name}", params={"passing": "true"})
        response.raise_for_status()
        instances = sorted(
            (
                Instance(entry["Service"]["ID"], entry["Service"]["Address"], entry["Service"]["Port"])
                for entry in response.json()
            ),
            key=lambda item: item.id,
        )
        self._cache[name] = (time.monotonic(), instances)
        return instances

    def invalidate(self, name: str) -> None:
        self._cache.pop(name, None)

    async def close(self) -> None:
        await self._http.aclose()
