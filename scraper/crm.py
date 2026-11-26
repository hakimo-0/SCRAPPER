"""Local SQLite CRM for imported leads and privacy requests."""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import re
import secrets
from pathlib import Path
import sqlite3
from typing import Any

from .leads import normalise_firma, valid_emails


LEGACY_STATUSES = ("new", "contacted", "replied", "follow_up", "closed", "opt_out")
PIPELINE_STATUSES = (
    "new", "sent", "replied", "interview", "rejected", "closed", "follow_up", "opt_out",
)
STATUSES = tuple(dict.fromkeys((*LEGACY_STATUSES, *PIPELINE_STATUSES)))
ACTIVITY_TYPES = ("note", "call", "email", "meeting")
DEFAULT_RETENTION_DAYS = 90
FOLLOW_UP_SCHEDULES = (3, 7, 14)
DEFAULT_FOLLOW_UP_DAYS = 3
SCORE_WEIGHTS = {
    "email": 25,
    "person": 10,
    "phone": 10,
    "source": 15,
    "city": 15,
    "recentness": 15,
    "workflow": 10,
}
_EMAIL_RE = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$", re.I)
_PIPELINE_STATUS_CHECK = "'new', 'sent', 'replied', 'interview', 'rejected', 'closed', 'follow_up', 'opt_out', 'contacted'"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def normalize_phone(value: Any) -> str:
    """Keep plausible international numbers; do not guess or rewrite country codes."""
    phone = " ".join(str(value or "").split()).strip()
    if phone and not re.fullmatch(r"[0-9+()\s./-]+", phone):
        return ""
    digits = re.sub(r"\D", "", phone)
    if not 7 <= len(digits) <= 15:
        return ""
    return phone


def _normalized_email(value: Any) -> str:
    return next(
        (email for email in valid_emails(value) if _EMAIL_RE.fullmatch(email)),
        "",
    )


def initialize(database_path: str | Path) -> None:
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS crm_leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_key TEXT NOT NULL UNIQUE,
                firma TEXT NOT NULL,
                person TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                telefonnummer TEXT NOT NULL DEFAULT '',
                adresse TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                website TEXT NOT NULL DEFAULT '',
                processing_purpose TEXT NOT NULL DEFAULT '',
                legal_basis TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new'
                    CHECK(status IN ('new', 'contacted', 'replied', 'follow_up', 'closed', 'opt_out')),
                follow_up_at TEXT,
                notes TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                retention_until TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS crm_activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL REFERENCES crm_leads(id) ON DELETE CASCADE,
                activity_type TEXT NOT NULL CHECK(activity_type IN ('note', 'call', 'email', 'meeting')),
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_crm_activities_lead
                ON crm_activities(lead_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS crm_gdpr_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference_hash TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS crm_suppression (
                email_hash TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS crm_exports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                export_type TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS crm_google_sends (
                email_hash TEXT PRIMARY KEY,
                lead_id INTEGER REFERENCES crm_leads(id) ON DELETE CASCADE,
                message_id TEXT UNIQUE,
                state TEXT NOT NULL CHECK(state IN ('SENDING', 'RETRY', 'SENT', 'ERROR', 'VERIFY_REQUIRED')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                sent_at TEXT,
                spreadsheet_id TEXT NOT NULL DEFAULT '',
                sheet_row INTEGER
            );
            CREATE TABLE IF NOT EXISTS crm_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(crm_leads)")}
        if "pipeline_status" not in columns:
            connection.execute(
                f"ALTER TABLE crm_leads ADD COLUMN pipeline_status TEXT NOT NULL DEFAULT '' "
                f"CHECK(pipeline_status IN ('', {_PIPELINE_STATUS_CHECK}))"
            )
        connection.execute(
            "INSERT OR IGNORE INTO crm_settings(setting_key, setting_value) VALUES ('hash_salt', ?)",
            (secrets.token_hex(32),),
        )
        connection.execute(
            "INSERT OR IGNORE INTO crm_settings(setting_key, setting_value) VALUES ('retention_days', ?)",
            (str(DEFAULT_RETENTION_DAYS),),
        )
        connection.commit()


def _connect(database_path: str | Path) -> sqlite3.Connection:
    initialize(database_path)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _hash(value: str, salt: str = "") -> str:
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()


def _record_key(row: dict[str, Any], company: str, email: str, source: str) -> str:
    url = str(row.get("link") or row.get("url") or row.get("source_url") or "").strip().lower()
    if url:
        identity = f"url:{url}"
    else:
        address = str(row.get("adresse") or row.get("address") or "").strip().casefold()
        city = str(row.get("city") or "").strip().casefold()
        identity = f"company:{company.casefold()}|email:{email}|address:{address}|city:{city}"
    return _hash(f"{source.casefold()}|{identity}")


def _to_record(row: dict[str, Any]) -> dict[str, str]:
    company = normalise_firma(row.get("firma") or row.get("name") or row.get("employer"))
    email = _normalized_email(row.get("emails") or row.get("email"))
    source = str(row.get("source") or row.get("source_id") or "").strip()
    person = str(row.get("person") or "").strip()
    if person.casefold() in ("damen und herren", "n/a"):
        person = ""
    return {
        "firma": company,
        "person": person,
        "email": email,
        "telefonnummer": normalize_phone(row.get("telefonnummer") or row.get("phone")),
        "adresse": str(row.get("adresse") or row.get("address") or "").strip(),
        "city": str(row.get("city") or "").strip(),
        "source": source,
        "source_url": str(row.get("link") or row.get("url") or row.get("source_url") or "").strip(),
        "website": str(row.get("website") or "").strip(),
    }


def _suppression_hash(email: str, salt: str) -> str:
    return _hash(email.casefold(), salt)


def import_leads(
    database_path: str | Path,
    rows: list[dict[str, Any]],
    *,
    processing_purpose: str = "",
    legal_basis: str = "",
    retention_days: int | None = None,
) -> dict[str, int]:
    """Import and deduplicate rows without replacing CRM-owned workflow fields."""
    counts = {"inserted": 0, "updated": 0, "suppressed": 0, "skipped": 0}
    with closing(_connect(database_path)) as connection:
        salt = connection.execute(
            "SELECT setting_value FROM crm_settings WHERE setting_key = 'hash_salt'"
        ).fetchone()[0]
        days = max(1, int(retention_days or get_retention_days(database_path)))
        now = _now()
        now_text = _timestamp(now)
        expires = _timestamp(now + timedelta(days=days))
        for row in rows:
            data = _to_record(dict(row or {}))
            if not data["firma"]:
                counts["skipped"] += 1
                continue
            if data["email"] and connection.execute(
                "SELECT 1 FROM crm_suppression WHERE email_hash = ?",
                (_suppression_hash(data["email"], salt),),
            ).fetchone():
                counts["suppressed"] += 1
                continue
            key = _record_key(dict(row), data["firma"], data["email"], data["source"])
            existing = connection.execute(
                "SELECT id, status FROM crm_leads WHERE lead_key = ?", (key,)
            ).fetchone()
            if existing:
                if existing["status"] == "opt_out":
                    counts["suppressed"] += 1
                    continue
                connection.execute(
                    """
                    UPDATE crm_leads SET
                        person = CASE WHEN ? != '' THEN ? ELSE person END,
                        email = CASE WHEN ? != '' THEN ? ELSE email END,
                        telefonnummer = CASE WHEN ? != '' THEN ? ELSE telefonnummer END,
                        adresse = CASE WHEN ? != '' THEN ? ELSE adresse END,
                        city = CASE WHEN ? != '' THEN ? ELSE city END,
                        source_url = CASE WHEN ? != '' THEN ? ELSE source_url END,
                        website = CASE WHEN ? != '' THEN ? ELSE website END,
                        processing_purpose = CASE WHEN ? != '' THEN ? ELSE processing_purpose END,
                        legal_basis = CASE WHEN ? != '' THEN ? ELSE legal_basis END,
                        last_seen_at = ?, updated_at = ?, retention_until = ?
                    WHERE id = ?
                    """,
                    (
                        data["person"], data["person"], data["email"], data["email"],
                        data["telefonnummer"], data["telefonnummer"], data["adresse"], data["adresse"],
                        data["city"], data["city"], data["source_url"], data["source_url"],
                        data["website"], data["website"], processing_purpose, processing_purpose,
                        legal_basis, legal_basis, now_text, now_text, expires, existing["id"],
                    ),
                )
                counts["updated"] += 1
                continue
            connection.execute(
                """
                INSERT INTO crm_leads (
                    lead_key, firma, person, email, telefonnummer, adresse, city, source,
                    source_url, website, processing_purpose, legal_basis, first_seen_at,
                    last_seen_at, updated_at, retention_until
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key, data["firma"], data["person"], data["email"], data["telefonnummer"],
                    data["adresse"], data["city"], data["source"], data["source_url"],
                    data["website"], processing_purpose, legal_basis, now_text, now_text,
                    now_text, expires,
                ),
            )
            counts["inserted"] += 1
        connection.commit()
    return counts


def list_leads(
    database_path: str | Path,
    *,
    status: str | None = None,
    query: str = "",
    limit: int = 1000,
) -> list[dict[str, Any]]:
    with closing(_connect(database_path)) as connection:
        clauses = []
        args: list[Any] = []
        if status and status != "Alle":
            if status not in STATUSES:
                raise ValueError(f"Unknown CRM status: {status}")
            clauses.append("(COALESCE(NULLIF(pipeline_status, ''), status) = ? OR status = ?)")
            args.extend([status, status])
        if query.strip():
            clauses.append("(firma LIKE ? OR person LIKE ? OR email LIKE ? OR telefonnummer LIKE ? OR city LIKE ? OR source LIKE ?)")
            match = f"%{query.strip()}%"
            args.extend([match] * 6)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            f"SELECT * FROM crm_leads {where} ORDER BY updated_at DESC LIMIT ?",
            (*args, max(1, min(int(limit), 5000))),
        ).fetchall()
        target_city_row = connection.execute(
            "SELECT setting_value FROM crm_settings WHERE setting_key = 'target_city'"
        ).fetchone()
    target_city = str(target_city_row[0]) if target_city_row else ""
    records = [dict(row) for row in rows]
    for record in records:
        record["status"] = record.get("pipeline_status") or record["status"]
        record["score_breakdown"] = score_breakdown(record, target_city=target_city)
        record["lead_score"] = record["score_breakdown"]["total"]
    return records


def update_lead(
    database_path: str | Path,
    lead_id: int,
    *,
    status: str,
    follow_up_at: str = "",
    notes: str = "",
) -> None:
    if status not in STATUSES:
        raise ValueError(f"Unknown CRM status: {status}")
    if status == "opt_out":
        raise ValueError("Use opt_out_lead to record and scrub an opt-out.")
    if follow_up_at:
        try:
            date.fromisoformat(follow_up_at)
        except ValueError as exc:
            raise ValueError("Follow-up date must use YYYY-MM-DD.") from exc
    legacy_status = {
        "sent": "contacted",
        "interview": "replied",
        "rejected": "closed",
    }.get(status, status)
    if status in {"sent", "contacted"} and not follow_up_at:
        follow_up_at = (_now().date() + timedelta(days=get_follow_up_days(database_path))).isoformat()
    with closing(_connect(database_path)) as connection:
        cursor = connection.execute(
            """
            UPDATE crm_leads SET status = ?, pipeline_status = ?, follow_up_at = ?, notes = ?, updated_at = ?
            WHERE id = ? AND status != 'opt_out'
            """,
            (legacy_status, status, follow_up_at or "", notes or "", _timestamp(), int(lead_id)),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"CRM lead {lead_id} does not exist or is opted out.")
        connection.commit()


def add_activity(
    database_path: str | Path,
    lead_id: int,
    activity_type: str,
    details: str,
) -> int:
    if activity_type not in ACTIVITY_TYPES:
        raise ValueError(f"Unknown activity type: {activity_type}")
    if not details.strip():
        raise ValueError("Activity details cannot be empty.")
    with closing(_connect(database_path)) as connection:
        if not connection.execute(
            "SELECT 1 FROM crm_leads WHERE id = ? AND status != 'opt_out'", (int(lead_id),)
        ).fetchone():
            raise ValueError(f"CRM lead {lead_id} does not exist or is opted out.")
        cursor = connection.execute(
            "INSERT INTO crm_activities(lead_id, activity_type, details, created_at) VALUES (?, ?, ?, ?)",
            (int(lead_id), activity_type, details.strip(), _timestamp()),
        )
        connection.commit()
        return int(cursor.lastrowid)


def list_activities(database_path: str | Path, lead_id: int) -> list[dict[str, Any]]:
    with closing(_connect(database_path)) as connection:
        rows = connection.execute(
            """
            SELECT id, lead_id, activity_type, details, created_at
            FROM crm_activities WHERE lead_id = ? ORDER BY created_at DESC, id DESC
            """,
            (int(lead_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def record_google_send(
    database_path: str | Path,
    email: str,
    message_id: str,
    sent_at: str,
    *,
    lead_id: int | None,
    spreadsheet_id: str,
    sheet_row: int,
) -> bool:
    """Persist a confirmed Gmail message ID before updating remote sheet status."""
    normalized = _normalized_email(email)
    if not normalized or not message_id.strip() or sheet_row < 2:
        raise ValueError("A valid recipient, Gmail message ID, and sheet row are required.")
    with closing(_connect(database_path)) as connection:
        salt = connection.execute(
            "SELECT setting_value FROM crm_settings WHERE setting_key = 'hash_salt'"
        ).fetchone()["setting_value"]
        cursor = connection.execute(
            """
            INSERT INTO crm_google_sends
            (email_hash, lead_id, message_id, state, sent_at, spreadsheet_id, sheet_row)
            VALUES (?, ?, ?, 'SENT', ?, ?, ?)
            ON CONFLICT(email_hash) DO UPDATE SET
                message_id = excluded.message_id,
                state = 'SENT',
                sent_at = excluded.sent_at,
                next_retry_at = NULL,
                last_error = ''
            WHERE crm_google_sends.state != 'SENT'
            """,
            (
                _hash(normalized, salt),
                int(lead_id) if lead_id is not None else None,
                message_id.strip(),
                sent_at,
                spreadsheet_id,
                int(sheet_row),
            ),
        )
        if cursor.rowcount != 1:
            return None
        connection.commit()
        return cursor.rowcount == 1


def google_send_ledger(database_path: str | Path, email: str) -> dict[str, Any] | None:
    normalized = _normalized_email(email)
    if not normalized:
        return None
    with closing(_connect(database_path)) as connection:
        salt = connection.execute(
            "SELECT setting_value FROM crm_settings WHERE setting_key = 'hash_salt'"
        ).fetchone()["setting_value"]
        row = connection.execute(
            """
            SELECT lead_id, message_id, state, attempt_count, next_retry_at,
                   last_error, sent_at, spreadsheet_id, sheet_row
            FROM crm_google_sends WHERE email_hash = ?
            """,
            (_hash(normalized, salt),),
        ).fetchone()
    return dict(row) if row else None


def record_confirmed_google_contact(
    database_path: str | Path,
    lead_id: int | None,
    message_id: str,
    sent_at: str,
) -> bool:
    """Idempotently mark a lead contacted after a Gmail message is confirmed."""
    if lead_id is None or not message_id.strip() or not sent_at.strip():
        return False
    marker = f"message_id={message_id.strip()}"
    with closing(_connect(database_path)) as connection:
        cursor = connection.execute(
            """
            UPDATE crm_leads
            SET status = 'contacted', pipeline_status = '', follow_up_at = '', updated_at = ?
            WHERE id = ? AND status != 'opt_out'
            """,
            (sent_at, int(lead_id)),
        )
        if cursor.rowcount != 1:
            return False
        exists = connection.execute(
            "SELECT 1 FROM crm_activities WHERE lead_id = ? AND instr(details, ?) > 0",
            (int(lead_id), marker),
        ).fetchone()
        if not exists:
            connection.execute(
                """
                INSERT INTO crm_activities(lead_id, activity_type, details, created_at)
                VALUES (?, 'email', ?, ?)
                """,
                (int(lead_id), f"Google Gmail application sent at {sent_at}; {marker}", sent_at),
            )
        connection.commit()
    return True


def google_sends_today(database_path: str | Path, local_date: str) -> int:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", local_date):
        raise ValueError("Date must use YYYY-MM-DD.")
    with closing(_connect(database_path)) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count FROM crm_google_sends
            WHERE state = 'SENT' AND substr(sent_at, 1, 10) = ?
            """,
            (local_date,),
        ).fetchone()
    return int(row["count"])


def reserve_google_send(
    database_path: str | Path,
    email: str,
    *,
    lead_id: int | None,
    spreadsheet_id: str,
    sheet_row: int,
    max_attempts: int = 3,
) -> int | None:
    """Atomically claim a recipient row so concurrent sessions cannot double-send."""
    normalized = _normalized_email(email)
    if not normalized or sheet_row < 2 or max_attempts < 1:
        raise ValueError("A valid recipient, sheet row, and retry limit are required.")
    now = _timestamp()
    with closing(_connect(database_path)) as connection:
        salt = connection.execute(
            "SELECT setting_value FROM crm_settings WHERE setting_key = 'hash_salt'"
        ).fetchone()["setting_value"]
        email_hash = _hash(normalized, salt)
        row = connection.execute(
            "SELECT state, attempt_count, next_retry_at FROM crm_google_sends WHERE email_hash = ?",
            (email_hash,),
        ).fetchone()
        if row:
            if row["state"] != "RETRY" or int(row["attempt_count"]) >= max_attempts:
                return None
            if row["next_retry_at"] and row["next_retry_at"] > now:
                return None
            attempt_count = int(row["attempt_count"]) + 1
            cursor = connection.execute(
                """
                UPDATE crm_google_sends
                SET state = 'SENDING', attempt_count = ?, next_retry_at = NULL,
                    last_error = '', lead_id = ?, spreadsheet_id = ?, sheet_row = ?
                WHERE email_hash = ? AND state = 'RETRY'
                """,
                (
                    attempt_count,
                    int(lead_id) if lead_id is not None else None,
                    spreadsheet_id,
                    int(sheet_row),
                    email_hash,
                ),
            )
            if cursor.rowcount != 1:
                return None
        else:
            attempt_count = 1
            cursor = connection.execute(
                """
                INSERT INTO crm_google_sends
                (email_hash, lead_id, state, attempt_count, spreadsheet_id, sheet_row)
                VALUES (?, ?, 'SENDING', ?, ?, ?)
                """,
                (
                    email_hash,
                    int(lead_id) if lead_id is not None else None,
                    attempt_count,
                    spreadsheet_id,
                    int(sheet_row),
                ),
            )
        connection.commit()
        return attempt_count


def update_google_send_state(
    database_path: str | Path,
    email: str,
    state: str,
    *,
    last_error: str = "",
    next_retry_at: str | None = None,
) -> None:
    if state not in {"SENDING", "RETRY", "SENT", "ERROR", "VERIFY_REQUIRED"}:
        raise ValueError("Invalid Google send state.")
    normalized = _normalized_email(email)
    if not normalized:
        raise ValueError("A valid recipient email is required.")
    with closing(_connect(database_path)) as connection:
        salt = connection.execute(
            "SELECT setting_value FROM crm_settings WHERE setting_key = 'hash_salt'"
        ).fetchone()["setting_value"]
        cursor = connection.execute(
            """
            UPDATE crm_google_sends
            SET state = ?, last_error = ?, next_retry_at = ?
            WHERE email_hash = ?
            """,
            (state, last_error[:500], next_retry_at, _hash(normalized, salt)),
        )
        if cursor.rowcount != 1:
            raise LookupError("No Google send attempt exists for this recipient.")
        connection.commit()


def _action_hash(lead_key: str) -> str:
    return _hash(lead_key, "gdpr-action")


def opt_out_lead(database_path: str | Path, lead_id: int, reason: str = "") -> None:
    """Record an opt-out, scrub contact data, and retain a salted suppression hash."""
    with closing(_connect(database_path)) as connection:
        row = connection.execute(
            "SELECT lead_key, email FROM crm_leads WHERE id = ?", (int(lead_id),)
        ).fetchone()
        if not row:
            raise ValueError(f"CRM lead {lead_id} does not exist.")
        salt = connection.execute(
            "SELECT setting_value FROM crm_settings WHERE setting_key = 'hash_salt'"
        ).fetchone()[0]
        if row["email"]:
            connection.execute(
                "INSERT OR IGNORE INTO crm_suppression(email_hash, created_at) VALUES (?, ?)",
                (_suppression_hash(row["email"], salt), _timestamp()),
            )
        connection.execute(
            """
            UPDATE crm_leads SET firma = '', person = '', email = '', telefonnummer = '',
                adresse = '', city = '', source_url = '', website = '',
                processing_purpose = '', legal_basis = '', status = 'opt_out', pipeline_status = 'opt_out',
                follow_up_at = '', notes = '', updated_at = ?
            WHERE id = ?
            """,
            (_timestamp(), int(lead_id)),
        )
        connection.execute("DELETE FROM crm_activities WHERE lead_id = ?", (int(lead_id),))
        connection.execute(
            "INSERT INTO crm_gdpr_actions(reference_hash, action, reason, created_at) VALUES (?, 'opt_out', ?, ?)",
            (_action_hash(row["lead_key"]), reason.strip(), _timestamp()),
        )
        connection.commit()


def delete_lead(database_path: str | Path, lead_id: int, reason: str = "") -> None:
    """Erase a lead and activities while retaining a non-identifying audit record."""
    with closing(_connect(database_path)) as connection:
        row = connection.execute(
            "SELECT lead_key, email FROM crm_leads WHERE id = ?", (int(lead_id),)
        ).fetchone()
        if not row:
            raise ValueError(f"CRM lead {lead_id} does not exist.")
        salt = connection.execute(
            "SELECT setting_value FROM crm_settings WHERE setting_key = 'hash_salt'"
        ).fetchone()[0]
        if row["email"]:
            connection.execute(
                "DELETE FROM crm_suppression WHERE email_hash = ?",
                (_suppression_hash(row["email"], salt),),
            )
        connection.execute(
            "INSERT INTO crm_gdpr_actions(reference_hash, action, reason, created_at) VALUES (?, 'delete', ?, ?)",
            (_action_hash(row["lead_key"]), reason.strip(), _timestamp()),
        )
        connection.execute("DELETE FROM crm_leads WHERE id = ?", (int(lead_id),))
        connection.commit()


def list_gdpr_actions(database_path: str | Path, limit: int = 50) -> list[dict[str, Any]]:
    with closing(_connect(database_path)) as connection:
        rows = connection.execute(
            """
            SELECT id, reference_hash, action, reason, created_at
            FROM crm_gdpr_actions ORDER BY id DESC LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_setting(database_path: str | Path, key: str, default: str = "") -> str:
    with closing(_connect(database_path)) as connection:
        row = connection.execute(
            "SELECT setting_value FROM crm_settings WHERE setting_key = ?", (key,)
        ).fetchone()
    return str(row[0]) if row else default


def get_retention_days(database_path: str | Path) -> int:
    try:
        return max(1, min(int(get_setting(database_path, "retention_days", str(DEFAULT_RETENTION_DAYS))), 3650))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def get_follow_up_days(database_path: str | Path) -> int:
    try:
        days = int(get_setting(database_path, "follow_up_days", str(DEFAULT_FOLLOW_UP_DAYS)))
    except ValueError:
        return DEFAULT_FOLLOW_UP_DAYS
    return days if days in FOLLOW_UP_SCHEDULES else DEFAULT_FOLLOW_UP_DAYS


def set_follow_up_days(database_path: str | Path, days: int) -> None:
    days = int(days)
    if days not in FOLLOW_UP_SCHEDULES:
        raise ValueError(f"Follow-up schedule must be one of {FOLLOW_UP_SCHEDULES}.")
    with closing(_connect(database_path)) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO crm_settings(setting_key, setting_value) VALUES ('follow_up_days', ?)",
            (str(days),),
        )
        connection.commit()


def get_target_city(database_path: str | Path) -> str:
    return get_setting(database_path, "target_city", "").strip()


def set_target_city(database_path: str | Path, city: str) -> None:
    with closing(_connect(database_path)) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO crm_settings(setting_key, setting_value) VALUES ('target_city', ?)",
            (city.strip()[:120],),
        )
        connection.commit()


def set_retention_days(database_path: str | Path, days: int) -> None:
    days = max(1, min(int(days), 3650))
    with closing(_connect(database_path)) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO crm_settings(setting_key, setting_value) VALUES ('retention_days', ?)",
            (str(days),),
        )
        connection.commit()


def expired_count(database_path: str | Path) -> int:
    now = _timestamp()
    with closing(_connect(database_path)) as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM crm_leads WHERE status != 'opt_out' AND retention_until <= ?",
            (now,),
        ).fetchone()[0])


def prune_expired(database_path: str | Path) -> int:
    now = _timestamp()
    with closing(_connect(database_path)) as connection:
        expired = connection.execute(
            "SELECT lead_key FROM crm_leads WHERE status != 'opt_out' AND retention_until <= ?",
            (now,),
        ).fetchall()
        for row in expired:
            connection.execute(
                "INSERT INTO crm_gdpr_actions(reference_hash, action, reason, created_at) VALUES (?, 'retention_delete', 'Retention period expired', ?)",
                (_action_hash(row["lead_key"]), now),
            )
        connection.execute(
            "DELETE FROM crm_leads WHERE status != 'opt_out' AND retention_until <= ?", (now,)
        )
        connection.commit()
    return len(expired)


def record_export(database_path: str | Path, export_type: str, row_count: int) -> None:
    with closing(_connect(database_path)) as connection:
        connection.execute(
            "INSERT INTO crm_exports(export_type, row_count, created_at) VALUES (?, ?, ?)",
            (export_type.strip() or "unknown", max(0, int(row_count)), _timestamp()),
        )
        connection.commit()


def score_breakdown(
    lead: dict[str, Any],
    *,
    target_city: str = "",
    today: date | None = None,
) -> dict[str, int | float]:
    """Return a score and its component points using fixed, inspectable weights."""
    email_points = SCORE_WEIGHTS["email"] if str(lead.get("email") or "").strip() else 0
    person_points = SCORE_WEIGHTS["person"] if str(lead.get("person") or "").strip() else 0
    phone_points = SCORE_WEIGHTS["phone"] if normalize_phone(lead.get("telefonnummer") or lead.get("phone")) else 0

    source = str(lead.get("source") or "").casefold()
    if any(name in source for name in ("arbeitsagentur", "jobbörse", "jobsuche")):
        source_points = SCORE_WEIGHTS["source"]
    elif any(name in source for name in ("azubiyo", "ausbildung.de", "stepstone")):
        source_points = 12
    elif "google maps" in source:
        source_points = 8
    else:
        source_points = 6 if source else 0

    city = str(lead.get("city") or "").strip().casefold()
    preferred_city = target_city.strip().casefold()
    if preferred_city:
        city_points = SCORE_WEIGHTS["city"] if city == preferred_city else 0
    else:
        city_points = SCORE_WEIGHTS["city"] // 2 if city else 0

    recentness_points = 0
    last_seen = str(lead.get("last_seen_at") or lead.get("first_seen_at") or "").strip()
    if last_seen:
        try:
            seen_at = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
            if seen_at.tzinfo is None:
                seen_at = seen_at.replace(tzinfo=timezone.utc)
            age_days = max(0, ((today or _now().date()) - seen_at.astimezone(timezone.utc).date()).days)
            recentness_points = (
                15 if age_days <= 7 else 12 if age_days <= 14 else
                8 if age_days <= 30 else 4 if age_days <= 60 else 0
            )
        except ValueError:
            recentness_points = 0

    status = str(lead.get("status") or lead.get("pipeline_status") or "new")
    workflow_points = {
        "new": 8,
        "contacted": 6,
        "sent": 6,
        "replied": 10,
        "interview": 10,
        "follow_up": 7,
        "closed": 0,
        "rejected": 0,
        "opt_out": 0,
    }.get(status, 0)
    components: dict[str, int | float] = {
        "email": email_points,
        "person": person_points,
        "phone": phone_points,
        "source": source_points,
        "city": city_points,
        "recentness": recentness_points,
        "workflow": workflow_points,
    }
    components["total"] = min(100, round(sum(components.values())))
    return components


def due_follow_ups(
    database_path: str | Path,
    *,
    today: date | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    cutoff = (today or _now().date()).isoformat()
    return [
        row for row in list_leads(database_path, limit=limit)
        if row["status"] not in {"closed", "rejected", "opt_out"}
        and row["follow_up_at"]
        and row["follow_up_at"] <= cutoff
    ]


def metrics(database_path: str | Path) -> dict[str, Any]:
    with closing(_connect(database_path)) as connection:
        total, with_email, with_phone, with_person, new_leads = connection.execute(
            """
            SELECT COUNT(*),
                SUM(CASE WHEN email != '' THEN 1 ELSE 0 END),
                SUM(CASE WHEN telefonnummer != '' THEN 1 ELSE 0 END),
                SUM(CASE WHEN person != '' THEN 1 ELSE 0 END),
                SUM(CASE WHEN first_seen_at >= ? THEN 1 ELSE 0 END)
            FROM crm_leads WHERE status != 'opt_out'
            """,
            (_timestamp(_now() - timedelta(days=7)),),
        ).fetchone()
        sources = connection.execute(
            """
            SELECT source, COUNT(*) AS count FROM crm_leads
            WHERE status != 'opt_out' GROUP BY source ORDER BY count DESC, source
            """
        ).fetchall()
        cities = connection.execute(
            """
            SELECT city, COUNT(*) AS count FROM crm_leads
            WHERE status != 'opt_out' AND city != '' GROUP BY city ORDER BY count DESC, city
            """
        ).fetchall()
        pipeline = connection.execute(
            """
            SELECT COALESCE(NULLIF(pipeline_status, ''), status) AS status, COUNT(*) AS count
            FROM crm_leads WHERE status != 'opt_out'
            GROUP BY COALESCE(NULLIF(pipeline_status, ''), status)
            ORDER BY count DESC, status
            """
        ).fetchall()
        exports = connection.execute("SELECT COUNT(*) FROM crm_exports").fetchone()[0]
    active_leads = list_leads(database_path, limit=5000)
    scores = [row["lead_score"] for row in active_leads if row["status"] != "opt_out"]
    due_count = len(due_follow_ups(database_path))
    return {
        "total": int(total or 0),
        "with_email": int(with_email or 0),
        "with_phone": int(with_phone or 0),
        "with_person": int(with_person or 0),
        "email_coverage": (float(with_email or 0) / int(total) * 100) if total else 0.0,
        "new_last_7_days": int(new_leads or 0),
        "source_counts": [dict(row) for row in sources],
        "city_counts": [dict(row) for row in cities],
        "pipeline_counts": [dict(row) for row in pipeline],
        "due_follow_ups": due_count,
        "average_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
        "exports": int(exports),
    }
