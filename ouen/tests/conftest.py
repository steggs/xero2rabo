import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner.config import Config          # noqa: E402
from runner.store import Store, connect, init_db  # noqa: E402


@pytest.fixture()
def store(tmp_path) -> Store:
    conn = connect(str(tmp_path / "runner.db"))
    init_db(conn)
    return Store(conn)


@pytest.fixture()
def config(tmp_path) -> Config:
    return Config(
        bearer_token="token",
        access_key="key",
        db_path=str(tmp_path / "runner.db"),
        webhook_signing_key=None,
    )
