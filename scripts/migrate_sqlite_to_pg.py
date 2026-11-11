"""Copy existing SQLite dedup rows to PostgreSQL without deleting the source."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from app.logic.dedup_store import (
    DEFAULT_MESSAGE_DB,
    DEFAULT_PROPERTY_DB,
    PostgresDedupStore,
)


def _sqlite_source(value: str | None, default: Path) -> Path:
    if not value:
        return default
    if value.startswith(("postgresql://", "postgres://")):
        return default
    return Path(value).expanduser()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def migrate(message_sqlite: Path, property_sqlite: Path, postgres_url: str) -> tuple[int, int]:
    store = PostgresDedupStore(postgres_url)
    migrated_message_ids: set[str] = set()
    migrated_property_keys: set[str] = set()

    if message_sqlite.is_file():
        with sqlite3.connect(message_sqlite) as source:
            if _table_exists(source, "seen_messages"):
                rows = source.execute(
                    "SELECT message_id, first_seen_at FROM seen_messages"
                ).fetchall()
                for message_id, processed_at in rows:
                    store.record_message(
                        message_id,
                        bucket="processing",
                        processed_at=processed_at,
                    )
                    migrated_message_ids.add(message_id)
            if _table_exists(source, "moveout_messages"):
                columns = {
                    row[1]
                    for row in source.execute("PRAGMA table_info(moveout_messages)")
                }
                required = {
                    "graph_message_id",
                    "conversation_id",
                    "society",
                    "flat",
                    "property_key",
                    "thm_home_id",
                    "parse_status",
                    "bucket",
                    "received_at",
                    "processed_at",
                    "raw_subject",
                    "raw_preview",
                }
                if required.issubset(columns):
                    rows = source.execute(
                        """
                        SELECT graph_message_id, conversation_id, society, flat,
                               property_key, thm_home_id, parse_status, bucket,
                               received_at, processed_at, raw_subject, raw_preview
                        FROM moveout_messages
                        """
                    ).fetchall()
                    for row in rows:
                        (
                            message_id,
                            conversation_id,
                            society,
                            flat,
                            property_key,
                            thm_home_id,
                            parse_status,
                            bucket,
                            received_at,
                            processed_at,
                            raw_subject,
                            raw_preview,
                        ) = row
                        store.record_message(
                            message_id,
                            bucket=bucket,
                            conversation_id=conversation_id,
                            society=society,
                            flat=flat,
                            property_key=property_key,
                            thm_home_id=thm_home_id,
                            parse_status=parse_status,
                            received_at=received_at,
                            processed_at=processed_at,
                            raw_subject=raw_subject,
                            raw_preview=raw_preview,
                        )
                    migrated_message_ids.update(row[0] for row in rows)

    if property_sqlite.is_file():
        with sqlite3.connect(property_sqlite) as source:
            if _table_exists(source, "processed_properties"):
                rows = source.execute(
                    "SELECT property_key, processed_at FROM processed_properties"
                ).fetchall()
                for property_key, processed_at in rows:
                    society, separator, flat = property_key.rpartition("_")
                    if not separator or not society or not flat:
                        continue
                    store.record_property(
                        property_key,
                        society,
                        flat,
                        processed_at=processed_at,
                    )
                    migrated_property_keys.add(property_key)
            if _table_exists(source, "moveout_properties"):
                columns = {
                    row[1]
                    for row in source.execute("PRAGMA table_info(moveout_properties)")
                }
                required = {
                    "property_key",
                    "society",
                    "flat",
                    "thm_home_id",
                    "thm_project_id",
                    "first_processed_at",
                    "last_processed_at",
                    "last_graph_message_id",
                    "pack_summary",
                }
                if required.issubset(columns):
                    rows = source.execute(
                        """
                        SELECT property_key, society, flat, thm_home_id,
                               thm_project_id, first_processed_at,
                               last_graph_message_id, pack_summary,
                               last_processed_at
                        FROM moveout_properties
                        """
                    ).fetchall()
                    for (
                        property_key,
                        society,
                        flat,
                        home_id,
                        project_id,
                        first_processed_at,
                        graph_message_id,
                        pack_summary,
                        processed_at,
                    ) in rows:
                        if isinstance(pack_summary, str):
                            pack_summary = json.loads(pack_summary)
                        store.record_property(
                            property_key,
                            society,
                            flat,
                            first_processed_at=first_processed_at,
                            thm_home_id=home_id,
                            thm_project_id=project_id,
                            pack_summary=pack_summary,
                            graph_message_id=graph_message_id,
                            processed_at=processed_at,
                        )
                    migrated_property_keys.update(row[0] for row in rows)

            if _table_exists(source, "moveout_sends"):
                columns = {
                    row[1]
                    for row in source.execute("PRAGMA table_info(moveout_sends)")
                }
                required = {
                    "property_key",
                    "channel",
                    "recipient",
                    "role",
                    "status",
                    "external_id",
                    "sent_at",
                    "error",
                    "payload",
                }
                if required.issubset(columns):
                    rows = source.execute(
                        """
                        SELECT property_key, channel, recipient, role, status,
                               external_id, error, payload, sent_at
                        FROM moveout_sends
                        """
                    ).fetchall()
                    for (
                        property_key,
                        channel,
                        recipient,
                        role,
                        status,
                        external_id,
                        error,
                        payload,
                        sent_at,
                    ) in rows:
                        if isinstance(payload, str):
                            payload = json.loads(payload)
                        store.record_send(
                            property_key,
                            channel,
                            recipient=recipient,
                            role=role,
                            status=status,
                            external_id=external_id,
                            error=error,
                            payload=payload,
                            sent_at=sent_at,
                        )

    if message_sqlite.is_file():
        with sqlite3.connect(message_sqlite) as source:
            if _table_exists(source, "moveout_sent_items"):
                columns = {
                    row[1]
                    for row in source.execute("PRAGMA table_info(moveout_sent_items)")
                }
                required = {
                    "graph_message_id",
                    "conversation_id",
                    "subject",
                    "body_preview",
                    "received_at",
                    "scanned_at",
                    "property_key_hint",
                }
                if required.issubset(columns):
                    rows = source.execute(
                        """
                        SELECT graph_message_id, conversation_id, subject,
                               body_preview, received_at, scanned_at,
                               property_key_hint
                        FROM moveout_sent_items
                        """
                    ).fetchall()
                    store.record_sent_items(
                        [
                            {
                                "graphMessageId": graph_message_id,
                                "conversationId": conversation_id,
                                "subject": subject,
                                "bodyPreview": preview,
                                "receivedDateTime": received_at,
                                "scanned_at": scanned_at,
                                "property_key_hint": property_key,
                            }
                            for (
                                graph_message_id,
                                conversation_id,
                                subject,
                                preview,
                                received_at,
                                scanned_at,
                                property_key,
                            ) in rows
                        ]
                    )

    return len(migrated_message_ids), len(migrated_property_keys)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres-url",
        default=os.environ.get("MOVEOUT_DEDUP_DB"),
        help="Target PostgreSQL URL (defaults to MOVEOUT_DEDUP_DB).",
    )
    parser.add_argument(
        "--message-sqlite",
        default=os.environ.get("MOVEOUT_DEDUP_DB"),
        help="Source message DB path.",
    )
    parser.add_argument(
        "--property-sqlite",
        default=os.environ.get("MOVEOUT_PROPERTY_DEDUP_DB"),
        help="Source property DB path.",
    )
    args = parser.parse_args()
    if not args.postgres_url or not args.postgres_url.startswith(
        ("postgresql://", "postgres://")
    ):
        parser.error("--postgres-url must be a PostgreSQL connection URL.")

    message_source = _sqlite_source(args.message_sqlite, DEFAULT_MESSAGE_DB)
    property_source = _sqlite_source(args.property_sqlite, DEFAULT_PROPERTY_DB)
    message_count, property_count = migrate(
        message_source,
        property_source,
        args.postgres_url,
    )
    print(
        f"Migrated {message_count} message records and "
        f"{property_count} property records. SQLite sources were retained."
    )


if __name__ == "__main__":
    main()
