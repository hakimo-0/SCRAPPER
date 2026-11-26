"""Opt-in Windows Task Scheduler integration for bounded Gmail application sends."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import subprocess
import sys
import time
import tomllib
from typing import Any

from . import applications, crm, google_workspace, local_schedule


TASK_NAME = "SCRAPPER-Google-Applications-Send"
_EMAIL_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)
DEFAULT_SCHEDULE = {
    "enabled": False,
    "schedule_time": "09:00",
    "spreadsheet_id": "",
    "sheet_name": "BEWERBUNG",
    "template_doc_id": "",
    "static_pdf_id": "",
    "sender_name": "",
    "recipients": [],
    "subject_template": "Bewerbung bei {COMPANY}",
    "message_template": "Guten Tag,\n\nanbei sende ich Ihnen meine Bewerbung.\n\nMit freundlichen Grüßen",
    "daily_limit": 10,
    "min_delay_minutes": 1,
    "max_delay_minutes": 3,
    "max_attempts": 3,
    "continue_on_error": True,
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
        raise ValueError(f"Unable to read Google send schedule: {exc}") from exc
    return {**DEFAULT_SCHEDULE, **data}


def save_config(path: str | Path, config: dict[str, Any]) -> None:
    config = {**DEFAULT_SCHEDULE, **config}
    config["enabled"] = bool(config["enabled"])
    try:
        datetime.strptime(str(config["schedule_time"]), "%H:%M")
        daily_limit = int(config["daily_limit"])
        min_delay = int(config["min_delay_minutes"])
        max_delay = int(config["max_delay_minutes"])
        max_attempts = int(config["max_attempts"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid Google send schedule settings.") from exc
    for key in ("spreadsheet_id", "template_doc_id", "static_pdf_id"):
        if not str(config[key]) or not all(
            char.isalnum() or char in "_-" for char in str(config[key])
        ):
            raise ValueError(f"A valid {key} is required for scheduled Gmail sending.")
    if daily_limit < 1 or daily_limit > 100:
        raise ValueError("Daily Gmail limit must be between 1 and 100.")
    if min_delay < 1 or max_delay < min_delay or max_delay > 1440:
        raise ValueError("Email delays must be between 1 and 1440 minutes.")
    if max_attempts < 1 or max_attempts > 5:
        raise ValueError("Maximum attempts must be between 1 and 5.")
    if not str(config["sender_name"]).strip():
        raise ValueError("A sender name is required.")
    if not isinstance(config["recipients"], list):
        raise ValueError("Scheduled recipients must be a list.")
    recipients = []
    seen_ids: set[int] = set()
    seen_emails: set[str] = set()
    for recipient in config["recipients"]:
        if not isinstance(recipient, dict):
            raise ValueError("Each scheduled recipient must include a lead ID and email.")
        try:
            lead_id = int(recipient["lead_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Each scheduled recipient must include a valid lead ID.") from exc
        email = str(recipient.get("email") or "").strip().casefold()
        if lead_id < 1 or not _EMAIL_RE.fullmatch(email):
            raise ValueError("Each scheduled recipient must include a valid CRM lead and email.")
        if lead_id in seen_ids or email in seen_emails:
            raise ValueError("Scheduled recipients must be unique.")
        seen_ids.add(lead_id)
        seen_emails.add(email)
        recipients.append({"lead_id": lead_id, "email": email})
    if config["enabled"] and not recipients:
        raise ValueError("Select at least one recipient before enabling scheduled sending.")
    config["recipients"] = recipients
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
    project_dir = Path(project_dir).resolve()
    config_path = Path(config_path).resolve()
    launcher_path = Path(launcher_path).resolve()
    python_executable = Path(python_executable or sys.executable).resolve()
    launcher_path.parent.mkdir(parents=True, exist_ok=True)
    launcher_path.write_text(
        "@echo off\r\n"
        f'cd /d "{project_dir}"\r\n'
        f'"{python_executable}" -m scraper.google_sender_schedule --config "{config_path}"\r\n',
        encoding="utf-8",
    )
    return launcher_path


def register_task(
    config_path: str | Path,
    launcher_path: str | Path,
    schedule_time: str,
) -> None:
    local_schedule.register_windows_task(
        config_path,
        launcher_path,
        schedule_time,
        task_name=TASK_NAME,
    )


def unregister_task() -> None:
    local_schedule.unregister_windows_task(TASK_NAME)


def _record_run(database_path: Path, status: str, sent: int, message: str) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS google_send_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                status TEXT NOT NULL,
                sent_count INTEGER NOT NULL,
                message TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            "INSERT INTO google_send_runs(started_at, status, sent_count, message) VALUES (?, ?, ?, ?)",
            (datetime.now().astimezone().isoformat(timespec="seconds"), status, sent, message[:500]),
        )
        connection.commit()


def recent_runs(database_path: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    database_path = Path(database_path)
    if not database_path.exists():
        return []
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, started_at, status, sent_count, message FROM google_send_runs ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    return [dict(row) for row in rows]


def _workspace_config(project_dir: Path) -> google_workspace.GoogleWorkspaceConfig:
    secrets_path = project_dir / ".streamlit" / "secrets.toml"
    values = {}
    if secrets_path.is_file():
        with secrets_path.open("rb") as file:
            values = tomllib.load(file)
    config = google_workspace.load_config(values, os.environ)
    if config is None:
        raise google_workspace.GoogleWorkspaceError(
            "Google OAuth credentials are not configured in .streamlit/secrets.toml or the environment."
        )
    return config


def _wait_or_stopped(config_path: Path, seconds: int) -> bool:
    deadline = time.monotonic() + max(0, seconds)
    while time.monotonic() < deadline:
        if not load_config(config_path).get("enabled"):
            return False
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    return bool(load_config(config_path).get("enabled"))


def run_scheduled_send(
    config_path: str | Path,
    *,
    project_dir: str | Path | None = None,
    crm_database_path: str | Path | None = None,
    runs_database_path: str | Path | None = None,
) -> dict[str, Any]:
    config_path = Path(config_path).resolve()
    project_dir = Path(project_dir or Path.cwd()).resolve()
    crm_path = Path(crm_database_path or project_dir / "output" / "crm.sqlite")
    runs_path = Path(runs_database_path or project_dir / "output" / "google_send_runs.sqlite")
    schedule = load_config(config_path)
    if not schedule["enabled"]:
        return {"status": "disabled", "sent": 0}
    today = datetime.now().astimezone().date().isoformat()
    if schedule.get("last_run_date") == today:
        return {"status": "already_ran_today", "sent": 0, "verify_required": 0, "errors": 0}
    save_config(config_path, schedule)
    recipient_snapshot = {
        int(item["lead_id"]): str(item["email"]).casefold()
        for item in schedule["recipients"]
    }

    sent_count = 0
    verify_count = 0
    error_count = 0
    sync_pending_count = 0
    daily_limit_reached = False
    last_error = ""

    def summary() -> str:
        message = (
            f"sent={sent_count}; verify_required={verify_count}; "
            f"errors={error_count}; sync_pending={sync_pending_count}"
        )
        return f"{message}; last_error={last_error[:300]}" if last_error else message

    try:
        workspace = _workspace_config(project_dir)
        restored = google_workspace.restore_persisted_session(workspace)
        if not restored:
            raise google_workspace.GoogleWorkspaceError(
                "No saved Google account is connected; sign in in the dashboard first."
            )
        tokens, sender_email = restored
        workspace = google_workspace.GoogleWorkspaceConfig(
            client_id=workspace.client_id,
            client_secret=workspace.client_secret,
            redirect_uri=workspace.redirect_uri,
            spreadsheet_id=str(schedule["spreadsheet_id"]),
            sheet_name=str(schedule["sheet_name"]),
            template_doc_id=str(schedule["template_doc_id"]),
            static_pdf_id=str(schedule["static_pdf_id"]),
            sender_name=str(schedule["sender_name"]),
        )
        schedule["last_run_date"] = today
        save_config(config_path, schedule)
        leads = [
            lead for lead in crm.list_leads(crm_path, limit=5000)
            if (
                recipient_snapshot.get(int(lead["id"])) == str(lead["email"]).casefold()
                and lead["status"] != "opt_out"
                and lead["email"].strip()
                and lead["firma"].strip()
            )
        ]
        matched_recipient_ids = {int(lead["id"]) for lead in leads}
        changed_or_unavailable = set(recipient_snapshot) - matched_recipient_ids
        if changed_or_unavailable:
            error_count += len(changed_or_unavailable)
            last_error = (
                "One or more confirmed recipients were changed, deleted, or opted out; skipped."
            )
        rows = applications.build_sheet_rows(
            leads,
            [int(lead["id"]) for lead in leads],
            subject_template=str(schedule["subject_template"]),
            message_template=str(schedule["message_template"]),
        )
        lead_by_email = {str(lead["email"]).casefold(): lead for lead in leads}
        result = google_workspace.sync_rows_to_sheet(
            workspace, tokens, workspace.spreadsheet_id, workspace.sheet_name, rows,
        )
        for sent_row in result.get("sent_rows", []):
            ledger = crm.google_send_ledger(crm_path, sent_row["email"])
            if ledger and ledger["state"] == "SENT":
                lead = lead_by_email.get(str(sent_row["email"]).casefold())
                crm.record_confirmed_google_contact(
                    crm_path,
                    ledger.get("lead_id") or (int(lead["id"]) if lead else None),
                    str(ledger.get("message_id") or ""),
                    str(ledger.get("sent_at") or ""),
                )
        for ready_index, ready in enumerate(result["ready_rows"]):
            if not load_config(config_path).get("enabled"):
                _record_run(runs_path, "stopped", sent_count, summary())
                return {
                    "status": "stopped", "sent": sent_count,
                    "verify_required": verify_count, "errors": error_count,
                    "sync_pending": sync_pending_count,
                }
            daily_count = crm.google_sends_today(crm_path, today)
            if daily_count >= int(schedule["daily_limit"]):
                daily_limit_reached = True
                break
            email = str(ready["email"]).casefold()
            lead = lead_by_email.get(email)
            ledger = crm.google_send_ledger(crm_path, email)
            if not ledger and ready.get("status") in {"SENDING", "VERIFY_REQUIRED"}:
                verify_count += 1
                google_workspace.update_sheet_status(
                    workspace, tokens, workspace.spreadsheet_id, workspace.sheet_name,
                    int(ready["row_number"]), "VERIFY_REQUIRED", "",
                )
                continue
            if ledger and ledger["state"] == "SENT":
                crm.record_confirmed_google_contact(
                    crm_path,
                    ledger.get("lead_id") or (int(lead["id"]) if lead else None),
                    str(ledger.get("message_id") or ""),
                    str(ledger.get("sent_at") or ""),
                )
                try:
                    google_workspace.update_sheet_status(
                        workspace, tokens, workspace.spreadsheet_id, workspace.sheet_name,
                        int(ready["row_number"]), "SENT", str(ledger.get("sent_at") or ""),
                    )
                except google_workspace.GoogleWorkspaceError as exc:
                    sync_pending_count += 1
                    last_error = str(exc)
                    if not schedule["continue_on_error"]:
                        break
                continue
            if ledger and ledger["state"] in {"SENDING", "VERIFY_REQUIRED", "ERROR"}:
                if ledger["state"] == "ERROR":
                    error_count += 1
                else:
                    verify_count += 1
                last_error = str(ledger.get("last_error") or "")
                google_workspace.update_sheet_status(
                    workspace, tokens, workspace.spreadsheet_id, workspace.sheet_name,
                    int(ready["row_number"]), ledger["state"], "",
                )
                continue
            attempts = crm.reserve_google_send(
                crm_path, email,
                lead_id=int(lead["id"]) if lead else None,
                spreadsheet_id=workspace.spreadsheet_id,
                sheet_row=int(ready["row_number"]),
                max_attempts=int(schedule["max_attempts"]),
            )
            if attempts is None:
                continue
            row_error = False
            while True:
                mail_confirmed = False
                try:
                    google_workspace.update_sheet_status(
                        workspace, tokens, workspace.spreadsheet_id, workspace.sheet_name,
                        int(ready["row_number"]), "SENDING", "",
                    )
                    salutation, person = applications._person_fields(
                        (lead or {}).get("person"), (lead or {}).get("anrede"),
                    )
                    greeting = "Sehr geehrte Damen und Herren,"
                    if person and salutation == "Herr":
                        greeting = f"Sehr geehrter Herr {person},"
                    elif person and salutation == "Frau":
                        greeting = f"Sehr geehrte Frau {person},"
                    elif person:
                        greeting = f"Sehr geehrte/r Frau/Herr {person},"
                    company = str((lead or {}).get("firma") or "")
                    subject = str(schedule["subject_template"]).replace("{COMPANY}", company).replace("{PERSON}", person)
                    body = (
                        str(schedule["message_template"]).replace("{SALUTATION}", greeting)
                        .replace("{COMPANY}", company).replace("{PERSON}", person)
                    )
                    cover = google_workspace.personalized_cover_pdf(
                        workspace, tokens, company=company, salutation=greeting,
                        today=datetime.now().strftime("%d.%m.%Y"),
                        person=workspace.sender_name,
                    )
                    dossier = google_workspace._drive_file_bytes(
                        workspace, tokens, workspace.static_pdf_id,
                    )
                    message_id = google_workspace.send_gmail(
                        workspace, tokens, sender_email=sender_email, to=email,
                        subject=subject, body=body,
                        attachments=[("Anschreiben.pdf", cover), ("Lebenslauf_und_Anlagen.pdf", dossier)],
                        sender_name=workspace.sender_name,
                    )
                    mail_confirmed = True
                    sent_at = datetime.now().astimezone().isoformat(timespec="seconds")
                    crm.record_google_send(
                        crm_path, email, message_id, sent_at,
                        lead_id=int(lead["id"]) if lead else None,
                        spreadsheet_id=workspace.spreadsheet_id,
                        sheet_row=int(ready["row_number"]),
                    )
                    sent_count += 1
                    crm.record_confirmed_google_contact(
                        crm_path,
                        int(lead["id"]) if lead else None,
                        message_id,
                        sent_at,
                    )
                    google_workspace.update_sheet_status(
                        workspace, tokens, workspace.spreadsheet_id, workspace.sheet_name,
                        int(ready["row_number"]), "SENT", sent_at,
                    )
                    break
                except google_workspace.GoogleWorkspaceError as exc:
                    if mail_confirmed:
                        ledger_after_send = crm.google_send_ledger(crm_path, email)
                        if ledger_after_send and ledger_after_send["state"] == "SENT":
                            crm.record_confirmed_google_contact(
                                crm_path,
                                ledger_after_send.get("lead_id") or (
                                    int(lead["id"]) if lead else None
                                ),
                                str(ledger_after_send.get("message_id") or ""),
                                str(ledger_after_send.get("sent_at") or ""),
                            )
                            try:
                                google_workspace.update_sheet_status(
                                    workspace, tokens, workspace.spreadsheet_id,
                                    workspace.sheet_name, int(ready["row_number"]),
                                    "SENT", str(ledger_after_send.get("sent_at") or ""),
                                )
                            except google_workspace.GoogleWorkspaceError:
                                sync_pending_count += 1
                                last_error = str(exc)
                        else:
                            crm.update_google_send_state(
                                crm_path, email, "VERIFY_REQUIRED", last_error=str(exc),
                            )
                            verify_count += 1
                            last_error = str(exc)
                        row_error = True
                        break
                    if exc.ambiguous:
                        verify_count += 1
                        last_error = str(exc)
                        crm.update_google_send_state(
                            crm_path, email, "VERIFY_REQUIRED", last_error=str(exc),
                        )
                        google_workspace.update_sheet_status(
                            workspace, tokens, workspace.spreadsheet_id, workspace.sheet_name,
                            int(ready["row_number"]), "VERIFY_REQUIRED", "",
                        )
                        row_error = True
                        break
                    if exc.transient and attempts < int(schedule["max_attempts"]):
                        delay = google_workspace.retry_delay_seconds(attempts)
                        next_retry = datetime.fromtimestamp(
                            datetime.now().timestamp() + delay,
                        ).astimezone().isoformat(timespec="seconds")
                        crm.update_google_send_state(
                            crm_path, email, "RETRY", last_error=str(exc),
                            next_retry_at=next_retry,
                        )
                        google_workspace.update_sheet_status(
                            workspace, tokens, workspace.spreadsheet_id, workspace.sheet_name,
                            int(ready["row_number"]), "RETRY", "",
                        )
                        if not _wait_or_stopped(config_path, delay):
                            return {"status": "stopped", "sent": sent_count}
                        attempts = crm.reserve_google_send(
                            crm_path, email,
                            lead_id=int(lead["id"]) if lead else None,
                            spreadsheet_id=workspace.spreadsheet_id,
                            sheet_row=int(ready["row_number"]),
                            max_attempts=int(schedule["max_attempts"]),
                        )
                        if attempts is None:
                            break
                        continue
                    crm.update_google_send_state(
                        crm_path, email, "ERROR", last_error=str(exc),
                    )
                    error_count += 1
                    last_error = str(exc)
                    google_workspace.update_sheet_status(
                        workspace, tokens, workspace.spreadsheet_id, workspace.sheet_name,
                        int(ready["row_number"]), "ERROR", "",
                    )
                    if not schedule["continue_on_error"]:
                        row_error = True
                        break
                    row_error = True
                    break
                except sqlite3.Error as exc:
                    last_error = str(exc)
                    ledger_after_send = crm.google_send_ledger(crm_path, email)
                    if not (mail_confirmed and ledger_after_send and ledger_after_send["state"] == "SENT"):
                        crm.update_google_send_state(
                            crm_path, email, "VERIFY_REQUIRED", last_error=str(exc),
                        )
                        verify_count += 1
                    else:
                        sync_pending_count += 1
                    row_error = True
                    break
            if row_error and not schedule["continue_on_error"]:
                break
            if sent_count >= int(schedule["daily_limit"]):
                daily_limit_reached = ready_index + 1 < len(result["ready_rows"])
                break
            if ready_index + 1 >= len(result["ready_rows"]):
                break
            delay_minutes = random.randint(
                int(schedule["min_delay_minutes"]),
                int(schedule["max_delay_minutes"]),
            )
            if not _wait_or_stopped(config_path, delay_minutes * 60):
                _record_run(runs_path, "stopped", sent_count, summary())
                return {
                    "status": "stopped", "sent": sent_count,
                    "verify_required": verify_count, "errors": error_count,
                    "sync_pending": sync_pending_count,
                }
        status = (
            "needs_review" if verify_count or error_count
            else "sync_pending" if sync_pending_count
            else "daily_limit" if daily_limit_reached
            else "complete"
        )
        _record_run(runs_path, status, sent_count, summary())
        return {
            "status": status, "sent": sent_count,
            "verify_required": verify_count, "errors": error_count,
            "sync_pending": sync_pending_count,
        }
    except (google_workspace.GoogleWorkspaceError, OSError, ValueError, sqlite3.Error) as exc:
        _record_run(runs_path, "error", sent_count, str(exc))
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the configured scheduled Google application sender.")
    parser.add_argument("--config", required=True, help="Local schedule configuration JSON.")
    args = parser.parse_args(argv)
    try:
        result = run_scheduled_send(args.config)
        print(
            f"Google scheduled send: {result['status']} "
            f"({result['sent']} sent, {result.get('verify_required', 0)} to verify, "
            f"{result.get('errors', 0)} errors, {result.get('sync_pending', 0)} sync pending)"
        )
        return 0
    except (google_workspace.GoogleWorkspaceError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"Google scheduled send failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
