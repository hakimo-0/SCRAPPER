import csv
import io

import pytest

from scraper import applications


def _config(**overrides):
    config = {
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/sheet_id/edit?gid=0#gid=0",
        "sheet_name": "BEWERBUNG",
        "template_doc_id": "doc_template_123",
        "static_pdf_id": "drive_pdf_456",
        "sender_name": 'Hakim "Example"',
        "timezone": "Africa/Casablanca",
        "max_per_day": 159,
        "min_delay_minutes": 1,
        "max_delay_minutes": 3,
    }
    config.update(overrides)
    return config


def test_build_sheet_rows_deduplicates_emails_and_excludes_opt_out_or_invalid():
    leads = [
        {"id": 1, "firma": "Example GmbH", "person": "Herr Max Beispiel", "email": "Info@Example.de", "status": "new"},
        {"id": 2, "firma": "Another GmbH", "person": "Frau Ada Muster", "email": "info@example.de", "status": "follow_up"},
        {"id": 3, "firma": "Blocked GmbH", "email": "blocked@example.de", "status": "opt_out"},
        {"id": 4, "firma": "Invalid GmbH", "email": "not-an-email", "status": "new"},
        {"id": 5, "firma": "", "email": "missing-company@example.de", "status": "new"},
    ]

    rows = applications.build_sheet_rows(
        leads,
        [1, 2, 3, 4, 5],
        subject_template="Bewerbung bei {COMPANY}",
        message_template="Guten Tag, {SALUTATION}",
    )

    assert rows == [{
        "Firma": "Example GmbH",
        "Anrede": "Herr",
        "Person": "Max Beispiel",
        "Email": "info@example.de",
        "Betreff": "Bewerbung bei {COMPANY}",
        "Nachricht": "Guten Tag, {SALUTATION}",
        "Status": "",
        "Date Sent": "",
    }]


def test_csv_uses_sheet_headers_and_quotes_cells_for_google_sheets():
    rows = [{
        "Firma": "Beispiel, GmbH",
        "Anrede": "Frau",
        "Person": "Ada Muster",
        "Email": "ada@example.de",
        "Betreff": "Bewerbung",
        "Nachricht": "Zeile 1\nZeile 2",
        "Status": "",
        "Date Sent": "",
    }]

    payload = applications.csv_bytes(rows)
    decoded = payload.decode("utf-8-sig")
    parsed = list(csv.DictReader(io.StringIO(decoded)))

    assert decoded.startswith(",".join(applications.SHEET_HEADERS))
    assert parsed == rows


def test_csv_neutralizes_formula_like_cells_from_scraped_data():
    rows = [{
        "Firma": "=HYPERLINK(\"https://example.test\")",
        "Anrede": "",
        "Person": "",
        "Email": "jobs@example.de",
        "Betreff": "+formula",
        "Nachricht": "-not a formula",
        "Status": "",
        "Date Sent": "",
    }]

    parsed = list(csv.DictReader(io.StringIO(applications.csv_bytes(rows).decode("utf-8-sig"))))

    assert parsed[0]["Firma"].startswith("'=")
    assert parsed[0]["Betreff"] == "'+formula"
    assert parsed[0]["Nachricht"] == "'-not a formula"


def test_apps_script_has_explicit_start_stop_dedup_and_attachments():
    source = applications.apps_script_source(_config())

    assert 'MAX_PER_DAY: 159' in source
    assert 'MIN_DELAY_MINUTES: 1' in source
    assert 'MAX_DELAY_MINUTES: 3' in source
    assert 'sender name' not in source.casefold()
    assert 'SENDER_NAME: "Hakim \\"Example\\""' in source
    assert 'function importCsvFromDrive()' in source
    assert 'knownEmails.has(email)' in source
    assert 'isValidEmail(email)' in source
    assert 'function safeCell(value)' in source
    assert 'const lock = LockService.getScriptLock();' in source
    assert 'function confirmAndSendNext()' in source
    assert 'function deleteAllEmailTriggers()' in source
    assert 'function setupDailyTrigger()' in source
    assert 'MailApp.sendEmail' in source
    assert 'attachments: [coverLetter, dossier]' in source
    assert '"SENDING"' in source
    assert '"RETRY"' in source
    assert '"SENT"' in source
    assert '"VERIFY_REQUIRED"' in source
    assert '"A VERIFIER"' not in source
    assert "let mailAccepted = false;" in source
    assert "function isRetryableSendRejection(message)" in source
    assert "if (!mailAccepted && isRetryableSendRejection(detail)" in source
    assert "attempt.verifyCount = Number(attempt.verifyCount || 0) + 1;" in source
    assert "if (attempt.verifyCount >= CONFIG.MAX_ATTEMPTS)" in source
    assert 'account rotation' not in source.casefold()


def test_zero_daily_limit_disables_sending_and_invalid_google_config_is_rejected():
    source = applications.apps_script_source(_config(max_per_day=0))
    assert 'MAX_PER_DAY: 0' in source
    assert "if (!isWorkingDay() || CONFIG.MAX_PER_DAY <= 0)" in source

    with pytest.raises(ValueError, match="Google-Sheets-URL"):
        applications.apps_script_source(_config(
            spreadsheet_url="https://docs.google.com.evil.example/spreadsheets/d/sheet_id"
        ))

    with pytest.raises(ValueError, match="Verzögerungen"):
        applications.apps_script_source(_config(min_delay_minutes=5, max_delay_minutes=2))


def test_script_config_normalizes_real_style_google_resource_urls_and_ids():
    sheet_id = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"
    doc_id = "1FAIpQLSc7g2exampleDocTemplate_ABC-123"
    pdf_id = "1xYz_Example-PDF-FileId1234567890"
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit?gid=0#gid=0"
    values = _config(
        spreadsheet_url=sheet_url,
        template_doc_id=f"https://docs.google.com/document/d/{doc_id}/edit?usp=sharing",
        static_pdf_id=f"https://drive.google.com/file/d/{pdf_id}/view?usp=drive_link",
    )

    normalized = applications.normalize_script_config(values)
    applications.validate_script_config(values)
    script = applications.apps_script_source(values)

    assert normalized["spreadsheet_url"] == sheet_url
    assert normalized["template_doc_id"] == doc_id
    assert normalized["static_pdf_id"] == pdf_id
    assert f'SPREADSHEET_URL: "{sheet_url}"' in script
    assert f'TEMPLATE_DOC_ID: "{doc_id}"' in script
    assert f'STATIC_PDF_ID: "{pdf_id}"' in script

    id_only = applications.normalize_script_config(_config(
        spreadsheet_url=sheet_id,
        template_doc_id=doc_id,
        static_pdf_id=pdf_id,
    ))
    assert id_only["spreadsheet_url"] == f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    assert id_only["template_doc_id"] == doc_id
    assert id_only["static_pdf_id"] == pdf_id


def test_script_config_accepts_drive_open_link_but_rejects_untrusted_file_urls():
    pdf_id = "1xYz_Example-PDF-FileId1234567890"
    normalized = applications.normalize_script_config(_config(
        static_pdf_id=f"https://drive.google.com/open?id={pdf_id}",
    ))
    assert normalized["static_pdf_id"] == pdf_id

    with pytest.raises(ValueError, match="Google-Drive-Datei-IDs"):
        applications.apps_script_source(_config(
            template_doc_id="https://docs.google.com.evil.example/document/d/doc_id/edit",
        ))
