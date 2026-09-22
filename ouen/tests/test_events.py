"""Parsing the two shapes Karbon delivers, including the field name and
resource type inconsistencies between them."""

import json

import pytest

from runner.events import (
    MalformedDelivery,
    compute_signature,
    normalise_primary_status,
    normalise_resource_type,
    parse_delivery,
    verify_signature,
)

SINGLE = {
    "ResourcePermaKey": "3GBF6TnYRc7C",
    "ResourceType": "Work",
    "ActionType": "Updated",
    "TimeStamp": "2026-09-02T03:49:42Z",
}

BATCH = {
    "BatchId": "70575b82-9a31-4576-a4e7-61e20c42189d",
    "Count": 2,
    "Events": [
        {
            "ResourcePermaKey": "AAA",
            "ResourceType": "WorkItem",
            "ActionType": "Inserted",
            "Timestamp": "2026-09-02T03:49:42Z",
        },
        {
            "ResourcePermaKey": "BBB",
            "ResourceType": "Contact",
            "ActionType": "Updated",
            "Timestamp": "2026-09-02T03:50:00Z",
        },
    ],
}


def test_single_delivery_parses():
    events = parse_delivery(json.dumps(SINGLE))
    assert len(events) == 1
    assert events[0].resource_type == "Work"
    assert events[0].resource_key == "3GBF6TnYRc7C"
    assert events[0].karbon_timestamp == "2026-09-02T03:49:42Z"
    assert events[0].batch_id is None


def test_batch_delivery_normalises_workitem_to_work():
    events = parse_delivery(json.dumps(BATCH))
    assert [e.resource_type for e in events] == ["Work", "Contact"]
    # The batch envelope spells the field Timestamp, not TimeStamp.
    assert events[0].karbon_timestamp == "2026-09-02T03:49:42Z"
    assert events[0].batch_id == "70575b82-9a31-4576-a4e7-61e20c42189d"
    assert [e.position_in_batch for e in events] == [0, 1]
    # The raw type is kept so the audit trail shows what Karbon actually said.
    assert events[0].raw_resource_type == "WorkItem"


def test_batch_count_mismatch_is_rejected():
    bad = dict(BATCH, Count=5)
    with pytest.raises(MalformedDelivery):
        parse_delivery(json.dumps(bad))


def test_bare_array_is_accepted():
    events = parse_delivery(json.dumps(BATCH["Events"]))
    assert len(events) == 2


@pytest.mark.parametrize("body", [b"not json", b"", json.dumps([1, 2]).encode()])
def test_malformed_bodies_raise(body):
    with pytest.raises(MalformedDelivery):
        parse_delivery(body)


def test_missing_resource_key_raises():
    with pytest.raises(MalformedDelivery):
        parse_delivery(json.dumps({"ResourceType": "Work"}))


def test_unknown_resource_type_is_kept_not_dropped():
    events = parse_delivery(
        json.dumps({"ResourcePermaKey": "X", "ResourceType": "Widget"})
    )
    assert events[0].resource_type == "Widget"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("InProgress", "In Progress"),
        ("In Progress", "In Progress"),
        ("ReadyToStart", "Ready To Start"),
        ("Completed", "Completed"),
        (None, None),
    ],
)
def test_primary_status_normalises_to_the_filterable_form(raw, expected):
    assert normalise_primary_status(raw) == expected


def test_organization_events_are_contact_events():
    assert normalise_resource_type("Organization") == "Contact"
    assert normalise_resource_type("ClientGroup") == "Contact"


def test_signature_round_trip():
    body = b'{"ResourcePermaKey":"X"}'
    key = "a-signing-key-at-least-16"
    assert verify_signature(body, key, compute_signature(body, key))
    assert not verify_signature(body, key, "deadbeef")
    assert not verify_signature(body, key, None)
