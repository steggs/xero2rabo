# Ouen Accounting runner, phase one

An auditable pipe between Karbon and a local runner, and the stall check that
sits on top of it. There are no department agents here, nothing that drafts
client communication, no connection to email or document storage, and no
capability to send or file anything even in principle. That is the whole scope,
and the constraint is enforced in code rather than described in a document.

Karbon stays the practice management spine. It holds the client list and the
Jersey deadline templates, and this runner does not duplicate either. What the
runner adds is a record: what moved, when, at whose instruction, and what is
still waiting on Carl.

## Where the constraint lives

Three mechanisms, in `runner/policy.py` and `runner/store.py`, with tests in
`tests/test_policy.py` and `tests/test_store.py`.

There are no outbound channels. `OUTBOUND_CHANNELS` is an empty frozenset and
`assert_outbound_allowed` raises unconditionally. There is no code path from
this runner to email, to a client portal or to an authority, and the day
someone adds one they add it in that one function, in front of a reviewer.

The Karbon client is read only. Every request passes a policy check before it
is built. A GET goes through; anything else is refused unless it is webhook
subscription management, and that additionally needs
`KARBON_ALLOW_SUBSCRIPTION_WRITES` turned on for the one command that wires the
pipe. Requests to any host other than `api.karbonhq.com` are refused, including
on the pagination hop.

Approval is human only. The task state machine permits exactly two moves out of
`awaiting_approval`, to approved and to rejected, and both are restricted to
actors of type human. An agent that reaches `awaiting_approval` has nowhere
further to go. Every refusal is written to the audit log, so an attempt to
cross the line leaves a permanent trace.

## Where the record lives

An append only, hash chained audit log in SQLite. Triggers abort any UPDATE or
DELETE on the log and on the raw event table. Every task mutation writes its
audit entry in the same transaction as the change. Each entry hashes its own
contents together with the previous entry's hash, so an edit made by dropping a
trigger and writing directly to the file breaks every hash that follows it.

```
python -m runner.cli audit-verify
```

replays the chain and names the first entry that does not reconcile. That is
what turns "the runner only ever produced drafts" from a claim into something
Carl can check.

## The five tasks

Task one, verify access, is not complete and could not be completed in this
session. No Karbon credentials were provided and the network this ran on cannot
reach `api.karbonhq.com`. The verification itself is written and tested and runs
as `python -m runner.cli verify-access`. See `docs/access-verification.md` for
what Carl needs to do and, more usefully, for how to read the result, because a
401 does not mean what it looks like it means.

Task two, map what exists in Karbon, is blocked on the same credentials, but
the audit is built and the Jersey rules it checks against are written down with
their sources in `config/jersey-deadlines.json`. Run it with
`python -m runner.cli audit-karbon`. It checks mechanically what can be checked
mechanically, that GST deadlines fall on the last day of a month, that combined
employer return deadlines fall on the fifteenth, that personal tax deadlines
fall on 31 May or 31 July, and that recurrence frequencies match. Everything it
cannot judge without knowing a client's circumstances it prints for Carl to
confirm rather than guessing at.

If the machine holding the credentials is not the machine doing the analysis,
`scripts/karbon-dump.sh` makes the same read only calls and saves the results
as JSON, and `python -m runner.cli audit-dump --dir ./karbon-dump` runs the
audit over those files with no network at all. The script checks access first,
so it also answers task one.

Task three, the runner schema, is complete. See `docs/schema.md` and
`sql/schema.sql`.

Task four, the webhook listener, is complete. It receives Karbon deliveries,
verifies the signature when one is configured, normalises both the single and
batched payload shapes, and writes them into the store. It then stops. No
triage, no dispatch, no write back. `tests/test_listener.py` includes a test
asserting that a delivery creates no task, because doing nothing else is the
specification.

Task five, stall detection, is complete. See below.

## Stall detection

Conservative in a specific direction: flag early rather than late, and treat
missing information as a reason to look rather than a reason to stay quiet.

Days are counted as calendar days in Jersey local time, not working days, so a
deadline three days out over a weekend counts as three days. A task with no
deadline recorded is not exempt; if nothing has moved on it for a month it is
flagged, because an absent deadline is usually a data gap. The escalation
ladder runs from a breached deadline, through unstarted work inside seven days
and silence inside three days of the deadline, down to a week of silence inside
the fourteen day horizon.

An approval left sitting in Carl's queue is itself treated as a stall, because
it is.

The check also looks at the pipe, not just the work. Karbon cancels a webhook
subscription after ten failed deliveries and does not tell you, so a runner
that has stopped hearing about work looks identical to a runner with nothing to
do. The check flags an unwired pipe, a pipe that never delivered, a pipe silent
for longer than the configured window, deliveries being rejected, and a
subscription with no signing key.

## The thing that changed the design

Karbon has no deadline approaching webhook. Its subscription types all fire on
a change to an entity, and none fires because time passed. A design that waited
for a deadline webhook would have looked correct and been silent forever. That
is why the runner mirrors work items into its own store and why stall detection
is a scheduled read rather than an event handler. The rest of what was verified
against Karbon's specification, including two field name inconsistencies
between single and batched deliveries that would otherwise have lost half the
traffic, is in `docs/karbon-facts.md`.

## Getting started

`docs/runbook.md`. Python 3.11 and the standard library, nothing to install.

```
python -m pytest tests -q
```
