# -*- coding: utf-8 -*-
"""Small SQLite store for durable, incremental Jobsuche retrievals."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .deduplication import listing_key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initialize(database_path: str | Path) -> None:
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS search_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                query_json TEXT NOT NULL,
                reported_total INTEGER,
                retrieved_count INTEGER NOT NULL DEFAULT 0,
                unique_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                message TEXT
            );
            CREATE TABLE IF NOT EXISTS listings (
                listing_key TEXT PRIMARY KEY,
                reference_number TEXT,
                source_url TEXT,
                title TEXT,
                employer TEXT,
                city TEXT,
                publication_date TEXT,
                retrieval_status TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_listings (
                run_id INTEGER NOT NULL REFERENCES search_runs(id),
                listing_key TEXT NOT NULL REFERENCES listings(listing_key),
                PRIMARY KEY (run_id, listing_key)
            );
            """
        )


def save_run(database_path: str | Path, query: dict, result) -> int:
    """Upsert current listings and retain one auditable search-run record."""
    initialize(database_path)
    now = _now()
    with closing(sqlite3.connect(database_path)) as connection:
        cursor = connection.execute(
            """
            INSERT INTO search_runs
                (started_at, completed_at, query_json, reported_total, retrieved_count,
                 unique_count, status, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                json.dumps(query, ensure_ascii=False),
                result.reported_total,
                result.retrieved_count,
                result.unique_count,
                result.status,
                result.message,
            ),
        )
        run_id = int(cursor.lastrowid)
        for listing in result.listings:
            key = listing_key(listing)
            connection.execute(
                """
                INSERT INTO listings
                    (listing_key, reference_number, source_url, title, employer, city,
                     publication_date, retrieval_status, first_seen_at, last_seen_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(listing_key) DO UPDATE SET
                    source_url=excluded.source_url,
                    title=excluded.title,
                    employer=excluded.employer,
                    city=excluded.city,
                    publication_date=excluded.publication_date,
                    retrieval_status=excluded.retrieval_status,
                    last_seen_at=excluded.last_seen_at,
                    payload_json=excluded.payload_json
                """,
                (
                    key,
                    listing.get("reference_number", ""),
                    listing.get("url", ""),
                    listing.get("title", ""),
                    listing.get("name", ""),
                    listing.get("city", ""),
                    str(listing.get("publication_date", "")),
                    result.status,
                    now,
                    now,
                    json.dumps(listing, ensure_ascii=False),
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO run_listings (run_id, listing_key) VALUES (?, ?)",
                (run_id, key),
            )
        connection.commit()
    return run_id
