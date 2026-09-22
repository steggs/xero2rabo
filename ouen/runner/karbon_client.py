"""A deliberately read-only client for the Karbon REST API, version 3.

Authentication takes two headers, issued together from the same Karbon API
application and not interchangeable:

    Authorization: Bearer <application token>
    AccessKey:     <access key from Settings > Connected Apps>

Every request passes through runner.policy first. A GET goes through. Anything
else is refused unless it is webhook subscription management with the
subscription write flag deliberately turned on. That is the mechanism behind
"this phase cannot change anything in Karbon and cannot send anything to
anyone": there is no method on this class that writes a work item, a contact,
a note or an invoice, and if one were added it would be refused at the seam.

Only stdlib is used, so the runner installs and runs with nothing but Python.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

from .config import Config
from .policy import PolicyViolation, assert_host_allowed, assert_request_allowed

USER_AGENT = "ouen-runner/0.1 (+phase-one read-only)"
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 4
RETRY_AFTER_PATTERN = re.compile(r"try again in (\d+)\s*second", re.IGNORECASE)


# OData syntax characters that must survive into the query string. Percent
# encoding the leading $ of $top or $filter is the kind of thing that returns
# an empty page rather than an error, so these are left alone deliberately.
ODATA_SAFE = "$,'()/"


def _odata_quote(value: object, safe: str, encoding: str | None, errors: str | None) -> str:
    return urllib.parse.quote(
        str(value), safe=ODATA_SAFE, encoding=encoding or "utf-8", errors=errors or "strict"
    )


class KarbonError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class KarbonAuthError(KarbonError):
    """401. Either the credentials are wrong or the application is not active."""


class KarbonForbidden(KarbonError):
    """403. Authenticated, but this application lacks permission for the call."""


class KarbonNotFound(KarbonError):
    """404."""


class KarbonRateLimited(KarbonError):
    """429, after retries were exhausted."""


@dataclass
class AccessCheck:
    """The result of the first thing this project does: can we call Karbon at all."""

    reachable: bool
    authenticated: bool
    status: int | None
    tenant_key: str | None
    work_types: list[str]
    work_statuses: list[str]
    client_access_activated: bool | None
    message: str
    raw: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and self.authenticated


class KarbonClient:
    def __init__(self, config: Config, *, timeout: int = DEFAULT_TIMEOUT) -> None:
        config.require_credentials()
        self.config = config
        self.timeout = timeout

    # -- transport ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.bearer_token}",
            "AccessKey": self.config.access_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """Issue one request, with the policy check first and 429 retries."""
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
            path = urllib.parse.urlsplit(url).path
        else:
            path = path_or_url if path_or_url.startswith("/") else "/" + path_or_url
            url = self.config.base_url + path

        assert_host_allowed(url)
        assert_request_allowed(
            method,
            path,
            allow_subscription_writes=self.config.allow_subscription_writes,
        )

        if params:
            query = urllib.parse.urlencode(params, quote_via=_odata_quote)
            url = f"{url}{'&' if '?' in url else '?'}{query}"

        payload = None
        headers = self._headers()
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            req = urllib.request.Request(url, data=payload, headers=headers, method=method.upper())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if not raw:
                        return None
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                if exc.code == 401:
                    raise KarbonAuthError(
                        "401 Unauthorized from Karbon.", status=401, body=text
                    ) from exc
                if exc.code == 403:
                    raise KarbonForbidden(
                        "403 Forbidden from Karbon. The credentials are valid but this "
                        "API application is not permitted to make this call.",
                        status=403,
                        body=text,
                    ) from exc
                if exc.code == 404:
                    raise KarbonNotFound(f"404 Not Found: {path}", status=404, body=text) from exc
                if exc.code == 429:
                    delay = self._retry_delay(exc, text, attempt)
                    last_error = exc
                    if attempt == MAX_RETRIES - 1:
                        raise KarbonRateLimited(
                            "Rate limited by Karbon after retries.", status=429, body=text
                        ) from exc
                    time.sleep(delay)
                    continue
                if 500 <= exc.code < 600 and attempt < MAX_RETRIES - 1:
                    last_error = exc
                    time.sleep(2 ** attempt)
                    continue
                raise KarbonError(
                    f"HTTP {exc.code} from Karbon on {method} {path}",
                    status=exc.code,
                    body=text,
                ) from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt == MAX_RETRIES - 1:
                    raise KarbonError(f"Could not reach Karbon: {exc.reason}") from exc
                time.sleep(2 ** attempt)

        raise KarbonError(f"Request failed after {MAX_RETRIES} attempts: {last_error}")

    @staticmethod
    def _retry_delay(exc: urllib.error.HTTPError, text: str, attempt: int) -> float:
        header = exc.headers.get("Retry-After") if exc.headers else None
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        match = RETRY_AFTER_PATTERN.search(text or "")
        if match:
            return float(match.group(1))
        return float(2 ** attempt)

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def paginate(self, path: str, **params: Any) -> Iterator[dict[str, Any]]:
        """Walk an OData collection using @odata.nextLink.

        The next link is used verbatim, as Karbon's documentation requires, and
        is host checked again before each hop.
        """
        params.setdefault("$top", 100)
        page = self.request("GET", path, params=params)
        while page:
            for item in page.get("value", []):
                yield item
            next_link = page.get("@odata.nextLink")
            if not next_link:
                return
            assert_host_allowed(next_link)
            page = self.request("GET", next_link)

    # -- task one: can we call the API at all -------------------------------

    def verify_access(self) -> AccessCheck:
        """Make one authenticated call and report precisely what came back.

        GET /v3/TenantSettings is the right probe: it is cheap, read only, and
        a success also hands back the tenant's work types and statuses, which
        the mapping step in task two needs anyway.
        """
        try:
            data = self.get("/v3/TenantSettings")
        except KarbonAuthError as exc:
            return AccessCheck(
                reachable=True,
                authenticated=False,
                status=401,
                tenant_key=None,
                work_types=[],
                work_statuses=[],
                client_access_activated=None,
                message=(
                    "Karbon answered, but rejected the credentials (401). Either the "
                    "Authorization token and AccessKey are swapped, one of them has been "
                    "regenerated, or the API application is not enabled on this "
                    "workspace. Check Settings > Connected Apps in Karbon."
                ),
                raw={"body": exc.body},
            )
        except KarbonForbidden as exc:
            return AccessCheck(
                reachable=True,
                authenticated=True,
                status=403,
                tenant_key=None,
                work_types=[],
                work_statuses=[],
                client_access_activated=None,
                message=(
                    "Karbon accepted the credentials but refused the call (403). The "
                    "API application exists and is authenticated, but is not permitted "
                    "to read tenant settings. This is a scope problem, not a plan "
                    "problem: widen the application's permissions in Karbon."
                ),
                raw={"body": exc.body},
            )
        except KarbonError as exc:
            return AccessCheck(
                reachable=False,
                authenticated=False,
                status=exc.status,
                tenant_key=None,
                work_types=[],
                work_statuses=[],
                client_access_activated=None,
                message=f"Could not complete a call to Karbon: {exc}",
                raw={"body": exc.body},
            )

        work_types = [w.get("Name") for w in (data.get("WorkTypes") or []) if w.get("Name")]
        statuses = [s.get("Name") for s in (data.get("WorkStatuses") or []) if s.get("Name")]
        return AccessCheck(
            reachable=True,
            authenticated=True,
            status=200,
            tenant_key=data.get("TenantKey"),
            work_types=work_types,
            work_statuses=statuses,
            client_access_activated=data.get("ClientAccessActivated"),
            message="Authenticated call to Karbon v3 succeeded.",
            raw=data,
        )

    # -- task two: read what Karbon already holds ---------------------------

    def iter_organizations(self) -> Iterator[dict[str, Any]]:
        yield from self.paginate("/v3/Organizations")

    def iter_contacts(self) -> Iterator[dict[str, Any]]:
        yield from self.paginate("/v3/Contacts")

    def iter_client_groups(self) -> Iterator[dict[str, Any]]:
        yield from self.paginate("/v3/ClientGroups")

    def iter_work_templates(self) -> Iterator[dict[str, Any]]:
        yield from self.paginate("/v3/WorkTemplates")

    def get_work_template(self, key: str) -> dict[str, Any]:
        return self.get(f"/v3/WorkTemplates/{urllib.parse.quote(key)}")

    def get_work_schedule(self, key: str) -> dict[str, Any]:
        return self.get(f"/v3/WorkSchedules/{urllib.parse.quote(key)}")

    def iter_work_items(self, odata_filter: str | None = None) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {}
        if odata_filter:
            # $filter matches PrimaryStatus only in its spaced form; see
            # events.normalise_primary_status.
            params["$filter"] = odata_filter
        yield from self.paginate("/v3/WorkItems", **params)

    def get_work_item(self, key: str) -> dict[str, Any]:
        return self.get(f"/v3/WorkItems/{urllib.parse.quote(key)}")

    def iter_users(self) -> Iterator[dict[str, Any]]:
        yield from self.paginate("/v3/Users")

    # -- task four: the webhook pipe ----------------------------------------

    def get_webhook_subscription(self, webhook_type: str) -> dict[str, Any] | None:
        try:
            return self.get(f"/v3/WebhookSubscriptions/{webhook_type}")
        except KarbonNotFound:
            return None

    def create_webhook_subscription(
        self,
        *,
        target_url: str,
        webhook_type: str | None = None,
        webhook_types: list[str] | None = None,
        signing_key: str | None = None,
        batch_size: int | None = None,
        batch_max_delay_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Create a subscription. Refused unless subscription writes are enabled.

        Karbon allows exactly one subscription per type, and one multi-type
        subscription per application, so this is create-or-conflict, not upsert.
        """
        if bool(webhook_type) == bool(webhook_types):
            raise ValueError("pass exactly one of webhook_type or webhook_types")
        body: dict[str, Any] = {"TargetUrl": target_url}
        if webhook_type:
            body["WebhookType"] = webhook_type
        else:
            body["WebhookTypes"] = webhook_types
        if signing_key:
            body["SigningKey"] = signing_key
        if batch_size is not None:
            body["BatchSize"] = batch_size
        if batch_max_delay_seconds is not None:
            body["BatchMaxDelaySeconds"] = batch_max_delay_seconds
        return self.request("POST", "/v3/WebhookSubscriptions", body=body)

    def delete_webhook_subscription(self, webhook_type: str) -> None:
        self.request("DELETE", f"/v3/WebhookSubscriptions/{webhook_type}")


__all__ = [
    "AccessCheck",
    "KarbonAuthError",
    "KarbonClient",
    "KarbonError",
    "KarbonForbidden",
    "KarbonNotFound",
    "KarbonRateLimited",
    "PolicyViolation",
]
