"""Доменна модель контексту «Планування календаря» (агрегати з ЛР1)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from soa_common.web import ConflictError, DomainError, ForbiddenError, PreconditionFailedError

SCHEDULED = "Scheduled"
CANCELLED = "Cancelled"


@dataclass(frozen=True)
class TimeSlot:
    """Об'єкт значення: незмінні початок і завершення у UTC та пояс IANA."""

    start_at: datetime
    end_at: datetime
    time_zone: str

    def __post_init__(self) -> None:
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise DomainError("Час має містити явне зміщення відносно UTC")
        if self.start_at >= self.end_at:
            raise DomainError("Початок події має передувати завершенню")
        try:
            ZoneInfo(self.time_zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise DomainError(f"Невідомий часовий пояс IANA: {self.time_zone}") from exc


@dataclass(frozen=True)
class EventTitle:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip() or len(self.value) > 200:
            raise DomainError("Назва події має бути непорожньою і не довшою за 200 символів")


@dataclass
class Calendar:
    id: uuid.UUID
    owner_id: str
    name: str
    time_zone: str

    @classmethod
    def create(cls, owner_id: str, name: str, time_zone: str) -> Calendar:
        if not name.strip():
            raise DomainError("Назва календаря не може бути порожньою")
        try:
            ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise DomainError(f"Невідомий часовий пояс IANA: {time_zone}") from exc
        return cls(uuid.uuid4(), owner_id, name.strip(), time_zone)


@dataclass
class Event:
    """Корінь агрегату «Подія»: керує часом, статусом і версією."""

    id: uuid.UUID
    calendar_id: uuid.UUID
    organizer_id: str
    title: EventTitle
    slot: TimeSlot
    invitee_ids: list[str]
    remind_before_minutes: int
    status: str = SCHEDULED
    version: int = 1

    @classmethod
    def create(
        cls,
        calendar: Calendar,
        organizer_id: str,
        title: EventTitle,
        slot: TimeSlot,
        invitee_ids: list[str],
        remind_before_minutes: int,
    ) -> Event:
        if calendar.owner_id != organizer_id:
            raise ForbiddenError("Створювати події може лише власник календаря")
        if remind_before_minutes <= 0:
            raise DomainError("Зсув нагадування має бути додатним")
        if len(set(invitee_ids)) != len(invitee_ids):
            raise DomainError("Ідентифікатори запрошених не повинні повторюватися")
        if organizer_id in invitee_ids:
            raise DomainError("Організатор не запрошує сам себе")
        return cls(uuid.uuid4(), calendar.id, organizer_id, title, slot, list(invitee_ids), remind_before_minutes)

    def _check_change(self, actor_id: str, expected_version: int) -> None:
        if actor_id != self.organizer_id:
            raise ForbiddenError("Змінювати подію може лише організатор")
        if expected_version != self.version:
            raise PreconditionFailedError(
                f"Очікувана версія {expected_version} не збігається з поточною {self.version}"
            )
        if self.status == CANCELLED:
            raise ConflictError("Скасовану подію не можна змінювати")

    def change(self, actor_id: str, expected_version: int, *, title: EventTitle | None = None,
               slot: TimeSlot | None = None) -> list[str]:
        """Перейменування та/або перенесення як одна атомарна зміна агрегату з однією новою версією."""
        self._check_change(actor_id, expected_version)
        changes = []
        if title is not None and title != self.title:
            self.title = title
            changes.append("title")
        if slot is not None and slot != self.slot:
            self.slot = slot
            changes.append("time")
        if not changes:
            raise DomainError("Запит не змінює подію")
        self.version += 1
        return changes

    def cancel(self, actor_id: str, expected_version: int) -> None:
        self._check_change(actor_id, expected_version)
        self.status = CANCELLED
        self.version += 1

    def snapshot(self) -> dict:
        """Знімок даних події для інтеграційних повідомлень і кроків оркестрації."""
        return {
            "eventId": str(self.id),
            "calendarId": str(self.calendar_id),
            "organizerId": self.organizer_id,
            "title": self.title.value,
            "status": self.status,
            "startAt": self.slot.start_at.isoformat(),
            "endAt": self.slot.end_at.isoformat(),
            "timeZone": self.slot.time_zone,
            "inviteeUserIds": self.invitee_ids,
            "remindBeforeMinutes": self.remind_before_minutes,
            "version": self.version,
        }
