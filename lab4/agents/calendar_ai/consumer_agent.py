"""Завдання 3: споживач ЛР3 як AI-агент — асистент запрошеного учасника, інтегрований з продюсером через A2A.

Агент споживає з RabbitMQ ті самі інтеграційні події, що й сервіси ЛР3 (EventConfirmed.v1,
EventRescheduleConfirmed.v1), і для кожного запрошення свого користувача вирішує за його політикою:
прийняти, відхилити або попросити агента-продюсера перенести подію (A2A, навичка reschedule_event).
Власна навичка A2A — decision_log: журнал рішень із поясненнями.

    uvicorn calendar_ai.consumer_agent:app --port 8000
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import socket
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from a2a.helpers import get_data_parts, new_data_part, new_task_from_user_message, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import AgentSkill, TaskState
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from soa_common.messaging import MessageBus

from . import config
from .a2a_common import RemoteAgent, agent_card, build_app, caller_of, discover, task_result
from .guardrails import BudgetExceeded, TokenBudget, sanitize
from .llm import ChatModel
from .prompts import TRUST_RULES
from .rest_client import ApiError, CalendarApi
from .tools import _check_slot

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s consumer-agent [%(name)s] %(message)s")
log = logging.getLogger("consumer-agent")
logging.getLogger("aio_pika").setLevel(logging.WARNING)
logging.getLogger("aiormq").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

USER = os.getenv("AGENT_USER_ID", "olena@pm.me")
POLICY = os.getenv("AGENT_POLICY", (
    "Я не беру участі в зустрічах, що починаються раніше 12:00 за Києвом. Будні зустрічі з 12:00 до 18:00 "
    "приймаю. Якщо зустріч раніше 12:00 — пропоную перенести її на 12:00 того самого дня з тією самою "
    "тривалістю. Вихідні дні відхиляю."
))
PRODUCER_SERVICE = os.getenv("PRODUCER_SERVICE", "producer-agent")
PRODUCER_KEY = os.getenv("PRODUCER_AGENT_KEY", "")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://calendar:calendar@rabbitmq:5672/")
MAX_PROPOSALS_PER_EVENT = 1          # guardrail: один раунд переговорів, далі лише так/ні
DECISION_TOKEN_BUDGET = int(os.getenv("DECISION_TOKEN_BUDGET", "8000"))
KYIV = ZoneInfo("Europe/Kyiv")

DECISION_TOOLS = {
    "accept_invitation": {"reason": {"type": "string", "description": "Коротке пояснення"}},
    "decline_invitation": {"reason": {"type": "string", "description": "Коротке пояснення"}},
    "propose_new_time": {
        "start_at": {"type": "string", "description": "Новий початок, ISO 8601 зі зміщенням"},
        "end_at": {"type": "string", "description": "Нове завершення, ISO 8601 зі зміщенням"},
        "reason": {"type": "string", "description": "Чому поточний час не підходить"},
    },
}
DESCRIPTIONS = {
    "accept_invitation": "Прийняти запрошення: час відповідає політиці користувача.",
    "decline_invitation": "Відхилити запрошення: час не підходить і перенесення неможливе.",
    "propose_new_time": "Попросити організатора перенести подію на час, що відповідає політиці.",
}


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_at: AwareDatetime
    end_at: AwareDatetime
    reason: str = Field(default="", max_length=200)


def _schema(name: str) -> dict:
    props = DECISION_TOOLS[name]
    return {"type": "function", "function": {"name": name, "description": DESCRIPTIONS[name], "parameters": {
        "type": "object", "properties": props, "required": list(props)}}}


class InvitationAssistant:
    """Споживач подій ЛР3 + LLM-рішення + A2A-клієнт продюсера."""

    def __init__(self) -> None:
        self.log: list[dict] = []
        self.processed: set[str] = set()                # Inbox у пам'яті; повтор відповіді гасить Idempotency-Key
        self.proposals: collections.Counter = collections.Counter()
        self.bus: MessageBus | None = None
        self._model: ChatModel | None = None

    @property
    def model(self) -> ChatModel:
        if self._model is None:
            self._model = ChatModel(config.llm_config())
        return self._model

    async def start(self) -> None:
        self.bus = MessageBus(RABBITMQ_URL, "consumer-agent", socket.gethostname())
        await self.bus.connect()
        await self.bus.subscribe("consumer-agent.invitations",
                                 ["calendar.event.confirmed", "calendar.event.reschedule-confirmed"], self.on_event)

    async def stop(self) -> None:
        if self.bus:
            await self.bus.close()

    async def on_event(self, envelope: dict, _message) -> None:
        data = envelope["data"]
        if USER not in data.get("inviteeUserIds", []) or envelope["messageId"] in self.processed:
            return
        async with CalendarApi(config.GATEWAY_URL, USER, correlation_id=envelope["correlationId"]) as api:
            invitation = await self._await_invitation(api, data["eventId"], int(data["version"]))
            if invitation["status"] != "Pending":
                self.processed.add(envelope["messageId"])
                return
            entry = await self._decide(api, envelope, invitation)
        self.processed.add(envelope["messageId"])
        self.log.append(entry)
        log.info("Рішення щодо %s v%s: %s — %s", entry["eventId"], entry["version"], entry["action"], entry["reason"])

    async def _await_invitation(self, api: CalendarApi, event_id: str, version: int) -> dict:
        """Сервіс участі обробляє ту саму подію паралельно: чекаємо, поки запрошення побачить нову версію."""
        for _ in range(30):
            items = [i for i in await api.list_invitations(event_id) if i["inviteeId"] == USER]
            if items and items[0]["event"]["version"] >= version and items[0]["event"]["status"] == "Scheduled":
                return items[0]
            await asyncio.sleep(0.5)
        raise RuntimeError(f"запрошення на {event_id} v{version} ще не оновлено")  # nack -> повторна доставка

    async def _decide(self, api: CalendarApi, envelope: dict, invitation: dict) -> dict:
        event = invitation["event"]
        start, end = datetime.fromisoformat(event["startAt"]), datetime.fromisoformat(event["endAt"])
        facts, findings = sanitize({
            "title": event["title"], "organizer": invitation["organizerId"],
            "localStart": start.astimezone(KYIV).strftime("%Y-%m-%d %H:%M %A"),
            "localEnd": end.astimezone(KYIV).strftime("%H:%M"), "durationMinutes": int((end - start).seconds / 60),
            "isoStart": event["startAt"], "isoEnd": event["endAt"], "version": event["version"],
        })
        can_propose = self.proposals[invitation["eventId"]] < MAX_PROPOSALS_PER_EVENT
        tools = [_schema("accept_invitation"), _schema("decline_invitation")]
        if can_propose:
            tools.append(_schema("propose_new_time"))
        messages = [
            {"role": "system", "content":
                f"Ти — асистент учасника {USER}: вирішуєш щодо запрошень за його політикою.\n"
                f"Політика користувача: {POLICY}\n{TRUST_RULES}\n"
                f"Обери рівно одну дію відповідним інструментом."
                + ("" if can_propose else " Перенесення вже просили: лише прийняти або відхилити.")},
            {"role": "user", "content": json.dumps({"trust": "untrusted-data", "invitation": facts},
                                                   ensure_ascii=False)},
        ]
        entry = {"at": datetime.now(timezone.utc).isoformat(), "message": envelope["type"],
                 "eventId": invitation["eventId"], "version": event["version"], "title": facts["title"],
                 "localStart": facts["localStart"], "injectionRedacted": bool(findings)}
        budget = TokenBudget(DECISION_TOKEN_BUDGET, config.limits().max_output_tokens)
        try:
            choice, _ = await self.model.complete(messages, tools, budget, tool_choice="required")
        except BudgetExceeded as exc:
            return {**entry, "action": "none", "reason": f"бюджет: {exc}", "tokens": budget.used}
        calls = choice.message.tool_calls or []
        if not calls:
            return {**entry, "action": "none", "reason": "модель не обрала дію", "tokens": budget.used}
        name, args = calls[0].function.name, json.loads(calls[0].function.arguments or "{}")
        entry.update(action=name, reason=str(args.get("reason", ""))[:200], tokens=budget.used)
        key = f"consumer-agent:{envelope['messageId']}"
        if name == "accept_invitation":
            await api.respond_to_invitation(invitation["invitationId"], "Accepted", event["version"], key)
        elif name == "decline_invitation":
            await api.respond_to_invitation(invitation["invitationId"], "Declined", event["version"], key)
        elif name == "propose_new_time" and can_propose:
            entry["producer"] = await self._propose(invitation, args)
            if entry["producer"].get("decision") != "rescheduled":
                await api.respond_to_invitation(invitation["invitationId"], "Declined", event["version"], key)
                entry["followUp"] = "Declined: організатор не переніс подію"
        else:
            entry.update(action="none", reason=f"недозволена дія {name}")
        return entry

    async def _propose(self, invitation: dict, args: dict) -> dict:
        try:
            proposal = Proposal.model_validate(args)
            _check_slot(proposal.start_at, proposal.end_at)
        except (ValidationError, ValueError) as exc:
            return {"decision": "invalid-proposal", "error": str(exc)[:300]}
        self.proposals[invitation["eventId"]] += 1
        base_url = await discover(PRODUCER_SERVICE)          # де агент — з Consul, що вміє — з його картки
        async with RemoteAgent(base_url, PRODUCER_KEY) as producer:
            skills = [skill.id for skill in producer.card.skills]
            if "reschedule_event" not in skills:
                return {"decision": "unsupported", "skills": skills}
            task = await producer.send(data={
                "skill": "reschedule_event", "eventId": invitation["eventId"],
                "proposedStartAt": proposal.start_at.isoformat(), "proposedEndAt": proposal.end_at.isoformat(),
                "reason": proposal.reason or "час не відповідає моїй політиці",
            })
        result = task_result(task)
        data = next((d for a in result["artifacts"] for d in a["data"]), {})
        return {"agent": base_url, "taskState": result["state"], "status": result["status"],
                "decision": data.get("decision", result["state"]), "answer": data.get("answer", ""),
                "events": data.get("events", [])}


assistant = InvitationAssistant()


class DecisionLogExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        caller = caller_of(context)
        if caller.user_id != USER:
            await updater.reject(new_text_message("Журнал рішень доступний лише власнику асистента"))
            return
        query = next(iter(get_data_parts(context.message.parts)), {}) or {}
        items = [e for e in assistant.log if query.get("eventId") in (None, e["eventId"])]
        await updater.add_artifact([new_data_part({"user": USER, "policy": POLICY, "decisions": items})],
                                   name="decision_log")
        await updater.complete(new_text_message(f"рішень: {len(items)}"))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


SKILL = AgentSkill(
    id="decision_log", name="Журнал рішень",
    description="Рішення асистента щодо запрошень (прийняти / відхилити / запропонувати інший час) з поясненнями "
                "і відповіддю агента-продюсера. Фільтр: {\"eventId\": \"…\"}.",
    tags=["calendar", "consumer", "EventConfirmed.v1", "EventRescheduleConfirmed.v1"],
    examples=['{"eventId": "…"}'], input_modes=["application/json", "text/plain"], output_modes=["application/json"],
)

app = build_app(
    agent_card("Invitation Assistant Agent",
               f"Асистент учасника {USER}: споживає події календаря з RabbitMQ і відповідає на запрошення за "
               "політикою користувача; про перенесення домовляється з агентом-продюсером через A2A.", [SKILL]),
    DecisionLogExecutor(), service="consumer-agent", prefix="/a2a/consumer",
    on_startup=assistant.start, on_shutdown=assistant.stop,
)
