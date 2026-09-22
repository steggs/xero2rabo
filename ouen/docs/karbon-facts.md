# What the Karbon API actually does, and what it does not

Everything below was read out of Karbon's own OpenAPI 3.1 specification and
reference documentation, published at `github.com/karbonhq/karbon-api-reference`,
rather than recalled. The runner is built against these facts, and several of
them changed the design.

## Authentication

Two headers, issued together from the same API application in Karbon, and not
interchangeable. Swapping them returns a 401.

```
Authorization: Bearer <application token>
AccessKey: <access key from Settings, Connected Apps>
```

The base URL is `https://api.karbonhq.com` and every path in this phase is
under `/v3`.

## There is no deadline approaching webhook

This is the finding that changed the shape of the build. The brief asked for
webhooks to trigger the runner when a work item's status changes or a deadline
approaches. Karbon delivers the first and does not deliver the second.

Karbon's webhook subscription types are Contact, CustomField, Work, Note, User,
IntegrationTask, Invoice and EstimateSummary. Every one of them fires on a
change to an entity. None of them fires because time passed. A deadline
approaching is not an event in Karbon, it is a fact about the clock, so nothing
will ever arrive in the listener to announce it.

The consequence is that deadline pressure has to be discovered by looking, on a
schedule, at dates the runner already holds. That is what `runner/stall.py`
does, and it is why the runner mirrors work items into its own store rather
than treating Karbon as the only place the dates live. A design that waited for
a deadline webhook would have looked correct and would have been silent forever.

## The webhook payload carries a key, not the thing

A single delivery is four fields.

```json
{"ResourcePermaKey": "...", "ResourceType": "...",
 "ActionType": "...", "TimeStamp": "2026-09-02T03:49:42Z"}
```

There is no work item in the payload. Anything that needs the work item has to
fetch it. In this phase the listener deliberately does not fetch: it records
the event and stops, and a separate `sync-work-items` command does the reading.

## Batched deliveries change two field names

A subscription created with a `BatchSize` above one wraps deliveries in an
envelope, and two things are spelled differently inside it. The single delivery
says `TimeStamp`; the batched one says `Timestamp`. A work event is
`ResourceType: "Work"` when delivered singly and `ResourceType: "WorkItem"`
when delivered inside a batch. Both are handled in `runner/events.py`, because
a listener that only knew the singular spelling would record half the traffic
under a type nothing else looks for.

## A failing listener loses the subscription

Karbon retries a delivery ten times with increasing delays. After ten failures
with no 2xx response, the subscription ends and has to be recreated. Nothing
warns you. This is why the listener returns 200 immediately and does nothing
slow on the request path, why a storage failure returns 500 on purpose so the
delivery is retried rather than acknowledged and lost, and why the scheduled
check includes pipe health and not only task health. A runner that has stopped
hearing from Karbon looks exactly like a runner with nothing to do.

## Payload signing is optional and should not be

A subscription can be created with a `SigningKey`, and deliveries then carry a
`Signature` header holding an HMAC of the body. Without it, anything that can
reach the listener URL can write events into the audit trail. The runner
supports it, enforces it when a key is configured, and raises a standing flag
when one is not.

One caveat worth checking against the first live delivery: the reference
example shows a sixty four character lowercase hex digest, which is SHA-256
hex, and `runner/events.py` assumes that. If the first signed delivery fails
verification, that assumption is the first thing to check.

## PrimaryStatus comes back in two different spellings

`GET /v3/WorkItems` returns the spaced form, `In Progress`. `GET
/v3/WorkItems/{key}` and the POST and PUT responses return `InProgress`. An
OData `$filter` matches only the spaced form, and the unspaced form is not
rejected, it silently returns zero rows. The runner normalises everything to
the spaced form on the way in, so the store never holds both.

`PrimaryStatus` is also the field to use. `WorkStatus` still exists and is
legacy.

## Other constraints the runner respects

Rate limiting returns a 429 whose message names the delay in seconds, so the
client parses it rather than guessing a backoff. List endpoints page at a
hundred items and the `@odata.nextLink` must be followed verbatim rather than
incrementing `$skip`. Only one webhook subscription may exist per type, and
only one multi type subscription per application, so subscribing is create or
conflict, not upsert.
