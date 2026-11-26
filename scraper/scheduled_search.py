"""Run one persisted scheduled Jobbörse search from Windows Task Scheduler."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import sqlite3
from pathlib import Path
from typing import Any

from . import crm, local_schedule
from .arbeitsagentur import search_all
from .database import initialize as initialize_search_db, save_run
from .deduplication import listing_key
from .search import SearchConfig


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCHEDULE_PATH = PROJECT_DIR / "output" / "scheduled_search.json"
SCHEDULED_RUNS_PATH = PROJECT_DIR / "output" / "scheduled_search.sqlite"
SEARCH_DATABASE_PATH = PROJECT_DIR / "output" / "arbeitsagentur.sqlite"
CRM_DATABASE_PATH = PROJECT_DIR / "output" / "crm.sqlite"


def _existing_keys(database_path: str | Path, rows: list[dict[str, Any]]) -> set[str]:
    if not rows:
        return set()
    initialize_search_db(database_path)
    wanted = {listing_key(row) for row in rows}
    with closing(sqlite3.connect(database_path)) as connection:
        existing = connection.execute("SELECT listing_key FROM listings").fetchall()
    return wanted.intersection(row[0] for row in existing)


def run_scheduled_search(
    config_path: str | Path = SCHEDULE_PATH,
    *,
    search_database_path: str | Path = SEARCH_DATABASE_PATH,
    crm_database_path: str | Path = CRM_DATABASE_PATH,
    runs_database_path: str | Path = SCHEDULED_RUNS_PATH,
    search_fn=search_all,
) -> dict[str, Any] | None:
    config = local_schedule.load_config(config_path)
    if not config.get("enabled"):
        return None
    today = datetime.now().astimezone().date().isoformat()
    if config.get("last_run_date") == today:
        return None
    config["last_run_date"] = today
    local_schedule.save_config(config_path, config)

    locations = config.get("locations") or [""]
    search_config = SearchConfig(
        keyword=config["keyword"],
        locations=tuple(locations),
        offer_type=int(config.get("offer_type", 4)),
        radius_km=config.get("radius_km"),
        published_since_days=config.get("published_since_days"),
    )
    results_by_location = []
    all_rows: list[dict[str, Any]] = []
    statuses: list[str] = []
    try:
        for location in search_config.locations_or_nationwide():
            result = search_fn(
                search_config.keyword,
                location,
                angebotsart=search_config.offer_type,
                radius=search_config.radius_km,
                veroeffentlichtseit=search_config.published_since_days,
                log=lambda _message: None,
            )
            results_by_location.append((location, result))
            statuses.append(result.status)
            all_rows.extend(result.listings)
        existing_keys = _existing_keys(search_database_path, all_rows)
        new_listing_count = len({
            listing_key(row) for row in all_rows
        } - existing_keys)
        for location, result in results_by_location:
            save_run(search_database_path, {
                "keyword": search_config.keyword,
                "locations": [location],
                "offer_type": search_config.offer_type,
                "radius_km": search_config.radius_km,
                "published_since_days": search_config.published_since_days,
            }, result)
        crm_rows = []
        for row in all_rows:
            lead = dict(row)
            if not lead.get("source"):
                lead["source"] = "Jobbörse"
            crm_rows.append(lead)
        imported = crm.import_leads(
            crm_database_path,
            crm_rows,
            processing_purpose=config.get("processing_purpose", ""),
            legal_basis=config.get("legal_basis", ""),
            retention_days=config.get("retention_days", crm.DEFAULT_RETENTION_DAYS),
        )
        status = (
            "failed" if statuses and all(item == "failed" for item in statuses)
            else "complete" if statuses and all(item == "complete" for item in statuses)
            else "incomplete"
        )
        message = (
            f"{len(statuses)} Stadt-/Ortssuche(n); "
            f"{new_listing_count} neue Suchergebnisse; "
            f"{imported['inserted']} neue CRM-Leads, {imported['updated']} aktualisiert."
        )
        local_schedule.record_run(
            runs_database_path, status, len(all_rows), new_listing_count, message,
        )
        return {
            "status": status,
            "searched_count": len(all_rows),
            "new_count": new_listing_count,
            "message": message,
            "imported_count": imported["inserted"],
        }
    except Exception as exc:
        message = f"Scheduled search failed: {type(exc).__name__}: {exc}"
        local_schedule.record_run(runs_database_path, "failed", len(all_rows), 0, message)
        return {
            "status": "failed",
            "searched_count": len(all_rows),
            "new_count": 0,
            "message": message,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=SCHEDULE_PATH)
    args = parser.parse_args()
    result = run_scheduled_search(args.config)
    if result:
        print(f"{result['status']}: {result['message']}")
    else:
        print("No scheduled search is enabled or this date has already run.")


if __name__ == "__main__":
    main()
