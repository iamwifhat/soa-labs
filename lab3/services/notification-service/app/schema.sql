CREATE TABLE IF NOT EXISTS reminders (
    id                     uuid PRIMARY KEY,
    event_id               uuid NOT NULL,
    user_id                text NOT NULL,
    event_title            text NOT NULL,
    event_start_at         timestamptz NOT NULL,
    event_time_zone        text NOT NULL,
    remind_before_minutes  integer NOT NULL,
    remind_at              timestamptz NOT NULL,
    event_version          integer NOT NULL,
    status                 text NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    sent_at                timestamptz,
    UNIQUE (event_id, user_id)
);

CREATE INDEX IF NOT EXISTS reminders_due_idx ON reminders (remind_at) WHERE status = 'Scheduled';

CREATE TABLE IF NOT EXISTS notifications (
    id          uuid PRIMARY KEY,
    user_id     text NOT NULL,
    event_id    uuid,
    kind        text NOT NULL,
    channel     text NOT NULL DEFAULT 'email',
    subject     text NOT NULL,
    status      text NOT NULL,
    dedup_key   text NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz,
    sent_by     text
);

CREATE INDEX IF NOT EXISTS notifications_queued_idx ON notifications (created_at) WHERE status = 'Queued';

CREATE TABLE IF NOT EXISTS preferences (
    user_id        text PRIMARY KEY,
    email_enabled  boolean NOT NULL DEFAULT true,
    updated_at     timestamptz NOT NULL DEFAULT now()
);
