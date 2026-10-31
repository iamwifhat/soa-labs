"""Спільні HTTP-механізми: ідентифікація екземпляра, кореляція запитів, помилки та стан сервісу."""
import contextvars
import uuid
from typing import Annotated

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from .settings import Settings

correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)


class DomainError(Exception):
    status_code = 422
    code = "domain_rule_violated"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ForbiddenError(DomainError):
    status_code = 403
    code = "forbidden"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class PreconditionFailedError(DomainError):
    status_code = 412
    code = "version_mismatch"


def current_correlation_id() -> str:
    return correlation_id_var.get() or str(uuid.uuid4())


def gateway_tags(settings: Settings, router: str) -> list[str]:
    """Теги, за якими Traefik будує маршрут до сервісу з каталогу Consul."""
    prefix = settings.gateway_prefix
    return [
        "traefik.enable=true",
        f"traefik.http.routers.{router}.rule=PathPrefix(`{prefix}`)",
        f"traefik.http.routers.{router}.entrypoints=web",
        f"traefik.http.routers.{router}.middlewares={router}-strip,{router}-retry",
        f"traefik.http.middlewares.{router}-strip.stripprefix.prefixes={prefix}",
        # Повтор лише при мережевій помилці: запит не дійшов до екземпляра.
        f"traefik.http.middlewares.{router}-retry.retry.attempts=3",
        f"traefik.http.middlewares.{router}-retry.retry.initialinterval=100ms",
    ]


def setup_http(app: FastAPI, settings: Settings) -> None:
    served_by = f"{settings.service_name}@{settings.instance_id}"

    @app.middleware("http")
    async def instance_headers(request: Request, call_next):
        correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())
        token = correlation_id_var.set(correlation_id)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["X-Served-By"] = served_by
        response.headers["X-Correlation-ID"] = correlation_id
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "message": exc.message})

    @app.get("/health", tags=["service"], summary="Стан екземпляра для Consul і Docker")
    async def health(request: Request) -> JSONResponse:
        checks = {"database": "DOWN", "broker": "DOWN"}
        try:
            async with request.app.state.pool.acquire() as connection:
                await connection.fetchval("SELECT 1")
            checks["database"] = "UP"
        except Exception:  # noqa: BLE001
            pass
        if request.app.state.bus.is_connected:
            checks["broker"] = "UP"
        status = "UP" if all(value == "UP" for value in checks.values()) else "DOWN"
        return JSONResponse(
            status_code=200 if status == "UP" else 503,
            content={"status": status, "service": settings.service_name, "instance": settings.instance_id, "checks": checks},
        )


UserId = Annotated[
    str,
    Header(alias="X-User-Id", description="Ідентифікатор користувача; до впровадження OIDC у ЛР5 його передає клієнт"),
]
