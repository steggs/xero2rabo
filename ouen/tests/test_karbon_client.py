"""The client is the seam where "read only against Karbon" is enforced.
No network is touched here: the refusals happen before any request is built."""

import json
import urllib.error

import pytest

from runner.config import Config
from runner.karbon_client import (
    KarbonAuthError,
    KarbonClient,
    KarbonForbidden,
    KarbonNotFound,
)
from runner.policy import PolicyViolation


def make_client(**overrides) -> KarbonClient:
    config = Config(
        bearer_token="a-token", access_key="an-access-key", **overrides
    )
    return KarbonClient(config)


def test_credentials_are_required():
    from runner.config import ConfigError

    with pytest.raises(ConfigError):
        KarbonClient(Config(bearer_token="", access_key=""))


def test_both_auth_headers_are_sent():
    headers = make_client()._headers()
    assert headers["Authorization"] == "Bearer a-token"
    assert headers["AccessKey"] == "an-access-key"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_writes_to_work_items_are_refused_before_any_request(method):
    client = make_client(allow_subscription_writes=True)
    with pytest.raises(PolicyViolation):
        client.request(method, "/v3/WorkItems/ABC", body={"Title": "x"})


def test_subscription_writes_are_refused_without_the_flag():
    client = make_client()
    with pytest.raises(PolicyViolation):
        client.create_webhook_subscription(
            target_url="https://example.je/karbon/webhook", webhook_type="Work"
        )


def test_a_next_link_pointing_elsewhere_is_refused(monkeypatch):
    client = make_client()
    pages = [
        {
            "value": [{"WorkItemKey": "A"}],
            "@odata.nextLink": "https://evil.example.com/v3/WorkItems?$skip=100",
        }
    ]

    def fake_request(method, url, params=None, body=None):
        return pages.pop(0)

    monkeypatch.setattr(client, "request", fake_request)
    with pytest.raises(PolicyViolation):
        list(client.paginate("/v3/WorkItems"))


def test_pagination_follows_the_next_link_verbatim(monkeypatch):
    client = make_client()
    seen: list[str] = []
    pages = {
        "/v3/WorkItems": {
            "value": [{"WorkItemKey": "A"}],
            "@odata.nextLink": "https://api.karbonhq.com/v3/WorkItems?$skip=100",
        },
        "https://api.karbonhq.com/v3/WorkItems?$skip=100": {
            "value": [{"WorkItemKey": "B"}]
        },
    }

    def fake_request(method, url, params=None, body=None):
        seen.append(url)
        return pages[url]

    monkeypatch.setattr(client, "request", fake_request)
    items = list(client.paginate("/v3/WorkItems"))
    assert [i["WorkItemKey"] for i in items] == ["A", "B"]
    assert seen[1] == "https://api.karbonhq.com/v3/WorkItems?$skip=100"


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: bytes = b"{}"):
        super().__init__("https://api.karbonhq.com/v3/x", code, "err", {}, None)
        self._body = body

    def read(self):
        return self._body


def _raise(exc):
    def _urlopen(req, timeout=None):
        raise exc

    return _urlopen


@pytest.mark.parametrize(
    "code,expected",
    [(401, KarbonAuthError), (403, KarbonForbidden), (404, KarbonNotFound)],
)
def test_http_errors_map_to_specific_exceptions(monkeypatch, code, expected):
    client = make_client()
    monkeypatch.setattr(
        "urllib.request.urlopen", _raise(FakeHTTPError(code)), raising=True
    )
    with pytest.raises(expected):
        client.get("/v3/TenantSettings")


def test_verify_access_reports_a_401_without_claiming_it_is_a_plan_problem(monkeypatch):
    client = make_client()
    monkeypatch.setattr(
        "urllib.request.urlopen", _raise(FakeHTTPError(401)), raising=True
    )
    check = client.verify_access()
    assert not check.ok
    assert check.reachable is True
    assert check.authenticated is False
    assert check.status == 401
    assert "Connected Apps" in check.message


def test_verify_access_on_success_returns_the_tenant_shape(monkeypatch):
    client = make_client()
    payload = {
        "TenantKey": "TEN1",
        "WorkTypes": [{"Name": "GST Return"}, {"Name": "Personal Tax"}],
        "WorkStatuses": [{"Name": "Planned"}],
        "ClientAccessActivated": False,
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=None: FakeResponse(), raising=True
    )
    check = client.verify_access()
    assert check.ok
    assert check.tenant_key == "TEN1"
    assert check.work_types == ["GST Return", "Personal Tax"]


def test_retry_delay_reads_the_message_karbon_actually_sends():
    exc = FakeHTTPError(429)
    delay = KarbonClient._retry_delay(
        exc, '{"statusCode":"429","message":"Rate limit is exceeded. Try again in 10 seconds."}', 0
    )
    assert delay == 10.0


def test_odata_query_parameters_keep_their_dollar_prefix(monkeypatch):
    """A percent encoded $top or $filter returns an empty page rather than an
    error, so the encoding is pinned here."""
    client = make_client()
    seen: list[str] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"value": []}'

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen, raising=True)
    list(client.iter_work_items("PrimaryStatus eq 'Ready To Start'"))

    url = seen[0]
    assert "$filter=" in url and "$top=" in url
    assert "%24" not in url
    # Spaces must still be escaped, and the OData quotes left intact.
    assert "PrimaryStatus%20eq%20'Ready%20To%20Start'" in url
