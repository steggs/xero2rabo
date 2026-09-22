"""The constraint layer. These tests exist so that a future change which
quietly widens what the runner may do fails here first."""

import pytest

from runner.policy import (
    OUTBOUND_CHANNELS,
    PolicyViolation,
    assert_approval_actor,
    assert_host_allowed,
    assert_outbound_allowed,
    assert_request_allowed,
)


def test_there_are_no_outbound_channels():
    assert OUTBOUND_CHANNELS == frozenset()


@pytest.mark.parametrize("channel", ["email", "smtp", "client-portal", "revenue-jersey"])
def test_every_outbound_attempt_is_refused(channel):
    with pytest.raises(PolicyViolation):
        assert_outbound_allowed(channel)


@pytest.mark.parametrize(
    "path",
    ["/v3/WorkItems/ABC", "/v3/Notes", "/v3/Contacts", "/v3/Invoices", "/v3/Files"],
)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_no_write_to_any_karbon_resource(method, path):
    with pytest.raises(PolicyViolation):
        assert_request_allowed(method, path, allow_subscription_writes=True)


@pytest.mark.parametrize(
    "path", ["/v3/WorkItems", "/v3/Contacts", "/v3/TenantSettings", "/v3/WorkTemplates"]
)
def test_reads_are_always_allowed(path):
    assert_request_allowed("GET", path, allow_subscription_writes=False)


def test_subscription_writes_need_the_flag():
    with pytest.raises(PolicyViolation):
        assert_request_allowed(
            "POST", "/v3/WebhookSubscriptions", allow_subscription_writes=False
        )
    assert_request_allowed(
        "POST", "/v3/WebhookSubscriptions", allow_subscription_writes=True
    )
    assert_request_allowed(
        "DELETE", "/v3/WebhookSubscriptions/Work", allow_subscription_writes=True
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/v3/WorkItems",
        "https://api.karbonhq.com.evil.example.com/v3/WorkItems",
        "https://localhost:8080/",
        "https://169.254.169.254/latest/meta-data/",
    ],
)
def test_only_the_karbon_host_is_reachable(url):
    with pytest.raises(PolicyViolation):
        assert_host_allowed(url)


def test_the_karbon_host_is_reachable():
    assert_host_allowed("https://api.karbonhq.com/v3/WorkItems?$top=100")


@pytest.mark.parametrize("actor", ["agent", "system", "karbon"])
def test_only_a_human_approves(actor):
    with pytest.raises(PolicyViolation):
        assert_approval_actor(actor)
    assert_approval_actor("human") is None
