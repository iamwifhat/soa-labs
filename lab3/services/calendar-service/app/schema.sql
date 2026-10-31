CREATE TABLE IF NOT EXISTS calendars (
    id          uuid PRIMARY KEY,
    owner_id    text NOT NULL,
    name        text NOT NULL,
    time_zone   text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    id                     uuid PRIMARY KEY,
    calendar_id            uuid NOT NULL REFERENCES calendars (id),
    organizer_id           text NOT NULL,
    title                  text NOT NULL,
    start_at               timestamptz NOT NULL,
    end_at                 timestamptz NOT NULL,
    time_zone              text NOT NULL,
    invitee_ids            text[] NOT NULL DEFAULT '{}',
    remind_before_minutes  integer NOT NULL,
    status                 text NOT NULL,
    version                integer NOT NULL,
    saga_id                uuid,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CHECK (start_at < end_at)
);

CREATE INDEX IF NOT EXISTS events_calendar_idx ON events (calendar_id, start_at);

-- Журнал SAGA (Saga log): стан кожного екземпляра та історія кроків.
CREATE TABLE IF NOT EXISTS sagas (
    id              uuid PRIMARY KEY,
    saga_type       text NOT NULL,
    event_id        uuid NOT NULL REFERENCES events (id),
    status          text NOT NULL,
    correlation_id  text NOT NULL,
    compensation    jsonb NOT NULL DEFAULT '{}',
    failure_reason  text,
    deadline_at     timestamptz NOT NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);

CREATE INDEX IF NOT EXISTS sagas_deadline_idx ON sagas (deadline_at) WHERE status = 'Started';

CREATE TABLE IF NOT EXISTS saga_log (
    id          bigserial PRIMARY KEY,
    saga_id     uuid NOT NULL REFERENCES sagas (id),
    entry       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
