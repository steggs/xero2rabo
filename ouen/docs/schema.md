# Task three, the runner schema

Karbon stays the system of record for clients, work items and deadlines. The
runner keeps what Karbon has no opinion about: whether a piece of work is
moving, whether it is sitting in Carl's approval queue, and a permanent record
of everything that happened to it and who or what did it. The full definition
is in `sql/schema.sql` and the only writer is `runner/store.py`.

## What every item carries

The brief asked that every item moving through the runner carry who or what
created it, what state it is in, whether it is waiting on Carl, and a
timestamped log of every state change. Those map onto four things.

Provenance lives in `tasks.created_by_type` and `tasks.created_by_id`. The type
is one of human, system, agent or karbon, constrained by the database rather
than by convention, so there is no such thing as an item of unknown origin.

State lives in `tasks.runner_state`, drawn from a fixed set of nine values and
governed by an explicit transition table in `runner/store.py`. The state is the
runner's own; Karbon's `PrimaryStatus` is mirrored alongside it in
`karbon_primary_status` and the two are never conflated.

Approval status lives in `tasks.awaiting_approval` and in the `approvals`
ledger, which records who asked, when, what for, who decided, when and why.

The log of every change is the `audit_log` table, described below.

## The audit layer

Three mechanisms, in order of how hard they are to defeat.

First, append only enforcement in the database. `audit_log`, `karbon_events`
and `rejected_deliveries` each carry triggers that abort any UPDATE or DELETE.
An ordinary mistake, or a well meaning script, cannot rewrite history.

Second, write coupling. Every task mutation in `runner/store.py` opens one
transaction and writes both the change and its audit entry inside it. There is
no code path that changes a task without leaving a record, because the two
either commit together or neither does.

Third, hash chaining. Each audit entry holds the hash of the previous entry,
and its own hash covers its contents plus that link. Someone with direct file
access can drop a trigger and edit a row, but they cannot then recompute every
following hash, so the edit becomes visible. `python -m runner.cli audit-verify`
replays the chain and names the first entry that does not reconcile, whether
the problem is altered contents, a broken link or a missing sequence number.
There is a test for each of those three cases.

This is what makes the constraint on drafts checkable rather than promised. The
claim "no agent ever approved its own work" is answered by reading the chain,
not by trusting the code that wrote it.

## Why the state machine is the enforcement point

The transition table maps each state to the states reachable from it and, for
each of those, the actor types permitted to make the move. Two entries do the
real work. Both routes out of `awaiting_approval`, to `approved` and to
`rejected`, are restricted to actors of type human. An agent, or the system, or
Karbon itself, attempting either raises and writes a refusal to the audit log.

That single restriction is what will hold the line when department agents
arrive. An agent can carry work all the way to `awaiting_approval` and no
further. There is no state beyond it that an agent can reach on its own, and
there is no state anywhere in the machine that means sent or filed, because
this phase has no such concept and adding one would mean editing this table in
front of a reviewer.

Cancellation is human only for the same reason. An agent that could cancel work
could make an obligation disappear quietly, which is the same failure as
sending something, viewed from the other end.

## The tables

`karbon_events` holds every webhook delivery verbatim, immutably, with the
normalised resource type alongside the raw one Karbon sent, whether a signature
was present and whether it verified, and the source address. It deduplicates on
the combination of resource, action, Karbon timestamp, batch and position,
which is why three of those columns are not nullable: SQLite treats nulls in a
unique index as distinct, so a nullable column there would silently stop
deduplicating Karbon's ten delivery retries.

`rejected_deliveries` holds what did not get in, and why. A delivery that fails
its signature or cannot be parsed is not dropped silently, because a run of
them means either someone is probing the listener or real Karbon events are
being discarded, and both are worth seeing.

`tasks` mirrors the Karbon work item fields the runner reasons about and adds
the runner's own state, provenance and three clocks: `created_at`,
`updated_at`, and `last_progress_at`. The last of those is the one the stall
check reads, and it only moves when something meaningful changes. Renaming a
work item is not progress. A status change, a reassignment or a date change is.

`approvals` is the ledger described above. `stall_flags` holds open and cleared
flags keyed on task and reason, so the same condition raises one flag rather
than one per run. `webhook_subscriptions` mirrors what the runner believes it
subscribed to, which is what lets the health check notice that Karbon has
cancelled a subscription underneath it. `sync_state` holds cursors and the time
of the last delivery.

## Deliberate omissions

There is no table for drafts, generated content or outbound messages, because
nothing in this phase produces any. Adding one is a visible schema change with
a visible review, which is the point.

There is no `task_id` column on `karbon_events`. That table forbids UPDATE, so
a link written after the fact would be impossible, and a column that can never
be filled is an invitation to a future bug. Events join to tasks through
`resource_key` against `tasks.karbon_work_item_key`.
