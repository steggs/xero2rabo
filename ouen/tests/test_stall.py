"""Stall detection. The thresholds are conservative on purpose, so these tests
pin the boundaries rather than just checking that something fires."""

from datetime import datetime, timedelta, timezone

import pytest

from zoneinfo import ZoneInfo

from runner.stall import (
    effective_deadline,
    parse_karbon_datetime,
    run_stall_check,
)

JERSEY = ZoneInfo("Europe/Jersey")

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)


def iso(days_from_now: int) -> str:
    return (NOW + timedelta(days=days_from_now)).strftime("%Y-%m-%dT00:00:00Z")


def make_task(store, *, key, deadline=None, due=None, status="In Progress",
              last_progress_days_ago=0, title="GST return"):
    task_id, _ = store.upsert_task_from_karbon(
        {
            "WorkItemKey": key,
            "Title": title,
            "ClientKey": "ORG1",
            "ClientName": "Pier Road Trading Ltd",
            "ClientType": "Organization",
            "WorkType": "GST Return",
            "PrimaryStatus": status,
            "StartDate": iso(-30),
            "DueDate": due,
            "DeadlineDate": deadline,
        }
    )
    stamp = (NOW - timedelta(days=last_progress_days_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    store.conn.execute(
        "UPDATE tasks SET last_progress_at = ?, last_synced_at = ? WHERE id = ?",
        (stamp, NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), task_id),
    )
    return task_id


def check(store, **kwargs):
    return run_stall_check(store, now=NOW, signing_key_set=True, **kwargs)


def codes(report):
    return {f.reason_code for f in report.findings}


# -- date handling ----------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    ["2026-10-31T00:00:00Z", "2026-10-31", "2026-10-31T00:00:00+00:00"],
)
def test_karbon_date_formats_all_parse(value):
    assert parse_karbon_datetime(value).date().isoformat() == "2026-10-31"


def test_unparseable_date_is_treated_as_missing_not_as_fine(store):
    make_task(store, key="W1", deadline="not a date", last_progress_days_ago=40)
    report = check(store)
    assert "stale_no_deadline" in codes(report)


def test_deadline_date_is_preferred_over_due_date(store):
    task_id = make_task(store, key="W1", deadline=iso(20), due=iso(2))
    task = store.get_task(task_id)
    resolved, source = effective_deadline(task, JERSEY)
    assert source == "deadline_date"
    assert resolved.isoformat() == (NOW + timedelta(days=20)).date().isoformat()


def test_due_date_is_used_when_there_is_no_deadline_date(store):
    task_id = make_task(store, key="W1", due=iso(5))
    task = store.get_task(task_id)
    _, source = effective_deadline(task, JERSEY)
    assert source == "due_date"


# -- the ladder -------------------------------------------------------------

def test_a_passed_deadline_is_a_breach(store):
    make_task(store, key="W1", deadline=iso(-1), status="In Progress")
    report = check(store)
    assert "deadline_passed" in codes(report)
    assert report.by_severity("breach")


def test_unstarted_inside_seven_days_is_urgent(store):
    make_task(store, key="W1", deadline=iso(6), status="Planned")
    report = check(store)
    assert "unstarted_near_deadline" in codes(report)
    assert {f.severity for f in report.findings if f.reason_code == "unstarted_near_deadline"} == {"urgent"}


def test_unstarted_between_eight_and_fourteen_days_is_a_concern_not_urgent(store):
    make_task(store, key="W1", deadline=iso(12), status="Planned")
    report = check(store)
    assert "unstarted_in_horizon" in codes(report)
    assert "unstarted_near_deadline" not in codes(report)


def test_unstarted_beyond_the_horizon_is_quiet(store):
    make_task(store, key="W1", deadline=iso(40), status="Planned")
    report = check(store)
    assert not [f for f in report.findings if f.scope == "task"]


def test_silence_in_the_final_stretch_is_urgent(store):
    make_task(store, key="W1", deadline=iso(2), status="In Progress",
              last_progress_days_ago=3)
    report = check(store)
    assert "idle_in_final_stretch" in codes(report)


def test_two_days_of_silence_three_days_out_is_the_boundary(store):
    """final_stretch_idle_days is 2: one day of silence is not yet a flag."""
    make_task(store, key="W1", deadline=iso(3), last_progress_days_ago=1)
    assert "idle_in_final_stretch" not in codes(check(store))

    make_task(store, key="W2", deadline=iso(3), last_progress_days_ago=2)
    assert "idle_in_final_stretch" in codes(check(store))


def test_a_week_of_silence_inside_the_horizon_is_a_concern(store):
    make_task(store, key="W1", deadline=iso(10), last_progress_days_ago=8)
    report = check(store)
    assert "idle_in_horizon" in codes(report)


def test_an_approval_left_sitting_is_itself_a_stall(store):
    task_id = make_task(store, key="W1", deadline=iso(30))
    store.request_approval(task_id, actor_type="agent", actor_id="agent-1")
    store.conn.execute(
        "UPDATE tasks SET last_progress_at = ? WHERE id = ?",
        ((NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), task_id),
    )
    report = check(store)
    assert "approval_waiting" in codes(report)


def test_a_task_with_no_deadline_is_not_exempt(store):
    make_task(store, key="W1", last_progress_days_ago=45)
    report = check(store)
    assert "stale_no_deadline" in codes(report)
    assert {f.severity for f in report.findings if f.reason_code == "stale_no_deadline"} == {"watch"}


def test_a_completed_task_is_not_evaluated(store):
    make_task(store, key="W1", deadline=iso(-30), status="Completed")
    report = check(store)
    assert report.evaluated_tasks == 0
    assert "deadline_passed" not in codes(report)


def test_calendar_days_not_working_days(store):
    """A deadline three calendar days out over a weekend still counts as three."""
    friday = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)  # Friday
    store.upsert_task_from_karbon(
        {
            "WorkItemKey": "W1",
            "Title": "CER September",
            "ClientKey": "ORG1",
            "ClientName": "Pier Road Trading Ltd",
            "ClientType": "Organization",
            "PrimaryStatus": "Planned",
            "StartDate": "2026-09-01T00:00:00Z",
            "DeadlineDate": "2026-09-28T00:00:00Z",  # Monday, three days later
        }
    )
    report = run_stall_check(store, now=friday, signing_key_set=True)
    finding = next(f for f in report.findings if f.reason_code == "unstarted_near_deadline")
    assert finding.days_to_deadline == 3


# -- flag lifecycle ---------------------------------------------------------

def test_a_flag_is_raised_once_and_cleared_when_it_no_longer_applies(store):
    task_id = make_task(store, key="W1", deadline=iso(5), status="Planned")
    first = check(store)
    assert len(first.new_flags) >= 1

    second = check(store)
    assert second.new_flags == [], "the same condition must not flag twice"

    store.transition_task(task_id, "in_progress", actor_type="human", actor_id="carl")
    third = check(store)
    assert "unstarted_near_deadline" not in codes(third)
    assert third.cleared_flags

    ok, problem = store.verify_audit_chain()
    assert ok, problem


# -- the pipe ---------------------------------------------------------------

def test_an_unwired_pipe_is_flagged(store):
    report = check(store)
    assert "pipe_not_wired" in codes(report)


def test_a_wired_pipe_that_never_delivered_is_flagged(store):
    store.record_subscription(
        webhook_type="Work", target_url="https://example.je/karbon/webhook",
        webhook_types=None, signing_key_set=True, batch_size=None, actor_id="carl",
    )
    report = check(store)
    assert "pipe_never_delivered" in codes(report)
    assert "pipe_not_wired" not in codes(report)


def test_a_silent_pipe_is_flagged(store):
    store.record_subscription(
        webhook_type="Work", target_url="https://example.je/karbon/webhook",
        webhook_types=None, signing_key_set=True, batch_size=None, actor_id="carl",
    )
    store.conn.execute(
        "INSERT INTO sync_state (key, value, updated_at) VALUES "
        "('last_webhook_received_at', ?, ?)",
        ((NOW - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "x"),
    )
    report = check(store, silence_hours=36)
    assert "pipe_silent" in codes(report)


def test_an_unsigned_pipe_is_flagged(store):
    report = run_stall_check(store, now=NOW, signing_key_set=False)
    assert "pipe_unsigned" in codes(report)


def test_rejected_deliveries_are_flagged(store):
    store.record_subscription(
        webhook_type="Work", target_url="https://example.je/karbon/webhook",
        webhook_types=None, signing_key_set=True, batch_size=None, actor_id="carl",
    )
    store.record_rejected_delivery(
        reason="signature did not match", raw_body="{}", source_ip="203.0.113.1"
    )
    report = check(store)
    assert "pipe_rejecting" in codes(report)
