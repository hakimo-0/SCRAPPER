"""Explicit Windows Task Scheduler integration for recurring local searches."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
from typing import Any


TASK_NAME = "SCRAPPER-Jobboerse-Search"
DEFAULT_SCHEDULE = {
    "enabled": False,
    "source": "arbeitsagentur",
    "keyword": "",
    "locations": [],
    "offer_type": 4,
    "radius_km": None,
    "published_since_days": None,
    "target_year": "Alle",
    "schedule_time": "09:00",
    "processing_purpose": "",
    "legal_basis": "",
    "retention_days": 90,
    "last_run_date": "",
}


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return dict(DEFAULT_SCHEDULE)
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read scheduled-search configuration: {exc}") from exc
    return {**DEFAULT_SCHEDULE, **data}


def save_config(path: str | Path, config: dict[str, Any]) -> None:
    config = {**DEFAULT_SCHEDULE, **config}
    if config["source"] != "arbeitsagentur":
        raise ValueError("Scheduled search currently supports only the official Jobbörse connector.")
    if not str(config["keyword"]).strip():
        raise ValueError("A scheduled Jobbörse search requires a keyword.")
    if not isinstance(config["locations"], list):
        raise ValueError("Scheduled search locations must be a list.")
    try:
        datetime.strptime(str(config["schedule_time"]), "%H:%M")
    except ValueError as exc:
        raise ValueError("Schedule time must use HH:MM in local time.") from exc
    config["enabled"] = bool(config["enabled"])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def write_launcher(
    config_path: str | Path,
    launcher_path: str | Path,
    *,
    project_dir: str | Path,
    python_executable: str | Path | None = None,
) -> Path:
    """Write a quoted user-level command file with an explicit project cwd."""
    project_dir = Path(project_dir).resolve()
    config_path = Path(config_path).resolve()
    launcher_path = Path(launcher_path).resolve()
    python_executable = Path(python_executable or sys.executable).resolve()
    launcher_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "@echo off\r\n"
        f'cd /d "{project_dir}"\r\n'
        f'"{python_executable}" -m scraper.scheduled_search --config "{config_path}"\r\n'
    )
    launcher_path.write_text(content, encoding="utf-8")
    return launcher_path


def register_windows_task(
    config_path: str | Path,
    launcher_path: str | Path,
    schedule_time: str,
    *,
    task_name: str = TASK_NAME,
) -> None:
    if os.name != "nt":
        raise RuntimeError("Windows Task Scheduler is only available on Windows.")
    try:
        datetime.strptime(schedule_time, "%H:%M")
    except ValueError as exc:
        raise ValueError("Schedule time must use HH:MM in local time.") from exc
    if not Path(config_path).is_file():
        raise FileNotFoundError(f"Scheduled-search configuration not found: {config_path}")
    if not Path(launcher_path).is_file():
        raise FileNotFoundError(f"Scheduled-search launcher not found: {launcher_path}")
    if not task_name or any(char in task_name for char in '\\/:*?"<>|'):
        raise ValueError("Invalid Windows Task Scheduler task name.")
    command = [
        "schtasks.exe", "/Create", "/SC", "DAILY", "/ST", schedule_time,
        "/TN", task_name, "/TR", f'"{Path(launcher_path).resolve()}"',
        "/F", "/IT",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        message = (result.stderr or result.stdout or "Task Scheduler rejected the task.").strip()
        raise RuntimeError(message)


def unregister_windows_task(task_name: str = TASK_NAME) -> None:
    if os.name != "nt":
        raise RuntimeError("Windows Task Scheduler is only available on Windows.")
    if not task_name or any(char in task_name for char in '\\/:*?"<>|'):
        raise ValueError("Invalid Windows Task Scheduler task name.")
    result = subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode and "cannot find" not in (result.stderr + result.stdout).casefold():
        raise RuntimeError((result.stderr or result.stdout or "Unable to remove scheduled task.").strip())


def _initialize_runs(database_path: str | Path) -> None:
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                status TEXT NOT NULL,
                searched_count INTEGER NOT NULL DEFAULT 0,
                new_count INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.commit()


def record_run(database_path: str | Path, status: str, searched_count: int, new_count: int, message: str) -> None:
    _initialize_runs(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO scheduled_runs(started_at, status, searched_count, new_count, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                datetime.now().astimezone().isoformat(timespec="seconds"),
                status,
                max(0, int(searched_count)),
                max(0, int(new_count)),
                message,
            ),
        )
        connection.commit()


def recent_runs(database_path: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    _initialize_runs(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM scheduled_runs ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    return [dict(row) for row in rows]
