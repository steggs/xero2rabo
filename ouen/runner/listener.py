"""The webhook listener.

Its only job in this phase is to prove the pipe: a change in Karbon becomes a
recorded, auditable event in the runner's own store, and nothing else happens.
There is no triage here, no agent dispatch, no write back to Karbon.

It is a plain WSGI application so it runs under wsgiref for a smoke test and
under gunicorn or waitress in front of a TLS terminator in real use, with no
framework dependency either way. Karbon requires an HTTPS target URL, so put
this behind a reverse proxy rather than exposing it directly.

Two response rules matter, because Karbon ends a subscription after ten failed
deliveries:

  * A delivery that is stored returns 200 immediately. Nothing slow happens on
    the request path.
  * A delivery that cannot be stored returns 500 on purpose, so Karbon retries
    rather than us acknowledging an event we lost. If that ever exhausts the
    retries and Karbon cancels the subscription, the pipe health check in
    runner.stall is what notices the silence.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable, Iterable

from .config import Config
from .events import MalformedDelivery, SIGNATURE_HEADER, parse_delivery, verify_signature
from .store import Store

log = logging.getLogger("ouen.listener")

WEBHOOK_PATH = "/karbon/webhook"
HEALTH_PATH = "/healthz"

StartResponse = Callable[[str, list[tuple[str, str]]], Any]


def _json_response(
    start_response: StartResponse, status: str, payload: dict[str, Any]
) -> list[bytes]:
    body = json.dumps(payload).encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ],
    )
    return [body]


def _client_ip(environ: dict[str, Any]) -> str | None:
    forwarded = environ.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        # Left-most entry is the original client as seen by the first proxy.
        return forwarded.split(",")[0].strip()
    return environ.get("REMOTE_ADDR")


class WebhookListener:
    """WSGI application receiving Karbon webhook deliveries."""

    def __init__(self, store: Store, config: Config) -> None:
        self.store = store
        self.config = config

    def __call__(
        self, environ: dict[str, Any], start_response: StartResponse
    ) -> Iterable[bytes]:
        path = environ.get("PATH_INFO", "")
        method = environ.get("REQUEST_METHOD", "GET").upper()

        if path == HEALTH_PATH and method == "GET":
            return self._health(start_response)

        if path != WEBHOOK_PATH:
            return _json_response(start_response, "404 Not Found", {"error": "not found"})

        if method != "POST":
            start_response(
                "405 Method Not Allowed",
                [("Content-Type", "application/json"), ("Allow", "POST")],
            )
            return [b'{"error":"POST only"}']

        return self._receive(environ, start_response)

    # -- handlers -----------------------------------------------------------

    def _health(self, start_response: StartResponse) -> list[bytes]:
        return _json_response(
            start_response,
            "200 OK",
            {
                "status": "ok",
                "events_recorded": self.store.event_count(),
                "tasks": self.store.task_count(),
                "audit_entries": self.store.audit_entry_count(),
                "last_webhook_received_at": self.store.last_webhook_received_at(),
                "signature_required": self.config.webhook_signing_key is not None,
                "outbound_channels": [],
            },
        )

    def _receive(
        self, environ: dict[str, Any], start_response: StartResponse
    ) -> list[bytes]:
        delivery_id = str(uuid.uuid4())
        source_ip = _client_ip(environ)

        raw_length = environ.get("CONTENT_LENGTH")
        try:
            declared = int(raw_length) if raw_length not in (None, "") else None
        except ValueError:
            declared = None

        if declared is None:
            # wsgi.input is a blocking stream with no reliable end of message,
            # so reading past Content-Length hangs until the client closes the
            # connection. Requiring the header is what keeps the read bounded.
            self.store.record_rejected_delivery(
                reason="missing or unparseable Content-Length",
                raw_body=None,
                source_ip=source_ip,
                delivery_id=delivery_id,
            )
            return _json_response(
                start_response,
                "411 Length Required",
                {"error": "Content-Length required"},
            )

        if declared > self.config.max_body_bytes:
            self.store.record_rejected_delivery(
                reason=f"body too large: {declared} bytes",
                raw_body=None,
                source_ip=source_ip,
                delivery_id=delivery_id,
            )
            return _json_response(
                start_response, "413 Payload Too Large", {"error": "body too large"}
            )

        # Read exactly what was declared, in a loop, because a WSGI input
        # stream may return a short read.
        stream = environ["wsgi.input"]
        chunks: list[bytes] = []
        remaining = declared
        while remaining > 0:
            chunk = stream.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)

        if len(body) != declared:
            self.store.record_rejected_delivery(
                reason=f"truncated body: declared {declared}, read {len(body)}",
                raw_body=None,
                source_ip=source_ip,
                delivery_id=delivery_id,
            )
            return _json_response(
                start_response, "400 Bad Request", {"error": "truncated body"}
            )

        provided_signature = environ.get(
            "HTTP_" + SIGNATURE_HEADER.upper().replace("-", "_")
        )
        signature_present = bool(provided_signature)
        signing_key = self.config.webhook_signing_key

        if signing_key:
            if not verify_signature(body, signing_key, provided_signature):
                self.store.record_rejected_delivery(
                    reason=(
                        "signature missing"
                        if not signature_present
                        else "signature did not match"
                    ),
                    raw_body=body.decode("utf-8", "replace")[:4000],
                    source_ip=source_ip,
                    delivery_id=delivery_id,
                )
                return _json_response(
                    start_response, "401 Unauthorized", {"error": "bad signature"}
                )
            signature_verified = True
        else:
            # No signing key configured. The delivery is still recorded, but it
            # is recorded as unverified so nobody later mistakes it for one we
            # proved came from Karbon.
            signature_verified = False

        try:
            events = parse_delivery(body)
        except MalformedDelivery as exc:
            self.store.record_rejected_delivery(
                reason=f"malformed delivery: {exc}",
                raw_body=body.decode("utf-8", "replace")[:4000],
                source_ip=source_ip,
                delivery_id=delivery_id,
            )
            # 400, not 500: retrying an unparseable body would not help.
            return _json_response(
                start_response, "400 Bad Request", {"error": str(exc)}
            )

        recorded = 0
        duplicates = 0
        try:
            for event in events:
                _, duplicate = self.store.record_webhook_event(
                    event,
                    delivery_id=delivery_id,
                    signature_present=signature_present,
                    signature_verified=signature_verified,
                    source_ip=source_ip,
                )
                if duplicate:
                    duplicates += 1
                else:
                    recorded += 1
        except Exception:
            # Fail loudly so Karbon retries. Acknowledging an event we did not
            # store would lose it permanently and silently, which is the one
            # outcome this phase exists to prevent.
            log.exception("failed to record delivery %s", delivery_id)
            return _json_response(
                start_response,
                "500 Internal Server Error",
                {"error": "could not record delivery", "delivery_id": delivery_id},
            )

        return _json_response(
            start_response,
            "200 OK",
            {
                "delivery_id": delivery_id,
                "recorded": recorded,
                "duplicates": duplicates,
            },
        )


def build_app(store: Store, config: Config) -> WebhookListener:
    return WebhookListener(store, config)


def serve(store: Store, config: Config) -> None:
    """Run the listener under wsgiref. Development and smoke testing only.

    For anything Carl relies on, run this WSGI app under waitress or gunicorn
    behind a TLS terminator, because Karbon will only deliver to an HTTPS URL.
    """
    from wsgiref.simple_server import make_server

    app = build_app(store, config)
    with make_server(config.listener_host, config.listener_port, app) as httpd:
        log.info(
            "listening on http://%s:%s%s",
            config.listener_host,
            config.listener_port,
            WEBHOOK_PATH,
        )
        httpd.serve_forever()
