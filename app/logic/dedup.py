"""Persistent message-ID deduplication and inbound email age filtering."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.logic.dedup_store import (
    SQLiteDedupStore,
    get_dedup_store,
)

AGE_LIMIT = timedelta(hours=24)
LOCAL_TIMEZONE = ZoneInfo("Asia/Kolkata")
RECEIVED_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%a %d %b %Y %H:%M",
    "%a %d %b %Y %H:%M:%S",
    "%d %b %Y %H:%M",
    "%d %b %Y %H:%M:%S",
)


def _parse_received_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None

    if parsed is None:
        for date_format in RECEIVED_DATE_FORMATS:
            try:
                parsed = datetime.strptime(text, date_format)
                break
            except ValueError:
                continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed


def _is_setup_reply(email: dict[str, Any]) -> bool:
    subject = email.get("subject")
    return isinstance(subject, str) and "[mo-setup]" in subject.lower()


def is_duplicate_or_stale(
    email: dict[str, Any],
    *,
    store_path: str | Path | None = None,
    now: datetime | None = None,
) -> bool:
    """Return whether an email is already seen or older than 24 hours.

    Setup replies skip only the age check; they remain subject to message-ID
    deduplication. Messages without a parseable received timestamp are allowed
    through the age check because the Outlook reader may provide display-only
    timestamps that do not identify a calendar date.
    """
    message_id = email.get("id")
    received_at = _parse_received_at(email.get("receivedDateTime"))
    current_time = now or datetime.now(LOCAL_TIMEZONE)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=LOCAL_TIMEZONE)

    stale = False
    if not _is_setup_reply(email) and received_at is not None:
        stale = current_time - received_at > AGE_LIMIT

    if stale:
        return True

    if not isinstance(message_id, str) or not message_id.strip():
        return False

    store = (
        SQLiteDedupStore(
            store_path,
            Path(__file__).resolve().parents[2]
            / "browser_data"
            / "outlook"
            / "property_dedup.sqlite3",
        )
        if store_path is not None
        else get_dedup_store()
    )
    return not store.claim_message(message_id)
