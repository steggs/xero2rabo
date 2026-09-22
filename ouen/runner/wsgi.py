"""WSGI entry point for running the listener under waitress or gunicorn.

    waitress-serve --port 8787 --call runner.wsgi:app
    gunicorn "runner.wsgi:app()"

Configuration comes from the environment exactly as it does for the CLI.
"""

from __future__ import annotations

from .config import load_config
from .listener import build_app
from .store import open_store


def app():
    config = load_config()
    store = open_store(config.db_path, create=True)
    return build_app(store, config)
