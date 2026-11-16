"""Синхронні виклики між сервісами: клієнтське балансування та Circuit Breaker."""
import itertools
import logging
import time

import httpx

from .discovery import ConsulRegistry
from .web import current_correlation_id

log = logging.getLogger("http-client")


class ServiceUnavailableError(Exception):
    pass


class CircuitOpenError(ServiceUnavailableError):
    pass


class CircuitBreaker:
    """CLOSED -> OPEN після серії відмов; OPEN -> HALF_OPEN після паузи; пробний виклик закриває або знову відкриває."""

    def __init__(self, name: str, failure_threshold: int = 3, reset_timeout: float = 10.0) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.state = "CLOSED"
        self.failures = 0
        self.opened_at = 0.0

    def allow_request(self) -> bool:
        if self.state == "OPEN":
            if time.monotonic() - self.opened_at < self.reset_timeout:
                return False
            self.state = "HALF_OPEN"
            log.info("Circuit breaker %s: HALF_OPEN, пробний виклик", self.name)
        return True

    def record_success(self) -> None:
        if self.state != "CLOSED":
            log.info("Circuit breaker %s: CLOSED", self.name)
        self.state = "CLOSED"
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == "HALF_OPEN" or self.failures >= self.failure_threshold:
            if self.state != "OPEN":
                log.warning("Circuit breaker %s: OPEN після %s відмов", self.name, self.failures)
            self.state = "OPEN"
            self.opened_at = time.monotonic()


class ServiceClient:
    """Клієнт іншого сервісу: адреси беруться з Consul, екземпляр обирається по колу (round robin)."""

    def __init__(self, registry: ConsulRegistry, service_name: str, timeout: float = 3.0, attempts: int = 2) -> None:
        self.service_name = service_name
        self.breaker = CircuitBreaker(service_name)
        self._registry = registry
        self._attempts = attempts
        self._counter = itertools.count()
        self._http = httpx.AsyncClient(timeout=timeout)

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self.breaker.allow_request():
            raise CircuitOpenError(f"{self.service_name}: circuit breaker OPEN")
        try:
            instances = await self._registry.healthy_instances(self.service_name)
        except httpx.HTTPError as exc:
            self.breaker.record_failure()
            raise ServiceUnavailableError(f"Consul недоступний: {exc}") from exc
        if not instances:
            self.breaker.record_failure()
            raise ServiceUnavailableError(f"{self.service_name}: немає справних екземплярів")

        kwargs["headers"] = {**kwargs.get("headers", {}), "X-Correlation-ID": current_correlation_id()}
        start = next(self._counter) % len(instances)
        ordered = instances[start:] + instances[:start]
        last_error: Exception | None = None
        for instance in ordered[: self._attempts]:
            try:
                response = await self._http.request(method, instance.base_url + path, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Запит не дійшов до екземпляра, тому повтор на іншому безпечний для будь-якого методу.
                log.warning("Екземпляр %s недоступний: %s", instance.id, type(exc).__name__)
                self._registry.invalidate(self.service_name)
                last_error = exc
                continue
            except httpx.HTTPError as exc:
                last_error = exc
                break
            if response.status_code >= 500:
                last_error = ServiceUnavailableError(f"{instance.id} повернув {response.status_code}")
                break
            self.breaker.record_success()
            return response
        self.breaker.record_failure()
        raise ServiceUnavailableError(f"{self.service_name}: {last_error}")

    async def close(self) -> None:
        await self._http.aclose()
