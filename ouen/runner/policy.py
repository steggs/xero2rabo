"""The constraint layer.

Phase one of the Ouen runner produces drafts only and sends nothing. That is
not a note in a design document, it is these three mechanisms:

  1. NO_OUTBOUND_CHANNELS. There is no code path from the runner to email, to a
     client portal, or to a filing authority. assert_outbound_allowed() raises
     unconditionally, so any future caller that tries to add one fails loudly
     at the seam rather than quietly succeeding.

  2. Karbon request allowlist. The API client refuses any request that is not
     a GET, except the small set of webhook subscription calls needed to wire
     the pipe, and those need an explicit configuration flag as well. Nothing
     in this phase can alter a work item, a contact, an invoice or a note in
     Karbon.

  3. Approval gating on state. Only an actor of type "human" may move a task
     out of awaiting_approval. An agent or the system attempting it raises.

Every refusal is written to the audit log by the caller, so an attempt to
cross the line leaves a permanent, hash chained trace.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .config import ALLOWED_API_HOST

ACTOR_TYPES = frozenset({"human", "system", "agent", "karbon"})

# Deliberately empty. Phase one has no outbound capability of any kind.
OUTBOUND_CHANNELS: frozenset[str] = frozenset()

# Karbon paths the runner may call with a method other than GET. Scoped to
# wiring and unwiring the webhook pipe, nothing else.
SUBSCRIPTION_PATH_PATTERN = re.compile(r"^/v3/WebhookSubscriptions(/[A-Za-z]+)?$")

READ_ONLY_METHODS = frozenset({"GET", "HEAD"})


class PolicyViolation(RuntimeError):
    """Raised when something attempts an action this phase forbids."""


def assert_outbound_allowed(channel: str) -> None:
    """Always raises. Phase one cannot send anything to anyone.

    Kept as a real function so that the day an outbound channel is added, it
    is added here, in one reviewable place, with the audit trail already
    wrapped around it.
    """
    raise PolicyViolation(
        f"Outbound channel {channel!r} is not permitted. The Ouen runner "
        "produces drafts only; sending to clients or filing with an authority "
        "requires Carl to act outside this system."
    )


def assert_host_allowed(url: str) -> None:
    """Refuse any request to a host other than the Karbon API."""
    host = (urlsplit(url).hostname or "").lower()
    if host != ALLOWED_API_HOST:
        raise PolicyViolation(
            f"Request to {host or '(no host)'} refused. The runner may only "
            f"reach {ALLOWED_API_HOST}."
        )


def assert_request_allowed(
    method: str,
    path: str,
    *,
    allow_subscription_writes: bool,
) -> None:
    """Refuse any Karbon call outside the read-only surface.

    The one exception is webhook subscription management, and it needs
    KARBON_ALLOW_SUBSCRIPTION_WRITES=true as well, so the default posture of a
    freshly deployed runner is strictly read only.
    """
    method = method.upper()
    if method in READ_ONLY_METHODS:
        return

    if not SUBSCRIPTION_PATH_PATTERN.match(path):
        raise PolicyViolation(
            f"{method} {path} refused. Phase one of the runner is read only "
            "against Karbon; the only writable surface is "
            "/v3/WebhookSubscriptions."
        )

    if not allow_subscription_writes:
        raise PolicyViolation(
            f"{method} {path} refused. Webhook subscription changes require "
            "KARBON_ALLOW_SUBSCRIPTION_WRITES=true, set deliberately for the "
            "one command that wires or unwires the pipe."
        )


def assert_approval_actor(actor_type: str) -> None:
    """Only a human may decide an approval."""
    if actor_type != "human":
        raise PolicyViolation(
            f"Actor type {actor_type!r} may not approve or reject. Approval is "
            "Carl's alone."
        )


def assert_known_actor(actor_type: str) -> None:
    if actor_type not in ACTOR_TYPES:
        raise PolicyViolation(
            f"Unknown actor type {actor_type!r}; expected one of "
            f"{sorted(ACTOR_TYPES)}."
        )
