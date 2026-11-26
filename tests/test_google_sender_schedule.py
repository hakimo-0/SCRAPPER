import json

import pytest

from scraper import crm, google_sender_schedule, google_workspace


def _schedule(**overrides):
    config = {
        "enabled": True,
        "schedule_time": "09:15",
        "spreadsheet_id": "sheet_id_123",
        "sheet_name": "BEWERBUNG",
        "template_doc_id": "template_id_123",
        "static_pdf_id": "pdf_id_123",
        "sender_name": "Hakim Example",
        "recipients": [{"lead_id": 1, "email": "jobs@example.de"}],
        "subject_template": "Bewerbung bei {COMPANY}",
        "message_template": "Guten Tag, {SALUTATION}",
        "daily_limit": 10,
        "min_delay_minutes": 1,
        "max_delay_minutes": 2,
        "max_attempts": 3,
        "continue_on_error": True,
    }
    config.update(overrides)
    return config


def _setup_worker(tmp_path, monkeypatch, *, send_gmail, update_sheet_status=None, row_count=1):
    config_path = tmp_path / "output" / "google_send_schedule.json"
    crm_path = tmp_path / "output" / "crm.sqlite"
    runs_path = tmp_path / "output" / "google_send_runs.sqlite"
    leads = [
        {
            "firma": f"Example {index} GmbH",
            "email": f"jobs{index}@beispiel-gmbh.de",
            "source": "Test",
            "link": f"https://example.de/company/{index}",
        }
        for index in range(row_count)
    ]
    crm.import_leads(crm_path, leads)
    imported_leads = crm.list_leads(crm_path)
    google_sender_schedule.save_config(
        config_path,
        _schedule(recipients=[{
            "lead_id": int(lead["id"]),
            "email": lead["email"],
        } for lead in imported_leads]),
    )
    workspace = google_workspace.GoogleWorkspaceConfig(
        client_id="client.apps.googleusercontent.com",
        client_secret="secret",
        redirect_uri="http://localhost:8501/",
        spreadsheet_id="sheet_id_123",
        template_doc_id="template_id_123",
        static_pdf_id="pdf_id_123",
        sender_name="Hakim Example",
    )
    row_numbers = {
        lead["email"].casefold(): index + 2
        for index, lead in enumerate(imported_leads)
    }
    statuses = []

    monkeypatch.setattr(google_sender_schedule, "_workspace_config", lambda _path: workspace)
    monkeypatch.setattr(
        google_workspace, "restore_persisted_session",
        lambda _config: ({"access_token": "test-token"}, "sender@example.de"),
    )
    def sync_rows(_config, _tokens, _spreadsheet, _sheet, rows, **_kwargs):
        return {
            "ready_rows": [{
                "email": row["Email"].casefold(),
                "row_number": row_numbers[row["Email"].casefold()],
                "status": "",
            } for row in rows],
            "sent_rows": [],
            "inserted": 0,
            "skipped": 0,
        }

    monkeypatch.setattr(google_workspace, "sync_rows_to_sheet", sync_rows)
    monkeypatch.setattr(google_workspace, "personalized_cover_pdf", lambda *_args, **_kwargs: b"cover")
    monkeypatch.setattr(google_workspace, "_drive_file_bytes", lambda *_args, **_kwargs: b"dossier")
    monkeypatch.setattr(google_workspace, "send_gmail", send_gmail)
    monkeypatch.setattr(google_workspace, "retry_delay_seconds", lambda _attempt: 0)
    monkeypatch.setattr(google_sender_schedule, "_wait_or_stopped", lambda *_args: True)

    def set_status(_config, _tokens, _spreadsheet, _sheet, row_number, status, _sent_at):
        statuses.append((row_number, status))
        if update_sheet_status:
            update_sheet_status(row_number, status)

    monkeypatch.setattr(google_workspace, "update_sheet_status", set_status)
    return config_path, crm_path, runs_path, imported_leads, statuses


def test_schedule_config_validation_launcher_and_task_registration(tmp_path, monkeypatch):
    config_path = tmp_path / "output" / "google_send_schedule.json"
    launcher_path = tmp_path / "output" / "run.cmd"
    google_sender_schedule.save_config(config_path, _schedule())
    launcher = google_sender_schedule.write_launcher(
        config_path,
        launcher_path,
        project_dir=tmp_path,
        python_executable=tmp_path / "venv" / "python.exe",
    )
    content = launcher.read_text(encoding="utf-8")
    assert "-m scraper.google_sender_schedule" in content
    assert json.loads(config_path.read_text(encoding="utf-8"))["enabled"] is True

    registered = {}

    def fake_register(*args, **kwargs):
        registered["args"] = args
        registered["kwargs"] = kwargs

    monkeypatch.setattr(google_sender_schedule.local_schedule, "register_windows_task", fake_register)
    google_sender_schedule.register_task(config_path, launcher_path, "09:15")
    assert registered["args"] == (config_path, launcher_path, "09:15")
    assert registered["kwargs"]["task_name"] == google_sender_schedule.TASK_NAME
    with pytest.raises(ValueError, match="at least one recipient"):
        google_sender_schedule.save_config(
            config_path, _schedule(recipients=[]),
        )


def test_scheduled_send_retries_transient_error_and_persists_confirmed_send(tmp_path, monkeypatch):
    calls = []

    def send(*_args, **_kwargs):
        calls.append("send")
        if len(calls) == 1:
            raise google_workspace.GoogleWorkspaceError("Gmail rate limited", transient=True)
        return "gmail-message-id"

    config_path, crm_path, runs_path, leads, statuses = _setup_worker(
        tmp_path, monkeypatch, send_gmail=send,
    )
    result = google_sender_schedule.run_scheduled_send(
        config_path,
        project_dir=tmp_path,
        crm_database_path=crm_path,
        runs_database_path=runs_path,
    )

    ledger = crm.google_send_ledger(crm_path, leads[0]["email"])
    assert result == {
        "status": "complete",
        "sent": 1,
        "verify_required": 0,
        "errors": 0,
        "sync_pending": 0,
    }
    assert len(calls) == 2
    assert ledger["state"] == "SENT"
    assert ledger["message_id"] == "gmail-message-id"
    assert (2, "RETRY") in statuses
    assert statuses.count((2, "SENDING")) == 2
    assert statuses[-1] == (2, "SENT")
    assert google_sender_schedule.recent_runs(runs_path)[0]["status"] == "complete"


def test_ambiguous_gmail_result_is_not_retried_and_needs_review(tmp_path, monkeypatch):
    calls = []

    def send(*_args, **_kwargs):
        calls.append("send")
        raise google_workspace.GoogleWorkspaceError(
            "Gmail send result is unknown", ambiguous=True,
        )

    config_path, crm_path, runs_path, leads, statuses = _setup_worker(
        tmp_path, monkeypatch, send_gmail=send,
    )
    result = google_sender_schedule.run_scheduled_send(
        config_path,
        project_dir=tmp_path,
        crm_database_path=crm_path,
        runs_database_path=runs_path,
    )

    assert len(calls) == 1
    assert result["status"] == "needs_review"
    assert result["verify_required"] == 1
    assert crm.google_send_ledger(crm_path, leads[0]["email"])["state"] == "VERIFY_REQUIRED"
    assert statuses[-1] == (2, "VERIFY_REQUIRED")


def test_sheet_finalize_failure_after_gmail_confirmation_never_resends(tmp_path, monkeypatch):
    calls = []

    def send(*_args, **_kwargs):
        calls.append("send")
        return "gmail-message-id"

    def fail_sent_status(_row, status):
        if status == "SENT":
            raise google_workspace.GoogleWorkspaceError(
                "Sheets unavailable after Gmail confirmation", transient=True,
            )

    config_path, crm_path, runs_path, leads, _statuses = _setup_worker(
        tmp_path,
        monkeypatch,
        send_gmail=send,
        update_sheet_status=fail_sent_status,
    )
    result = google_sender_schedule.run_scheduled_send(
        config_path,
        project_dir=tmp_path,
        crm_database_path=crm_path,
        runs_database_path=runs_path,
    )

    assert calls == ["send"]
    assert crm.google_send_ledger(crm_path, leads[0]["email"])["state"] == "SENT"
    assert result["status"] == "sync_pending"
    assert result["sync_pending"] == 1
    assert crm.list_leads(crm_path)[0]["status"] == "contacted"
    assert len(crm.list_activities(crm_path, leads[0]["id"])) == 1


def test_existing_sent_ledger_reconciles_crm_without_sending_again(tmp_path, monkeypatch):
    def unexpected_send(*_args, **_kwargs):
        raise AssertionError("A confirmed recipient must not be sent again.")

    config_path, crm_path, runs_path, leads, _statuses = _setup_worker(
        tmp_path, monkeypatch, send_gmail=unexpected_send,
    )
    sent_at = google_sender_schedule.datetime.now().astimezone().isoformat(timespec="seconds")
    crm.record_google_send(
        crm_path, leads[0]["email"], "confirmed-message-id", sent_at,
        lead_id=leads[0]["id"], spreadsheet_id="sheet_id_123", sheet_row=2,
    )

    result = google_sender_schedule.run_scheduled_send(
        config_path,
        project_dir=tmp_path,
        crm_database_path=crm_path,
        runs_database_path=runs_path,
    )

    assert result["status"] == "complete"
    assert result["sent"] == 0
    assert crm.list_leads(crm_path)[0]["status"] == "contacted"
    assert len(crm.list_activities(crm_path, leads[0]["id"])) == 1


def test_scheduled_send_honors_daily_limit_and_records_sent_count(tmp_path, monkeypatch):
    calls = []

    def send(*_args, **_kwargs):
        calls.append("send")
        return f"gmail-message-{len(calls)}"

    config_path, crm_path, runs_path, _leads, _statuses = _setup_worker(
        tmp_path, monkeypatch, send_gmail=send, row_count=2,
    )
    config = google_sender_schedule.load_config(config_path)
    config["daily_limit"] = 1
    google_sender_schedule.save_config(config_path, config)

    result = google_sender_schedule.run_scheduled_send(
        config_path,
        project_dir=tmp_path,
        crm_database_path=crm_path,
        runs_database_path=runs_path,
    )

    assert len(calls) == 1
    assert result["status"] == "daily_limit"
    assert result["sent"] == 1
    assert crm.google_sends_today(
        crm_path, google_sender_schedule.datetime.now().astimezone().date().isoformat(),
    ) == 1


def test_permanent_failure_stops_when_continue_on_error_is_disabled(tmp_path, monkeypatch):
    calls = []

    def send(*_args, **_kwargs):
        calls.append("send")
        raise google_workspace.GoogleWorkspaceError("Gmail permission denied")

    config_path, crm_path, runs_path, leads, _statuses = _setup_worker(
        tmp_path, monkeypatch, send_gmail=send, row_count=2,
    )
    config = google_sender_schedule.load_config(config_path)
    config["continue_on_error"] = False
    google_sender_schedule.save_config(config_path, config)

    result = google_sender_schedule.run_scheduled_send(
        config_path,
        project_dir=tmp_path,
        crm_database_path=crm_path,
        runs_database_path=runs_path,
    )

    assert len(calls) == 1
    assert result["status"] == "needs_review"
    assert result["errors"] == 1
    assert crm.google_send_ledger(crm_path, leads[0]["email"])["state"] == "ERROR"


def test_stop_during_inter_row_delay_prevents_next_recipient(tmp_path, monkeypatch):
    calls = []

    def send(*_args, **_kwargs):
        calls.append("send")
        return "gmail-message-id"

    config_path, crm_path, runs_path, _leads, _statuses = _setup_worker(
        tmp_path, monkeypatch, send_gmail=send, row_count=2,
    )
    monkeypatch.setattr(google_sender_schedule, "_wait_or_stopped", lambda *_args: False)

    result = google_sender_schedule.run_scheduled_send(
        config_path,
        project_dir=tmp_path,
        crm_database_path=crm_path,
        runs_database_path=runs_path,
    )

    assert len(calls) == 1
    assert result["status"] == "stopped"
    assert result["sent"] == 1
    assert google_sender_schedule.recent_runs(runs_path)[0]["status"] == "stopped"


def test_scheduled_send_only_processes_the_confirmed_recipient_snapshot(tmp_path, monkeypatch):
    sent_to = []

    def send(*_args, **kwargs):
        sent_to.append(kwargs["to"])
        return "gmail-message-id"

    config_path, crm_path, runs_path, leads, _statuses = _setup_worker(
        tmp_path, monkeypatch, send_gmail=send, row_count=2,
    )
    selected = leads[0]
    schedule = google_sender_schedule.load_config(config_path)
    schedule["recipients"] = [{
        "lead_id": selected["id"],
        "email": selected["email"],
    }]
    google_sender_schedule.save_config(config_path, schedule)

    result = google_sender_schedule.run_scheduled_send(
        config_path,
        project_dir=tmp_path,
        crm_database_path=crm_path,
        runs_database_path=runs_path,
    )

    assert result["sent"] == 1
    assert sent_to == [selected["email"]]
    assert crm.google_send_ledger(crm_path, selected["email"])["state"] == "SENT"
    other = next(lead for lead in leads if lead["id"] != selected["id"])
    assert crm.google_send_ledger(crm_path, other["email"]) is None


def test_changed_scheduled_email_is_skipped_for_review(tmp_path, monkeypatch):
    def unexpected_send(*_args, **_kwargs):
        raise AssertionError("A changed recipient address must not be sent.")

    config_path, crm_path, runs_path, leads, _statuses = _setup_worker(
        tmp_path, monkeypatch, send_gmail=unexpected_send,
    )
    crm.import_leads(crm_path, [{
        "firma": leads[0]["firma"],
        "email": "changed@beispiel-gmbh.de",
        "source": leads[0]["source"],
        "link": leads[0]["source_url"],
    }])

    result = google_sender_schedule.run_scheduled_send(
        config_path,
        project_dir=tmp_path,
        crm_database_path=crm_path,
        runs_database_path=runs_path,
    )

    assert result["status"] == "needs_review"
    assert result["sent"] == 0
    assert result["errors"] == 1
