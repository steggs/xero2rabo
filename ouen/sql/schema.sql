-- Ouen Accounting runner: phase one store.
--
-- Design rules enforced here rather than promised in documentation:
--   1. karbon_events and audit_log are append only. UPDATE and DELETE are
--      blocked by triggers, so the record of what happened cannot be edited
--      after the fact by the runner, an agent, or a careless operator.
--   2. Every mutation of tasks writes a matching audit_log entry inside the
--      same transaction. The store module is the only writer and it will not
--      commit one without the other.
--   3. audit_log rows are hash chained. Each entry hashes its own contents
--      plus the previous entry's hash, so removing or rewriting history at the
--      file level is detectable by replaying the chain.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Raw inbound webhook deliveries from Karbon. Immutable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS karbon_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at        TEXT    NOT NULL,          -- ISO8601 UTC, runner clock
    delivery_id        TEXT,                      -- our per-request id
    -- batch_id, action_type and karbon_timestamp are NOT NULL with an empty
    -- string default on purpose. They take part in the UNIQUE key below, and
    -- SQLite treats NULLs in a UNIQUE index as distinct, so a nullable column
    -- there would silently stop deduplicating Karbon's delivery retries.
    batch_id           TEXT    NOT NULL DEFAULT '',
    position_in_batch  INTEGER NOT NULL DEFAULT 0,
    resource_type      TEXT    NOT NULL,          -- normalised: Work, Contact, ...
    raw_resource_type  TEXT,                      -- exactly as Karbon sent it
    resource_key       TEXT    NOT NULL,          -- ResourcePermaKey
    action_type        TEXT    NOT NULL DEFAULT '',  -- Inserted / Updated / Deleted
    karbon_timestamp   TEXT    NOT NULL DEFAULT '',  -- TimeStamp or Timestamp field
    signature_present  INTEGER NOT NULL DEFAULT 0,
    signature_verified INTEGER NOT NULL DEFAULT 0,
    source_ip          TEXT,
    raw_payload        TEXT    NOT NULL,          -- verbatim body of this event
    -- No task_id column: this table blocks UPDATE, so a link written after
    -- the fact would be impossible. Events join to tasks through
    -- resource_key = tasks.karbon_work_item_key instead.
    UNIQUE (resource_type, resource_key, action_type, karbon_timestamp, batch_id, position_in_batch)
);

CREATE INDEX IF NOT EXISTS ix_karbon_events_received  ON karbon_events (received_at);
CREATE INDEX IF NOT EXISTS ix_karbon_events_resource  ON karbon_events (resource_type, resource_key);

CREATE TRIGGER IF NOT EXISTS karbon_events_no_update
BEFORE UPDATE ON karbon_events
BEGIN
    SELECT RAISE(ABORT, 'karbon_events is append only');
END;

CREATE TRIGGER IF NOT EXISTS karbon_events_no_delete
BEFORE DELETE ON karbon_events
BEGIN
    SELECT RAISE(ABORT, 'karbon_events is append only');
END;

-- Deliveries that failed signature verification or could not be parsed. Kept
-- separately so a bad or hostile sender cannot pollute the event stream, and
-- kept at all so that a silent drop is never invisible.
CREATE TABLE IF NOT EXISTS rejected_deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    delivery_id TEXT,
    reason      TEXT NOT NULL,
    source_ip   TEXT,
    raw_body    TEXT
);

CREATE TRIGGER IF NOT EXISTS rejected_deliveries_no_update
BEFORE UPDATE ON rejected_deliveries
BEGIN
    SELECT RAISE(ABORT, 'rejected_deliveries is append only');
END;

CREATE TRIGGER IF NOT EXISTS rejected_deliveries_no_delete
BEFORE DELETE ON rejected_deliveries
BEGIN
    SELECT RAISE(ABORT, 'rejected_deliveries is append only');
END;

-- ---------------------------------------------------------------------------
-- The runner's own view of work. Karbon remains the system of record for the
-- work item itself; these columns mirror it, and runner_state is ours alone.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    karbon_work_item_key  TEXT UNIQUE,
    title                 TEXT,
    client_key            TEXT,
    client_name           TEXT,
    client_type           TEXT,
    work_type             TEXT,
    work_template_key     TEXT,
    work_schedule_key     TEXT,
    assignee_email        TEXT,
    assignee_name         TEXT,

    -- Mirrored from Karbon, normalised (see events.normalise_primary_status).
    karbon_primary_status   TEXT,
    karbon_secondary_status TEXT,
    start_date              TEXT,
    due_date                TEXT,
    deadline_date           TEXT,
    completed_date          TEXT,

    -- Owned by the runner.
    runner_state       TEXT NOT NULL,
    created_by_type    TEXT NOT NULL,   -- human | system | agent | karbon
    created_by_id      TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    last_progress_at   TEXT NOT NULL,   -- last state change or mirrored change
    last_synced_at     TEXT,            -- last successful refresh from Karbon
    awaiting_approval  INTEGER NOT NULL DEFAULT 0,
    CHECK (created_by_type IN ('human', 'system', 'agent', 'karbon')),
    CHECK (awaiting_approval IN (0, 1))
);

CREATE INDEX IF NOT EXISTS ix_tasks_state    ON tasks (runner_state);
CREATE INDEX IF NOT EXISTS ix_tasks_deadline ON tasks (deadline_date);
CREATE INDEX IF NOT EXISTS ix_tasks_due      ON tasks (due_date);

-- ---------------------------------------------------------------------------
-- Approval ledger. Phase one has no agents, but the ledger exists now so that
-- "drafts only, Carl approves" is a row in a table rather than a convention.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS approvals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL REFERENCES tasks(id),
    requested_at  TEXT    NOT NULL,
    requested_by_type TEXT NOT NULL,
    requested_by_id   TEXT NOT NULL,
    summary       TEXT,
    decision      TEXT,                -- NULL until decided: approved | rejected
    decided_at    TEXT,
    decided_by_id TEXT,                -- always a human identity
    decision_note TEXT,
    CHECK (decision IS NULL OR decision IN ('approved', 'rejected'))
);

CREATE INDEX IF NOT EXISTS ix_approvals_open ON approvals (task_id, decision);

-- ---------------------------------------------------------------------------
-- Stall flags raised by the scheduled check.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stall_flags (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          INTEGER REFERENCES tasks(id),
    scope            TEXT NOT NULL,     -- task | pipe
    detected_at      TEXT NOT NULL,
    severity         TEXT NOT NULL,     -- watch | concern | urgent | breach
    reason_code      TEXT NOT NULL,
    detail           TEXT,
    days_to_deadline INTEGER,
    idle_days        INTEGER,
    cleared_at       TEXT,
    CHECK (severity IN ('watch', 'concern', 'urgent', 'breach')),
    CHECK (scope IN ('task', 'pipe'))
);

CREATE INDEX IF NOT EXISTS ix_stall_open ON stall_flags (task_id, reason_code, cleared_at);

-- ---------------------------------------------------------------------------
-- Local mirror of the webhook subscriptions we asked Karbon for. Karbon
-- cancels a subscription after 10 failed deliveries, so knowing what we
-- believe is subscribed is what lets the pipe health check notice the gap.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_type      TEXT NOT NULL UNIQUE,
    target_url        TEXT NOT NULL,
    webhook_types     TEXT,             -- JSON array for a Multi subscription
    signing_key_set   INTEGER NOT NULL DEFAULT 0,
    batch_size        INTEGER,
    created_at        TEXT NOT NULL,
    last_checked_at   TEXT,
    last_check_result TEXT
);

-- ---------------------------------------------------------------------------
-- Cursors and small key/value state (last poll times, last event seen).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- The audit layer. Append only, hash chained.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    actor_type   TEXT NOT NULL,      -- human | system | agent | karbon
    actor_id     TEXT NOT NULL,
    subject_type TEXT NOT NULL,      -- task | event | subscription | runner | approval
    subject_id   TEXT,
    event_type   TEXT NOT NULL,
    from_state   TEXT,
    to_state     TEXT,
    detail       TEXT NOT NULL,      -- canonical JSON
    prev_hash    TEXT NOT NULL,
    entry_hash   TEXT NOT NULL,
    CHECK (actor_type IN ('human', 'system', 'agent', 'karbon'))
);

CREATE INDEX IF NOT EXISTS ix_audit_subject ON audit_log (subject_type, subject_id);
CREATE INDEX IF NOT EXISTS ix_audit_ts      ON audit_log (ts);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append only');
END;
