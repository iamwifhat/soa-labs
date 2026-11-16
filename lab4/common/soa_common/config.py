"""Централізована конфігурація з Consul KV (роль Config service)."""
import logging
import time

import httpx

log = logging.getLogger("config")


class ConsulConfig:
    """Читає ключі config/<service>/<name>; значення кешується, щоб не звертатися до Consul на кожне повідомлення."""

    def __init__(self, consul_url: str, service_name: str, ttl: float = 2.0) -> None:
        self._http = httpx.AsyncClient(base_url=consul_url, timeout=2.0)
        self._prefix = f"config/{service_name}"
        self._ttl = ttl
        self._cache: dict[str, tuple[float, str | None]] = {}

    async def get(self, name: str, default: str) -> str:
        cached = self._cache.get(name)
        if cached and time.monotonic() - cached[0] < self._ttl:
            return cached[1] or default
        try:
            response = await self._http.get(f"/v1/kv/{self._prefix}/{name}", params={"raw": "true"})
            value = response.text.strip() if response.status_code == 200 else None
        except httpx.HTTPError as exc:
            log.warning("Consul KV недоступний: %s", exc)
            value = cached[1] if cached else None
        self._cache[name] = (time.monotonic(), value)
        return value or default

    async def close(self) -> None:
        await self._http.aclose()
