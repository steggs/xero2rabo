"""Task two: read what Karbon already holds, and check it against Jersey rules.

This is an audit, not a build. Nothing here writes to Karbon and nothing here
changes a template. It pulls the client list, the work templates, the work
schedules behind the recurring jobs, and the work items those schedules have
produced, then compares what it finds against config/jersey-deadlines.json.

The comparison is deliberately split in two:

  * Checks that can be made mechanically, and are therefore made: does the
    recurrence frequency match the statutory frequency, does a deadline date
    exist at all, and does each deadline date land on the day of the month the
    rule requires. A GST deadline that is not the last day of a month, or a CER
    deadline that is not the 15th, is wrong regardless of anything else.

  * Everything else, which is printed for Carl to confirm rather than asserted.
    Karbon's schedule fields do not map one to one onto "the last day of the
    month after the period ends", and a client may hold approval for a
    non-standard GST period. Guessing there would produce confident nonsense,
    so the report shows the configuration and the rule side by side and asks.
"""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .karbon_client import KarbonClient, KarbonError
from .stall import parse_karbon_datetime

EXPECTATIONS_PATH = Path(__file__).resolve().parent.parent / "config" / "jersey-deadlines.json"


def load_expectations(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or EXPECTATIONS_PATH).read_text())


@dataclass
class Observation:
    level: str          # ok | check | mismatch
    subject: str
    message: str


@dataclass
class ClientInventory:
    organizations: list[dict[str, Any]] = field(default_factory=list)
    contacts: list[dict[str, Any]] = field(default_factory=list)
    client_groups: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.organizations) + len(self.contacts) + len(self.client_groups)


@dataclass
class AuditResult:
    tenant_key: str | None
    inventory: ClientInventory
    work_types: list[str]
    templates: list[dict[str, Any]]
    schedules: list[dict[str, Any]]
    matched_work_items: dict[str, list[dict[str, Any]]]
    observations: list[Observation] = field(default_factory=list)

    def by_level(self, level: str) -> list[Observation]:
        return [o for o in self.observations if o.level == level]


# ---------------------------------------------------------------------------
# Deadline shape checks
# ---------------------------------------------------------------------------

def deadline_matches_shape(deadline_iso: str | None, shape: dict[str, Any]) -> bool | None:
    """True, False, or None when there is no date to judge."""
    parsed = parse_karbon_datetime(deadline_iso)
    if parsed is None:
        return None
    day = parsed.day
    kind = shape.get("kind")
    if kind == "last_day_of_month":
        return day == calendar.monthrange(parsed.year, parsed.month)[1]
    if kind == "day_of_month":
        return day == int(shape["day"])
    if kind == "fixed_dates":
        return parsed.strftime("%m-%d") in set(shape.get("dates", []))
    return None


def classify(text: str | None, hints: dict[str, list[str]]) -> str | None:
    """Map a work type or title onto a deadline type using the configured hints."""
    if not text:
        return None
    lowered = text.lower()
    for deadline_id, needles in hints.items():
        if any(needle in lowered for needle in needles):
            return deadline_id
    return None


# ---------------------------------------------------------------------------
# The audit itself
# ---------------------------------------------------------------------------

def pull_client_inventory(client: KarbonClient) -> ClientInventory:
    inventory = ClientInventory()
    inventory.organizations = list(client.iter_organizations())
    inventory.contacts = list(client.iter_contacts())
    try:
        inventory.client_groups = list(client.iter_client_groups())
    except KarbonError:
        # Client groups are optional in a small practice; absence is not a fault.
        inventory.client_groups = []
    return inventory


def run_audit(
    client: KarbonClient,
    *,
    expectations: dict[str, Any] | None = None,
    work_item_sample: int = 500,
) -> AuditResult:
    expectations = expectations or load_expectations()
    hints = expectations["work_type_hints"]
    by_id = {entry["id"]: entry for entry in expectations["deadline_types"]}

    settings = client.get("/v3/TenantSettings") or {}
    work_types = [w.get("Name") for w in (settings.get("WorkTypes") or []) if w.get("Name")]

    inventory = pull_client_inventory(client)
    templates = list(client.iter_work_templates())

    work_items: list[dict[str, Any]] = []
    for item in client.iter_work_items():
        work_items.append(item)
        if len(work_items) >= work_item_sample:
            break

    # Pull the schedule behind each recurring work item we care about, once each.
    schedule_keys = {
        item.get("WorkScheduleKey")
        for item in work_items
        if item.get("WorkScheduleKey")
    }
    schedules: list[dict[str, Any]] = []
    for key in sorted(k for k in schedule_keys if k):
        try:
            schedules.append(client.get_work_schedule(key))
        except KarbonError as exc:
            schedules.append({"WorkScheduleKey": key, "_error": str(exc)})

    matched: dict[str, list[dict[str, Any]]] = {deadline_id: [] for deadline_id in by_id}
    for item in work_items:
        deadline_id = classify(item.get("WorkType"), hints) or classify(item.get("Title"), hints)
        if deadline_id:
            matched[deadline_id].append(item)

    observations: list[Observation] = []

    # 1. Does the tenant even have a work type for each obligation?
    for deadline_id, entry in by_id.items():
        needles = hints.get(deadline_id, [])
        present = [wt for wt in work_types if any(n in wt.lower() for n in needles)]
        if present:
            observations.append(
                Observation(
                    "ok",
                    entry["label"],
                    f"Karbon work type(s) found: {', '.join(present)}.",
                )
            )
        else:
            observations.append(
                Observation(
                    "check",
                    entry["label"],
                    "No Karbon work type name matches this obligation. Either it is "
                    "tracked under a name the hints do not cover, or it is not tracked.",
                )
            )

    # 2. Do the work items carrying each obligation have deadline dates at all,
    #    and do those dates land where the statutory rule says they must?
    for deadline_id, entry in by_id.items():
        items = matched[deadline_id]
        if not items:
            observations.append(
                Observation(
                    "check",
                    entry["label"],
                    "No work items matched this obligation in the sample. Confirm "
                    "whether any client of the firm has this obligation at all.",
                )
            )
            continue

        missing_deadline = [i for i in items if not i.get("DeadlineDate")]
        if missing_deadline:
            observations.append(
                Observation(
                    "mismatch",
                    entry["label"],
                    f"{len(missing_deadline)} of {len(items)} work items have no "
                    f"DeadlineDate. Without one the runner counts down to the internal "
                    f"due date instead of the statutory one.",
                )
            )

        wrong_shape = []
        for item in items:
            verdict = deadline_matches_shape(item.get("DeadlineDate"), entry["deadline_shape"])
            if verdict is False:
                wrong_shape.append(item)
        if wrong_shape:
            examples = ", ".join(
                f"{i.get('ClientName') or '?'} {str(i.get('DeadlineDate'))[:10]}"
                for i in wrong_shape[:5]
            )
            observations.append(
                Observation(
                    "mismatch",
                    entry["label"],
                    f"{len(wrong_shape)} work items have a DeadlineDate that does not "
                    f"match the rule ({entry['deadline_shape']['description']}). "
                    f"Examples: {examples}.",
                )
            )
        elif items and not missing_deadline:
            observations.append(
                Observation(
                    "ok",
                    entry["label"],
                    f"All {len(items)} matched work items carry a DeadlineDate "
                    f"consistent with: {entry['deadline_shape']['description']}",
                )
            )

    # 3. Does the recurrence on each schedule match the statutory frequency?
    schedules_by_key = {s.get("WorkScheduleKey"): s for s in schedules}
    for deadline_id, entry in by_id.items():
        expected = set(entry["karbon_recurrence_frequency"])
        seen: dict[str, int] = {}
        for item in matched[deadline_id]:
            schedule = schedules_by_key.get(item.get("WorkScheduleKey"))
            if not schedule or "_error" in schedule:
                continue
            frequency = schedule.get("RecurrenceFrequency")
            if frequency:
                seen[frequency] = seen.get(frequency, 0) + 1
        if not seen:
            observations.append(
                Observation(
                    "check",
                    entry["label"],
                    "No recurring work schedule sits behind these work items. If this "
                    "obligation repeats, it is being created by hand each period.",
                )
            )
            continue
        unexpected = {f: n for f, n in seen.items() if f not in expected}
        if unexpected:
            observations.append(
                Observation(
                    "mismatch",
                    entry["label"],
                    f"Recurrence is {unexpected} but the statutory frequency is "
                    f"{entry['default_frequency']} ({'/'.join(sorted(expected))}).",
                )
            )
        else:
            observations.append(
                Observation(
                    "ok",
                    entry["label"],
                    f"Recurrence {seen} matches the statutory frequency.",
                )
            )

        # Whether the schedule sets a deadline date at all is mechanical; how it
        # derives it is not, so it is shown rather than judged.
        for item in matched[deadline_id]:
            schedule = schedules_by_key.get(item.get("WorkScheduleKey"))
            if not schedule or "_error" in schedule:
                continue
            method = schedule.get("ScheduleDeadlineDateMethod")
            if method in (None, "NoDeadlineDate"):
                observations.append(
                    Observation(
                        "mismatch",
                        entry["label"],
                        f"Schedule {schedule.get('WorkScheduleKey')} sets no deadline "
                        f"date ({method}). Generated work items will carry only an "
                        f"internal due date.",
                    )
                )
                break

    return AuditResult(
        tenant_key=settings.get("TenantKey"),
        inventory=inventory,
        work_types=work_types,
        templates=templates,
        schedules=schedules,
        matched_work_items=matched,
        observations=observations,
    )


def format_audit(result: AuditResult, expectations: dict[str, Any] | None = None) -> str:
    expectations = expectations or load_expectations()
    lines: list[str] = []
    lines.append("Karbon audit")
    lines.append(f"Tenant key: {result.tenant_key or 'unknown'}")
    lines.append("")
    lines.append("Client list")
    lines.append(f"  Organizations: {len(result.inventory.organizations)}")
    lines.append(f"  Contacts:      {len(result.inventory.contacts)}")
    lines.append(f"  Client groups: {len(result.inventory.client_groups)}")
    lines.append(f"  Total records: {result.inventory.total}")
    lines.append("")
    lines.append(f"Work types configured ({len(result.work_types)}): "
                 f"{', '.join(result.work_types) or 'none'}")
    lines.append(f"Work templates: {len(result.templates)}")
    lines.append(f"Work schedules behind sampled work items: {len(result.schedules)}")
    lines.append("")

    for level, heading in (
        ("mismatch", "Does not match the Jersey rule"),
        ("check", "Needs Carl to confirm"),
        ("ok", "Consistent with the Jersey rule"),
    ):
        group = result.by_level(level)
        if not group:
            continue
        lines.append(f"{heading} ({len(group)})")
        for obs in group:
            lines.append(f"  {obs.subject}: {obs.message}")
        lines.append("")

    lines.append("Rules this audit was checked against")
    for entry in expectations["deadline_types"]:
        lines.append(f"  {entry['label']}")
        lines.append(f"    {entry['rule']}")
        for caveat in entry.get("caveats", []):
            lines.append(f"    Caveat: {caveat}")
        lines.append(f"    Source: {entry['source']}")
    return "\n".join(lines).rstrip() + "\n"
