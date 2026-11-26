from datetime import datetime, timedelta, timezone
from email import message_from_bytes
import base64

import pytest

from scraper import google_workspace
from scraper.applications import SHEET_HEADERS
from scraper.google_workspace import (
    GoogleWorkspaceConfig,
    GoogleWorkspaceError,
    exchange_code,
    load_config,
    oauth_state_matches,
    send_gmail,
    sync_rows_to_sheet,
)


def _config():
    return GoogleWorkspaceConfig.from_mapping({
        "client_id": "client.apps.googleusercontent.com",
        "client_secret": "do-not-print-this-secret",
        "redirect_uri": "http://localhost:8501/",
    })


def test_google_oauth_config_requires_real_client_values_and_local_redirect():
    assert load_config({"google": {
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "http://localhost:8501/",
    }}, {}) is None
    assert GoogleWorkspaceConfig.from_mapping({
        "client_id": "client.apps.googleusercontent.com",
        "client_secret": "secret",
        "redirect_uri": "https://example.com/callback",
    }) is None

    config = _config()
    assert config is not None
    assert "do-not-print-this-secret" not in repr(config)
    assert "do-not-print-this-secret" not in str(config.public_settings())


def test_oauth_errors_do_not_expose_provider_response_or_client_secret():
    class Response:
        ok = False
        status_code = 400

        def json(self):
            return {"error": "invalid_grant", "debug": "provider-secret"}

    class Session:
        @staticmethod
        def post(*args, **kwargs):
            return Response()

    with pytest.raises(GoogleWorkspaceError) as error:
        exchange_code(_config(), "authorization-code", "verifier", session=Session())

    rendered = str(error.value)
    assert "400" in rendered
    assert "provider-secret" not in rendered
    assert "do-not-print-this-secret" not in rendered


def test_oauth_state_is_required_and_compared_without_timing_sensitive_equality():
    assert oauth_state_matches("random-state", "random-state")
    assert not oauth_state_matches("random-state", "other-state")
    assert not oauth_state_matches("", "")
    assert not oauth_state_matches("random-state", "")


def test_authorized_api_requests_use_bearer_token_and_redact_provider_body():
    from scraper.google_workspace import api_request

    class Response:
        ok = False
        status_code = 403
        text = "private-provider-response"

    class Session:
        calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return Response()

    session = Session()
    tokens = {
        "access_token": "private-access-token",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
    }

    with pytest.raises(GoogleWorkspaceError) as error:
        api_request(_config(), tokens, "GET", "https://sheets.googleapis.com/test", session=session)

    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url.endswith("/test")
    assert kwargs["headers"]["Authorization"] == "Bearer private-access-token"
    assert "private-provider-response" not in str(error.value)
    assert "private-access-token" not in str(error.value)


def test_sheet_sync_deduplicates_emails_and_uses_formula_safe_raw_values():
    class Response:
        ok = True
        status_code = 200
        content = b""

        def __init__(self, value=None):
            self.value = value or {}

        def json(self):
            return self.value

    class Session:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if method == "GET":
                return Response({"values": [
                    list(SHEET_HEADERS),
                    ["Existing", "", "", "old@example.com", "", "", "", ""],
                    ["Sent", "", "", "sent@example.de", "", "", "SENT", "2026-04-04"],
                ]})
            return Response()

    session = Session()
    tokens = {
        "access_token": "session-token",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
    }
    rows = [
        {
            "Firma": "=HYPERLINK(\"https://example.invalid\")",
            "Anrede": "",
            "Person": "",
            "Email": "new@example.com",
            "Betreff": "+formula",
            "Nachricht": "@formula",
            "Status": "",
            "Date Sent": "",
        },
        {
            "Firma": "Duplicate",
            "Anrede": "",
            "Person": "",
            "Email": "NEW@example.com",
            "Betreff": "",
            "Nachricht": "",
            "Status": "",
            "Date Sent": "",
        },
        {
            "Firma": "Already sent",
            "Anrede": "",
            "Person": "",
            "Email": "sent@example.de",
            "Betreff": "",
            "Nachricht": "",
            "Status": "",
            "Date Sent": "",
        },
    ]

    result = sync_rows_to_sheet(
        _config(), tokens, "sheet_123", "BEWERBUNG", rows, session=session,
    )

    assert result["inserted"] == 1
    assert result["skipped"] == 2
    assert result["sent_rows"] == [{
        "email": "sent@example.de",
        "row_number": 3,
        "status": "SENT",
    }]
    append = next(call for call in session.calls if call[0] == "POST")
    values = append[2]["json"]["values"][0]
    assert values[0].startswith("'=")
    assert values[4].startswith("'+")
    assert values[5].startswith("'@")
    assert append[2]["params"]["valueInputOption"] == "RAW"


def test_gmail_send_returns_provider_message_id_and_attaches_pdf_without_sending_live_mail():
    class Response:
        ok = True
        status_code = 200
        content = b""

        @staticmethod
        def json():
            return {"id": "message_123"}

    class Session:
        calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return Response()

    session = Session()
    tokens = {
        "access_token": "test-token",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
    }
    message_id = send_gmail(
        _config(),
        tokens,
        sender_email="sender@example.de",
        to="jobs@example.de",
        subject="Bewerbung",
        body="Guten Tag",
        attachments=[("Anschreiben.pdf", b"%PDF-cover")],
        sender_name="Example Sender",
        session=session,
    )

    assert message_id == "message_123"
    method, url, kwargs = session.calls[0]
    assert (method, url) == (
        "POST",
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
    )
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    raw = kwargs["json"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    message = message_from_bytes(decoded)
    assert message["To"] == "jobs@example.de"
    assert message["Subject"] == "Bewerbung"
    assert [part.get_filename() for part in message.walk() if part.get_filename()] == [
        "Anschreiben.pdf",
    ]


def test_gmail_send_without_verifiable_message_id_requires_manual_verification():
    class Response:
        ok = True
        status_code = 200
        content = b""

        @staticmethod
        def json():
            return {}

    class Session:
        @staticmethod
        def request(*_args, **_kwargs):
            return Response()

    tokens = {
        "access_token": "test-token",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
    }
    with pytest.raises(GoogleWorkspaceError, match="no verifiable message ID") as error:
        send_gmail(
            _config(), tokens,
            sender_email="sender@example.de", to="jobs@example.de",
            subject="Bewerbung", body="Guten Tag", attachments=[],
            session=Session(),
        )
    assert error.value.ambiguous


def test_workspace_spreadsheet_docs_template_and_drive_pdf_operations_are_authenticated():
    class Response:
        ok = True
        status_code = 200

        def __init__(self, value=None, content=b""):
            self.value = value or {}
            self.content = content

        def json(self):
            return self.value

    class Session:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return self.responses.pop(0)

    tokens = {
        "access_token": "test-token",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
    }

    sheets_session = Session([Response({
        "spreadsheetId": "sheet_123",
        "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/sheet_123/edit",
    })])
    spreadsheet = google_workspace.create_spreadsheet(
        _config(), tokens, "Applications", session=sheets_session,
    )
    assert spreadsheet["spreadsheet_id"] == "sheet_123"
    assert sheets_session.calls[0][2]["headers"]["Authorization"] == "Bearer test-token"
    assert sheets_session.calls[0][2]["json"]["sheets"][0]["properties"]["title"] == "BEWERBUNG"

    docs_session = Session([
        Response({"files": []}),
        Response({"documentId": "doc_123"}),
        Response(),
    ])
    document_id = google_workspace.find_or_create_template_document(
        _config(), tokens, session=docs_session,
    )
    assert document_id == "doc_123"
    assert "{COMPANY}" in docs_session.calls[2][2]["json"]["requests"][0]["insertText"]["text"]
    assert all(call[2]["headers"]["Authorization"] == "Bearer test-token" for call in docs_session.calls)

    drive_session = Session([
        Response({"files": [{"id": "pdf_123", "name": "Lebenslauf.pdf"}]}),
        Response(content=b"%PDF-dossier"),
    ])
    pdfs = google_workspace.list_dossier_pdfs(_config(), tokens, session=drive_session)
    payload = google_workspace._drive_file_bytes(
        _config(), tokens, pdfs[0]["id"], session=drive_session,
    )
    assert pdfs == [{"id": "pdf_123", "name": "Lebenslauf.pdf"}]
    assert payload == b"%PDF-dossier"
    assert all(call[2]["headers"]["Authorization"] == "Bearer test-token" for call in drive_session.calls)
    assert drive_session.calls[1][2]["params"] == {"alt": "media"}


def test_personalized_docs_pdf_is_exported_and_temporary_copy_is_deleted():
    class Response:
        ok = True
        status_code = 200
        content = b"%PDF-personalized"

        def __init__(self, value=None):
            self.value = value or {}

        def json(self):
            return self.value

    class Session:
        def __init__(self):
            self.responses = [
                Response({"id": "temporary_doc"}),
                Response(),
                Response(),
                Response(),
            ]
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return self.responses.pop(0)

    session = Session()
    config = GoogleWorkspaceConfig(
        client_id="client.apps.googleusercontent.com",
        client_secret="secret",
        redirect_uri="http://localhost:8501/",
        template_doc_id="template_doc",
    )
    tokens = {
        "access_token": "test-token",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
    }

    payload = google_workspace.personalized_cover_pdf(
        config, tokens, company="Example GmbH",
        salutation="Guten Tag", today="26.11.2026",
        person="Max Muster", session=session,
    )

    assert payload == b"%PDF-personalized"
    assert "/files/template_doc/copy" in session.calls[0][1]
    replace_requests = session.calls[1][2]["json"]["requests"]
    assert {item["replaceAllText"]["containsText"]["text"] for item in replace_requests} == {
        "{COMPANY}", "{SALUTATION}", "{DATE}", "{PERSON}",
    }
    assert "/files/temporary_doc/export" in session.calls[2][1]
    assert session.calls[3][0] == "DELETE"
    assert all(call[2]["headers"]["Authorization"] == "Bearer test-token" for call in session.calls)
