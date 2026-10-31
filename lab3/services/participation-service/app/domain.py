"""Доменна модель контексту «Участь у подіях» (агрегат «Запрошення» з ЛР1, кроки SAGA з ЛР3)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime

from soa_common.web import ConflictError, DomainError, ForbiddenError

PENDING = "Pending"
ACCEPTED = "Accepted"
DECLINED = "Declined"
REVOKED = "Revoked"

# Локальний погляд на стан події.
EVENT_PENDING = "Pending"            # SAGA створення ще не завершена
EVENT_SCHEDULED = "Scheduled"
EVENT_RESCHEDULING = "Rescheduling"  # SAGA перенесення ще не завершена


@dataclass(frozen=True)
class EventSnapshot:
    """Локальний знімок події: назва, час, версія та її стан з погляду участі."""

    title: str
    start_at: datetime
    end_at: datetime
    time_zone: str
    version: int
    remind_before_minutes: int
    status: str = EVENT_PENDING

    @classmethod
    def from_data(cls, data: dict, status: str) -> EventSnapshot:
        return cls(
            title=data["title"],
            start_at=datetime.fromisoformat(data["startAt"]),
            end_at=datetime.fromisoformat(data["endAt"]),
            time_zone=data["timeZone"],
            version=int(data["version"]),
            remind_before_minutes=int(data["remindBeforeMinutes"]),
            status=status,
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
    previous_status: str | None = None

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
        if self.snapshot.status != EVENT_SCHEDULED:
            raise ConflictError(f"Подія ще в стані {self.snapshot.status}: дочекайтеся завершення SAGA")
        if event_version != self.snapshot.version:
            raise ConflictError(
                f"Відповідь стосується версії події {event_version}, актуальна версія {self.snapshot.version}"
            )
        self.status = decision
        self.responded_at = now
        self.version += 1

    def confirm_event(self, version: int) -> bool:
        """EventConfirmed / EventRescheduleConfirmed: подію підтверджено, відповіді знову дозволені."""
        if self.status == REVOKED or version < self.snapshot.version:
            return False
        self.snapshot = replace(self.snapshot, status=EVENT_SCHEDULED, version=version)
        self.previous_status = None
        self.version += 1
        return True

    def apply_reschedule(self, snapshot: EventSnapshot) -> bool:
        """Новий час потребує повторного підтвердження; попередній стан зберігається для можливої компенсації."""
        if snapshot.version <= self.snapshot.version or self.status == REVOKED:
            return False
        self.previous_status = self.status
        self.snapshot = snapshot
        if self.status in (ACCEPTED, DECLINED):
            self.status = PENDING
            self.responded_at = None
        self.version += 1
        return True

    def revert_reschedule(self, reverted_version: int, restored: EventSnapshot) -> bool:
        """Компенсація перенесення: якщо невдалу версію було застосовано, повертається попередня відповідь."""
        if self.status == REVOKED or restored.version <= self.snapshot.version:
            return False
        if self.snapshot.version == reverted_version and self.previous_status:
            self.status = self.previous_status
        self.previous_status = None
        self.snapshot = restored
        self.version += 1
        return True

    def refresh(self, snapshot: EventSnapshot) -> bool:
        if snapshot.version <= self.snapshot.version or self.status == REVOKED:
            return False
        self.snapshot = replace(snapshot, status=self.snapshot.status)
        self.version += 1
        return True

    def revoke(self) -> bool:
        if self.status == REVOKED:
            return False
        self.status = REVOKED
        self.version += 1
        return True

    @property
    def announced(self) -> bool:
        """Адресат дізнається про запрошення лише після підтвердження події."""
        return self.snapshot.status != EVENT_PENDING

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
                "status": self.snapshot.status,
                "remindBeforeMinutes": self.snapshot.remind_before_minutes,
            },
        }
