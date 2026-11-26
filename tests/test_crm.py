import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from scraper import crm


def _lead(**overrides):
    row = {
        "firma": " Example GmbH ",
        "person": "Herr Max Beispiel",
        "email": "Info@beispiel-gmbh.de",
        "telefonnummer": "+49 30 12345678",
        "adresse": "Hauptstrasse 1",
        "city": "Berlin",
        "source": "Google Maps",
        "link": "https://maps.example/place/1",
        "website": "https://example.de",
    }
    row.update(overrides)
    return row


def test_import_deduplicates_and_preserves_crm_fields(tmp_path):
    database = tmp_path / "crm.sqlite"
    first = crm.import_leads(
        database, [_lead(), _lead(email="bewerbung@beispiel-gmbh.de")],
        processing_purpose="Recruiting outreach", legal_basis="Review required",
    )
    lead = crm.list_leads(database)[0]
    crm.update_lead(
        database, lead["id"], status="follow_up",
        follow_up_at="2026-12-01", notes="Call next week",
    )

    second = crm.import_leads(database, [_lead(email="jobs@beispiel-gmbh.de")])
    stored = crm.list_leads(database)[0]

    assert first == {"inserted": 1, "updated": 1, "suppressed": 0, "skipped": 0}
    assert second["updated"] == 1
    assert stored["email"] == "jobs@beispiel-gmbh.de"
    assert stored["status"] == "follow_up"
    assert stored["follow_up_at"] == "2026-12-01"
    assert stored["notes"] == "Call next week"
    assert stored["processing_purpose"] == "Recruiting outreach"
    assert stored["source"] == "Google Maps"


def test_activity_and_crm_records_are_durable(tmp_path):
    database = tmp_path / "crm.sqlite"
    crm.import_leads(database, [_lead()])
    lead_id = crm.list_leads(database)[0]["id"]

    activity_id = crm.add_activity(database, lead_id, "call", "Asked for a follow-up email.")
    activity = crm.list_activities(database, lead_id)[0]

    assert activity["id"] == activity_id
    assert activity["lead_id"] == lead_id
    assert activity["activity_type"] == "call"
    assert activity["details"] == "Asked for a follow-up email."
    assert activity["created_at"]
    assert crm.list_leads(database)[0]["firma"] == "Example GmbH"


def test_list_leads_treats_all_ui_sentinel_and_empty_statuses_as_unfiltered(tmp_path):
    database = tmp_path / "crm.sqlite"
    crm.import_leads(database, [
        _lead(),
        _lead(
            firma="Second Example GmbH",
            email="jobs@second-example.de",
            link="https://maps.example/place/2",
        ),
    ])
    leads = crm.list_leads(database)
    crm.update_lead(database, leads[0]["id"], status="contacted")

    all_statuses = crm.list_leads(database, status="Alle")
    no_status = crm.list_leads(database, status=None)
    empty_status = crm.list_leads(database, status="")

    assert {lead["id"] for lead in all_statuses} == {lead["id"] for lead in no_status}
    assert {lead["id"] for lead in all_statuses} == {lead["id"] for lead in empty_status}
    assert {lead["status"] for lead in all_statuses} == {"new", "contacted"}
    assert len(crm.list_leads(database, status="contacted")) == 1


def test_google_send_ledger_reserves_retries_and_prevents_duplicate_recipients(tmp_path):
    database = tmp_path / "crm.sqlite"
    crm.import_leads(database, [_lead()])
    lead_id = crm.list_leads(database)[0]["id"]

    assert crm.reserve_google_send(
        database, "Info@beispiel-gmbh.de", lead_id=lead_id,
        spreadsheet_id="sheet-id", sheet_row=2,
    ) == 1
    assert crm.reserve_google_send(
        database, "info@beispiel-gmbh.de", lead_id=lead_id,
        spreadsheet_id="sheet-id", sheet_row=2,
    ) is None

    crm.update_google_send_state(
        database, "info@beispiel-gmbh.de", "RETRY",
        last_error="temporary Gmail failure",
        next_retry_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    assert crm.reserve_google_send(
        database, "info@beispiel-gmbh.de", lead_id=lead_id,
        spreadsheet_id="sheet-id", sheet_row=2,
    ) == 2
    assert crm.record_google_send(
        database, "info@beispiel-gmbh.de", "gmail-message-id", "2026-04-04T12:00:00Z",
        lead_id=lead_id, spreadsheet_id="sheet-id", sheet_row=2,
    )
    assert crm.google_send_ledger(database, "INFO@beispiel-gmbh.de")["state"] == "SENT"
    assert crm.reserve_google_send(
        database, "INFO@beispiel-gmbh.de", lead_id=lead_id,
        spreadsheet_id="sheet-id", sheet_row=2,
    ) is None


def test_confirmed_google_send_reconciliation_is_idempotent_and_respects_opt_out(tmp_path):
    database = tmp_path / "crm.sqlite"
    crm.import_leads(database, [_lead()])
    lead = crm.list_leads(database)[0]
    crm.record_google_send(
        database, lead["email"], "gmail-message-id", "2026-04-04T12:00:00Z",
        lead_id=lead["id"], spreadsheet_id="sheet-id", sheet_row=2,
    )

    assert crm.record_confirmed_google_contact(
        database, lead["id"], "gmail-message-id", "2026-04-04T12:00:00Z",
    )
    assert crm.record_confirmed_google_contact(
        database, lead["id"], "gmail-message-id", "2026-04-04T12:00:00Z",
    )
    stored = crm.list_leads(database)[0]
    activities = crm.list_activities(database, lead["id"])
    assert stored["status"] == "contacted"
    assert len(activities) == 1
    assert "message_id=gmail-message-id" in activities[0]["details"]

    crm.opt_out_lead(database, lead["id"], "Opt-out")
    assert not crm.record_confirmed_google_contact(
        database, lead["id"], "gmail-message-id", "2026-04-04T12:00:00Z",
    )
    assert crm.list_leads(database, status="opt_out")[0]["status"] == "opt_out"


def test_opt_out_scrubs_contact_data_and_suppresses_reimport(tmp_path):
    database = tmp_path / "crm.sqlite"
    original = _lead()
    crm.import_leads(database, [original])
    lead = crm.list_leads(database)[0]

    crm.opt_out_lead(database, lead["id"], "Requested no further contact")
    stored = crm.list_leads(database, status="opt_out")[0]
    imported_again = crm.import_leads(database, [original])

    assert stored["firma"] == stored["email"] == stored["telefonnummer"] == ""
    assert imported_again["suppressed"] == 1
    assert len(crm.list_leads(database)) == 1
    assert crm.list_gdpr_actions(database)[0]["action"] == "opt_out"


def test_delete_erases_lead_and_activity_but_keeps_non_identifying_audit(tmp_path):
    database = tmp_path / "crm.sqlite"
    crm.import_leads(database, [_lead()])
    lead = crm.list_leads(database)[0]
    crm.add_activity(database, lead["id"], "note", "Private note")

    crm.delete_lead(database, lead["id"], "Erasure request")

    assert crm.list_leads(database) == []
    assert crm.list_activities(database, lead["id"]) == []
    action = crm.list_gdpr_actions(database)[0]
    assert action["action"] == "delete"
    assert action["reason"] == "Erasure request"
    assert lead["email"] not in str(action)
    assert lead["firma"] not in str(action)


def test_retention_and_analytics_metrics(tmp_path):
    database = tmp_path / "crm.sqlite"
    crm.import_leads(database, [
        _lead(),
        _lead(
            firma="No Email GmbH",
            email="",
            link="https://maps.example/place/2",
            city="Bonn",
        ),
    ])
    crm.record_export(database, "csv", 2)

    metrics = crm.metrics(database)

    assert metrics["total"] == 2
    assert metrics["with_email"] == 1
    assert metrics["email_coverage"] == 50
    assert metrics["source_counts"] == [{"source": "Google Maps", "count": 2}]
    assert metrics["new_last_7_days"] == 2
    assert metrics["exports"] == 1

    lead_id = crm.list_leads(database)[0]["id"]
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE crm_leads SET retention_until = ? WHERE id = ?", (expired, lead_id)
        )

    assert crm.expired_count(database) == 1
    assert crm.prune_expired(database) == 1
    assert crm.metrics(database)["total"] == 1


def test_contact_validation_keeps_supported_email_and_phone_formats():
    assert crm.normalize_phone("+49 (30) 1234-5678") == "+49 (30) 1234-5678"
    assert crm.normalize_phone("Call me at 12345678") == ""
    assert crm.normalize_phone("12345") == ""


def test_follow_up_date_must_be_iso_formatted(tmp_path):
    database = tmp_path / "crm.sqlite"
    crm.import_leads(database, [_lead()])
    lead_id = crm.list_leads(database)[0]["id"]

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        crm.update_lead(database, lead_id, status="follow_up", follow_up_at="next week")


def test_lead_score_exposes_weighted_components_and_city_match():
    points = crm.score_breakdown(
        {
            "email": "jobs@example.de",
            "person": "Max Beispiel",
            "telefonnummer": "+49 30 12345678",
            "source": "Google Maps",
            "city": "Berlin",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "status": "new",
        },
        target_city="berlin",
    )

    assert points == {
        "email": 25,
        "person": 10,
        "phone": 10,
        "source": 8,
        "city": 15,
        "recentness": 15,
        "workflow": 8,
        "total": 91,
    }


def test_pipeline_status_keeps_legacy_status_and_schedules_follow_up(tmp_path):
    database = tmp_path / "crm.sqlite"
    crm.import_leads(database, [_lead()])
    lead_id = crm.list_leads(database)[0]["id"]
    crm.set_follow_up_days(database, 7)

    crm.update_lead(database, lead_id, status="sent")

    stored = crm.list_leads(database)[0]
    with sqlite3.connect(database) as connection:
        legacy_status = connection.execute(
            "SELECT status FROM crm_leads WHERE id = ?", (lead_id,)
        ).fetchone()[0]
    assert stored["status"] == "sent"
    assert legacy_status == "contacted"
    assert crm.list_leads(database, status="contacted")[0]["id"] == lead_id
    assert stored["follow_up_at"] == (datetime.now(timezone.utc).date() + timedelta(days=7)).isoformat()
    assert crm.get_follow_up_days(database) == 7
    assert crm.due_follow_ups(database, today=date.fromisoformat(stored["follow_up_at"]))[0]["id"] == lead_id


def test_score_filters_and_due_follow_up_metrics(tmp_path):
    database = tmp_path / "crm.sqlite"
    crm.import_leads(database, [
        _lead(),
        _lead(
            firma="No Email GmbH",
            email="",
            person="",
            telefonnummer="",
            source="",
            city="",
            link="https://example.test/no-email",
        ),
    ])
    leads = crm.list_leads(database)
    high_score = next(row for row in leads if row["firma"] == "Example GmbH")
    low_score = next(row for row in leads if row["firma"] == "No Email GmbH")
    crm.update_lead(
        database, high_score["id"], status="follow_up",
        follow_up_at=(datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat(),
    )

    assert high_score["lead_score"] > low_score["lead_score"]
    assert crm.list_leads(database, status="follow_up")[0]["id"] == high_score["id"]
    assert [row["id"] for row in crm.due_follow_ups(database)] == [high_score["id"]]
    metrics = crm.metrics(database)
    assert metrics["pipeline_counts"] == [{"status": "follow_up", "count": 1}, {"status": "new", "count": 1}]
    assert metrics["due_follow_ups"] == 1
    assert metrics["average_score"] > 0


def test_follow_up_schedule_rejects_unsupported_days(tmp_path):
    with pytest.raises(ValueError, match="Follow-up schedule"):
        crm.set_follow_up_days(tmp_path / "crm.sqlite", 5)
