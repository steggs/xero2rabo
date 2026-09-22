"""Configuration, read from the environment.

Credentials are never written to the store, never logged, and never committed.
Loading is deliberately strict: a missing credential raises rather than
defaulting to something that would produce a confusing 401 later.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://api.karbonhq.com"
DEFAULT_TIMEZONE = "Europe/Jersey"
DEFAULT_DB_PATH = "ouen-runner.db"

# The only host this runner is permitted to talk to. Enforced in karbon_client.
ALLOWED_API_HOST = "api.karbonhq.com"


class ConfigError(RuntimeError):
    """Raised when required configuration is absent or malformed."""


@dataclass(frozen=True)
class Config:
    bearer_token: str
    access_key: str
    base_url: str = DEFAULT_BASE_URL
    db_path: str = DEFAULT_DB_PATH
    timezone: str = DEFAULT_TIMEZONE
    webhook_signing_key: str | None = None
    listener_host: str = "127.0.0.1"
    listener_port: int = 8787
    # Subscription management is the single write the runner may perform
    # against Karbon, and it is off unless explicitly enabled.
    allow_subscription_writes: bool = False
    # Maximum accepted webhook body, to bound memory on a hostile POST.
    max_body_bytes: int = 1_048_576
    # Pipe health: hours without any inbound delivery before we call it silent.
    pipe_silence_hours: int = 36

    def require_credentials(self) -> None:
        if not self.bearer_token or not self.access_key:
            raise ConfigError(
                "KARBON_BEARER_TOKEN and KARBON_ACCESS_KEY must both be set. "
                "The two values are issued together from the same Karbon API "
                "application and are not interchangeable."
            )


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def load_config(env: dict[str, str] | None = None) -> Config:
    """Build a Config from the process environment.

    Credentials may be absent here; call require_credentials() at the point
    where an API call is actually about to be made, so that offline commands
    such as init-db and stall-check still run without them.
    """
    env = dict(os.environ if env is None else env)
    base_url = env.get("KARBON_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    db_path = env.get("OUEN_DB_PATH", DEFAULT_DB_PATH)
    return Config(
        bearer_token=env.get("KARBON_BEARER_TOKEN", "").strip(),
        access_key=env.get("KARBON_ACCESS_KEY", "").strip(),
        base_url=base_url,
        db_path=str(Path(db_path).expanduser()),
        timezone=env.get("OUEN_TIMEZONE", DEFAULT_TIMEZONE),
        webhook_signing_key=(env.get("KARBON_WEBHOOK_SIGNING_KEY") or "").strip() or None,
        listener_host=env.get("OUEN_LISTENER_HOST", "127.0.0.1"),
        listener_port=_env_int("OUEN_LISTENER_PORT", 8787),
        allow_subscription_writes=_env_bool("KARBON_ALLOW_SUBSCRIPTION_WRITES", False),
        max_body_bytes=_env_int("OUEN_MAX_BODY_BYTES", 1_048_576),
        pipe_silence_hours=_env_int("OUEN_PIPE_SILENCE_HOURS", 36),
    )
