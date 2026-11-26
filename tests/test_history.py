from datetime import datetime, timedelta, timezone
import sqlite3

from scraper.history import RETENTION_DAYS, recent_results, save_result


def test_history_saves_results_and_prunes_expired_entries(tmp_path):
    database = tmp_path / "history.sqlite"
    record_id = save_result(
        database,
        title="Recherche · Google Maps",
        source_ids=["google_maps"],
        rows=[{"firma": "ACME", "email": "info@acme.de"}],
        metadata={"status": "done"},
    )

    entries = recent_results(database)
    assert entries[0]["id"] == record_id
    assert entries[0]["rows"][0]["firma"] == "ACME"
    assert entries[0]["source_ids"] == ["google_maps"]
    assert "+00:00" in entries[0]["expires_at"]

    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO saved_results (created_at, expires_at, title, source_ids_json, metadata_json, rows_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (expired, expired, "old", "[]", "{}", "[]"),
        )
        connection.commit()

    assert [entry["id"] for entry in recent_results(database)] == [record_id]
    assert RETENTION_DAYS == 10
