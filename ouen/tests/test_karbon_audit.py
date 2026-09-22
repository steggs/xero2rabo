"""Task two's mechanical checks. The statutory rules are the fixture here, so
a change to the Jersey expectations file that breaks them shows up as a
failure rather than as a quietly wrong audit."""

import pytest

from runner.karbon_audit import (
    classify,
    deadline_matches_shape,
    format_audit,
    load_expectations,
    run_audit,
)

EXPECTATIONS = load_expectations()
BY_ID = {e["id"]: e for e in EXPECTATIONS["deadline_types"]}


def shape(deadline_id):
    return BY_ID[deadline_id]["deadline_shape"]


# -- the rules --------------------------------------------------------------

@pytest.mark.parametrize(
    "date,ok",
    [
        ("2026-04-30T00:00:00Z", True),    # quarter to 31 March, due 30 April
        ("2026-07-31T00:00:00Z", True),
        ("2026-02-28T00:00:00Z", True),
        ("2028-02-29T00:00:00Z", True),    # leap year
        ("2026-04-25T00:00:00Z", False),
        ("2026-02-29T00:00:00Z", False),   # not a real date in 2026
    ],
)
def test_gst_deadlines_must_land_on_the_last_day_of_a_month(date, ok):
    if date == "2026-02-29T00:00:00Z":
        assert deadline_matches_shape(date, shape("gst_return")) is None
        return
    assert deadline_matches_shape(date, shape("gst_return")) is ok


@pytest.mark.parametrize(
    "date,ok",
    [("2026-10-15T00:00:00Z", True), ("2026-10-14T00:00:00Z", False)],
)
def test_cer_deadlines_must_land_on_the_fifteenth(date, ok):
    assert deadline_matches_shape(date, shape("combined_employer_return")) is ok


@pytest.mark.parametrize(
    "date,ok",
    [
        ("2027-05-31T00:00:00Z", True),   # paper
        ("2027-07-31T00:00:00Z", True),   # online
        ("2027-06-30T00:00:00Z", False),
    ],
)
def test_personal_tax_deadlines_are_31_may_or_31_july(date, ok):
    assert deadline_matches_shape(date, shape("personal_tax_return")) is ok


def test_a_missing_deadline_is_unjudgeable_not_compliant():
    assert deadline_matches_shape(None, shape("gst_return")) is None


def test_expectations_carry_a_source_and_caveats_for_every_obligation():
    for entry in EXPECTATIONS["deadline_types"]:
        assert entry["source"].startswith("https://www.gov.je/")
        assert entry["caveats"], f"{entry['id']} has no caveats recorded"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("GST Return", "gst_return"),
        ("Combined Employer Return", "combined_employer_return"),
        ("Monthly ITIS", "combined_employer_return"),
        ("Personal Tax Return 2025", "personal_tax_return"),
        ("Annual accounts", None),
        (None, None),
    ],
)
def test_work_types_map_onto_obligations(text, expected):
    assert classify(text, EXPECTATIONS["work_type_hints"]) == expected


# -- the audit against a stand-in Karbon ------------------------------------

class FakeClient:
    """Stands in for KarbonClient. Read only, like the real one."""

    def __init__(self, *, work_items, schedules, work_types):
        self._work_items = work_items
        self._schedules = schedules
        self._work_types = work_types

    def get(self, path, **params):
        assert path == "/v3/TenantSettings"
        return {
            "TenantKey": "TEN1",
            "WorkTypes": [{"Name": name} for name in self._work_types],
        }

    def iter_organizations(self):
        return iter([{"FullName": "Pier Road Trading Ltd", "OrganizationKey": "ORG1"}])

    def iter_contacts(self):
        return iter([{"FullName": "A Person", "ContactKey": "C1"}])

    def iter_client_groups(self):
        return iter([])

    def iter_work_templates(self):
        return iter([{"WorkTemplateKey": "T1", "Title": "GST Return"}])

    def iter_work_items(self, odata_filter=None):
        return iter(self._work_items)

    def get_work_schedule(self, key):
        return self._schedules[key]


def _gst_item(deadline, schedule_key="S1"):
    return {
        "WorkItemKey": "W" + deadline[:10],
        "Title": "GST return",
        "WorkType": "GST Return",
        "ClientName": "Pier Road Trading Ltd",
        "DeadlineDate": deadline,
        "WorkScheduleKey": schedule_key,
    }


def test_a_correctly_configured_gst_schedule_reports_clean():
    client = FakeClient(
        work_items=[_gst_item("2026-04-30T00:00:00Z"), _gst_item("2026-07-31T00:00:00Z")],
        schedules={
            "S1": {
                "WorkScheduleKey": "S1",
                "RecurrenceFrequency": "Quarter",
                "ScheduleDeadlineDateMethod": "LastDayOfMonth",
            }
        },
        work_types=["GST Return", "Combined Employer Return", "Personal Tax Return"],
    )
    result = run_audit(client)
    gst = [o for o in result.observations if "GST" in o.subject]
    assert not [o for o in gst if o.level == "mismatch"]
    assert result.inventory.total == 2


def test_a_gst_deadline_on_the_wrong_day_is_a_mismatch():
    client = FakeClient(
        work_items=[_gst_item("2026-04-20T00:00:00Z")],
        schedules={
            "S1": {
                "WorkScheduleKey": "S1",
                "RecurrenceFrequency": "Quarter",
                "ScheduleDeadlineDateMethod": "LastDayOfMonth",
            }
        },
        work_types=["GST Return"],
    )
    result = run_audit(client)
    mismatches = [o.message for o in result.by_level("mismatch")]
    assert any("does not match the rule" in m for m in mismatches)


def test_a_monthly_recurrence_on_gst_is_a_mismatch():
    client = FakeClient(
        work_items=[_gst_item("2026-04-30T00:00:00Z")],
        schedules={
            "S1": {
                "WorkScheduleKey": "S1",
                "RecurrenceFrequency": "Month",
                "ScheduleDeadlineDateMethod": "LastDayOfMonth",
            }
        },
        work_types=["GST Return"],
    )
    result = run_audit(client)
    assert any("Recurrence" in o.message for o in result.by_level("mismatch"))


def test_a_schedule_with_no_deadline_date_is_a_mismatch():
    client = FakeClient(
        work_items=[_gst_item("2026-04-30T00:00:00Z")],
        schedules={
            "S1": {
                "WorkScheduleKey": "S1",
                "RecurrenceFrequency": "Quarter",
                "ScheduleDeadlineDateMethod": "NoDeadlineDate",
            }
        },
        work_types=["GST Return"],
    )
    result = run_audit(client)
    assert any("sets no deadline date" in o.message for o in result.by_level("mismatch"))


def test_a_missing_obligation_is_raised_for_carl_to_confirm():
    client = FakeClient(work_items=[], schedules={}, work_types=["Annual accounts"])
    result = run_audit(client)
    subjects = {o.subject for o in result.by_level("check")}
    assert len(subjects) == 3, "all three obligations should be queried"


def test_the_report_prints_the_rule_and_its_source():
    client = FakeClient(work_items=[], schedules={}, work_types=[])
    text = format_audit(run_audit(client))
    assert "last day of the month following" in text
    assert "15th day of the month following" in text
    assert "31 May" in text and "31 July" in text
    assert "https://www.gov.je/" in text
