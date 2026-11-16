"""Спільне для A2A-агентів: Agent Card, автентифікація агентів, пошук через Consul і клієнт."""
from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_data_parts, get_text_parts, new_data_part, new_message, new_text_part
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill, APIKeySecurityScheme, Role,
    SecurityScheme, SendMessageRequest, Task,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials, SimpleUser
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import config
from .discovery import gateway_route, register_in_consul

log = logging.getLogger("a2a")

AGENT_KEY_HEADER = "X-Agent-Key"
PUBLIC_PATHS = {AGENT_CARD_WELL_KNOWN_PATH, "/health"}


# ---------- Хто викликає агента ----------

@dataclass(frozen=True)
class Caller:
    name: str       # ідентичність агента-клієнта
    user_id: str    # користувач, від імені якого він діє


def load_callers() -> dict[str, Caller]:
    """A2A_CLIENT_KEYS="назва|користувач|ключ,..." — ключі агентів-клієнтів (до OAuth 2.0 у ЛР5)."""
    callers = {}
    for item in filter(None, os.getenv("A2A_CLIENT_KEYS", "").split(",")):
        name, user_id, key = (part.strip() for part in item.split("|"))
        callers[key] = Caller(name, user_id)
    return callers


class AgentKeyMiddleware:
    """Картка агента публічна, а JSON-RPC — лише для агентів із відомим ключем (схема з Agent Card)."""

    def __init__(self, app, callers: dict[str, Caller]) -> None:
        self.app, self.callers = app, callers

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            return await self.app(scope, receive, send)
        caller = self.callers.get(Headers(scope=scope).get(AGENT_KEY_HEADER.lower(), ""))
        if caller is None:
            response = JSONResponse({"error": "unauthorized", "message": f"потрібен дійсний {AGENT_KEY_HEADER}"},
                                    status_code=401)
            return await response(scope, receive, send)
        # SDK бере користувача з request.user: так ідентичність потрапляє в RequestContext виконавця.
        scope["user"], scope["auth"] = SimpleUser(f"{caller.name}|{caller.user_id}"), AuthCredentials(["agent"])
        return await self.app(scope, receive, send)


def caller_of(context) -> Caller:
    name, _, user_id = context.call_context.user.user_name.partition("|")
    return Caller(name, user_id)


# ---------- Agent Card і застосунок ----------

def agent_card(name: str, description: str, skills: list[AgentSkill]) -> AgentCard:
    return AgentCard(
        name=name,
        description=description,
        version="4.0.0",
        provider=AgentProvider(organization="КН-61мп, ЛР4", url="https://github.com/iamwifhat/soa-labs"),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url="http://placeholder/",
                                             protocol_version="1.0")],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        security_schemes={"agentKey": SecurityScheme(api_key_security_scheme=APIKeySecurityScheme(
            name=AGENT_KEY_HEADER, location="header", description="Ключ агента-клієнта"))},
        skills=skills,
    )


def public_url(request: Request) -> str:
    """Адреса інтерфейсу — та, через яку прийшов запит: шлюз (X-Forwarded-*) або пряма адреса в мережі."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    prefix = request.headers.get("x-forwarded-prefix", "")
    return f"{proto}://{host}{prefix}/"


def build_app(card: AgentCard, executor, *, service: str, prefix: str,
              on_startup: Callable[[], Awaitable[None]] | None = None,
              on_shutdown: Callable[[], Awaitable[None]] | None = None) -> Starlette:
    handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card)

    async def card_endpoint(request: Request) -> JSONResponse:
        served = AgentCard()
        served.CopyFrom(card)
        served.supported_interfaces[0].url = public_url(request)
        return JSONResponse(agent_card_to_dict(served))

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "UP", "service": service})

    @contextlib.asynccontextmanager
    async def lifespan(_app) -> AsyncIterator[None]:
        meta = {"protocol": "a2a", "card": AGENT_CARD_WELL_KNOWN_PATH, "gatewayPrefix": prefix, "lab": "4"}
        async with register_in_consul(service, 8000, gateway_route(service, prefix), meta):
            if on_startup:
                await on_startup()
            yield
            if on_shutdown:
                await on_shutdown()

    routes = [Route(AGENT_CARD_WELL_KNOWN_PATH, card_endpoint, methods=["GET"]), Route("/health", health)]
    routes += create_jsonrpc_routes(handler, "/")
    return Starlette(routes=routes, lifespan=lifespan,
                     middleware=[Middleware(AgentKeyMiddleware, callers=load_callers())])


# ---------- Клієнт: пошук агента і виклик навички ----------

async def discover(service: str) -> str:
    """Адресу агента дає Consul (лише екземпляри зі справною перевіркою стану), навички — його картка."""
    async with httpx.AsyncClient(base_url=config.CONSUL_URL, timeout=5) as consul:
        response = await consul.get(f"/v1/health/service/{service}", params={"passing": "true"})
        response.raise_for_status()
        entries = response.json()
    if not entries:
        raise RuntimeError(f"У Consul немає справного екземпляра {service}")
    item = entries[0]["Service"]
    return f"http://{item['Address']}:{item['Port']}"


class RemoteAgent:
    def __init__(self, base_url: str, key: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(headers={AGENT_KEY_HEADER: key}, timeout=timeout)
        self.card: AgentCard | None = None
        self._client = None

    async def __aenter__(self) -> RemoteAgent:
        self.card = await A2ACardResolver(httpx_client=self._http, base_url=self.base_url).get_agent_card()
        self._client = await create_client(agent=self.card,
                                           client_config=ClientConfig(streaming=False, httpx_client=self._http))
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._client:
            await self._client.close()
        await self._http.aclose()

    async def send(self, text: str | None = None, data: dict | None = None) -> Task:
        parts = ([new_text_part(text)] if text else []) + ([new_data_part(data)] if data is not None else [])
        request = SendMessageRequest(message=new_message(parts=parts, role=Role.ROLE_USER))
        task = None
        async for chunk in self._client.send_message(request):
            if chunk.HasField("task"):
                task = chunk.task
        if task is None:
            raise RuntimeError("агент не повернув задачу")
        return task


def task_result(task: Task) -> dict[str, Any]:
    """Стан задачі, текст статусу та дані артефактів — у вигляді, зручному для журналу і перевірок."""
    from a2a.types import TaskState
    artifacts = [{"name": a.name, "data": get_data_parts(a.parts), "text": get_text_parts(a.parts)}
                 for a in task.artifacts]
    status_text = get_text_parts(task.status.message.parts) if task.status.HasField("message") else []
    return {"taskId": task.id, "state": TaskState.Name(task.status.state), "status": " ".join(status_text),
            "artifacts": artifacts}
