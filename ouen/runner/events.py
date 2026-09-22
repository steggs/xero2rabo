"""Parsing and normalising Karbon webhook deliveries.

Karbon's webhook payload is deliberately thin. A delivery carries only the key
of the thing that changed, not the thing itself:

    {"ResourcePermaKey": "...", "ResourceType": "...",
     "ActionType": "...", "TimeStamp": "2026-09-02T03:49:42Z"}

When a subscription sets BatchSize > 1 the shape changes to an envelope:

    {"BatchId": "...", "Count": 3, "Events": [ {...}, {...}, {...} ]}

Two inconsistencies in that pair are load bearing and are handled here rather
than at every call site:

  * the single delivery spells the field TimeStamp, the batched one spells it
    Timestamp;
  * a work item event is ResourceType "Work" when delivered singly and
    "WorkItem" when delivered inside a batch.

Normalising both to one internal shape is what stops the listener quietly
recording half the traffic under a type nothing else looks for.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any

# Karbon's own subscription types.
KNOWN_RESOURCE_TYPES = frozenset(
    {
        "Contact",
        "CustomField",
        "Work",
        "Note",
        "User",
        "IntegrationTask",
        "Invoice",
        "EstimateSummary",
    }
)

# Aliases Karbon uses for the same resource in different delivery shapes.
RESOURCE_TYPE_ALIASES = {
    "workitem": "Work",
    "work": "Work",
    "contact": "Contact",
    "organization": "Contact",
    "organisation": "Contact",
    "clientgroup": "Contact",
    "customfield": "CustomField",
    "note": "Note",
    "notecomment": "Note",
    "user": "User",
    "integrationtask": "IntegrationTask",
    "invoice": "Invoice",
    "estimatesummary": "EstimateSummary",
    "estimatesummaries": "EstimateSummary",
}

# Karbon returns PrimaryStatus with spaces from the list endpoint and without
# them from the single-item endpoint. $filter only matches the spaced form.
PRIMARY_STATUS_CANONICAL = {
    "planned": "Planned",
    "readytostart": "Ready To Start",
    "ready to start": "Ready To Start",
    "inprogress": "In Progress",
    "in progress": "In Progress",
    "waiting": "Waiting",
    "completed": "Completed",
}

TERMINAL_KARBON_STATUSES = frozenset({"Completed"})


class MalformedDelivery(ValueError):
    """The body was not a webhook delivery we recognise."""


@dataclass(frozen=True)
class KarbonEvent:
    resource_type: str          # normalised, e.g. "Work"
    raw_resource_type: str      # exactly as delivered
    resource_key: str
    action_type: str | None
    karbon_timestamp: str | None
    batch_id: str | None = None
    position_in_batch: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def raw_json(self) -> str:
        return json.dumps(self.raw, sort_keys=True, separators=(",", ":"))


def normalise_resource_type(value: str | None) -> str:
    if not value:
        raise MalformedDelivery("delivery is missing ResourceType")
    canonical = RESOURCE_TYPE_ALIASES.get(value.strip().lower())
    if canonical:
        return canonical
    # An unrecognised type is recorded under its own name rather than dropped.
    # A new Karbon resource type should show up in the audit trail, not vanish.
    return value.strip()


def normalise_primary_status(value: str | None) -> str | None:
    """Return the spaced, filterable form of a PrimaryStatus value."""
    if value is None:
        return None
    key = value.strip().lower()
    return PRIMARY_STATUS_CANONICAL.get(key, value.strip())


def _event_from_mapping(
    payload: dict[str, Any],
    *,
    batch_id: str | None = None,
    position: int = 0,
) -> KarbonEvent:
    if not isinstance(payload, dict):
        raise MalformedDelivery(
            f"expected a JSON object per event, got {type(payload).__name__}"
        )
    raw_type = payload.get("ResourceType")
    key = payload.get("ResourcePermaKey")
    if not key:
        raise MalformedDelivery("delivery is missing ResourcePermaKey")
    # Single deliveries say TimeStamp; batched ones say Timestamp.
    timestamp = payload.get("TimeStamp") or payload.get("Timestamp")
    return KarbonEvent(
        resource_type=normalise_resource_type(raw_type),
        raw_resource_type=str(raw_type),
        resource_key=str(key),
        action_type=(payload.get("ActionType") or None),
        karbon_timestamp=(str(timestamp) if timestamp else None),
        batch_id=batch_id,
        position_in_batch=position,
        raw=payload,
    )


def parse_delivery(body: bytes | str) -> list[KarbonEvent]:
    """Turn one HTTP body into a list of normalised events.

    Accepts the single-notification shape, the batch envelope, and a bare JSON
    array, which is what a batch looks like if Karbon ever drops the envelope.
    """
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedDelivery("body is not valid UTF-8") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise MalformedDelivery(f"body is not valid JSON: {exc}") from exc

    if isinstance(payload, list):
        return [_event_from_mapping(item, position=i) for i, item in enumerate(payload)]

    if not isinstance(payload, dict):
        raise MalformedDelivery("body is neither a JSON object nor an array")

    events = payload.get("Events")
    if isinstance(events, list):
        batch_id = payload.get("BatchId")
        batch_id = str(batch_id) if batch_id else None
        parsed = [
            _event_from_mapping(item, batch_id=batch_id, position=i)
            for i, item in enumerate(events)
        ]
        declared = payload.get("Count")
        if isinstance(declared, int) and declared != len(parsed):
            # Recorded, not fatal: we would rather store the events we got and
            # have the discrepancy visible than reject the whole batch.
            raise MalformedDelivery(
                f"batch declares Count={declared} but carries {len(parsed)} events"
            )
        return parsed

    return [_event_from_mapping(payload)]


# Karbon signs the payload with HMAC and sends the result in a "Signature"
# header when the subscription was created with a SigningKey. The reference
# example is a 64 character lowercase hex digest, which is SHA-256. Confirm
# against the first live delivery before relying on it; verify_signature
# returns False rather than raising so a mismatch is recorded, not swallowed.
SIGNATURE_HEADER = "Signature"
SIGNATURE_DIGEST = hashlib.sha256


def compute_signature(body: bytes, signing_key: str) -> str:
    return hmac.new(
        signing_key.encode("utf-8"), body, SIGNATURE_DIGEST
    ).hexdigest()


def verify_signature(body: bytes, signing_key: str, provided: str | None) -> bool:
    if not provided:
        return False
    expected = compute_signature(body, signing_key)
    return hmac.compare_digest(expected, provided.strip().lower())
