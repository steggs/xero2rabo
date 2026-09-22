"""Command line entry point for the Ouen runner.

    python -m runner.cli <command>

Commands that touch Karbon need KARBON_BEARER_TOKEN and KARBON_ACCESS_KEY in
the environment. Commands that only touch the local store do not, so the
schema, the stall check and the audit chain verification all work offline.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from .config import Config, ConfigError, load_config
from .karbon_audit import format_audit, load_expectations, run_audit
from .karbon_client import KarbonClient, KarbonError
from .policy import PolicyViolation
from .stall import Thresholds, format_report, run_stall_check
from .store import Store, open_store, utc_now_iso

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _store(config: Config, *, create: bool = False) -> Store:
    return open_store(config.db_path, create=create)


def _client(config: Config) -> KarbonClient:
    config.require_credentials()
    return KarbonClient(config)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init_db(args: argparse.Namespace, config: Config) -> int:
    store = _store(config, create=True)
    store.audit(
        actor_type="human",
        actor_id=args.actor,
        subject_type="runner",
        subject_id=None,
        event_type="runner.initialised",
        detail={"db_path": config.db_path},
    )
    print(f"Store initialised at {config.db_path}")
    print("Audit chain started; verify at any time with: audit-verify")
    return 0


def cmd_verify_access(args: argparse.Namespace, config: Config) -> int:
    """Task one. One authenticated call, and an honest report of the result."""
    try:
        client = _client(config)
    except ConfigError as exc:
        print(f"Cannot attempt a call: {exc}", file=sys.stderr)
        return 2

    check = client.verify_access()
    store = _store(config, create=True)
    store.audit(
        actor_type="human",
        actor_id=args.actor,
        subject_type="runner",
        subject_id=None,
        event_type="karbon.access_checked",
        detail={
            "ok": check.ok,
            "status": check.status,
            "tenant_key": check.tenant_key,
            "message": check.message,
        },
    )

    print(check.message)
    print(f"  HTTP status:   {check.status}")
    print(f"  Reachable:     {check.reachable}")
    print(f"  Authenticated: {check.authenticated}")
    if check.ok:
        print(f"  Tenant key:    {check.tenant_key}")
        print(f"  Work types:    {', '.join(check.work_types) or 'none configured'}")
        print(f"  Work statuses: {', '.join(check.work_statuses) or 'none configured'}")
        print(f"  Karbon for Clients activated: {check.client_access_activated}")
        return 0

    print()
    print("What this does and does not tell you:")
    print("  A 401 means the credentials were rejected, not necessarily that the")
    print("  plan excludes API access. Check in Karbon under Settings >")
    print("  Connected Apps that an API application exists and is enabled, and")
    print("  that the Authorization token and the AccessKey are not swapped.")
    print("  If no API application can be created there at all, that is the")
    print("  plan-level answer, and it is the point to stop and tell Carl.")
    return 1


def cmd_audit_karbon(args: argparse.Namespace, config: Config) -> int:
    """Task two. Read only: pull clients, templates, schedules and work items."""
    client = _client(config)
    expectations = load_expectations()
    result = run_audit(client, expectations=expectations, work_item_sample=args.sample)

    store = _store(config, create=True)
    store.audit(
        actor_type="human",
        actor_id=args.actor,
        subject_type="runner",
        subject_id=None,
        event_type="karbon.audited",
        detail={
            "organizations": len(result.inventory.organizations),
            "contacts": len(result.inventory.contacts),
            "client_groups": len(result.inventory.client_groups),
            "templates": len(result.templates),
            "mismatches": len(result.by_level("mismatch")),
            "to_confirm": len(result.by_level("check")),
        },
    )

    if args.json:
        print(json.dumps(
            {
                "tenant_key": result.tenant_key,
                "clients": {
                    "organizations": len(result.inventory.organizations),
                    "contacts": len(result.inventory.contacts),
                    "client_groups": len(result.inventory.client_groups),
                },
                "work_types": result.work_types,
                "observations": [
                    {"level": o.level, "subject": o.subject, "message": o.message}
                    for o in result.observations
                ],
            },
            indent=2,
        ))
    else:
        print(format_audit(result, expectations))

    if args.list_clients:
        print("\nOrganizations")
        for org in sorted(result.inventory.organizations, key=lambda o: o.get("FullName") or ""):
            print(f"  {org.get('FullName')}  [{org.get('OrganizationKey')}]")
        print("\nContacts")
        for contact in sorted(result.inventory.contacts, key=lambda c: c.get("FullName") or ""):
            print(f"  {contact.get('FullName')}  [{contact.get('ContactKey')}]")

    return 1 if result.by_level("mismatch") else 0


def cmd_sync_work_items(args: argparse.Namespace, config: Config) -> int:
    """Mirror Karbon work items into the runner's store. Read only against Karbon."""
    client = _client(config)
    store = _store(config, create=True)

    counts = {"created": 0, "updated": 0, "unchanged": 0}
    for item in client.iter_work_items(args.filter):
        _, action = store.upsert_task_from_karbon(item, actor_type="system", actor_id="karbon-sync")
        counts[action] += 1

    store.set_sync_state("last_work_item_sync_at", utc_now_iso())
    store.audit(
        actor_type="human",
        actor_id=args.actor,
        subject_type="runner",
        subject_id=None,
        event_type="karbon.work_items_synced",
        detail={**counts, "filter": args.filter},
    )
    print(f"created {counts['created']}, updated {counts['updated']}, "
          f"unchanged {counts['unchanged']}")
    return 0


def cmd_subscribe(args: argparse.Namespace, config: Config) -> int:
    """Wire the Karbon webhook pipe. The one write this phase permits."""
    client = _client(config)
    store = _store(config, create=True)

    types = [t.strip() for t in args.types.split(",") if t.strip()]
    if not types:
        print("Pass at least one webhook type with --types", file=sys.stderr)
        return 2

    single = types[0] if len(types) == 1 else None
    try:
        response = client.create_webhook_subscription(
            target_url=args.target_url,
            webhook_type=single,
            webhook_types=None if single else types,
            signing_key=config.webhook_signing_key,
            batch_size=args.batch_size,
        )
    except PolicyViolation as exc:
        store.record_policy_refusal(
            actor_type="human",
            actor_id=args.actor,
            attempted=f"POST /v3/WebhookSubscriptions {types}",
            reason=str(exc),
        )
        print(f"Refused: {exc}", file=sys.stderr)
        return 3
    except KarbonError as exc:
        print(f"Karbon refused the subscription: {exc}", file=sys.stderr)
        if exc.body:
            print(exc.body, file=sys.stderr)
        return 1

    store.record_subscription(
        webhook_type=single or "Multi",
        target_url=args.target_url,
        webhook_types=None if single else types,
        signing_key_set=config.webhook_signing_key is not None,
        batch_size=args.batch_size,
        actor_id=args.actor,
    )
    print(f"Subscribed {single or types} to {args.target_url}")
    if config.webhook_signing_key is None:
        print("Warning: no KARBON_WEBHOOK_SIGNING_KEY set, so deliveries cannot be "
              "verified as genuine. The stall check will keep flagging this.")
    print(json.dumps(response, indent=2) if response else "")
    return 0


def cmd_subscriptions(args: argparse.Namespace, config: Config) -> int:
    store = _store(config, create=True)
    local = store.subscriptions()
    print(f"Recorded locally: {len(local)}")
    for row in local:
        print(f"  {row['webhook_type']} -> {row['target_url']} "
              f"(signed: {bool(row['signing_key_set'])}, created {row['created_at']})")

    if args.live:
        client = _client(config)
        for row in local:
            live = client.get_webhook_subscription(row["webhook_type"])
            state = "present in Karbon" if live else "MISSING in Karbon"
            print(f"  {row['webhook_type']}: {state}")
            if live is None:
                print("    Karbon cancels a subscription after ten failed deliveries. "
                      "Re-run subscribe to reinstate it.")
    return 0


def cmd_unsubscribe(args: argparse.Namespace, config: Config) -> int:
    client = _client(config)
    store = _store(config, create=True)
    try:
        client.delete_webhook_subscription(args.type)
    except PolicyViolation as exc:
        store.record_policy_refusal(
            actor_type="human",
            actor_id=args.actor,
            attempted=f"DELETE /v3/WebhookSubscriptions/{args.type}",
            reason=str(exc),
        )
        print(f"Refused: {exc}", file=sys.stderr)
        return 3
    store.forget_subscription(args.type, actor_id=args.actor)
    print(f"Unsubscribed {args.type}")
    return 0


def cmd_serve(args: argparse.Namespace, config: Config) -> int:
    from .listener import serve

    store = _store(config, create=True)
    store.audit(
        actor_type="human",
        actor_id=args.actor,
        subject_type="runner",
        subject_id=None,
        event_type="listener.started",
        detail={"host": config.listener_host, "port": config.listener_port},
    )
    serve(store, config)
    return 0


def cmd_stall_check(args: argparse.Namespace, config: Config) -> int:
    """Task five. Runs offline against the local store."""
    store = _store(config, create=True)
    report = run_stall_check(
        store,
        timezone_name=config.timezone,
        thresholds=Thresholds(),
        signing_key_set=config.webhook_signing_key is not None,
        silence_hours=config.pipe_silence_hours,
    )
    print(format_report(report))
    if args.fail_on:
        severities = {"breach": 0, "urgent": 1, "concern": 2, "watch": 3}
        threshold = severities[args.fail_on]
        worst = min(
            (severities[f.severity] for f in report.findings), default=99
        )
        if worst <= threshold:
            return 1
    return 0


def cmd_audit_verify(args: argparse.Namespace, config: Config) -> int:
    store = _store(config)
    ok, problem = store.verify_audit_chain()
    count = store.audit_entry_count()
    if ok:
        print(f"Audit chain intact across {count} entries.")
        return 0
    print(f"AUDIT CHAIN BROKEN after {count} entries: {problem}", file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    store = _store(config, create=True)
    ok, problem = store.verify_audit_chain()
    flags = store.open_stall_flags()
    print(f"Database:        {config.db_path}")
    print(f"Timezone:        {config.timezone}")
    print(f"Events recorded: {store.event_count()}")
    print(f"Tasks mirrored:  {store.task_count()}")
    print(f"Audit entries:   {store.audit_entry_count()} "
          f"({'intact' if ok else 'BROKEN: ' + str(problem)})")
    print(f"Open approvals:  {len(store.open_approvals())}")
    print(f"Open stall flags:{len(flags)}")
    print(f"Last webhook:    {store.last_webhook_received_at() or 'never'}")
    print("Outbound channels: none. This phase cannot send or file anything.")
    for flag in flags:
        print(f"  [{flag['severity']}] {flag['reason_code']}: {flag['detail']}")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ouen-runner",
        description="Phase one: an auditable, read-only pipe between Karbon and "
                    "a local runner. Nothing here can send or file anything.",
    )
    parser.add_argument("--actor", default="carl", help="Identity recorded in the audit log")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create the store and start the audit chain").set_defaults(
        func=cmd_init_db
    )

    sub.add_parser(
        "verify-access", help="Make one authenticated call to Karbon and report the result"
    ).set_defaults(func=cmd_verify_access)

    audit = sub.add_parser(
        "audit-karbon", help="Pull clients, templates and schedules and check them against Jersey rules"
    )
    audit.add_argument("--json", action="store_true")
    audit.add_argument("--list-clients", action="store_true")
    audit.add_argument("--sample", type=int, default=500, help="Max work items to sample")
    audit.set_defaults(func=cmd_audit_karbon)

    sync = sub.add_parser("sync-work-items", help="Mirror Karbon work items into the store")
    sync.add_argument("--filter", default=None, help="OData $filter, spaced status form")
    sync.set_defaults(func=cmd_sync_work_items)

    subscribe = sub.add_parser("subscribe", help="Create the Karbon webhook subscription")
    subscribe.add_argument("--target-url", required=True, help="HTTPS URL of this listener")
    subscribe.add_argument("--types", default="Work", help="Comma separated Karbon webhook types")
    subscribe.add_argument("--batch-size", type=int, default=None)
    subscribe.set_defaults(func=cmd_subscribe)

    subs = sub.add_parser("subscriptions", help="Show webhook subscriptions")
    subs.add_argument("--live", action="store_true", help="Also ask Karbon what still exists")
    subs.set_defaults(func=cmd_subscriptions)

    unsub = sub.add_parser("unsubscribe", help="Delete a Karbon webhook subscription")
    unsub.add_argument("--type", required=True)
    unsub.set_defaults(func=cmd_unsubscribe)

    sub.add_parser("serve", help="Run the webhook listener").set_defaults(func=cmd_serve)

    stall = sub.add_parser("stall-check", help="Flag tasks and pipe problems")
    stall.add_argument(
        "--fail-on",
        choices=["breach", "urgent", "concern", "watch"],
        default=None,
        help="Exit non-zero if any finding is at least this severe",
    )
    stall.set_defaults(func=cmd_stall_check)

    sub.add_parser("audit-verify", help="Replay the audit hash chain").set_defaults(
        func=cmd_audit_verify
    )
    sub.add_parser("status", help="Summary of the runner's state").set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format=LOG_FORMAT
    )
    config = load_config()
    try:
        return int(args.func(args, config))
    except PolicyViolation as exc:
        print(f"Refused by policy: {exc}", file=sys.stderr)
        return 3
    except ConfigError as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return 2
    except KarbonError as exc:
        print(f"Karbon error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
