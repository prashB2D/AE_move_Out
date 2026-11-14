"""Isolate tests from the developer's .env.

app/main.py calls load_dotenv() at import time, so local settings such as
MOVEOUT_DISABLE_TICKETS, MOVEOUT_TEST_EMAIL or a Postgres MOVEOUT_DEDUP_DB
would otherwise leak into every test. Tests that need a setting set it
themselves with monkeypatch.
"""

import os

import pytest

_PREFIXES = ("MOVEOUT_", "THM_")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    try:
        from app.logic import dedup_store
        cache_clear = getattr(dedup_store._cached_store, "cache_clear", None)
        if cache_clear:
            cache_clear()
    except Exception:
        pass
    yield
