"""Property-level idempotency backed by the configured deduplication store."""

from __future__ import annotations

import os
from pathlib import Path

from app.logic.dedup_store import (
    DEFAULT_PROPERTY_DB,
    SQLiteDedupStore,
    get_dedup_store,
    property_key_for,
)

DB_PATH = Path(os.environ.get("MOVEOUT_PROPERTY_DEDUP_DB", DEFAULT_PROPERTY_DB))


def _store():
    configured = os.environ.get("MOVEOUT_PROPERTY_DEDUP_DB")
    configured_path = Path(configured) if configured else Path(DEFAULT_PROPERTY_DB)
    if DB_PATH == configured_path:
        return get_dedup_store()
    if configured and configured.startswith(("postgresql://", "postgres://")):
        return get_dedup_store()
    return SQLiteDedupStore(property_db_path=DB_PATH)


def check_property_dedup(
    society: str,
    flat: str,
    days_window: int = 60,
) -> bool:
    """Return whether this property was processed within the configured window."""
    if not society.strip() or not flat.strip():
        return False
    if days_window < 0:
        raise ValueError("days_window must not be negative.")
    return _store().is_property_processed(
        property_key_for(society, flat),
        days_window,
    )


def mark_property_processed(society: str, flat: str) -> None:
    """Persist that a property has been processed."""
    if not society.strip() or not flat.strip():
        return
    _store().record_property(
        property_key_for(society, flat),
        society.strip(),
        flat.strip(),
    )
