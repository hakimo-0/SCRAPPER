"""Ten-day local archive for completed searches and live extractions."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any


RETENTION_DAYS = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _initialize(database_path: str | Path) -> None:
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                title TEXT NOT NULL,
                source_ids_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                rows_json TEXT NOT NULL
            )
            """
        )


def _prune(connection: sqlite3.Connection, now: datetime) -> None:
    connection.execute("DELETE FROM saved_results WHERE expires_at <= ?", (_timestamp(now),))


def save_result(
    database_path: str | Path,
    *,
    title: str,
    source_ids: list[str] | tuple[str, ...],
    rows: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> int:
    """Save a completed result locally and keep it for exactly ten days."""
    _initialize(database_path)
    now = _now()
    expires_at = now + timedelta(days=RETENTION_DAYS)
    with closing(sqlite3.connect(database_path)) as connection:
        _prune(connection, now)
        cursor = connection.execute(
            """
            INSERT INTO saved_results
                (created_at, expires_at, title, source_ids_json, metadata_json, rows_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _timestamp(now),
                _timestamp(expires_at),
                title,
                json.dumps(list(source_ids), ensure_ascii=False),
                json.dumps(metadata or {}, ensure_ascii=False, default=str),
                json.dumps(rows, ensure_ascii=False, default=str),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def recent_results(database_path: str | Path) -> list[dict[str, Any]]:
    """Return non-expired archive entries, newest first, then prune old rows."""
    _initialize(database_path)
    now = _now()
    with closing(sqlite3.connect(database_path)) as connection:
        _prune(connection, now)
        rows = connection.execute(
            """
            SELECT id, created_at, expires_at, title, source_ids_json, metadata_json, rows_json
            FROM saved_results
            ORDER BY id DESC
            """
        ).fetchall()
        connection.commit()
    return [
        {
            "id": int(row[0]),
            "created_at": row[1],
            "expires_at": row[2],
            "title": row[3],
            "source_ids": json.loads(row[4]),
            "metadata": json.loads(row[5]),
            "rows": json.loads(row[6]),
        }
        for row in rows
    ]
