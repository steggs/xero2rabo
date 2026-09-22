"""The runner's own store and its audit layer.

Karbon stays the system of record for work items and clients. This store holds
what Karbon does not: the runner's task state, the approval ledger, stall
flags, and an append only, hash chained audit log of every change.

The hash chain is the point. Each audit entry hashes its own contents together
with the previous entry's hash, so an entry cannot be altered or removed after
the fact without breaking every hash that follows it. verify_audit_chain()
replays the chain and reports the first break. That is what turns "the runner
only ever produced drafts and never sent anything" from a claim into something
Carl can check.

Every task mutation goes through this module, and every one of them writes its
audit entry in the same transaction as the change itself. There is no code
path that changes a task without leaving a record.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .events import KarbonEvent, normalise_primary_status
from .policy import PolicyViolation, assert_approval_actor, assert_known_actor

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
SCHEMA_VERSION = "1"

GENESIS_HASH = "0" * 64

# ---------------------------------------------------------------------------
# Task state machine
# ---------------------------------------------------------------------------

STATES = (
    "received",
    "triaged",
    "in_progress",
    "blocked",
    "awaiting_approval",
    "approved",
    "rejected",
    "completed",
    "cancelled",
)

TERMINAL_STATES = frozenset({"completed", "cancelled"})

ANY_ACTOR = frozenset({"human", "system", "agent"})
HUMAN_ONLY = frozenset({"human"})
HUMAN_OR_SYSTEM = frozenset({"human", "system"})

# from_state -> to_state -> actor types permitted to make that move.
#
# The two entries that matter for the drafts-only constraint are the pair out
# of awaiting_approval: both are human only, so nothing automated can ever
# mark its own output approved.
TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    "received": {
        "triaged": ANY_ACTOR,
        "in_progress": ANY_ACTOR,
        "blocked": ANY_ACTOR,
        "completed": HUMAN_OR_SYSTEM,
        "cancelled": HUMAN_ONLY,
    },
    "triaged": {
        "in_progress": ANY_ACTOR,
        "blocked": ANY_ACTOR,
        "completed": HUMAN_OR_SYSTEM,
        "cancelled": HUMAN_ONLY,
    },
    "in_progress": {
        "blocked": ANY_ACTOR,
        "awaiting_approval": ANY_ACTOR,
        "completed": HUMAN_OR_SYSTEM,
        "cancelled": HUMAN_ONLY,
    },
    "blocked": {
        "triaged": ANY_ACTOR,
        "in_progress": ANY_ACTOR,
        "completed": HUMAN_OR_SYSTEM,
        "cancelled": HUMAN_ONLY,
    },
    "awaiting_approval": {
        "approved": HUMAN_ONLY,
        "rejected": HUMAN_ONLY,
        "blocked": HUMAN_ONLY,
        "cancelled": HUMAN_ONLY,
    },
    "approved": {
        "completed": HUMAN_OR_SYSTEM,
        "cancelled": HUMAN_ONLY,
    },
    "rejected": {
        "in_progress": ANY_ACTOR,
        "cancelled": HUMAN_ONLY,
    },
    "completed": {},
    "cancelled": {},
}

# How a Karbon PrimaryStatus lands in the runner when a task is first mirrored.
KARBON_STATUS_TO_RUNNER_STATE = {
    "Planned": "received",
    "Ready To Start": "received",
    "In Progress": "in_progress",
    "Waiting": "blocked",
    "Completed": "completed",
}

# Mirrored fields whose change counts as real progress on the work item, and so
# resets the idle clock the stall check reads.
PROGRESS_BEARING_FIELDS = (
    "karbon_primary_status",
    "karbon_secondary_status",
    "assignee_email",
    "start_date",
    "due_date",
    "deadline_date",
    "completed_date",
)


class StoreError(RuntimeError):
    """A store-level rule was broken."""


class AuditChainBroken(StoreError):
    """The audit chain does not replay cleanly."""


def utc_now_iso() -> str:
    """UTC, microsecond precision, sortable as a string.

    Microseconds rather than milliseconds because two audit entries written in
    the same transaction must not share a timestamp: ordering the trail by time
    has to agree with ordering it by sequence.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )


class Store:
    """All reads and writes go through here."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- transactions -------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction. BEGIN IMMEDIATE because the audit chain reads
        the previous hash and then writes the next one; two concurrent writers
        must not interleave there."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # -- audit --------------------------------------------------------------

    @staticmethod
    def _entry_hash(row: dict[str, Any]) -> str:
        payload = canonical_json(
            [
                row["seq"],
                row["ts"],
                row["actor_type"],
                row["actor_id"],
                row["subject_type"],
                row["subject_id"],
                row["event_type"],
                row["from_state"],
                row["to_state"],
                row["detail"],
                row["prev_hash"],
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _append_audit(
        self,
        *,
        actor_type: str,
        actor_id: str,
        subject_type: str,
        subject_id: str | None,
        event_type: str,
        from_state: str | None = None,
        to_state: str | None = None,
        detail: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> int:
        """Append one entry. Must be called inside an open transaction."""
        assert_known_actor(actor_type)
        cur = self.conn.execute("SELECT seq, entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1")
        last = cur.fetchone()
        seq = (last["seq"] + 1) if last else 1
        prev_hash = last["entry_hash"] if last else GENESIS_HASH

        row = {
            "seq": seq,
            "ts": ts or utc_now_iso(),
            "actor_type": actor_type,
            "actor_id": actor_id,
            "subject_type": subject_type,
            "subject_id": None if subject_id is None else str(subject_id),
            "event_type": event_type,
            "from_state": from_state,
            "to_state": to_state,
            "detail": canonical_json(detail or {}),
            "prev_hash": prev_hash,
        }
        row["entry_hash"] = self._entry_hash(row)
        self.conn.execute(
            "INSERT INTO audit_log (seq, ts, actor_type, actor_id, subject_type, "
            "subject_id, event_type, from_state, to_state, detail, prev_hash, entry_hash) "
            "VALUES (:seq, :ts, :actor_type, :actor_id, :subject_type, :subject_id, "
            ":event_type, :from_state, :to_state, :detail, :prev_hash, :entry_hash)",
            row,
        )
        return seq

    def audit(self, **kwargs: Any) -> int:
        """Append a standalone audit entry in its own transaction."""
        with self.transaction():
            return self._append_audit(**kwargs)

    def verify_audit_chain(self) -> tuple[bool, str | None]:
        """Replay the chain. Returns (ok, first_problem_description)."""
        prev_hash = GENESIS_HASH
        expected_seq = 1
        cur = self.conn.execute("SELECT * FROM audit_log ORDER BY seq ASC")
        for record in cur:
            row = dict(record)
            if row["seq"] != expected_seq:
                return False, (
                    f"sequence gap: expected seq {expected_seq}, found {row['seq']}"
                )
            if row["prev_hash"] != prev_hash:
                return False, (
                    f"seq {row['seq']}: prev_hash does not match the preceding entry"
                )
            recomputed = self._entry_hash(row)
            if recomputed != row["entry_hash"]:
                return False, f"seq {row['seq']}: contents do not match entry_hash"
            prev_hash = row["entry_hash"]
            expected_seq += 1
        return True, None

    def audit_entry_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"])

    def record_policy_refusal(
        self,
        *,
        actor_type: str,
        actor_id: str,
        attempted: str,
        reason: str,
        subject_type: str = "runner",
        subject_id: str | None = None,
    ) -> int:
        """Write a refusal to the audit log.

        A blocked attempt is more interesting than a permitted one, so it is
        recorded with the same permanence.
        """
        return self.audit(
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type=subject_type,
            subject_id=subject_id,
            event_type="policy.refused",
            detail={"attempted": attempted, "reason": reason},
        )

    # -- webhook events -----------------------------------------------------

    def record_webhook_event(
        self,
        event: KarbonEvent,
        *,
        delivery_id: str | None,
        signature_present: bool,
        signature_verified: bool,
        source_ip: str | None,
        received_at: str | None = None,
    ) -> tuple[int | None, bool]:
        """Store one normalised delivery. Returns (row_id, was_duplicate).

        Karbon retries a delivery up to ten times, so duplicates are expected
        rather than exceptional. A duplicate is not stored twice but is still
        audited, because a burst of them means our 2xx responses are not
        getting back to Karbon.
        """
        received_at = received_at or utc_now_iso()
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO karbon_events (received_at, delivery_id, batch_id, "
                "position_in_batch, resource_type, raw_resource_type, resource_key, "
                "action_type, karbon_timestamp, signature_present, signature_verified, "
                "source_ip, raw_payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT DO NOTHING",
                (
                    received_at,
                    delivery_id,
                    event.batch_id or "",
                    event.position_in_batch,
                    event.resource_type,
                    event.raw_resource_type,
                    event.resource_key,
                    event.action_type or "",
                    event.karbon_timestamp or "",
                    1 if signature_present else 0,
                    1 if signature_verified else 0,
                    source_ip,
                    event.raw_json(),
                ),
            )
            duplicate = cur.rowcount == 0
            row_id = None if duplicate else int(cur.lastrowid)
            self._append_audit(
                actor_type="karbon",
                actor_id="webhook",
                subject_type="event",
                subject_id=str(row_id) if row_id else event.resource_key,
                event_type="webhook.duplicate" if duplicate else "webhook.received",
                detail={
                    "resource_type": event.resource_type,
                    "resource_key": event.resource_key,
                    "action_type": event.action_type,
                    "karbon_timestamp": event.karbon_timestamp,
                    "batch_id": event.batch_id,
                    "delivery_id": delivery_id,
                    "signature_verified": signature_verified,
                },
                ts=received_at,
            )
            self.conn.execute(
                "INSERT INTO sync_state (key, value, updated_at) VALUES "
                "('last_webhook_received_at', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (received_at, received_at),
            )
        return row_id, duplicate

    def record_rejected_delivery(
        self,
        *,
        reason: str,
        raw_body: str | None,
        source_ip: str | None,
        delivery_id: str | None = None,
    ) -> int:
        received_at = utc_now_iso()
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO rejected_deliveries (received_at, delivery_id, reason, "
                "source_ip, raw_body) VALUES (?,?,?,?,?)",
                (received_at, delivery_id, reason, source_ip, raw_body),
            )
            self._append_audit(
                actor_type="system",
                actor_id="listener",
                subject_type="event",
                subject_id=str(cur.lastrowid),
                event_type="webhook.rejected",
                detail={"reason": reason, "source_ip": source_ip, "delivery_id": delivery_id},
                ts=received_at,
            )
            return int(cur.lastrowid)

    def work_event_keys(self, *, since_id: int = 0, limit: int = 1000) -> list[sqlite3.Row]:
        """Work item keys seen on the wire, newest events last.

        This is what a later phase would drive a refresh from. Phase one only
        reads it, because the listener's job is to record, not to react.
        """
        return list(
            self.conn.execute(
                "SELECT id, resource_key, action_type, karbon_timestamp, received_at "
                "FROM karbon_events WHERE resource_type = 'Work' AND id > ? "
                "ORDER BY id ASC LIMIT ?",
                (since_id, limit),
            )
        )

    def event_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM karbon_events").fetchone()["n"])

    def last_webhook_received_at(self) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM sync_state WHERE key = 'last_webhook_received_at'"
        ).fetchone()
        return row["value"] if row else None

    # -- tasks --------------------------------------------------------------

    def get_task_by_work_item(self, work_item_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM tasks WHERE karbon_work_item_key = ?", (work_item_key,)
        ).fetchone()

    def get_task(self, task_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    def upsert_task_from_karbon(
        self,
        work_item: dict[str, Any],
        *,
        actor_type: str = "system",
        actor_id: str = "karbon-sync",
    ) -> tuple[int, str]:
        """Mirror a Karbon work item into the runner. Returns (task_id, action).

        action is one of "created", "updated", "unchanged". This is a mirror,
        not a write back: nothing here ever calls Karbon.
        """
        key = work_item.get("WorkItemKey")
        if not key:
            raise StoreError("work item has no WorkItemKey")

        mirrored = {
            "title": work_item.get("Title"),
            "client_key": work_item.get("ClientKey"),
            "client_name": work_item.get("ClientName"),
            "client_type": work_item.get("ClientType"),
            "work_type": work_item.get("WorkType"),
            "work_template_key": work_item.get("WorkTemplateKey"),
            "work_schedule_key": work_item.get("WorkScheduleKey"),
            "assignee_email": work_item.get("AssigneeEmailAddress"),
            "assignee_name": work_item.get("AssigneeName"),
            "karbon_primary_status": normalise_primary_status(work_item.get("PrimaryStatus")),
            "karbon_secondary_status": work_item.get("SecondaryStatus"),
            "start_date": work_item.get("StartDate"),
            "due_date": work_item.get("DueDate"),
            "deadline_date": work_item.get("DeadlineDate"),
            "completed_date": work_item.get("CompletedDate"),
        }

        now = utc_now_iso()
        existing = self.get_task_by_work_item(str(key))

        with self.transaction():
            if existing is None:
                runner_state = KARBON_STATUS_TO_RUNNER_STATE.get(
                    mirrored["karbon_primary_status"] or "", "received"
                )
                columns = list(mirrored) + [
                    "karbon_work_item_key",
                    "runner_state",
                    "created_by_type",
                    "created_by_id",
                    "created_at",
                    "updated_at",
                    "last_progress_at",
                    "last_synced_at",
                ]
                values = list(mirrored.values()) + [
                    str(key),
                    runner_state,
                    actor_type,
                    actor_id,
                    now,
                    now,
                    now,
                    now,
                ]
                cur = self.conn.execute(
                    f"INSERT INTO tasks ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    values,
                )
                task_id = int(cur.lastrowid)
                self._append_audit(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    subject_type="task",
                    subject_id=str(task_id),
                    event_type="task.created",
                    to_state=runner_state,
                    detail={"work_item_key": str(key), "mirrored": mirrored},
                    ts=now,
                )
                return task_id, "created"

            task_id = int(existing["id"])
            changed = {
                field: value
                for field, value in mirrored.items()
                if (existing[field] or None) != (value or None)
            }
            if not changed:
                self.conn.execute(
                    "UPDATE tasks SET last_synced_at = ? WHERE id = ?", (now, task_id)
                )
                return task_id, "unchanged"

            progress = any(field in changed for field in PROGRESS_BEARING_FIELDS)
            assignments = ", ".join(f"{field} = ?" for field in changed)
            params = list(changed.values()) + [now, now, task_id]
            if progress:
                self.conn.execute(
                    f"UPDATE tasks SET {assignments}, updated_at = ?, last_synced_at = ?, "
                    "last_progress_at = ? WHERE id = ?",
                    list(changed.values()) + [now, now, now, task_id],
                )
            else:
                self.conn.execute(
                    f"UPDATE tasks SET {assignments}, updated_at = ?, last_synced_at = ? "
                    "WHERE id = ?",
                    params,
                )
            self._append_audit(
                actor_type=actor_type,
                actor_id=actor_id,
                subject_type="task",
                subject_id=str(task_id),
                event_type="task.mirrored",
                detail={
                    "work_item_key": str(key),
                    "changed": {
                        field: {"from": existing[field], "to": value}
                        for field, value in changed.items()
                    },
                    "counted_as_progress": progress,
                },
                ts=now,
            )
            return task_id, "updated"

    def transition_task(
        self,
        task_id: int,
        to_state: str,
        *,
        actor_type: str,
        actor_id: str,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> str:
        """Move a task to a new runner state, or raise.

        The transition table is the enforcement point for the approval rule:
        awaiting_approval can only be left by a human.
        """
        assert_known_actor(actor_type)
        if to_state not in STATES:
            raise StoreError(f"unknown state {to_state!r}")

        task = self.get_task(task_id)
        if task is None:
            raise StoreError(f"no task with id {task_id}")
        from_state = task["runner_state"]

        if from_state == to_state:
            return from_state

        allowed = TRANSITIONS.get(from_state, {})
        if to_state not in allowed:
            raise StoreError(
                f"transition {from_state} -> {to_state} is not defined"
            )
        if actor_type not in allowed[to_state]:
            raise PolicyViolation(
                f"actor type {actor_type!r} may not move a task from "
                f"{from_state} to {to_state}; permitted: "
                f"{sorted(allowed[to_state])}"
            )

        now = utc_now_iso()
        awaiting = 1 if to_state == "awaiting_approval" else 0
        with self.transaction():
            self.conn.execute(
                "UPDATE tasks SET runner_state = ?, awaiting_approval = ?, "
                "updated_at = ?, last_progress_at = ? WHERE id = ?",
                (to_state, awaiting, now, now, task_id),
            )
            self._append_audit(
                actor_type=actor_type,
                actor_id=actor_id,
                subject_type="task",
                subject_id=str(task_id),
                event_type="task.transition",
                from_state=from_state,
                to_state=to_state,
                detail={"reason": reason, **(detail or {})},
                ts=now,
            )
        return to_state

    def open_tasks(self) -> list[sqlite3.Row]:
        placeholders = ", ".join("?" for _ in TERMINAL_STATES)
        return list(
            self.conn.execute(
                f"SELECT * FROM tasks WHERE runner_state NOT IN ({placeholders})",
                tuple(sorted(TERMINAL_STATES)),
            )
        )

    def task_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"])

    # -- approvals ----------------------------------------------------------

    def request_approval(
        self,
        task_id: int,
        *,
        actor_type: str,
        actor_id: str,
        summary: str | None = None,
    ) -> int:
        now = utc_now_iso()
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO approvals (task_id, requested_at, requested_by_type, "
                "requested_by_id, summary) VALUES (?,?,?,?,?)",
                (task_id, now, actor_type, actor_id, summary),
            )
            self._append_audit(
                actor_type=actor_type,
                actor_id=actor_id,
                subject_type="approval",
                subject_id=str(cur.lastrowid),
                event_type="approval.requested",
                detail={"task_id": task_id, "summary": summary},
                ts=now,
            )
            approval_id = int(cur.lastrowid)
        self.transition_task(
            task_id,
            "awaiting_approval",
            actor_type=actor_type,
            actor_id=actor_id,
            reason="approval requested",
            detail={"approval_id": approval_id},
        )
        return approval_id

    def decide_approval(
        self,
        approval_id: int,
        *,
        decision: str,
        decided_by_id: str,
        actor_type: str = "human",
        note: str | None = None,
    ) -> None:
        """Record Carl's decision. Human actors only, by policy and by check."""
        assert_approval_actor(actor_type)
        if decision not in {"approved", "rejected"}:
            raise StoreError(f"decision must be approved or rejected, got {decision!r}")

        row = self.conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"no approval with id {approval_id}")
        if row["decision"] is not None:
            raise StoreError(f"approval {approval_id} is already {row['decision']}")

        now = utc_now_iso()
        with self.transaction():
            self.conn.execute(
                "UPDATE approvals SET decision = ?, decided_at = ?, decided_by_id = ?, "
                "decision_note = ? WHERE id = ?",
                (decision, now, decided_by_id, note, approval_id),
            )
            self._append_audit(
                actor_type=actor_type,
                actor_id=decided_by_id,
                subject_type="approval",
                subject_id=str(approval_id),
                event_type=f"approval.{decision}",
                detail={"task_id": row["task_id"], "note": note},
                ts=now,
            )
        self.transition_task(
            int(row["task_id"]),
            decision,
            actor_type=actor_type,
            actor_id=decided_by_id,
            reason=f"approval {approval_id} {decision}",
        )

    def open_approvals(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM approvals WHERE decision IS NULL ORDER BY requested_at")
        )

    # -- stall flags --------------------------------------------------------

    def open_stall_flag(
        self,
        *,
        task_id: int | None,
        scope: str,
        severity: str,
        reason_code: str,
        detail: str | None,
        days_to_deadline: int | None,
        idle_days: int | None,
        detected_at: str | None = None,
    ) -> tuple[int, bool]:
        """Raise a flag, or return the existing open one. (flag_id, is_new)."""
        detected_at = detected_at or utc_now_iso()
        existing = self.conn.execute(
            "SELECT * FROM stall_flags WHERE reason_code = ? AND cleared_at IS NULL "
            "AND ((task_id IS NULL AND ? IS NULL) OR task_id = ?)",
            (reason_code, task_id, task_id),
        ).fetchone()
        if existing is not None:
            return int(existing["id"]), False

        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO stall_flags (task_id, scope, detected_at, severity, "
                "reason_code, detail, days_to_deadline, idle_days) VALUES (?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    scope,
                    detected_at,
                    severity,
                    reason_code,
                    detail,
                    days_to_deadline,
                    idle_days,
                ),
            )
            self._append_audit(
                actor_type="system",
                actor_id="stall-check",
                subject_type="task" if task_id else "runner",
                subject_id=str(task_id) if task_id else None,
                event_type="stall.flagged",
                detail={
                    "severity": severity,
                    "reason_code": reason_code,
                    "detail": detail,
                    "days_to_deadline": days_to_deadline,
                    "idle_days": idle_days,
                },
                ts=detected_at,
            )
            return int(cur.lastrowid), True

    def clear_stall_flag(self, flag_id: int, *, reason: str) -> None:
        now = utc_now_iso()
        with self.transaction():
            self.conn.execute(
                "UPDATE stall_flags SET cleared_at = ? WHERE id = ? AND cleared_at IS NULL",
                (now, flag_id),
            )
            self._append_audit(
                actor_type="system",
                actor_id="stall-check",
                subject_type="runner",
                subject_id=str(flag_id),
                event_type="stall.cleared",
                detail={"flag_id": flag_id, "reason": reason},
                ts=now,
            )

    def open_stall_flags(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM stall_flags WHERE cleared_at IS NULL "
                "ORDER BY CASE severity WHEN 'breach' THEN 0 WHEN 'urgent' THEN 1 "
                "WHEN 'concern' THEN 2 ELSE 3 END, detected_at"
            )
        )

    # -- subscriptions and sync state ---------------------------------------

    def record_subscription(
        self,
        *,
        webhook_type: str,
        target_url: str,
        webhook_types: list[str] | None,
        signing_key_set: bool,
        batch_size: int | None,
        actor_id: str,
    ) -> None:
        now = utc_now_iso()
        with self.transaction():
            self.conn.execute(
                "INSERT INTO webhook_subscriptions (webhook_type, target_url, "
                "webhook_types, signing_key_set, batch_size, created_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(webhook_type) DO UPDATE SET target_url = excluded.target_url, "
                "webhook_types = excluded.webhook_types, "
                "signing_key_set = excluded.signing_key_set, "
                "batch_size = excluded.batch_size, created_at = excluded.created_at",
                (
                    webhook_type,
                    target_url,
                    canonical_json(webhook_types) if webhook_types else None,
                    1 if signing_key_set else 0,
                    batch_size,
                    now,
                ),
            )
            self._append_audit(
                actor_type="human",
                actor_id=actor_id,
                subject_type="subscription",
                subject_id=webhook_type,
                event_type="subscription.created",
                detail={
                    "target_url": target_url,
                    "webhook_types": webhook_types,
                    "signing_key_set": signing_key_set,
                    "batch_size": batch_size,
                },
                ts=now,
            )

    def forget_subscription(self, webhook_type: str, *, actor_id: str) -> None:
        with self.transaction():
            self.conn.execute(
                "DELETE FROM webhook_subscriptions WHERE webhook_type = ?", (webhook_type,)
            )
            self._append_audit(
                actor_type="human",
                actor_id=actor_id,
                subject_type="subscription",
                subject_id=webhook_type,
                event_type="subscription.deleted",
                detail={},
            )

    def subscriptions(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM webhook_subscriptions ORDER BY webhook_type"))

    def set_sync_state(self, key: str, value: str | None) -> None:
        now = utc_now_iso()
        self.conn.execute(
            "INSERT INTO sync_state (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, now),
        )

    def get_sync_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def open_store(db_path: str, *, create: bool = False) -> Store:
    exists = Path(db_path).exists()
    conn = connect(db_path)
    if create or not exists:
        init_db(conn)
    return Store(conn)
