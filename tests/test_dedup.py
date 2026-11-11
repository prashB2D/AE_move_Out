from datetime import datetime

from app.logic.dedup import is_duplicate_or_stale


def test_detects_duplicate_message_id(tmp_path) -> None:
    email = {
        "id": "same-message",
        "receivedDateTime": "2026-11-10T09:00:00+05:30",
    }
    store = tmp_path / "dedup.sqlite3"

    assert not is_duplicate_or_stale(
        email,
        store_path=store,
        now=datetime.fromisoformat("2026-11-10T10:00:00+05:30"),
    )
    assert is_duplicate_or_stale(
        email,
        store_path=store,
        now=datetime.fromisoformat("2026-11-10T10:01:00+05:30"),
    )


def test_drops_messages_older_than_24_hours(tmp_path) -> None:
    assert is_duplicate_or_stale(
        {
            "id": "old-message",
            "receivedDateTime": "2026-11-09T09:59:00+05:30",
        },
        store_path=tmp_path / "dedup.sqlite3",
        now=datetime.fromisoformat("2026-11-10T10:00:00+05:30"),
    )


def test_setup_reply_bypasses_age_check(tmp_path) -> None:
    email = {
        "id": "old-setup-reply",
        "subject": "Re: [MO-SETUP] Move-Out review",
        "receivedDateTime": "2026-11-01T09:00:00+05:30",
    }

    assert not is_duplicate_or_stale(
        email,
        store_path=tmp_path / "dedup.sqlite3",
        now=datetime.fromisoformat("2026-11-10T10:00:00+05:30"),
    )


def test_unparseable_timestamp_is_not_rejected_as_stale(tmp_path) -> None:
    assert not is_duplicate_or_stale(
        {"id": "display-only-time", "receivedDateTime": "Sat 17:43"},
        store_path=tmp_path / "dedup.sqlite3",
        now=datetime.fromisoformat("2026-11-10T10:00:00+05:30"),
    )
