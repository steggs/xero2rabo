"""End to end through the WSGI app: a Karbon delivery becomes an audited row."""

import io
import json

from runner.config import Config
from runner.events import compute_signature
from runner.listener import build_app

SINGLE = {
    "ResourcePermaKey": "WI1",
    "ResourceType": "Work",
    "ActionType": "Updated",
    "TimeStamp": "2026-09-02T03:49:42Z",
}


class Capture:
    def __init__(self):
        self.status = None
        self.headers = None

    def __call__(self, status, headers):
        self.status = status
        self.headers = headers


def post(app, body: bytes, *, signature: str | None = None, path="/karbon/webhook"):
    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
        "REMOTE_ADDR": "203.0.113.10",
    }
    if signature is not None:
        environ["HTTP_SIGNATURE"] = signature
    capture = Capture()
    result = b"".join(app(environ, capture))
    return capture.status, json.loads(result or b"{}")


def get(app, path):
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": path, "wsgi.input": io.BytesIO(b"")}
    capture = Capture()
    result = b"".join(app(environ, capture))
    return capture.status, json.loads(result or b"{}")


def test_delivery_is_recorded_and_acknowledged(store, config):
    app = build_app(store, config)
    status, payload = post(app, json.dumps(SINGLE).encode())
    assert status.startswith("200")
    assert payload["recorded"] == 1
    assert store.event_count() == 1
    ok, problem = store.verify_audit_chain()
    assert ok, problem


def test_the_listener_does_nothing_but_record(store, config):
    """No task is created, no agent is dispatched. That is the whole point."""
    app = build_app(store, config)
    post(app, json.dumps(SINGLE).encode())
    assert store.task_count() == 0
    assert store.open_approvals() == []


def test_retry_of_the_same_delivery_is_deduplicated(store, config):
    app = build_app(store, config)
    post(app, json.dumps(SINGLE).encode())
    status, payload = post(app, json.dumps(SINGLE).encode())
    assert status.startswith("200")
    assert payload["duplicates"] == 1
    assert store.event_count() == 1


def test_batch_delivery_records_every_event(store, config):
    app = build_app(store, config)
    batch = {
        "BatchId": "b1",
        "Count": 2,
        "Events": [
            {"ResourcePermaKey": "A", "ResourceType": "WorkItem",
             "ActionType": "Inserted", "Timestamp": "2026-09-02T03:49:42Z"},
            {"ResourcePermaKey": "B", "ResourceType": "Contact",
             "ActionType": "Updated", "Timestamp": "2026-09-02T03:50:00Z"},
        ],
    }
    status, payload = post(app, json.dumps(batch).encode())
    assert status.startswith("200")
    assert payload["recorded"] == 2
    types = [r["resource_type"] for r in store.conn.execute(
        "SELECT resource_type FROM karbon_events ORDER BY id")]
    assert types == ["Work", "Contact"]


def test_malformed_body_is_rejected_with_400_and_kept(store, config):
    app = build_app(store, config)
    status, _ = post(app, b"{not json")
    assert status.startswith("400")
    assert store.event_count() == 0
    row = store.conn.execute("SELECT * FROM rejected_deliveries").fetchone()
    assert "malformed" in row["reason"]


def test_signature_is_enforced_when_a_key_is_configured(store, tmp_path):
    config = Config(
        bearer_token="t",
        access_key="k",
        db_path=str(tmp_path / "x.db"),
        webhook_signing_key="a-signing-key-at-least-16",
    )
    app = build_app(store, config)
    body = json.dumps(SINGLE).encode()

    status, _ = post(app, body)
    assert status.startswith("401")
    assert store.event_count() == 0

    status, _ = post(app, body, signature="00" * 32)
    assert status.startswith("401")

    good = compute_signature(body, config.webhook_signing_key)
    status, payload = post(app, body, signature=good)
    assert status.startswith("200")
    assert payload["recorded"] == 1
    row = store.conn.execute("SELECT * FROM karbon_events").fetchone()
    assert row["signature_verified"] == 1


def test_unsigned_traffic_is_recorded_as_unverified(store, config):
    app = build_app(store, config)
    post(app, json.dumps(SINGLE).encode())
    row = store.conn.execute("SELECT * FROM karbon_events").fetchone()
    assert row["signature_verified"] == 0


def test_storage_failure_returns_500_so_karbon_retries(store, config, monkeypatch):
    app = build_app(store, config)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "record_webhook_event", boom)
    status, payload = post(app, json.dumps(SINGLE).encode())
    assert status.startswith("500")
    assert "delivery_id" in payload


def test_oversized_body_is_refused(store, tmp_path):
    config = Config(
        bearer_token="t", access_key="k",
        db_path=str(tmp_path / "x.db"), max_body_bytes=32,
    )
    app = build_app(store, config)
    status, _ = post(app, b"x" * 200)
    assert status.startswith("413")


def test_health_endpoint_reports_no_outbound_channels(store, config):
    app = build_app(store, config)
    status, payload = get(app, "/healthz")
    assert status.startswith("200")
    assert payload["outbound_channels"] == []
    assert payload["status"] == "ok"


def test_wrong_method_and_path(store, config):
    app = build_app(store, config)
    status, _ = get(app, "/karbon/webhook")
    assert status.startswith("405")
    status, _ = get(app, "/nope")
    assert status.startswith("404")


def test_missing_content_length_is_refused_rather_than_read(store, config):
    """A WSGI input stream has no reliable end of message, so a delivery
    without Content-Length would block the listener rather than fail."""
    app = build_app(store, config)
    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/karbon/webhook",
        "wsgi.input": io.BytesIO(json.dumps(SINGLE).encode()),
        "REMOTE_ADDR": "203.0.113.10",
    }
    capture = Capture()
    b"".join(app(environ, capture))
    assert capture.status.startswith("411")


def test_short_read_is_reported_not_silently_accepted(store, config):
    app = build_app(store, config)
    body = json.dumps(SINGLE).encode()
    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/karbon/webhook",
        "CONTENT_LENGTH": str(len(body) + 50),
        "wsgi.input": io.BytesIO(body),
        "REMOTE_ADDR": "203.0.113.10",
    }
    capture = Capture()
    b"".join(app(environ, capture))
    assert capture.status.startswith("400")
    assert store.event_count() == 0
