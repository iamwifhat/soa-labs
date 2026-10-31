"""Доменна модель контексту «Участь у подіях» (агрегат «Запрошення» з ЛР1)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from soa_common.web import ConflictError, DomainError, ForbiddenError

PENDING = "Pending"
ACCEPTED = "Accepted"
DECLINED = "Declined"
REVOKED = "Revoked"


@dataclass(frozen=True)
class EventSnapshot:
    """Локальний знімок події: назва, час і версія, на яку відповідає адресат."""

    title: str
    start_at: datetime
    end_at: datetime
    time_zone: str
    version: int
    remind_before_minutes: int

    @classmethod
    def from_data(cls, data: dict) -> EventSnapshot:
        return cls(
            title=data["title"],
            start_at=datetime.fromisoformat(data["startAt"]),
            end_at=datetime.fromisoformat(data["endAt"]),
            time_zone=data["timeZone"],
            version=int(data["version"]),
            remind_before_minutes=int(data["remindBeforeMinutes"]),
        )


@dataclass
class Invitation:
    id: uuid.UUID
    event_id: uuid.UUID
    organizer_id: str
    invitee_id: str
    status: str
    snapshot: EventSnapshot
    version: int = 1
    responded_at: datetime | None = None

    @classmethod
    def issue(cls, event_id: uuid.UUID, organizer_id: str, invitee_id: str, snapshot: EventSnapshot) -> Invitation:
        return cls(uuid.uuid4(), event_id, organizer_id, invitee_id, PENDING, snapshot)

    def respond(self, actor_id: str, decision: str, event_version: int, now: datetime) -> None:
        if actor_id != self.invitee_id:
            raise ForbiddenError("Відповідати може лише адресат запрошення")
        if decision not in (ACCEPTED, DECLINED):
            raise DomainError("Рішення має бути Accepted або Declined")
        if self.status == REVOKED:
            raise ConflictError("Запрошення відкликано")
        if event_version != self.snapshot.version:
            raise ConflictError(
                f"Відповідь стосується версії події {event_version}, актуальна версія {self.snapshot.version}"
            )
        self.status = decision
        self.responded_at = now
        self.version += 1

    def apply_reschedule(self, snapshot: EventSnapshot) -> bool:
        """Новий час потребує повторного підтвердження; застаріла або повторна версія ігнорується."""
        if snapshot.version <= self.snapshot.version or self.status == REVOKED:
            return False
        self.snapshot = snapshot
        if self.status in (ACCEPTED, DECLINED):
            self.status = PENDING
            self.responded_at = None
        self.version += 1
        return True

    def refresh(self, snapshot: EventSnapshot) -> bool:
        if snapshot.version <= self.snapshot.version or self.status == REVOKED:
            return False
        self.snapshot = snapshot
        self.version += 1
        return True

    def revoke(self) -> bool:
        if self.status == REVOKED:
            return False
        self.status = REVOKED
        self.version += 1
        return True

    def to_dict(self) -> dict:
        return {
            "invitationId": str(self.id),
            "eventId": str(self.event_id),
            "organizerId": self.organizer_id,
            "inviteeId": self.invitee_id,
            "status": self.status,
            "version": self.version,
            "respondedAt": self.responded_at.isoformat() if self.responded_at else None,
            "event": {
                "title": self.snapshot.title,
                "startAt": self.snapshot.start_at.isoformat(),
                "endAt": self.snapshot.end_at.isoformat(),
                "timeZone": self.snapshot.time_zone,
                "version": self.snapshot.version,
                "remindBeforeMinutes": self.snapshot.remind_before_minutes,
            },
        }
