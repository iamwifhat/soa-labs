"""Репозиторій запрошень."""
import uuid

import asyncpg

from soa_common.web import NotFoundError

from .domain import EventSnapshot, Invitation


def _invitation(row: asyncpg.Record) -> Invitation:
    return Invitation(
        id=row["id"],
        event_id=row["event_id"],
        organizer_id=row["organizer_id"],
        invitee_id=row["invitee_id"],
        status=row["status"],
        snapshot=EventSnapshot(
            title=row["event_title"],
            start_at=row["event_start_at"],
            end_at=row["event_end_at"],
            time_zone=row["event_time_zone"],
            version=row["event_version"],
            remind_before_minutes=row["remind_before_minutes"],
        ),
        version=row["version"],
        responded_at=row["responded_at"],
    )


async def insert_if_absent(connection: asyncpg.Connection, invitation: Invitation) -> bool:
    """Для пари «подія — адресат» існує одне запрошення; повторне повідомлення не створює дубліката."""
    snapshot = invitation.snapshot
    inserted = await connection.fetchval(
        """
        INSERT INTO invitations (id, event_id, organizer_id, invitee_id, status, event_title, event_start_at,
                                 event_end_at, event_time_zone, event_version, remind_before_minutes, version)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT (event_id, invitee_id) DO NOTHING
        RETURNING id
        """,
        invitation.id, invitation.event_id, invitation.organizer_id, invitation.invitee_id, invitation.status,
        snapshot.title, snapshot.start_at, snapshot.end_at, snapshot.time_zone, snapshot.version,
        snapshot.remind_before_minutes, invitation.version,
    )
    return inserted is not None


async def save(connection: asyncpg.Connection, invitation: Invitation) -> None:
    snapshot = invitation.snapshot
    await connection.execute(
        """
        UPDATE invitations
           SET status = $2, event_title = $3, event_start_at = $4, event_end_at = $5, event_time_zone = $6,
               event_version = $7, remind_before_minutes = $8, version = $9, responded_at = $10, updated_at = now()
         WHERE id = $1
        """,
        invitation.id, invitation.status, snapshot.title, snapshot.start_at, snapshot.end_at, snapshot.time_zone,
        snapshot.version, snapshot.remind_before_minutes, invitation.version, invitation.responded_at,
    )


async def get(connection: asyncpg.Connection, invitation_id: uuid.UUID, for_update: bool = False) -> Invitation:
    query = "SELECT * FROM invitations WHERE id = $1" + (" FOR UPDATE" if for_update else "")
    row = await connection.fetchrow(query, invitation_id)
    if row is None:
        raise NotFoundError(f"Запрошення {invitation_id} не знайдено")
    return _invitation(row)


async def for_event(connection: asyncpg.Connection, event_id: uuid.UUID, for_update: bool = False) -> list[Invitation]:
    query = "SELECT * FROM invitations WHERE event_id = $1 ORDER BY invitee_id" + (" FOR UPDATE" if for_update else "")
    return [_invitation(row) for row in await connection.fetch(query, event_id)]


async def for_invitee(connection: asyncpg.Connection, invitee_id: str) -> list[Invitation]:
    rows = await connection.fetch(
        "SELECT * FROM invitations WHERE invitee_id = $1 ORDER BY event_start_at", invitee_id
    )
    return [_invitation(row) for row in rows]
