CREATE TABLE IF NOT EXISTS invitations (
    id                     uuid PRIMARY KEY,
    event_id               uuid NOT NULL,
    organizer_id           text NOT NULL,
    invitee_id             text NOT NULL,
    status                 text NOT NULL,
    event_title            text NOT NULL,
    event_start_at         timestamptz NOT NULL,
    event_end_at           timestamptz NOT NULL,
    event_time_zone        text NOT NULL,
    event_version          integer NOT NULL,
    event_status           text NOT NULL,
    remind_before_minutes  integer NOT NULL,
    version                integer NOT NULL,
    previous_status        text,
    responded_at           timestamptz,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, invitee_id)
);

CREATE INDEX IF NOT EXISTS invitations_invitee_idx ON invitations (invitee_id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         text PRIMARY KEY,
    user_id     text NOT NULL,
    response    jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
