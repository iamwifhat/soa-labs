"""REST-клієнт до API системи календаря (ЛР3) через шлюз Traefik.

Користувач задається при створенні клієнта і передається в X-User-Id; модель його не обирає.
До впровадження OIDC у ЛР5 це єдиний спосіб автентифікації в системі.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx

SERVICES = {"calendar": "/api/calendar", "participation": "/api/participation", "notification": "/api/notification"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status, self.code, self.message = status, code, message

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "status": self.status, "error": self.code, "message": self.message}


class CalendarApi:
    def __init__(self, base_url: str, user_id: str, *, timeout: float = 10.0, correlation_id: str | None = None):
        self.user_id = user_id
        # Один correlation id на сесію агента: у журналах сервісів видно всі його виклики.
        self.correlation_id = correlation_id or f"agent-{uuid.uuid4()}"
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"X-User-Id": user_id, "X-Correlation-ID": self.correlation_id},
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> CalendarApi:
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def _call(self, method: str, path: str, *, json: Any = None, params: dict | None = None,
                    headers: dict | None = None) -> tuple[Any, httpx.Headers]:
        try:
            response = await self._http.request(method, path, json=json, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise ApiError(503, "unavailable", f"API недоступний: {exc.__class__.__name__}") from exc
        body = response.json() if response.content else None
        if response.status_code >= 400:
            if isinstance(body, dict) and "error" in body:
                raise ApiError(response.status_code, body["error"], body.get("message", ""))
            if isinstance(body, dict) and "detail" in body:  # помилка валідації FastAPI
                raise ApiError(response.status_code, "validation_error", str(body["detail"])[:500])
            raise ApiError(response.status_code, "http_error", response.text[:300])
        return body, response.headers

    # ---------- Календарі ----------

    async def list_calendars(self) -> list[dict]:
        return (await self._call("GET", "/api/calendar/calendars"))[0]

    async def create_calendar(self, name: str, time_zone: str) -> dict:
        return (await self._call("POST", "/api/calendar/calendars", json={"name": name, "timeZone": time_zone}))[0]

    async def get_calendar(self, calendar_id: str) -> dict:
        return (await self._call("GET", f"/api/calendar/calendars/{calendar_id}"))[0]

    # ---------- Події ----------

    async def list_events(self, calendar_id: str) -> list[dict]:
        return (await self._call("GET", "/api/calendar/events", params={"calendarId": calendar_id}))[0]

    async def get_event(self, event_id: str) -> dict:
        return (await self._call("GET", f"/api/calendar/events/{event_id}"))[0]

    async def create_event(self, calendar_id: str, title: str, start_at: str, end_at: str, time_zone: str,
                           invitee_user_ids: list[str], remind_before_minutes: int = 15) -> dict:
        body = {
            "calendarId": calendar_id, "title": title, "startAt": start_at, "endAt": end_at, "timeZone": time_zone,
            "inviteeUserIds": invitee_user_ids, "remindBeforeMinutes": remind_before_minutes,
        }
        return (await self._call("POST", "/api/calendar/events", json=body))[0]

    async def update_event(self, event_id: str, expected_version: int, **changes: Any) -> dict:
        names = {"title": "title", "start_at": "startAt", "end_at": "endAt", "time_zone": "timeZone"}
        body = {names[key]: value for key, value in changes.items() if value is not None}
        return (await self._call("PATCH", f"/api/calendar/events/{event_id}", json=body,
                                 headers={"If-Match": f'"{expected_version}"'}))[0]

    async def cancel_event(self, event_id: str, expected_version: int) -> dict:
        return (await self._call("POST", f"/api/calendar/events/{event_id}/cancel",
                                 headers={"If-Match": f'"{expected_version}"'}))[0]

    async def event_overview(self, event_id: str) -> dict:
        return (await self._call("GET", f"/api/calendar/events/{event_id}/overview"))[0]

    # ---------- SAGA ----------

    async def get_saga(self, saga_id: str) -> dict:
        return (await self._call("GET", f"/api/calendar/sagas/{saga_id}"))[0]

    async def wait_for_saga(self, saga_id: str, timeout: float = 20.0, on_poll=None) -> dict:
        """SAGA асинхронна: чекаємо, доки координатор переведе її зі стану Started."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            saga = await self.get_saga(saga_id)
            if saga["status"] != "Started" or loop.time() >= deadline:
                return saga
            if on_poll:
                await on_poll(saga)
            await asyncio.sleep(0.5)

    async def outbox_messages(self, aggregate_id: str) -> list[dict]:
        body = (await self._call("GET", "/api/calendar/diagnostics/outbox", params={"aggregateId": aggregate_id}))[0]
        return body.get("messages", [])

    # ---------- Запрошення та сповіщення ----------

    async def list_invitations(self, event_id: str | None = None) -> list[dict]:
        params = {"eventId": event_id} if event_id else None
        return (await self._call("GET", "/api/participation/invitations", params=params))[0]

    async def respond_to_invitation(self, invitation_id: str, decision: str, event_version: int,
                                    idempotency_key: str | None = None) -> dict:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return (await self._call("POST", f"/api/participation/invitations/{invitation_id}/respond",
                                 json={"decision": decision, "eventVersion": event_version}, headers=headers))[0]

    async def list_notifications(self) -> list[dict]:
        return (await self._call("GET", "/api/notification/notifications"))[0]

    # ---------- Контракти ----------

    async def openapi(self, service: str) -> dict:
        return (await self._call("GET", f"{SERVICES[service]}/openapi.json"))[0]
