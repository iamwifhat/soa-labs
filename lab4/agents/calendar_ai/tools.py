"""Інструменти для function calling поверх REST-клієнта: схеми аргументів, валідація і виконання.

Pydantic-модель аргументів — єдине джерело правди: з неї будується JSON Schema для моделі, і нею ж
перевіряються аргументи, які модель повернула (модель може згенерувати що завгодно).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from .guardrails import ToolDenied, ToolPolicy, untrusted_tool_message
from .rest_client import ApiError, CalendarApi

log = logging.getLogger("tools")


# ---------- Типи аргументів із доменними обмеженнями ЛР1 ----------

def _time_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"невідомий часовий пояс IANA: {value}") from exc
    return value


def _no_control_chars(value: str) -> str:
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("текст не може містити керівних символів")
    return value.strip()


TimeZone = Annotated[str, Field(description="Часовий пояс IANA, наприклад Europe/Kyiv"), AfterValidator(_time_zone)]
Title = Annotated[str, Field(min_length=1, max_length=200, description="Назва події"), AfterValidator(_no_control_chars)]
EMAIL_PATTERN = r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,180}\.[A-Za-z]{2,24}$"
UserEmail = Annotated[str, Field(pattern=EMAIL_PATTERN, description="Ідентифікатор користувача (email)")]
MAX_HORIZON = timedelta(days=366)


class Args(BaseModel):
    # forbid: зайві поля, які модель «вигадала», — помилка, а не мовчазне ігнорування.
    model_config = ConfigDict(extra="forbid")


class NoArgs(Args):
    pass


class CreateCalendarArgs(Args):
    name: Annotated[str, Field(min_length=1, max_length=100), AfterValidator(_no_control_chars)]
    time_zone: TimeZone = "Europe/Kyiv"


class CalendarRef(Args):
    calendar_id: uuid.UUID = Field(description="UUID календаря з list_my_calendars")


class EventRef(Args):
    event_id: uuid.UUID = Field(description="UUID події")


def _check_slot(start_at: datetime | None, end_at: datetime | None) -> None:
    now = datetime.now(timezone.utc)
    if start_at and not (now - timedelta(minutes=5) <= start_at <= now + MAX_HORIZON):
        raise ValueError(f"початок {start_at.isoformat()} має бути в майбутньому і не далі ніж через рік")
    if start_at and end_at:
        if start_at >= end_at:
            raise ValueError("початок має передувати завершенню")
        if end_at - start_at > timedelta(hours=12):
            raise ValueError("подія не може тривати довше за 12 годин")


class CreateEventArgs(Args):
    calendar_id: uuid.UUID = Field(description="UUID календаря")
    title: Title
    start_at: AwareDatetime = Field(description="Початок, ISO 8601 зі зміщенням, напр. 2026-11-20T15:00:00+02:00")
    end_at: AwareDatetime = Field(description="Завершення, ISO 8601 зі зміщенням")
    time_zone: TimeZone = "Europe/Kyiv"
    invitee_user_ids: list[UserEmail] = Field(default_factory=list, max_length=20,
                                              description="Email запрошених (без організатора)")
    remind_before_minutes: int = Field(default=15, ge=1, le=1440)

    @model_validator(mode="after")
    def _slot(self) -> CreateEventArgs:
        _check_slot(self.start_at, self.end_at)
        return self


class UpdateEventArgs(Args):
    event_id: uuid.UUID = Field(description="UUID події")
    expected_version: int = Field(ge=1, description="Поточне поле version події (оптимістичне блокування)")
    title: Title | None = None
    start_at: AwareDatetime | None = Field(default=None, description="Новий початок, ISO 8601 зі зміщенням")
    end_at: AwareDatetime | None = Field(default=None, description="Нове завершення, ISO 8601 зі зміщенням")
    time_zone: TimeZone | None = None

    @model_validator(mode="after")
    def _changes(self) -> UpdateEventArgs:
        if not any((self.title, self.start_at, self.end_at, self.time_zone)):
            raise ValueError("потрібно змінити хоча б одне поле: title, start_at, end_at або time_zone")
        _check_slot(self.start_at, self.end_at)
        return self


class CancelEventArgs(Args):
    event_id: uuid.UUID
    expected_version: int = Field(ge=1)


class SagaArgs(Args):
    saga_id: uuid.UUID
    wait_seconds: int = Field(default=10, ge=0, le=20, description="Скільки чекати завершення SAGA")


class InvitationsArgs(Args):
    event_id: uuid.UUID | None = Field(default=None, description="Лише запрошення цієї події (для організатора)")


class RespondArgs(Args):
    invitation_id: uuid.UUID
    decision: Literal["Accepted", "Declined"]
    event_version: int = Field(ge=1, description="Версія події, на яку відповідає учасник (event.version)")


# ---------- Проєкції: у контекст моделі потрапляє лише потрібне ----------

def local_time(start_at: str | None, end_at: str | None, time_zone: str | None) -> str | None:
    """API зберігає час в UTC; моделі даємо готовий місцевий час, щоб вона не перераховувала пояси сама."""
    if not (start_at and end_at and time_zone):
        return None
    zone = ZoneInfo(time_zone)
    start, end = datetime.fromisoformat(start_at).astimezone(zone), datetime.fromisoformat(end_at).astimezone(zone)
    return f"{start:%d.%m.%Y %H:%M}–{end:%H:%M} ({time_zone})"


def event_brief(event: dict) -> dict:
    keys = ("eventId", "title", "status", "startAt", "endAt", "timeZone", "version", "inviteeUserIds", "sagaId")
    brief = {key: event.get(key) for key in keys}
    brief["localTime"] = local_time(event.get("startAt"), event.get("endAt"), event.get("timeZone"))
    return brief


def invitation_brief(item: dict) -> dict:
    event = item.get("event", {})
    return {
        "invitationId": item["invitationId"], "eventId": item["eventId"], "organizerId": item["organizerId"],
        "inviteeId": item["inviteeId"], "status": item["status"], "eventTitle": event.get("title"),
        "startAt": event.get("startAt"), "endAt": event.get("endAt"), "timeZone": event.get("timeZone"),
        "localTime": local_time(event.get("startAt"), event.get("endAt"), event.get("timeZone")),
        "eventVersion": event.get("version"), "eventStatus": event.get("status"),
    }


def saga_brief(saga: dict) -> dict:
    return {key: saga.get(key) for key in ("sagaId", "type", "status", "failureReason", "eventId")}


# ---------- Реєстр інструментів ----------

Handler = Callable[[CalendarApi, Any], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[Args]
    handler: Handler
    read_only: bool = True
    destructive: bool = False


async def _create_event(api: CalendarApi, a: CreateEventArgs) -> dict:
    result = await api.create_event(str(a.calendar_id), a.title, a.start_at.isoformat(), a.end_at.isoformat(),
                                    a.time_zone, a.invitee_user_ids, a.remind_before_minutes)
    return {"event": event_brief(result["event"]), "sagaId": result["sagaId"],
            "note": "Подія Pending, доки SAGA не завершиться; перевір get_saga_status"}


async def _update_event(api: CalendarApi, a: UpdateEventArgs) -> dict:
    result = await api.update_event(
        str(a.event_id), a.expected_version, title=a.title, time_zone=a.time_zone,
        start_at=a.start_at.isoformat() if a.start_at else None, end_at=a.end_at.isoformat() if a.end_at else None,
    )
    return {**result, "event": event_brief(result["event"])}


async def _saga(api: CalendarApi, a: SagaArgs) -> dict:
    return saga_brief(await api.wait_for_saga(str(a.saga_id), timeout=a.wait_seconds))


TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in [
    ToolSpec("list_my_calendars", "Календарі поточного користувача: id, назва, часовий пояс.",
             NoArgs, lambda api, a: api.list_calendars()),
    ToolSpec("create_calendar", "Створити календар поточного користувача.",
             CreateCalendarArgs, lambda api, a: api.create_calendar(a.name, a.time_zone), read_only=False),
    ToolSpec("list_events", "Події календаря (коротко): id, назва, статус, час, version, запрошені.",
             CalendarRef, lambda api, a: _map(api.list_events(str(a.calendar_id)), event_brief)),
    ToolSpec("get_event", "Повні дані однієї події, зокрема поточне поле version.",
             EventRef, lambda api, a: api.get_event(str(a.event_id))),
    ToolSpec("create_event", "Створити подію і запросити учасників. Запускає SAGA CreateEvent; "
             "повертає sagaId. Час — ISO 8601 зі зміщенням.", CreateEventArgs, _create_event, read_only=False),
    ToolSpec("update_event", "Перейменувати та/або перенести подію. Потрібна expected_version = поточне поле "
             "version (інакше 412). Перенесення запускає SAGA RescheduleEvent.", UpdateEventArgs, _update_event,
             read_only=False),
    ToolSpec("cancel_event", "Скасувати подію (незворотно). Лише за явним проханням користувача.",
             CancelEventArgs, lambda api, a: api.cancel_event(str(a.event_id), a.expected_version),
             read_only=False, destructive=True),
    ToolSpec("get_saga_status", "Стан SAGA за sagaId; чекає до wait_seconds, поки SAGA у стані Started.",
             SagaArgs, _saga),
    ToolSpec("list_invitations", "Запрошення поточного користувача або (з event_id) усі запрошення його події.",
             InvitationsArgs, lambda api, a: _map(api.list_invitations(str(a.event_id) if a.event_id else None),
                                                  invitation_brief)),
    ToolSpec("respond_to_invitation", "Прийняти (Accepted) або відхилити (Declined) запрошення.",
             RespondArgs, lambda api, a: api.respond_to_invitation(str(a.invitation_id), a.decision, a.event_version),
             read_only=False),
]}


async def _map(coro: Awaitable[list[dict]], projection: Callable[[dict], dict], limit: int = 20) -> dict:
    items = await coro
    return {"items": [projection(item) for item in items[:limit]], "total": len(items),
            "truncated": len(items) > limit}


def _llm_schema(node: Any) -> Any:
    """Спрощує JSON Schema Pydantic до підмножини, яку приймають усі OpenAI-сумісні API (зокрема Gemini)."""
    if isinstance(node, dict):
        if "anyOf" in node:  # Optional[X] -> X; обов'язковість задає список required
            variants = [item for item in node["anyOf"] if item.get("type") != "null"]
            merged = {**variants[0], **{k: v for k, v in node.items() if k not in ("anyOf", "default")}}
            return _llm_schema(merged)
        result = {}
        for key, value in node.items():
            if key == "properties":  # тут ключі — імена полів (зокрема поле title), їх не чіпаємо
                result[key] = {name: _llm_schema(prop) for name, prop in value.items()}
                continue
            if key in ("title", "default", "additionalProperties", "$defs"):
                continue
            if key == "format" and value == "uuid":
                result["description"] = (node.get("description", "") + " (UUID)").strip()
                continue
            result[key] = _llm_schema(value)
        return result
    if isinstance(node, list):
        return [_llm_schema(item) for item in node]
    return node


def openai_schema(spec: ToolSpec) -> dict:
    schema = _llm_schema(spec.args.model_json_schema())
    schema.setdefault("properties", {})
    return {"type": "function", "function": {"name": spec.name, "description": spec.description, "parameters": schema}}


def validation_message(error: ValidationError) -> str:
    parts = []
    for item in error.errors()[:5]:
        where = ".".join(str(p) for p in item["loc"]) or "arguments"
        parts.append(f"{where}: {item['msg']}")
    return "; ".join(parts)


@dataclass
class ToolCallRecord:
    tool: str
    arguments: Any
    ok: bool
    outcome: str
    ms: int
    injection: list[dict] = field(default_factory=list)
    result: Any = None


class ToolBox:
    """Виконавець викликів: allowlist імен, валідація аргументів, політика, таймаут і spotlighting результату."""

    def __init__(self, api: CalendarApi, policy: ToolPolicy, *, result_max_chars: int,
                 names: list[str] | None = None, timeout: float = 25.0) -> None:
        self.api, self.policy = api, policy
        self.specs = {name: TOOLS[name] for name in (names or TOOLS)}
        self.result_max_chars = result_max_chars
        self.timeout = timeout
        self.trace: list[ToolCallRecord] = []

    def schemas(self) -> list[dict]:
        return [openai_schema(spec) for spec in self.specs.values()]

    async def call(self, name: str, raw_arguments: str) -> str:
        """Повертає текст для повідомлення role=tool; помилки теж повертаються моделі, щоб вона могла виправитися."""
        started = time.perf_counter()

        def record(ok: bool, outcome: str, args: Any, findings: list[dict] | None = None, result: Any = None) -> None:
            self.trace.append(ToolCallRecord(name, args, ok, outcome,
                                             int((time.perf_counter() - started) * 1000), findings or [], result))

        def error(outcome: str, payload: dict, args: Any = raw_arguments) -> str:
            record(False, outcome, args)
            return json.dumps({"tool": name, "ok": False, **payload}, ensure_ascii=False)

        spec = self.specs.get(name)
        if spec is None:
            return error("unknown-tool", {"error": "unknown_tool", "message": f"інструмента {name} немає"})
        try:
            raw = json.loads(raw_arguments or "{}")
            args = spec.args.model_validate(raw)
        except json.JSONDecodeError:
            return error("bad-json", {"error": "invalid_arguments", "message": "аргументи не є JSON"})
        except ValidationError as exc:
            return error("invalid-arguments", {"error": "invalid_arguments", "message": validation_message(exc)}, raw)
        dumped = args.model_dump(mode="json", exclude_none=True)
        try:
            self.policy.authorize(name, spec.destructive, dumped)
        except ToolDenied as exc:
            return error("denied", {"error": "denied_by_policy", "message": str(exc)}, dumped)
        try:
            result = await asyncio.wait_for(spec.handler(self.api, args), self.timeout)
        except ApiError as exc:
            return error(f"http-{exc.status}", exc.to_dict(), dumped)
        except asyncio.TimeoutError:
            return error("timeout", {"error": "timeout", "message": f"понад {self.timeout} с"}, dumped)
        content, findings = untrusted_tool_message(name, result, self.result_max_chars)
        record(True, "ok, injection redacted" if findings else "ok", dumped, findings, result)
        return content
