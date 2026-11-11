import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.logic import dedup_store
from app.logic.dedup_store import SQLiteDedupStore
from scripts import migrate_sqlite_to_pg


def test_sqlite_store_persists_message_property_and_channel_audit(tmp_path) -> None:
    store = SQLiteDedupStore(
        tmp_path / "messages.sqlite3",
        tmp_path / "properties.sqlite3",
    )

    assert store.claim_message("message-1")
    assert store.is_message_seen("message-1")
    assert not store.claim_message("message-1")
    store.record_message(
        "message-1",
        bucket="auto_processed",
        society="Prestige Park",
        flat="302",
        property_key="prestige park_302",
        thm_home_id="home-1",
        parse_status="success",
        received_at="2026-11-10T09:00:00+05:30",
        raw_subject="Move-Out Notice",
    )
    store.record_property(
        "prestige park_302",
        "Prestige Park",
        "302",
        thm_home_id="home-1",
        thm_project_id="project-1",
        graph_message_id="message-1",
        pack_summary={"tickets": 11},
    )
    store.record_send(
        "prestige park_302",
        "email",
        recipient="tenant@example.net",
        role="tenant",
        status="planned",
        payload={"subject": "Move-out"},
    )
    store.record_sent_items(
        [
            {
                "id": "sent-row-1",
                "conversationId": "conversation-1",
                "subject": "Move-out acknowledgment",
                "bodyPreview": "Acknowledgment for Prestige Park 302",
            }
        ]
    )

    assert store.is_property_processed("prestige park_302", since_days=60)
    with sqlite3.connect(tmp_path / "messages.sqlite3") as connection:
        message = connection.execute(
            "SELECT bucket, property_key, thm_home_id FROM moveout_messages "
            "WHERE graph_message_id = ?",
            ("message-1",),
        ).fetchone()
        sent = connection.execute(
            "SELECT subject FROM moveout_sent_items WHERE graph_message_id = ?",
            ("sent-row-1",),
        ).fetchone()
    with sqlite3.connect(tmp_path / "properties.sqlite3") as connection:
        audit = connection.execute(
            "SELECT channel, role, status FROM moveout_sends"
        ).fetchone()

    assert message == ("auto_processed", "prestige park_302", "home-1")
    assert sent == ("Move-out acknowledgment",)
    assert audit == ("email", "tenant", "planned")


def test_sqlite_store_migrates_existing_dedup_tables(tmp_path) -> None:
    message_db = tmp_path / "messages.sqlite3"
    property_db = tmp_path / "properties.sqlite3"
    old_seen_at = "2026-11-10T10:00:00+00:00"
    with sqlite3.connect(message_db) as connection:
        connection.execute(
            "CREATE TABLE seen_messages "
            "(message_id TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO seen_messages VALUES (?, ?)",
            ("legacy-message", old_seen_at),
        )
    with sqlite3.connect(property_db) as connection:
        connection.execute(
            "CREATE TABLE processed_properties "
            "(property_key TEXT PRIMARY KEY, processed_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO processed_properties VALUES (?, ?)",
            ("prestige park_302", old_seen_at),
        )

    store = SQLiteDedupStore(message_db, property_db)

    assert store.is_message_seen("legacy-message")
    assert store.is_property_processed("prestige park_302", 1)


def test_old_property_marker_expires_after_window(tmp_path) -> None:
    store = SQLiteDedupStore(
        tmp_path / "messages.sqlite3",
        tmp_path / "properties.sqlite3",
    )
    old_time = datetime.now(timezone.utc) - timedelta(days=61)
    store.record_property(
        "prestige park_302",
        "Prestige Park",
        "302",
        processed_at=old_time,
    )

    assert not store.is_property_processed("prestige park_302", since_days=60)


def test_backend_is_selected_from_database_configuration(monkeypatch, tmp_path) -> None:
    selected: list[str] = []

    class FakePostgresStore:
        def __init__(self, connection_string: str) -> None:
            selected.append(connection_string)

    monkeypatch.setattr(dedup_store, "PostgresDedupStore", FakePostgresStore)
    dedup_store._cached_store.cache_clear()
    monkeypatch.setenv("MOVEOUT_DEDUP_DB", "postgresql://test/db")
    monkeypatch.setenv("MOVEOUT_PROPERTY_DEDUP_DB", "postgresql://test/db")
    try:
        dedup_store.get_dedup_store()
    finally:
        dedup_store._cached_store.cache_clear()

    assert selected == ["postgresql://test/db"]

    with pytest.raises(ValueError, match="both be PostgreSQL URLs"):
        dedup_store.get_dedup_store(
            message_db_path="postgresql://test/db",
            property_db_path=tmp_path / "properties.sqlite3",
        )


def test_migration_copies_legacy_rows_without_removing_sqlite_sources(
    tmp_path,
    monkeypatch,
) -> None:
    message_db = tmp_path / "legacy-messages.sqlite3"
    property_db = tmp_path / "legacy-properties.sqlite3"
    with sqlite3.connect(message_db) as connection:
        connection.execute(
            "CREATE TABLE seen_messages "
            "(message_id TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO seen_messages VALUES (?, ?)",
            ("legacy-message", "2026-11-10T10:00:00+00:00"),
        )
    with sqlite3.connect(property_db) as connection:
        connection.execute(
            "CREATE TABLE processed_properties "
            "(property_key TEXT PRIMARY KEY, processed_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO processed_properties VALUES (?, ?)",
            ("prestige park_302", "2026-11-10T10:00:00+00:00"),
        )

    migrated: dict[str, list[tuple[object, ...]]] = {
        "messages": [],
        "properties": [],
    }

    class FakePostgresStore:
        def __init__(self, _connection_string: str) -> None:
            pass

        def record_message(self, graph_message_id, **kwargs) -> None:
            migrated["messages"].append((graph_message_id, kwargs))

        def record_property(self, property_key, society, flat, **kwargs) -> None:
            migrated["properties"].append(
                (property_key, society, flat, kwargs)
            )

    monkeypatch.setattr(migrate_sqlite_to_pg, "PostgresDedupStore", FakePostgresStore)

    counts = migrate_sqlite_to_pg.migrate(
        message_db,
        property_db,
        "postgresql://test/db",
    )

    assert counts == (1, 1)
    assert migrated["messages"][0][0] == "legacy-message"
    assert migrated["properties"][0][:3] == (
        "prestige park_302",
        "prestige park",
        "302",
    )
    assert message_db.exists()
    assert property_db.exists()
