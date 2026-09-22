# Running it

Python 3.11 or later and nothing else. The runner uses only the standard
library, so there is no install step and no dependency to keep current. Pytest
is needed to run the tests and is not needed to run the runner.

## First time

```
cp .env.example .env          # then fill in the two Karbon credentials
set -a; . ./.env; set +a
python -m runner.cli init-db
python -m runner.cli verify-access
```

If `verify-access` fails, read `docs/access-verification.md` before going
further. Nothing downstream is meaningful until it passes.

## Auditing what Karbon already holds

```
python -m runner.cli audit-karbon --list-clients
```

This reads the client list, the work templates, the schedules behind recurring
work and a sample of work items, then compares them with the Jersey rules in
`config/jersey-deadlines.json`. It writes nothing to Karbon. The output has
three sections: what does not match the rule, what needs Carl to confirm, and
what is consistent. Exit status is non zero if anything is in the first
section, so it can be run from cron once it is clean.

Before trusting the first section, read the caveats printed underneath. The
common false positive is a client holding Revenue Jersey approval for a non
standard GST period, which is correct for that client and looks like a
frequency mismatch to the audit.

## Wiring the webhook pipe

Karbon only delivers to an HTTPS URL, so the listener goes behind a TLS
terminator. It is a plain WSGI application, so run it under waitress or
gunicorn in anything other than a smoke test.

```
python -m runner.cli serve                         # wsgiref, for a smoke test
waitress-serve --port 8787 --call runner.wsgi:app  # if you prefer waitress
```

Set a signing key before subscribing. Without one, anything that can reach the
listener URL can write into the audit trail, and the stall check will keep
saying so.

```
export KARBON_WEBHOOK_SIGNING_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
export KARBON_ALLOW_SUBSCRIPTION_WRITES=true
python -m runner.cli subscribe --target-url https://runner.example.je/karbon/webhook --types Work
unset KARBON_ALLOW_SUBSCRIPTION_WRITES
```

Turning the flag off again afterwards is the intended pattern. The runner's
resting posture is strictly read only against Karbon, and subscribing is the
one moment it is not.

Check the pipe is still there, from Karbon's point of view rather than ours:

```
python -m runner.cli subscriptions --live
```

A subscription that shows as missing has been cancelled by Karbon after ten
failed deliveries. Re-run subscribe to reinstate it, and find out why the
deliveries failed before you do.

## Mirroring work items

The listener records events and does nothing else, by design. Work items reach
the runner's store through an explicit read:

```
python -m runner.cli sync-work-items
python -m runner.cli sync-work-items --filter "PrimaryStatus eq 'In Progress'"
```

Note the spaced status form in the filter. The unspaced form is not rejected by
Karbon, it silently returns nothing.

## The stall check

Run it on a schedule. Daily is enough for quarterly and monthly obligations,
and the thresholds are set on the assumption of roughly daily runs.

```
python -m runner.cli stall-check
python -m runner.cli stall-check --fail-on urgent    # non zero exit for cron mail
```

A crontab entry that mails Carl only when something needs him:

```
30 7 * * * cd /srv/ouen && set -a && . ./.env && set +a && \
  python -m runner.cli sync-work-items >/dev/null && \
  python -m runner.cli stall-check --fail-on concern
```

The `sync-work-items` first matters. The stall check reads the runner's store,
not Karbon, so a check run against a stale mirror is a check run against last
week. It will tell you when that has happened, with a `mirror_stale` flag, but
it is better not to need telling.

## Checking the audit trail

```
python -m runner.cli audit-verify
python -m runner.cli status
```

`audit-verify` replays the hash chain and exits non zero if it does not
reconcile. It is worth running from the same cron entry, because a broken chain
is the one problem that gets harder to investigate the longer it sits.

## Backing up

The database file is the audit trail. Back it up, and note that WAL mode means
the `-wal` and `-shm` files beside it matter during a copy. Use
`sqlite3 ouen-runner.db ".backup out.db"` rather than copying the file while
the listener is running.

## Tests

```
python -m pytest tests -q
```

They run offline and touch no network. The ones worth reading before changing
anything are `tests/test_policy.py` and the constraint section of
`tests/test_store.py`, because those encode the rules this phase exists to
enforce.
