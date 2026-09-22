"""Stall detection.

This is the first part of the runner that protects Carl rather than just
recording what happened, so the thresholds are set to be conservative in a
specific direction: raise a flag too early rather than too late, and treat
missing information as a reason to look rather than a reason to stay quiet.

Three consequences of that stance, all deliberate:

  * Days are counted as calendar days, not working days. A deadline three days
    out over a weekend is treated as three days out, not one working day.
  * A task with no deadline recorded in Karbon is not exempt. If nothing has
    moved on it for a month it is flagged, because an absent deadline is more
    often a data gap than a genuinely open-ended job.
  * The health of the pipe itself is checked on the same schedule. Karbon ends
    a webhook subscription after ten failed deliveries, so a runner that has
    simply stopped hearing about work would otherwise look perfectly calm.

Dates are evaluated against the local date in Jersey, not UTC, so a deadline
counts down on Carl's calendar rather than the server's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .store import Store

# The escalation ladder. Named so the reasoning is visible and so Carl can move
# a number without reading the code around it.
@dataclass(frozen=True)
class Thresholds:
    # A job not yet started this close to its deadline is urgent.
    unstarted_urgent_days: int = 7
    # A job not yet started this close is worth looking at.
    unstarted_concern_days: int = 14
    # Inside this window, any silence at all is urgent.
    final_stretch_days: int = 3
    final_stretch_idle_days: int = 2
    # Outside it but still inside the horizon, a week of silence is a concern.
    horizon_days: int = 14
    horizon_idle_days: int = 7
    # An approval sitting in Carl's queue is itself a stall.
    approval_idle_concern_days: int = 3
    # A task with no deadline at all that has not moved in this long.
    no_deadline_idle_days: int = 30
    # A mirrored task we have not refreshed from Karbon in this long.
    mirror_stale_days: int = 7


UNSTARTED_STATES = frozenset({"received", "triaged"})


@dataclass
class Finding:
    task_id: int | None
    scope: str
    severity: str
    reason_code: str
    detail: str
    days_to_deadline: int | None = None
    idle_days: int | None = None


@dataclass
class StallReport:
    run_at: str
    evaluated_tasks: int
    findings: list[Finding] = field(default_factory=list)
    new_flags: list[int] = field(default_factory=list)
    cleared_flags: list[int] = field(default_factory=list)

    def by_severity(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]


def parse_karbon_datetime(value: str | None) -> datetime | None:
    """Parse the date formats Karbon returns, tolerantly.

    Karbon sends ISO 8601, sometimes with a trailing Z, sometimes date only.
    A value we cannot parse returns None, and the caller treats that the same
    way it treats an absent date: as missing information, not as "fine".
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def local_date(value: str | None, tz: ZoneInfo) -> date | None:
    parsed = parse_karbon_datetime(value)
    return parsed.astimezone(tz).date() if parsed else None


def effective_deadline(task: Any, tz: ZoneInfo) -> tuple[date | None, str | None]:
    """Prefer DeadlineDate over DueDate.

    DueDate is the firm's internal target. DeadlineDate is the one imposed from
    outside, by Revenue Jersey or by Social Security, and it is the one that
    carries a penalty, so it is the one the stall check counts down to. If only
    a due date exists, that is used and labelled as such.
    """
    deadline = local_date(task["deadline_date"], tz)
    if deadline:
        return deadline, "deadline_date"
    due = local_date(task["due_date"], tz)
    if due:
        return due, "due_date"
    return None, None


def _idle_days(task: Any, today: date, tz: ZoneInfo) -> int | None:
    last = local_date(task["last_progress_at"], tz)
    return (today - last).days if last else None


def evaluate_task(task: Any, *, today: date, tz: ZoneInfo, thresholds: Thresholds) -> list[Finding]:
    findings: list[Finding] = []
    task_id = int(task["id"])
    state = task["runner_state"]
    title = task["title"] or task["karbon_work_item_key"] or f"task {task_id}"
    client = task["client_name"] or "unknown client"

    deadline, source = effective_deadline(task, tz)
    idle = _idle_days(task, today, tz)
    days = (deadline - today).days if deadline else None

    if deadline is None:
        if idle is not None and idle >= thresholds.no_deadline_idle_days:
            findings.append(
                Finding(
                    task_id=task_id,
                    scope="task",
                    severity="watch",
                    reason_code="stale_no_deadline",
                    detail=(
                        f"{client}: {title} has no due or deadline date in Karbon and "
                        f"has not moved in {idle} days. Either it is finished and not "
                        f"closed, or a date is missing from the work item."
                    ),
                    idle_days=idle,
                )
            )
    else:
        if days is not None and days < 0:
            findings.append(
                Finding(
                    task_id=task_id,
                    scope="task",
                    severity="breach",
                    reason_code="deadline_passed",
                    detail=(
                        f"{client}: {title} passed its {source.replace('_', ' ')} "
                        f"{abs(days)} days ago and is still {state}."
                    ),
                    days_to_deadline=days,
                    idle_days=idle,
                )
            )
        else:
            if state in UNSTARTED_STATES and days <= thresholds.unstarted_urgent_days:
                findings.append(
                    Finding(
                        task_id=task_id,
                        scope="task",
                        severity="urgent",
                        reason_code="unstarted_near_deadline",
                        detail=(
                            f"{client}: {title} is due in {days} days and has not been "
                            f"started (state {state})."
                        ),
                        days_to_deadline=days,
                        idle_days=idle,
                    )
                )
            elif (
                state in UNSTARTED_STATES
                and days <= thresholds.unstarted_concern_days
            ):
                findings.append(
                    Finding(
                        task_id=task_id,
                        scope="task",
                        severity="concern",
                        reason_code="unstarted_in_horizon",
                        detail=(
                            f"{client}: {title} is due in {days} days and has not been "
                            f"started (state {state})."
                        ),
                        days_to_deadline=days,
                        idle_days=idle,
                    )
                )

            if idle is not None:
                if (
                    days <= thresholds.final_stretch_days
                    and idle >= thresholds.final_stretch_idle_days
                ):
                    findings.append(
                        Finding(
                            task_id=task_id,
                            scope="task",
                            severity="urgent",
                            reason_code="idle_in_final_stretch",
                            detail=(
                                f"{client}: {title} is due in {days} days and nothing "
                                f"has changed on it for {idle} days."
                            ),
                            days_to_deadline=days,
                            idle_days=idle,
                        )
                    )
                elif (
                    days <= thresholds.horizon_days
                    and idle >= thresholds.horizon_idle_days
                ):
                    findings.append(
                        Finding(
                            task_id=task_id,
                            scope="task",
                            severity="concern",
                            reason_code="idle_in_horizon",
                            detail=(
                                f"{client}: {title} is due in {days} days and nothing "
                                f"has changed on it for {idle} days."
                            ),
                            days_to_deadline=days,
                            idle_days=idle,
                        )
                    )

    if state == "awaiting_approval" and idle is not None:
        if idle >= thresholds.approval_idle_concern_days:
            severity = (
                "urgent"
                if days is not None and days <= thresholds.final_stretch_days
                else "concern"
            )
            findings.append(
                Finding(
                    task_id=task_id,
                    scope="task",
                    severity=severity,
                    reason_code="approval_waiting",
                    detail=(
                        f"{client}: {title} has been waiting on approval for "
                        f"{idle} days."
                    ),
                    days_to_deadline=days,
                    idle_days=idle,
                )
            )

    last_synced = local_date(task["last_synced_at"], tz)
    if last_synced is not None:
        stale_days = (today - last_synced).days
        if stale_days >= thresholds.mirror_stale_days:
            findings.append(
                Finding(
                    task_id=task_id,
                    scope="task",
                    severity="watch",
                    reason_code="mirror_stale",
                    detail=(
                        f"{client}: {title} has not been refreshed from Karbon for "
                        f"{stale_days} days, so anything above is based on stale data."
                    ),
                    days_to_deadline=days,
                    idle_days=idle,
                )
            )

    return findings


def evaluate_pipe(store: Store, *, now: datetime, silence_hours: int, signing_key_set: bool) -> list[Finding]:
    """Check that the pipe itself is alive.

    A runner with no work flagged and no events arriving looks identical to a
    runner with nothing to do. These findings tell the two apart.
    """
    findings: list[Finding] = []

    subscriptions = store.subscriptions()
    if not subscriptions:
        findings.append(
            Finding(
                task_id=None,
                scope="pipe",
                severity="concern",
                reason_code="pipe_not_wired",
                detail=(
                    "No Karbon webhook subscription is recorded. Nothing will reach "
                    "the runner until one is created."
                ),
            )
        )
    else:
        last_seen = parse_karbon_datetime(store.last_webhook_received_at())
        if last_seen is None:
            findings.append(
                Finding(
                    task_id=None,
                    scope="pipe",
                    severity="concern",
                    reason_code="pipe_never_delivered",
                    detail=(
                        "A subscription is recorded but no delivery has ever arrived. "
                        "Check that the target URL is reachable over HTTPS from "
                        "outside."
                    ),
                )
            )
        else:
            silent_hours = (now - last_seen).total_seconds() / 3600
            if silent_hours >= silence_hours:
                findings.append(
                    Finding(
                        task_id=None,
                        scope="pipe",
                        severity="concern",
                        reason_code="pipe_silent",
                        detail=(
                            f"No Karbon delivery for {silent_hours:.0f} hours. Karbon "
                            "cancels a subscription after ten failed deliveries, so "
                            "confirm the subscription still exists."
                        ),
                    )
                )

    if not signing_key_set:
        findings.append(
            Finding(
                task_id=None,
                scope="pipe",
                severity="watch",
                reason_code="pipe_unsigned",
                detail=(
                    "The webhook subscription has no signing key, so deliveries are "
                    "recorded but cannot be proved to have come from Karbon."
                ),
            )
        )

    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    rejected = store.conn.execute(
        "SELECT COUNT(*) AS n FROM rejected_deliveries WHERE received_at >= ?", (since,)
    ).fetchone()["n"]
    if rejected:
        findings.append(
            Finding(
                task_id=None,
                scope="pipe",
                severity="concern",
                reason_code="pipe_rejecting",
                detail=(
                    f"{rejected} delivery/deliveries were rejected in the last 24 hours. "
                    "If these are genuine Karbon events being dropped, the subscription "
                    "is ten failures away from being cancelled."
                ),
            )
        )

    return findings


def run_stall_check(
    store: Store,
    *,
    timezone_name: str = "Europe/Jersey",
    thresholds: Thresholds | None = None,
    now: datetime | None = None,
    signing_key_set: bool = False,
    silence_hours: int = 36,
) -> StallReport:
    """Evaluate every open task and the pipe, then reconcile the flag table.

    Flags are keyed on (task, reason). A finding that is already flagged does
    not raise a second one, and a flag whose condition has gone away is cleared
    with an audit entry, so the open flag list is always the current picture.
    """
    thresholds = thresholds or Thresholds()
    tz = ZoneInfo(timezone_name)
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(tz).date()

    tasks = store.open_tasks()
    findings: list[Finding] = []
    for task in tasks:
        findings.extend(
            evaluate_task(task, today=today, tz=tz, thresholds=thresholds)
        )
    findings.extend(
        evaluate_pipe(
            store,
            now=now,
            silence_hours=silence_hours,
            signing_key_set=signing_key_set,
        )
    )

    report = StallReport(run_at=now.isoformat(), evaluated_tasks=len(tasks))
    report.findings = findings

    current_keys = {(f.task_id, f.reason_code) for f in findings}
    for finding in findings:
        flag_id, is_new = store.open_stall_flag(
            task_id=finding.task_id,
            scope=finding.scope,
            severity=finding.severity,
            reason_code=finding.reason_code,
            detail=finding.detail,
            days_to_deadline=finding.days_to_deadline,
            idle_days=finding.idle_days,
        )
        if is_new:
            report.new_flags.append(flag_id)

    for flag in store.open_stall_flags():
        key = (flag["task_id"], flag["reason_code"])
        if key not in current_keys:
            store.clear_stall_flag(
                int(flag["id"]), reason="condition no longer met at stall check"
            )
            report.cleared_flags.append(int(flag["id"]))

    store.set_sync_state("last_stall_check_at", now.isoformat())
    return report


def format_report(report: StallReport) -> str:
    """A plain text summary, suitable for a cron job's output."""
    lines = [
        f"Stall check at {report.run_at}",
        f"Open tasks evaluated: {report.evaluated_tasks}",
        f"Findings: {len(report.findings)} "
        f"(new flags {len(report.new_flags)}, cleared {len(report.cleared_flags)})",
        "",
    ]
    if not report.findings:
        lines.append("Nothing flagged.")
        return "\n".join(lines)

    for severity in ("breach", "urgent", "concern", "watch"):
        group = report.by_severity(severity)
        if not group:
            continue
        lines.append(f"{severity.upper()} ({len(group)})")
        for finding in group:
            lines.append(f"  [{finding.reason_code}] {finding.detail}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
