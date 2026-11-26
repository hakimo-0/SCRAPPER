"""Explicit, session-scoped Google OAuth and Workspace API helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
import base64
import hashlib
import json
import os
import re
import secrets
from typing import Any
from urllib.parse import urlencode, quote

import requests

from .applications import SHEET_HEADERS


OAUTH_STATE_KEY = "google_oauth_state"
OAUTH_VERIFIER_KEY = "google_oauth_verifier"
TOKENS_KEY = "google_workspace_tokens"
ACCOUNT_KEY = "google_workspace_account"
KEYRING_SERVICE = "SCRAPPER Google Workspace"
KEYRING_ACCOUNT = "active"
GOOGLE_SCOPES = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.send",
)


class GoogleWorkspaceError(RuntimeError):
    """Safe-to-display error without request bodies, OAuth tokens, or secrets."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        transient: bool = False,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient
        self.ambiguous = ambiguous


def retry_delay_seconds(attempt_number: int, *, maximum: int = 1800) -> int:
    if attempt_number < 1:
        raise ValueError("Attempt number must be positive.")
    return min(maximum, 60 * (2 ** (attempt_number - 1)))


@dataclass(frozen=True)
class GoogleWorkspaceConfig:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    spreadsheet_id: str = ""
    sheet_name: str = "BEWERBUNG"
    template_doc_id: str = ""
    static_pdf_id: str = ""
    sender_name: str = ""

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "GoogleWorkspaceConfig | None":
        client_id = str(values.get("client_id") or "").strip()
        client_secret = str(values.get("client_secret") or "").strip()
        redirect_uri = str(values.get("redirect_uri") or "").strip()
        if not (client_id and client_secret and redirect_uri):
            return None
        if not client_id.endswith(".apps.googleusercontent.com"):
            return None
        if not re.fullmatch(r"https?://(?:localhost|127\.0\.0\.1)(?::\d+)?/?", redirect_uri):
            return None
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            spreadsheet_id=str(values.get("spreadsheet_id") or "").strip(),
            sheet_name=str(values.get("sheet_name") or "BEWERBUNG").strip() or "BEWERBUNG",
            template_doc_id=str(values.get("template_doc_id") or "").strip(),
            static_pdf_id=str(values.get("static_pdf_id") or "").strip(),
            sender_name=str(values.get("sender_name") or "").strip(),
        )

    def public_settings(self) -> dict[str, str]:
        """Return settings safe for UI rendering; never include the OAuth secret."""
        return {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "spreadsheet_id": self.spreadsheet_id,
            "sheet_name": self.sheet_name,
            "template_doc_id": self.template_doc_id,
            "static_pdf_id": self.static_pdf_id,
        }


def load_config(secrets_mapping: Any = None, environ: dict[str, str] | None = None) -> GoogleWorkspaceConfig | None:
    env = environ if environ is not None else os.environ
    try:
        google_values = dict(secrets_mapping.get("google", {})) if secrets_mapping else {}
    except Exception:
        google_values = {}
    values = {
        "client_id": google_values.get("client_id") or env.get("GOOGLE_CLIENT_ID"),
        "client_secret": google_values.get("client_secret") or env.get("GOOGLE_CLIENT_SECRET"),
        "redirect_uri": google_values.get("redirect_uri") or env.get("GOOGLE_REDIRECT_URI"),
        "spreadsheet_id": google_values.get("spreadsheet_id") or env.get("GOOGLE_SPREADSHEET_ID"),
        "sheet_name": google_values.get("sheet_name") or env.get("GOOGLE_SHEET_NAME"),
        "template_doc_id": google_values.get("template_doc_id") or env.get("GOOGLE_DOC_TEMPLATE_ID"),
        "static_pdf_id": google_values.get("static_pdf_id") or env.get("GOOGLE_STATIC_PDF_ID"),
        "sender_name": google_values.get("sender_name") or env.get("GOOGLE_SENDER_NAME"),
    }
    return GoogleWorkspaceConfig.from_mapping(values)


def persist_refresh_token(tokens: dict[str, Any], account_email: str) -> None:
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not refresh_token or not account_email.strip():
        raise GoogleWorkspaceError(
            "Google did not grant a persistent refresh token. Revoke this app's access and sign in again."
        )
    try:
        import keyring

        keyring.set_password(
            KEYRING_SERVICE,
            KEYRING_ACCOUNT,
            json.dumps({"refresh_token": refresh_token, "email": account_email.strip()}),
        )
    except Exception as exc:
        raise GoogleWorkspaceError(
            "Unable to securely save the Google refresh token in the operating-system credential store."
        ) from exc


def restore_persisted_session(
    config: GoogleWorkspaceConfig,
    *,
    session: Any = requests,
) -> tuple[dict[str, Any], str] | None:
    try:
        import keyring

        stored = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except Exception as exc:
        raise GoogleWorkspaceError(
            "Unable to read Google credentials from the operating-system credential store."
        ) from exc
    if not stored:
        return None
    try:
        saved = json.loads(stored)
        refresh_token = str(saved["refresh_token"])
        email = str(saved["email"])
    except (ValueError, KeyError, TypeError) as exc:
        raise GoogleWorkspaceError(
            "Stored Google credential data is invalid; disconnect and sign in again."
        ) from exc
    tokens: dict[str, Any] = {"refresh_token": refresh_token, "expires_at": 0}
    _refresh_if_needed(config, tokens, session=session)
    return tokens, email


def clear_persisted_session() -> None:
    try:
        import keyring

        if keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT) is not None:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except Exception as exc:
        raise GoogleWorkspaceError(
            "Unable to remove the Google credential from the operating-system credential store."
        ) from exc


def make_pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def oauth_state_matches(expected_state: str, returned_state: str) -> bool:
    return bool(
        expected_state
        and returned_state
        and secrets.compare_digest(expected_state, returned_state)
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def authorization_url(config: GoogleWorkspaceConfig, state: str, verifier: str) -> str:
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    query = urlencode({
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "include_granted_scopes": "true",
    })
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


def exchange_code(
    config: GoogleWorkspaceConfig,
    code: str,
    verifier: str,
    *,
    session: Any = requests,
) -> dict[str, Any]:
    try:
        response = session.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "redirect_uri": config.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        raise GoogleWorkspaceError("Google OAuth token exchange failed; check the network and redirect URI.") from exc
    if not response.ok:
        raise GoogleWorkspaceError(f"Google OAuth token exchange failed (HTTP {response.status_code}).")
    try:
        tokens = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise GoogleWorkspaceError("Google OAuth returned an invalid token response.") from exc
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise GoogleWorkspaceError("Google OAuth returned no access token.")
    tokens["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    ).timestamp()
    return tokens


def _refresh_if_needed(
    config: GoogleWorkspaceConfig,
    tokens: dict[str, Any],
    *,
    session: Any = requests,
) -> str:
    expires_at = float(tokens.get("expires_at", 0))
    if expires_at > datetime.now(timezone.utc).timestamp() + 60:
        return str(tokens["access_token"])
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise GoogleWorkspaceError("Google session expired; disconnect and sign in again.")
    try:
        response = session.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        raise GoogleWorkspaceError("Google token refresh failed; check the network.") from exc
    if not response.ok:
        raise GoogleWorkspaceError(f"Google token refresh failed (HTTP {response.status_code}).")
    try:
        updated = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise GoogleWorkspaceError("Google returned an invalid token refresh response.") from exc
    if not updated.get("access_token"):
        raise GoogleWorkspaceError("Google returned no access token during refresh.")
    tokens["access_token"] = updated["access_token"]
    tokens["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=int(updated.get("expires_in", 3600)))
    ).timestamp()
    return str(tokens["access_token"])


def api_request(
    config: GoogleWorkspaceConfig,
    tokens: dict[str, Any],
    method: str,
    url: str,
    *,
    session: Any = requests,
    **kwargs: Any,
) -> requests.Response:
    access_token = _refresh_if_needed(config, tokens, session=session)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {access_token}"
    try:
        response = session.request(method, url, headers=headers, timeout=30, **kwargs)
    except requests.RequestException as exc:
        is_gmail_send = url == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
        raise GoogleWorkspaceError(
            f"Google API request failed ({method.upper()}).",
            transient=not is_gmail_send,
            ambiguous=is_gmail_send,
        ) from exc
    if not response.ok:
        is_gmail_send = url == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
        is_rate_limited = response.status_code == 429
        is_ambiguous_send = is_gmail_send and response.status_code >= 500
        raise GoogleWorkspaceError(
            f"Google API request failed (HTTP {response.status_code}, {method.upper()}).",
            status_code=response.status_code,
            transient=is_rate_limited or (response.status_code >= 500 and not is_gmail_send),
            ambiguous=is_ambiguous_send,
        )
    return response


def account_email(
    config: GoogleWorkspaceConfig,
    tokens: dict[str, Any],
    *,
    session: Any = requests,
) -> str:
    response = api_request(
        config, tokens, "GET", "https://openidconnect.googleapis.com/v1/userinfo", session=session
    )
    try:
        result = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise GoogleWorkspaceError("Google returned invalid account information.") from exc
    email = str(result.get("email") or "").strip()
    if not email:
        raise GoogleWorkspaceError("Google account did not return an email address.")
    return email


def _sheet_range(sheet_name: str) -> str:
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'!A:H"


def create_spreadsheet(
    config: GoogleWorkspaceConfig,
    tokens: dict[str, Any],
    title: str,
    *,
    session: Any = requests,
) -> dict[str, str]:
    response = api_request(
        config,
        tokens,
        "POST",
        "https://sheets.googleapis.com/v4/spreadsheets",
        session=session,
        json={"properties": {"title": title}, "sheets": [{"properties": {"title": config.sheet_name}}]},
    )
    try:
        result = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise GoogleWorkspaceError("Google Sheets returned invalid spreadsheet information.") from exc
    spreadsheet_id = str(result.get("spreadsheetId") or "")
    spreadsheet_url = str(result.get("spreadsheetUrl") or "")
    if not spreadsheet_id or not spreadsheet_url:
        raise GoogleWorkspaceError("Google Sheets did not return a spreadsheet URL.")
    return {"spreadsheet_id": spreadsheet_id, "spreadsheet_url": spreadsheet_url}


def _drive_files(
    config: GoogleWorkspaceConfig,
    tokens: dict[str, Any],
    query: str,
    *,
    session: Any = requests,
) -> list[dict[str, Any]]:
    response = api_request(
        config,
        tokens,
        "GET",
        "https://www.googleapis.com/drive/v3/files",
        session=session,
        params={
            "q": query,
            "fields": "files(id,name,mimeType,webViewLink,modifiedTime)",
            "pageSize": 100,
            "orderBy": "modifiedTime desc",
        },
    )
    try:
        files = response.json().get("files", [])
    except (ValueError, json.JSONDecodeError, AttributeError) as exc:
        raise GoogleWorkspaceError("Google Drive returned invalid file data.") from exc
    if not isinstance(files, list):
        raise GoogleWorkspaceError("Google Drive returned an invalid file list.")
    return [item for item in files if isinstance(item, dict)]


def _spreadsheet_details(
    config: GoogleWorkspaceConfig,
    tokens: dict[str, Any],
    spreadsheet_id: str,
    sheet_name: str,
    *,
    session: Any = requests,
) -> dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id):
        raise ValueError("Invalid Google spreadsheet ID.")
    response = api_request(
        config,
        tokens,
        "GET",
        f"https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet_id, safe='')}",
        session=session,
        params={"fields": "spreadsheetId,spreadsheetUrl,sheets(properties(title))"},
    )
    try:
        details = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise GoogleWorkspaceError("Google Sheets returned invalid spreadsheet information.") from exc
    titles = {
        str(sheet.get("properties", {}).get("title", ""))
        for sheet in details.get("sheets", [])
        if isinstance(sheet, dict)
    }
    if sheet_name not in titles:
        api_request(
            config,
            tokens,
            "POST",
            f"https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet_id, safe='')}:batchUpdate",
            session=session,
            json={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
        )
    return {
        "spreadsheet_id": str(details.get("spreadsheetId") or spreadsheet_id),
        "spreadsheet_url": str(
            details.get("spreadsheetUrl")
            or f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        ),
    }


def find_or_create_spreadsheet(
    config: GoogleWorkspaceConfig,
    tokens: dict[str, Any],
    *,
    title: str = "Ausbildung Lead Finder · Bewerbungen",
    session: Any = requests,
) -> dict[str, str]:
    """Reuse the configured/uniquely named workbook, otherwise create it."""
    spreadsheet_id = config.spreadsheet_id
    if not spreadsheet_id:
        escaped_title = title.replace("\\", "\\\\").replace("'", "\\'")
        matches = _drive_files(
            config,
            tokens,
            f"name = '{escaped_title}' and mimeType = "
            "'application/vnd.google-apps.spreadsheet' and trashed = false",
            session=session,
        )
        if matches:
            spreadsheet_id = str(matches[0].get("id") or "")
    if spreadsheet_id:
        return _spreadsheet_details(
            config, tokens, spreadsheet_id, config.sheet_name, session=session
        )
    return create_spreadsheet(config, tokens, title, session=session)


def find_or_create_template_document(
    config: GoogleWorkspaceConfig,
    tokens: dict[str, Any],
    *,
    title: str = "Ausbildung Lead Finder · Anschreiben",
    session: Any = requests,
) -> str:
    """Reuse a configured/matching Docs template or create a placeholder template."""
    if config.template_doc_id:
        return config.template_doc_id
    escaped_title = title.replace("\\", "\\\\").replace("'", "\\'")
    matches = _drive_files(
        config,
        tokens,
        f"name = '{escaped_title}' and mimeType = "
        "'application/vnd.google-apps.document' and trashed = false",
        session=session,
    )
    if matches and matches[0].get("id"):
        return str(matches[0]["id"])
    response = api_request(
        config,
        tokens,
        "POST",
        "https://docs.googleapis.com/v1/documents",
        session=session,
        json={"title": title},
    )
    try:
        document_id = str(response.json().get("documentId") or "")
    except (ValueError, json.JSONDecodeError) as exc:
        raise GoogleWorkspaceError("Google Docs returned invalid document information.") from exc
    if not document_id:
        raise GoogleWorkspaceError("Google Docs did not return a template ID.")
    template = (
        "{DATE}\n\n{SALUTATION}\n\n"
        "hiermit bewerbe ich mich um eine passende Ausbildungsstelle bei {COMPANY}.\n\n"
        "Meine Bewerbungsunterlagen finden Sie im Anhang.\n\n"
        "Mit freundlichen Grüßen\n\n"
        "{PERSON}\n"
    )
    api_request(
        config,
        tokens,
        "POST",
        f"https://docs.googleapis.com/v1/documents/{quote(document_id, safe='')}:batchUpdate",
        session=session,
        json={"requests": [{"insertText": {"location": {"index": 1}, "text": template}}]},
    )
    return document_id


def list_dossier_pdfs(
    config: GoogleWorkspaceConfig,
    tokens: dict[str, Any],
    *,
    session: Any = requests,
) -> list[dict[str, str]]:
    """List Drive PDFs likely to be the applicant's CV or application dossier."""
    if config.static_pdf_id:
        return [{"id": config.static_pdf_id, "name": "Configured application PDF"}]
    files = _drive_files(
        config, tokens, "mimeType = 'application/pdf' and trashed = false", session=session
    )
    candidates = [
        {"id": str(item.get("id") or ""), "name": str(item.get("name") or "")}
        for item in files
        if re.search(r"lebenslauf|bewerbungsunterlagen|anlagen", str(item.get("name") or ""), re.I)
        and item.get("id")
    ]
    if not candidates:
        candidates = [{
            "id": str(item["id"]),
            "name": str(item.get("name") or "PDF"),
        } for item in files if item.get("id")]
    return candidates


def sync_rows_to_sheet(
    config: GoogleWorkspaceConfig,
    tokens: dict[str, Any],
    spreadsheet_id: str,
    sheet_name: str,
    rows: list[dict[str, str]],
    *,
    session: Any = requests,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id):
        raise ValueError("Invalid Google spreadsheet ID.")
    encoded_range = quote(_sheet_range(sheet_name), safe="!")
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{encoded_range}"
    existing_response = api_request(config, tokens, "GET", base, session=session)
    try:
        existing_values = existing_response.json().get("values", [])
    except (ValueError, json.JSONDecodeError) as exc:
        raise GoogleWorkspaceError("Google Sheets returned invalid row data.") from exc
    if not existing_values:
        header_range = quote(f"'{sheet_name.replace(chr(39), chr(39) * 2)}'!A1:H1", safe="!")
        api_request(
            config,
            tokens,
            "PUT",
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{header_range}",
            session=session,
            params={"valueInputOption": "RAW"},
            json={"values": [list(SHEET_HEADERS)]},
        )
    known_rows = {
        str(row[3]).strip().casefold(): (row_number, str(row[6] if len(row) > 6 else "").strip())
        for row_number, row in enumerate(existing_values[1:], start=2)
        if len(row) > 3 and str(row[3]).strip()
    }
    known_emails = set(known_rows)
    append_values = []
    seen = set(known_emails)
    append_emails = []
    for row in rows:
        email = str(row.get("Email") or "").strip().casefold()
        if not email or email in seen:
            continue
        values = [str(row.get(header) or "") for header in SHEET_HEADERS]
        for index in (0, 1, 2, 4, 5):
            if re.match(r"^\s*[=+\-@]", values[index]):
                values[index] = "'" + values[index]
        append_values.append(values)
        append_emails.append(email)
        seen.add(email)
    if append_values:
        api_request(
            config,
            tokens,
            "POST",
            f"{base}:append",
            session=session,
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json={"values": append_values},
        )
    first_appended_row = max(2, len(existing_values) + 1)
    for offset, email in enumerate(append_emails):
        known_rows[email] = (first_appended_row + offset, "")
    requested_emails = {
        str(row.get("Email") or "").strip().casefold()
        for row in rows
        if str(row.get("Email") or "").strip()
    }
    ready_rows = [
        {"email": email, "row_number": row_number, "status": status}
        for email, (row_number, status) in known_rows.items()
        if email in requested_emails
        and status in {"", "READY", "RETRY", "SENDING", "VERIFY_REQUIRED"}
    ]
    sent_rows = [
        {"email": email, "row_number": row_number, "status": status}
        for email, (row_number, status) in known_rows.items()
        if email in requested_emails and status in {"SENT", "Gesendet"}
    ]
    return {
        "inserted": len(append_values),
        "skipped": len(rows) - len(append_values),
        "ready_rows": ready_rows,
        "sent_rows": sent_rows,
    }


def update_sheet_status(
    config: GoogleWorkspaceConfig,
    tokens: dict[str, Any],
    spreadsheet_id: str,
    sheet_name: str,
    row_number: int,
    status: str,
    date_sent: str,
    *,
    session: Any = requests,
) -> None:
    if row_number < 2:
        raise ValueError("Invalid Google Sheets row number.")
    cell_range = quote(f"'{sheet_name.replace(chr(39), chr(39) * 2)}'!G{row_number}:H{row_number}", safe="!:$")
    api_request(
        config,
        tokens,
        "PUT",
        f"https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet_id, safe='')}/values/{cell_range}",
        session=session,
        params={"valueInputOption": "RAW"},
        json={"values": [[status, date_sent]]},
    )


def _drive_file_bytes(
    config: GoogleWorkspaceConfig,
    tokens: dict[str, Any],
    file_id: str,
    *,
    export: bool = False,
    session: Any = requests,
) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
        raise ValueError("Invalid Google Drive file ID.")
    endpoint = (
        f"https://www.googleapis.com/drive/v3/files/{quote(file_id, safe='')}/export"
        if export
        else f"https://www.googleapis.com/drive/v3/files/{quote(file_id, safe='')}"
    )
    params = {"mimeType": "application/pdf"} if export else {"alt": "media"}
    return api_request(config, tokens, "GET", endpoint, session=session, params=params).content


def personalized_cover_pdf(
    config: GoogleWorkspaceConfig,
    tokens: dict[str, Any],
    *,
    company: str,
    salutation: str,
    today: str,
    person: str = "",
    session: Any = requests,
) -> bytes:
    """Copy the user's Docs template, replace placeholders, export, and trash copy."""
    template_id = config.template_doc_id
    if not re.fullmatch(r"[A-Za-z0-9_-]+", template_id):
        raise ValueError("Configure a valid Google Docs template ID first.")
    response = api_request(
        config,
        tokens,
        "POST",
        f"https://www.googleapis.com/drive/v3/files/{quote(template_id, safe='')}/copy",
        session=session,
        json={"name": f"Temp cover letter {secrets.token_hex(6)}"},
    )
    try:
        document_id = str(response.json().get("id") or "")
    except (ValueError, json.JSONDecodeError) as exc:
        raise GoogleWorkspaceError("Google Drive returned invalid document information.") from exc
    if not document_id:
        raise GoogleWorkspaceError("Google Drive did not return a temporary document ID.")
    try:
        replacements = {
            "{COMPANY}": company,
            "{SALUTATION}": salutation,
            "{DATE}": today,
            "{PERSON}": person,
        }
        api_request(
            config,
            tokens,
            "POST",
            f"https://docs.googleapis.com/v1/documents/{quote(document_id, safe='')}:batchUpdate",
            session=session,
            json={"requests": [{
                "replaceAllText": {
                    "containsText": {"text": placeholder, "matchCase": True},
                    "replaceText": value,
                }
            } for placeholder, value in replacements.items()]},
        )
        return _drive_file_bytes(config, tokens, document_id, export=True, session=session)
    finally:
        api_request(
            config,
            tokens,
            "DELETE",
            f"https://www.googleapis.com/drive/v3/files/{quote(document_id, safe='')}",
            session=session,
        )


def send_gmail(
    config: GoogleWorkspaceConfig,
    tokens: dict[str, Any],
    *,
    sender_email: str,
    to: str,
    subject: str,
    body: str,
    attachments: list[tuple[str, bytes]],
    sender_name: str = "",
    session: Any = requests,
) -> str:
    email = EmailMessage()
    email["To"] = to
    email["From"] = formataddr((sender_name.strip(), sender_email)) if sender_name.strip() else sender_email
    email["Subject"] = subject
    email.set_content(body)
    for filename, content in attachments:
        email.add_attachment(content, maintype="application", subtype="pdf", filename=filename)
    raw_message = _b64url(email.as_bytes())
    response = api_request(
        config,
        tokens,
        "POST",
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        session=session,
        json={"raw": raw_message},
    )
    try:
        message_id = str(response.json().get("id") or "")
    except (ValueError, json.JSONDecodeError, AttributeError) as exc:
        raise GoogleWorkspaceError(
            "Gmail accepted the request but returned no verifiable message ID.",
            ambiguous=True,
        ) from exc
    if not message_id:
        raise GoogleWorkspaceError(
            "Gmail accepted the request but returned no verifiable message ID.",
            ambiguous=True,
        )
    return message_id
