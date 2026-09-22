"""The audit layer is the thing that makes the drafts-only constraint
checkable, so these tests check it as a security property, not a convenience."""

import sqlite3

import pytest

from runner.events import parse_delivery
from runner.policy import PolicyViolation
from runner.store import StoreError

DELIVERY = (
    '{"ResourcePermaKey":"WI1","ResourceType":"Work","ActionType":"Updated",'
    '"TimeStamp":"2026-09-02T03:49:42Z"}'
)

WORK_ITEM = {
    "WorkItemKey": "WI1",
    "Title": "GST return Q3 2026",
    "ClientKey": "ORG1",
    "ClientName": "Pier Road Trading Ltd",
    "ClientType": "Organization",
    "WorkType": "GST Return",
    "PrimaryStatus": "InProgress",
    "StartDate": "2026-10-01T00:00:00Z",
    "DueDate": "2026-10-25T00:00:00Z",
    "DeadlineDate": "2026-10-31T00:00:00Z",
}


def _record(store):
    event = parse_delivery(DELIVERY)[0]
    return store.record_webhook_event(
        event,
        delivery_id="d1",
        signature_present=False,
        signature_verified=False,
        source_ip="203.0.113.10",
    )


def test_audit_chain_verifies_after_normal_use(store):
    _record(store)
    store.upsert_task_from_karbon(WORK_ITEM)
    ok, problem = store.verify_audit_chain()
    assert ok, problem
    assert store.audit_entry_count() >= 2


def test_audit_log_cannot_be_updated_or_deleted(store):
    _record(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE audit_log SET detail = '{}' WHERE seq = 1")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("DELETE FROM audit_log WHERE seq = 1")


def test_karbon_events_are_immutable(store):
    _record(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE karbon_events SET resource_key = 'other'")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("DELETE FROM karbon_events")


def test_tampering_with_the_chain_is_detected(store):
    """Drop the triggers, edit history, and confirm the hash chain notices.

    Someone with direct file access can always drop a trigger. What they
    cannot do is recompute every following hash, so the chain is what makes
    the edit visible.
    """
    _record(store)
    store.upsert_task_from_karbon(WORK_ITEM)
    ok, _ = store.verify_audit_chain()
    assert ok

    store.conn.execute("DROP TRIGGER audit_log_no_update")
    store.conn.execute("UPDATE audit_log SET detail = '{\"tampered\":true}' WHERE seq = 1")

    ok, problem = store.verify_audit_chain()
    assert not ok
    assert "seq 1" in problem


def test_removing_an_entry_is_detected_as_a_sequence_gap(store):
    _record(store)
    store.upsert_task_from_karbon(WORK_ITEM)
    store.conn.execute("DROP TRIGGER audit_log_no_delete")
    store.conn.execute("DELETE FROM audit_log WHERE seq = 1")
    ok, problem = store.verify_audit_chain()
    assert not ok
    assert "sequence gap" in problem


def test_duplicate_delivery_is_not_stored_twice_but_is_audited(store):
    first_id, first_dup = _record(store)
    second_id, second_dup = _record(store)
    assert first_id is not None and not first_dup
    assert second_id is None and second_dup
    assert store.event_count() == 1
    kinds = [r["event_type"] for r in store.conn.execute("SELECT event_type FROM audit_log")]
    assert "webhook.received" in kinds
    assert "webhook.duplicate" in kinds


def test_mirroring_a_work_item_creates_then_updates(store):
    task_id, action = store.upsert_task_from_karbon(WORK_ITEM)
    assert action == "created"
    task = store.get_task(task_id)
    # PrimaryStatus is stored in the spaced, filterable form whichever endpoint
    # it came from.
    assert task["karbon_primary_status"] == "In Progress"
    assert task["runner_state"] == "in_progress"

    _, action = store.upsert_task_from_karbon(WORK_ITEM)
    assert action == "unchanged"

    moved = dict(WORK_ITEM, PrimaryStatus="Waiting")
    _, action = store.upsert_task_from_karbon(moved)
    assert action == "updated"
    assert store.get_task(task_id)["karbon_primary_status"] == "Waiting"


def test_progress_clock_only_moves_on_meaningful_change(store):
    task_id, _ = store.upsert_task_from_karbon(WORK_ITEM)
    before = store.get_task(task_id)["last_progress_at"]

    store.upsert_task_from_karbon(dict(WORK_ITEM, Title="GST return Q3 2026 (renamed)"))
    after_rename = store.get_task(task_id)["last_progress_at"]
    assert after_rename == before, "a title change is not progress"

    store.upsert_task_from_karbon(dict(WORK_ITEM, PrimaryStatus="Waiting"))
    after_status = store.get_task(task_id)["last_progress_at"]
    assert after_status > before, "a status change is progress"


# -- the constraint ---------------------------------------------------------

def test_an_agent_cannot_approve_its_own_work(store):
    task_id, _ = store.upsert_task_from_karbon(WORK_ITEM)
    store.request_approval(task_id, actor_type="agent", actor_id="bookkeeping-agent")
    assert store.get_task(task_id)["runner_state"] == "awaiting_approval"
    assert store.get_task(task_id)["awaiting_approval"] == 1

    with pytest.raises(PolicyViolation):
        store.transition_task(
            task_id, "approved", actor_type="agent", actor_id="bookkeeping-agent"
        )
    with pytest.raises(PolicyViolation):
        store.transition_task(
            task_id, "approved", actor_type="system", actor_id="scheduler"
        )
    assert store.get_task(task_id)["runner_state"] == "awaiting_approval"


def test_only_a_human_may_decide_an_approval(store):
    task_id, _ = store.upsert_task_from_karbon(WORK_ITEM)
    approval_id = store.request_approval(task_id, actor_type="agent", actor_id="agent-1")
    with pytest.raises(PolicyViolation):
        store.decide_approval(
            approval_id, decision="approved", decided_by_id="agent-1", actor_type="agent"
        )

    store.decide_approval(
        approval_id, decision="approved", decided_by_id="carl", actor_type="human"
    )
    assert store.get_task(task_id)["runner_state"] == "approved"
    assert store.get_task(task_id)["awaiting_approval"] == 0
    ok, problem = store.verify_audit_chain()
    assert ok, problem


def test_an_approval_cannot_be_decided_twice(store):
    task_id, _ = store.upsert_task_from_karbon(WORK_ITEM)
    approval_id = store.request_approval(task_id, actor_type="human", actor_id="carl")
    store.decide_approval(approval_id, decision="approved", decided_by_id="carl")
    with pytest.raises(StoreError):
        store.decide_approval(approval_id, decision="rejected", decided_by_id="carl")


def test_undefined_transitions_are_refused(store):
    task_id, _ = store.upsert_task_from_karbon(WORK_ITEM)
    store.transition_task(task_id, "completed", actor_type="system", actor_id="karbon-sync")
    with pytest.raises(StoreError):
        store.transition_task(task_id, "in_progress", actor_type="human", actor_id="carl")


def test_only_a_human_may_cancel(store):
    task_id, _ = store.upsert_task_from_karbon(WORK_ITEM)
    with pytest.raises(PolicyViolation):
        store.transition_task(task_id, "cancelled", actor_type="agent", actor_id="agent-1")
    store.transition_task(task_id, "cancelled", actor_type="human", actor_id="carl")


def test_policy_refusal_is_recorded_permanently(store):
    store.record_policy_refusal(
        actor_type="agent",
        actor_id="gst-agent",
        attempted="POST /v3/Notes",
        reason="phase one is read only",
    )
    row = store.conn.execute(
        "SELECT * FROM audit_log WHERE event_type = 'policy.refused'"
    ).fetchone()
    assert row is not None
    assert "POST /v3/Notes" in row["detail"]
    ok, _ = store.verify_audit_chain()
    assert ok
