import sqlite3
from datetime import datetime, timedelta, timezone

from app.logic import idempotency


def test_property_is_marked_and_matched_case_insensitively(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "property_dedup.sqlite3")

    assert not idempotency.check_property_dedup("Prestige Park", "302")
    idempotency.mark_property_processed("Prestige Park", "302")

    assert idempotency.check_property_dedup(" prestige park ", "302")


def test_property_is_available_after_window_expires(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "property_dedup.sqlite3"
    monkeypatch.setattr(idempotency, "DB_PATH", db_path)
    idempotency.mark_property_processed("Prestige Park", "302")

    processed_at = (
        datetime.now(timezone.utc) - timedelta(days=61)
    ).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE moveout_properties SET last_processed_at = ? "
            "WHERE property_key = ?",
            (processed_at, "prestige park_302"),
        )

    assert not idempotency.check_property_dedup("Prestige Park", "302")


def test_blank_property_fields_are_not_marked_or_deduplicated(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "property_dedup.sqlite3")

    assert not idempotency.check_property_dedup(" ", "302")
    idempotency.mark_property_processed("Prestige Park", "")
    assert not idempotency.DB_PATH.exists()
