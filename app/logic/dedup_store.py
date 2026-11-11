"""SQLite and PostgreSQL persistence for move-out deduplication and audit data."""

from __future__ import annotations

import json
import os
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_MESSAGE_DB = (
    Path(__file__).resolve().parents[2]
    / "browser_data"
    / "outlook"
    / "message_dedup.sqlite3"
)
DEFAULT_PROPERTY_DB = (
    Path(__file__).resolve().parents[2]
    / "browser_data"
    / "outlook"
    / "property_dedup.sqlite3"
)
POSTGRES_PREFIXES = ("postgresql://", "postgres://")

_SQLITE_MESSAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS moveout_messages (
    graph_message_id TEXT PRIMARY KEY,
    conversation_id TEXT,
    society TEXT,
    flat TEXT,
    property_key TEXT,
    thm_home_id TEXT,
    parse_status TEXT,
    bucket TEXT NOT NULL,
    received_at TEXT,
    processed_at TEXT NOT NULL,
    raw_subject TEXT,
    raw_preview TEXT
);
CREATE INDEX IF NOT EXISTS moveout_messages_property_processed_idx
    ON moveout_messages (property_key, processed_at DESC);
CREATE TABLE IF NOT EXISTS moveout_sent_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    graph_message_id TEXT UNIQUE,
    conversation_id TEXT,
    subject TEXT,
    body_preview TEXT,
    received_at TEXT,
    scanned_at TEXT NOT NULL,
    property_key_hint TEXT
);
CREATE INDEX IF NOT EXISTS moveout_sent_items_property_scanned_idx
    ON moveout_sent_items (property_key_hint, scanned_at DESC);
"""

_SQLITE_PROPERTY_SCHEMA = """
CREATE TABLE IF NOT EXISTS moveout_properties (
    property_key TEXT PRIMARY KEY,
    society TEXT NOT NULL,
    flat TEXT NOT NULL,
    thm_home_id TEXT,
    thm_project_id TEXT,
    first_processed_at TEXT NOT NULL,
    last_processed_at TEXT NOT NULL,
    last_graph_message_id TEXT,
    pack_summary TEXT
);
CREATE TABLE IF NOT EXISTS moveout_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_key TEXT NOT NULL REFERENCES moveout_properties(property_key),
    channel TEXT NOT NULL,
    recipient TEXT,
    role TEXT,
    status TEXT NOT NULL,
    external_id TEXT,
    sent_at TEXT NOT NULL,
    error TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS moveout_sends_property_sent_idx
    ON moveout_sends (property_key, sent_at DESC);
"""

_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS moveout_messages (
    graph_message_id TEXT PRIMARY KEY,
    conversation_id TEXT,
    society TEXT,
    flat TEXT,
    property_key TEXT,
    thm_home_id TEXT,
    parse_status TEXT,
    bucket TEXT NOT NULL,
    received_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_subject TEXT,
    raw_preview TEXT
);
CREATE INDEX IF NOT EXISTS moveout_messages_property_processed_idx
    ON moveout_messages (property_key, processed_at DESC);
CREATE TABLE IF NOT EXISTS moveout_properties (
    property_key TEXT PRIMARY KEY,
    society TEXT NOT NULL,
    flat TEXT NOT NULL,
    thm_home_id TEXT,
    thm_project_id TEXT,
    first_processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_graph_message_id TEXT,
    pack_summary JSONB
);
CREATE TABLE IF NOT EXISTS moveout_sends (
    id BIGSERIAL PRIMARY KEY,
    property_key TEXT NOT NULL REFERENCES moveout_properties(property_key),
    channel TEXT NOT NULL,
    recipient TEXT,
    role TEXT,
    status TEXT NOT NULL,
    external_id TEXT,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error TEXT,
    payload JSONB
);
CREATE INDEX IF NOT EXISTS moveout_sends_property_sent_idx
    ON moveout_sends (property_key, sent_at DESC);
CREATE TABLE IF NOT EXISTS moveout_sent_items (
    id BIGSERIAL PRIMARY KEY,
    graph_message_id TEXT UNIQUE,
    conversation_id TEXT,
    subject TEXT,
    body_preview TEXT,
    received_at TIMESTAMPTZ,
    scanned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    property_key_hint TEXT
);
CREATE INDEX IF NOT EXISTS moveout_sent_items_property_scanned_idx
    ON moveout_sent_items (property_key_hint, scanned_at DESC);
"""


def _as_datetime(value: datetime | str | None) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sqlite_datetime(value: datetime | str | None) -> str | None:
    parsed = _as_datetime(value)
    return parsed.isoformat() if parsed else None


def _json_text(value: Any) -> str | None:
    return json.dumps(value, ensure_ascii=False, default=str) if value is not None else None


def property_key_for(society: str, flat: str) -> str:
    return f"{society.strip().casefold()}_{flat.strip().casefold()}"


class DedupStore(ABC):
    """Persistence operations shared by the message and property gates."""

    @abstractmethod
    def is_message_seen(self, graph_message_id: str) -> bool: ...

    @abstractmethod
    def claim_message(self, graph_message_id: str) -> bool: ...

    @abstractmethod
    def is_property_processed(self, property_key: str, since_days: int) -> bool: ...

    @abstractmethod
    def record_message(
        self,
        graph_message_id: str,
        *,
        bucket: str,
        conversation_id: str | None = None,
        society: str | None = None,
        flat: str | None = None,
        property_key: str | None = None,
        thm_home_id: str | None = None,
        parse_status: str | None = None,
        received_at: datetime | str | None = None,
        processed_at: datetime | str | None = None,
        raw_subject: str | None = None,
        raw_preview: str | None = None,
    ) -> None: ...

    @abstractmethod
    def record_property(
        self,
        property_key: str,
        society: str,
        flat: str,
        *,
        first_processed_at: datetime | str | None = None,
        thm_home_id: str | None = None,
        thm_project_id: str | None = None,
        graph_message_id: str | None = None,
        pack_summary: dict[str, Any] | None = None,
        processed_at: datetime | str | None = None,
    ) -> None: ...

    @abstractmethod
    def record_send(
        self,
        property_key: str,
        channel: str,
        *,
        recipient: str | None = None,
        role: str | None = None,
        status: str,
        external_id: str | None = None,
        error: str | None = None,
        payload: dict[str, Any] | None = None,
        sent_at: datetime | str | None = None,
    ) -> None: ...

    @abstractmethod
    def record_sent_items(self, messages: list[dict[str, Any]]) -> None: ...


class SQLiteDedupStore(DedupStore):
    """SQLite backend preserving the project's existing two-file layout."""

    def __init__(
        self,
        message_db_path: str | Path = DEFAULT_MESSAGE_DB,
        property_db_path: str | Path = DEFAULT_PROPERTY_DB,
    ) -> None:
        self.message_db_path = Path(message_db_path).expanduser().resolve()
        self.property_db_path = Path(property_db_path).expanduser().resolve()

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(path, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        if os.name == "posix":
            path.chmod(0o600)
        return connection

    def _message_connection(self) -> sqlite3.Connection:
        connection = self._connect(self.message_db_path)
        connection.executescript(_SQLITE_MESSAGE_SCHEMA)
        legacy = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'seen_messages'"
        ).fetchone()
        if legacy:
            connection.execute(
                """
                INSERT OR IGNORE INTO moveout_messages
                    (graph_message_id, bucket, processed_at)
                SELECT message_id, 'processing', first_seen_at
                FROM seen_messages
                """
            )
        return connection

    def _property_connection(self) -> sqlite3.Connection:
        connection = self._connect(self.property_db_path)
        connection.executescript(_SQLITE_PROPERTY_SCHEMA)
        legacy = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'processed_properties'"
        ).fetchone()
        if legacy:
            rows = connection.execute(
                "SELECT property_key, processed_at FROM processed_properties"
            ).fetchall()
            for key, processed_at in rows:
                society, separator, flat = key.rpartition("_")
                if not separator or not society or not flat:
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO moveout_properties
                        (property_key, society, flat, first_processed_at, last_processed_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (key, society, flat, processed_at, processed_at),
                )
        return connection

    def is_message_seen(self, graph_message_id: str) -> bool:
        with self._message_connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM moveout_messages WHERE graph_message_id = ?",
                    (graph_message_id,),
                ).fetchone()
                is not None
            )

    def claim_message(self, graph_message_id: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._message_connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO moveout_messages
                    (graph_message_id, bucket, processed_at)
                VALUES (?, 'processing', ?)
                """,
                (graph_message_id, now),
            )
            return cursor.rowcount == 1

    def is_property_processed(self, property_key: str, since_days: int) -> bool:
        with self._property_connection() as connection:
            row = connection.execute(
                "SELECT last_processed_at FROM moveout_properties WHERE property_key = ?",
                (property_key,),
            ).fetchone()
        if row is None:
            return False
        processed_at = _as_datetime(row[0])
        return bool(
            processed_at
            and datetime.now(timezone.utc) - processed_at < timedelta(days=since_days)
        )

    def record_message(
        self,
        graph_message_id: str,
        *,
        bucket: str,
        conversation_id: str | None = None,
        society: str | None = None,
        flat: str | None = None,
        property_key: str | None = None,
        thm_home_id: str | None = None,
        parse_status: str | None = None,
        received_at: datetime | str | None = None,
        processed_at: datetime | str | None = None,
        raw_subject: str | None = None,
        raw_preview: str | None = None,
    ) -> None:
        now = _sqlite_datetime(processed_at) or datetime.now(timezone.utc).isoformat()
        with self._message_connection() as connection:
            connection.execute(
                """
                INSERT INTO moveout_messages (
                    graph_message_id, conversation_id, society, flat, property_key,
                    thm_home_id, parse_status, bucket, received_at, processed_at,
                    raw_subject, raw_preview
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(graph_message_id) DO UPDATE SET
                    conversation_id = COALESCE(excluded.conversation_id, conversation_id),
                    society = COALESCE(excluded.society, society),
                    flat = COALESCE(excluded.flat, flat),
                    property_key = COALESCE(excluded.property_key, property_key),
                    thm_home_id = COALESCE(excluded.thm_home_id, thm_home_id),
                    parse_status = COALESCE(excluded.parse_status, parse_status),
                    bucket = excluded.bucket,
                    received_at = COALESCE(excluded.received_at, received_at),
                    processed_at = excluded.processed_at,
                    raw_subject = COALESCE(excluded.raw_subject, raw_subject),
                    raw_preview = COALESCE(excluded.raw_preview, raw_preview)
                """,
                (
                    graph_message_id,
                    conversation_id,
                    society,
                    flat,
                    property_key,
                    thm_home_id,
                    parse_status,
                    bucket,
                    _sqlite_datetime(received_at),
                    now,
                    raw_subject,
                    raw_preview,
                ),
            )

    def record_property(
        self,
        property_key: str,
        society: str,
        flat: str,
        *,
        first_processed_at: datetime | str | None = None,
        thm_home_id: str | None = None,
        thm_project_id: str | None = None,
        graph_message_id: str | None = None,
        pack_summary: dict[str, Any] | None = None,
        processed_at: datetime | str | None = None,
    ) -> None:
        first = (
            _sqlite_datetime(first_processed_at)
            or _sqlite_datetime(processed_at)
            or datetime.now(timezone.utc).isoformat()
        )
        now = _sqlite_datetime(processed_at) or first
        with self._property_connection() as connection:
            connection.execute(
                """
                INSERT INTO moveout_properties (
                    property_key, society, flat, thm_home_id, thm_project_id,
                    first_processed_at, last_processed_at, last_graph_message_id,
                    pack_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(property_key) DO UPDATE SET
                    society = excluded.society,
                    flat = excluded.flat,
                    thm_home_id = COALESCE(excluded.thm_home_id, thm_home_id),
                    thm_project_id = COALESCE(excluded.thm_project_id, thm_project_id),
                    last_processed_at = excluded.last_processed_at,
                    last_graph_message_id = COALESCE(
                        excluded.last_graph_message_id, last_graph_message_id
                    ),
                    pack_summary = COALESCE(excluded.pack_summary, pack_summary)
                """,
                (
                    property_key,
                    society,
                    flat,
                    thm_home_id,
                    thm_project_id,
                    first,
                    now,
                    graph_message_id,
                    _json_text(pack_summary),
                ),
            )

    def record_send(
        self,
        property_key: str,
        channel: str,
        *,
        recipient: str | None = None,
        role: str | None = None,
        status: str,
        external_id: str | None = None,
        error: str | None = None,
        payload: dict[str, Any] | None = None,
        sent_at: datetime | str | None = None,
    ) -> None:
        timestamp = _sqlite_datetime(sent_at) or datetime.now(timezone.utc).isoformat()
        with self._property_connection() as connection:
            connection.execute(
                """
                INSERT INTO moveout_sends (
                    property_key, channel, recipient, role, status, external_id,
                    sent_at, error, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    property_key,
                    channel,
                    recipient,
                    role,
                    status,
                    external_id,
                    timestamp,
                    error,
                    _json_text(payload),
                ),
            )

    def record_sent_items(self, messages: list[dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._message_connection() as connection:
            for message in messages:
                graph_message_id = message.get("graphMessageId") or message.get("id")
                conversation_id = message.get("conversationId")
                scanned_at = (
                    _sqlite_datetime(message.get("scanned_at"))
                    or _sqlite_datetime(message.get("scannedAt"))
                    or now
                )
                connection.execute(
                    """
                    INSERT INTO moveout_sent_items (
                        graph_message_id, conversation_id, subject, body_preview,
                        received_at, scanned_at, property_key_hint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(graph_message_id) DO UPDATE SET
                        conversation_id = COALESCE(
                            excluded.conversation_id, conversation_id
                        ),
                        subject = COALESCE(excluded.subject, subject),
                        body_preview = COALESCE(excluded.body_preview, body_preview),
                        received_at = COALESCE(excluded.received_at, received_at),
                        scanned_at = excluded.scanned_at,
                        property_key_hint = COALESCE(
                            excluded.property_key_hint, property_key_hint
                        )
                    """,
                    (
                        graph_message_id,
                        conversation_id,
                        message.get("subject"),
                        message.get("bodyPreview"),
                        _sqlite_datetime(message.get("receivedDateTime")),
                        scanned_at,
                        message.get("property_key_hint"),
                    ),
                )


class PostgresDedupStore(DedupStore):
    """PostgreSQL backend; all audit and dedup tables share one database."""

    def __init__(self, connection_string: str) -> None:
        self.connection_string = connection_string
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError(
                "PostgreSQL persistence requires psycopg[binary]; install "
                "the project requirements."
            ) from error
        self._psycopg = psycopg
        with self._connect() as connection:
            for statement in _POSTGRES_SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)

    def _connect(self):
        try:
            return self._psycopg.connect(self.connection_string)
        except self._psycopg.Error as error:
            scheme = urlsplit(self.connection_string).scheme or "postgres"
            raise RuntimeError(
                f"MOVEOUT_DEDUP_DB is set to {scheme}://... but connection "
                f"failed: {error}"
            ) from error

    def is_message_seen(self, graph_message_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM moveout_messages WHERE graph_message_id = %s",
                (graph_message_id,),
            ).fetchone()
        return row is not None

    def claim_message(self, graph_message_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO moveout_messages (graph_message_id, bucket)
                VALUES (%s, 'processing')
                ON CONFLICT (graph_message_id) DO NOTHING
                RETURNING graph_message_id
                """,
                (graph_message_id,),
            ).fetchone()
        return row is not None

    def is_property_processed(self, property_key: str, since_days: int) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM moveout_properties
                WHERE property_key = %s AND last_processed_at >= %s
                """,
                (property_key, cutoff),
            ).fetchone()
        return row is not None

    def record_message(
        self,
        graph_message_id: str,
        *,
        bucket: str,
        conversation_id: str | None = None,
        society: str | None = None,
        flat: str | None = None,
        property_key: str | None = None,
        thm_home_id: str | None = None,
        parse_status: str | None = None,
        received_at: datetime | str | None = None,
        processed_at: datetime | str | None = None,
        raw_subject: str | None = None,
        raw_preview: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO moveout_messages (
                    graph_message_id, conversation_id, society, flat, property_key,
                    thm_home_id, parse_status, bucket, received_at, processed_at,
                    raw_subject, raw_preview
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                          %s, COALESCE(%s, NOW()), %s, %s)
                ON CONFLICT (graph_message_id) DO UPDATE SET
                    conversation_id = COALESCE(
                        EXCLUDED.conversation_id, moveout_messages.conversation_id
                    ),
                    society = COALESCE(EXCLUDED.society, moveout_messages.society),
                    flat = COALESCE(EXCLUDED.flat, moveout_messages.flat),
                    property_key = COALESCE(
                        EXCLUDED.property_key, moveout_messages.property_key
                    ),
                    thm_home_id = COALESCE(
                        EXCLUDED.thm_home_id, moveout_messages.thm_home_id
                    ),
                    parse_status = COALESCE(
                        EXCLUDED.parse_status, moveout_messages.parse_status
                    ),
                    bucket = EXCLUDED.bucket,
                    received_at = COALESCE(
                        EXCLUDED.received_at, moveout_messages.received_at
                    ),
                    processed_at = EXCLUDED.processed_at,
                    raw_subject = COALESCE(
                        EXCLUDED.raw_subject, moveout_messages.raw_subject
                    ),
                    raw_preview = COALESCE(
                        EXCLUDED.raw_preview, moveout_messages.raw_preview
                    )
                """,
                (
                    graph_message_id,
                    conversation_id,
                    society,
                    flat,
                    property_key,
                    thm_home_id,
                    parse_status,
                    bucket,
                    _as_datetime(received_at),
                    _as_datetime(processed_at),
                    raw_subject,
                    raw_preview,
                ),
            )

    def record_property(
        self,
        property_key: str,
        society: str,
        flat: str,
        *,
        first_processed_at: datetime | str | None = None,
        thm_home_id: str | None = None,
        thm_project_id: str | None = None,
        graph_message_id: str | None = None,
        pack_summary: dict[str, Any] | None = None,
        processed_at: datetime | str | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        first = _as_datetime(first_processed_at) or _as_datetime(processed_at)
        now = _as_datetime(processed_at)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO moveout_properties (
                    property_key, society, flat, thm_home_id, thm_project_id,
                    first_processed_at, last_processed_at, last_graph_message_id,
                    pack_summary
                ) VALUES (%s, %s, %s, %s, %s, COALESCE(%s, NOW()),
                          COALESCE(%s, NOW()), %s, %s)
                ON CONFLICT (property_key) DO UPDATE SET
                    society = EXCLUDED.society,
                    flat = EXCLUDED.flat,
                    thm_home_id = COALESCE(
                        EXCLUDED.thm_home_id, moveout_properties.thm_home_id
                    ),
                    thm_project_id = COALESCE(
                        EXCLUDED.thm_project_id, moveout_properties.thm_project_id
                    ),
                    last_processed_at = EXCLUDED.last_processed_at,
                    last_graph_message_id = COALESCE(
                        EXCLUDED.last_graph_message_id,
                        moveout_properties.last_graph_message_id
                    ),
                    pack_summary = COALESCE(
                        EXCLUDED.pack_summary, moveout_properties.pack_summary
                    )
                """,
                (
                    property_key,
                    society,
                    flat,
                    thm_home_id,
                    thm_project_id,
                    first,
                    now,
                    graph_message_id,
                    Jsonb(pack_summary) if pack_summary is not None else None,
                ),
            )

    def record_send(
        self,
        property_key: str,
        channel: str,
        *,
        recipient: str | None = None,
        role: str | None = None,
        status: str,
        external_id: str | None = None,
        error: str | None = None,
        payload: dict[str, Any] | None = None,
        sent_at: datetime | str | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO moveout_sends (
                    property_key, channel, recipient, role, status, external_id,
                    sent_at, error, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()), %s, %s)
                """,
                (
                    property_key,
                    channel,
                    recipient,
                    role,
                    status,
                    external_id,
                    _as_datetime(sent_at),
                    error,
                    Jsonb(payload) if payload is not None else None,
                ),
            )

    def record_sent_items(self, messages: list[dict[str, Any]]) -> None:
        with self._connect() as connection:
            for message in messages:
                graph_message_id = message.get("graphMessageId") or message.get("id")
                connection.execute(
                    """
                    INSERT INTO moveout_sent_items (
                        graph_message_id, conversation_id, subject, body_preview,
                        received_at, scanned_at, property_key_hint
                    ) VALUES (%s, %s, %s, %s, %s, COALESCE(%s, NOW()), %s)
                    ON CONFLICT (graph_message_id) DO UPDATE SET
                        conversation_id = COALESCE(
                            EXCLUDED.conversation_id,
                            moveout_sent_items.conversation_id
                        ),
                        subject = COALESCE(
                            EXCLUDED.subject, moveout_sent_items.subject
                        ),
                        body_preview = COALESCE(
                            EXCLUDED.body_preview, moveout_sent_items.body_preview
                        ),
                        received_at = COALESCE(
                            EXCLUDED.received_at, moveout_sent_items.received_at
                        ),
                        scanned_at = NOW(),
                        property_key_hint = COALESCE(
                            EXCLUDED.property_key_hint,
                            moveout_sent_items.property_key_hint
                        )
                    """,
                    (
                        graph_message_id,
                        message.get("conversationId"),
                        message.get("subject"),
                        message.get("bodyPreview"),
                        _as_datetime(message.get("receivedDateTime")),
                        _as_datetime(
                            message.get("scanned_at") or message.get("scannedAt")
                        ),
                        message.get("property_key_hint"),
                    ),
                )


def _is_postgres_url(value: str | None) -> bool:
    return bool(value and value.startswith(POSTGRES_PREFIXES))


@lru_cache(maxsize=8)
def _cached_store(
    message_config: str,
    property_config: str,
    backend: str,
) -> DedupStore:
    if backend == "postgres":
        if message_config != property_config:
            raise ValueError(
                "MOVEOUT_DEDUP_DB and MOVEOUT_PROPERTY_DEDUP_DB must use the "
                "same PostgreSQL connection string."
            )
        return PostgresDedupStore(message_config)
    return SQLiteDedupStore(message_config, property_config)


def get_dedup_store(
    *,
    message_db_path: str | Path | None = None,
    property_db_path: str | Path | None = None,
) -> DedupStore:
    """Select a persistence backend from the configured database locations."""
    message_config = str(
        message_db_path
        if message_db_path is not None
        else os.environ.get("MOVEOUT_DEDUP_DB", DEFAULT_MESSAGE_DB)
    )
    property_config = str(
        property_db_path
        if property_db_path is not None
        else os.environ.get("MOVEOUT_PROPERTY_DEDUP_DB", DEFAULT_PROPERTY_DB)
    )
    message_is_postgres = _is_postgres_url(message_config)
    property_is_postgres = _is_postgres_url(property_config)
    if message_is_postgres != property_is_postgres:
        raise ValueError(
            "MOVEOUT_DEDUP_DB and MOVEOUT_PROPERTY_DEDUP_DB must both be "
            "PostgreSQL URLs or both be SQLite paths."
        )
    backend = "postgres" if message_is_postgres else "sqlite"
    return _cached_store(message_config, property_config, backend)


def record_send(
    property_key: str,
    channel: str,
    *,
    recipient: str | None = None,
    role: str | None = None,
    status: str,
    external_id: str | None = None,
    error: str | None = None,
    payload: dict[str, Any] | None = None,
    sent_at: datetime | str | None = None,
) -> None:
    get_dedup_store().record_send(
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


def record_message(
    graph_message_id: str | None,
    *,
    bucket: str,
    conversation_id: str | None = None,
    society: str | None = None,
    flat: str | None = None,
    property_key: str | None = None,
    thm_home_id: str | None = None,
    parse_status: str | None = None,
    received_at: datetime | str | None = None,
    processed_at: datetime | str | None = None,
    raw_subject: str | None = None,
    raw_preview: str | None = None,
) -> None:
    if not isinstance(graph_message_id, str) or not graph_message_id.strip():
        return
    get_dedup_store().record_message(
        graph_message_id,
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


def record_property(
    property_key: str,
    society: str,
    flat: str,
    *,
    thm_home_id: str | None = None,
    thm_project_id: str | None = None,
    graph_message_id: str | None = None,
    pack_summary: dict[str, Any] | None = None,
    processed_at: datetime | str | None = None,
) -> None:
    get_dedup_store().record_property(
        property_key,
        society,
        flat,
        thm_home_id=thm_home_id,
        thm_project_id=thm_project_id,
        graph_message_id=graph_message_id,
        pack_summary=pack_summary,
        processed_at=processed_at,
    )
