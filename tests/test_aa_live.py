# -*- coding: utf-8 -*-
import io
from unittest.mock import ANY, Mock, call, patch

from openpyxl import load_workbook

from scraper.aa_live import (
    BrowserContacts, ExtractionJob, clean_emails, export_xlsx, format_date_de,
    parse_contact_text, parse_offer, search_offers,
)
from scraper.arbeitsagentur import SearchResult


def test_parse_offer_aa_live():
    item = {
        "arbeitgeber": "ACME GmbH",
        "arbeitsort": {"plz": "10115", "ort": "Berlin", "strasse": "Hauptstr. 1", "region": "Berlin"},
        "eintrittsdatum": "2026-09-01",
        "refnr": "123",
        "titel": "Ausbildung",
    }
    r = parse_offer(item)
    assert r["firma"] == "ACME GmbH"
    assert r["adresse"] == "Hauptstr. 1 | 10115 Berlin"
    assert r["beginn"] == "01.09.2026"
    assert r["link"].endswith("123")


def test_format_date_de():
    assert format_date_de("2026-09-01") == "01.09.2026"
    assert format_date_de("") == ""


def test_clean_emails():
    assert "a@firma.de" in clean_emails("x a@firma.de y")
    assert clean_emails("x logo.png@x.de") == []


def test_parse_contact_text():
    text = "Ansprechperson: Frau Berger\nTel.: +49 30 12345\nE-Mail: berger@firma.de"
    person, email, phone = parse_contact_text(text)
    assert "Frau Berger" in person
    assert email == "berger@firma.de"
    assert phone != ""


def test_captcha_selectors_match_the_live_jobdetail_captcha_image():
    assert "#kontaktdaten-captcha-image" in BrowserContacts.CAPTCHA_SELECTORS
    assert "#jobdetails-kontaktdaten-container" in BrowserContacts.CONTACT_READY_SELECTOR


def test_captcha_input_and_submit_selectors_match_the_live_markup():
    assert BrowserContacts.CAPTCHA_IMAGE_SELECTOR == "#kontaktdaten-captcha-image"
    assert "#kontaktdaten-captcha-input" in BrowserContacts.CAPTCHA_INPUT_SELECTORS
    assert "#kontaktdaten-captcha-absenden-button" in BrowserContacts.CAPTCHA_SUBMIT_SELECTORS


def test_answer_captcha_closes_the_dialog_and_wakes_the_worker():
    params = {"angebotsart": 4, "locations": [], "was": "", "umkreis": 0,
              "seit": None, "year": "Alle", "max_results": 0, "headed": False}
    job = ExtractionJob(params)
    job.captcha_png = b"captcha-image"
    job.captcha_event.clear()

    job.answer_captcha("AB12")

    assert job.captcha_png is None
    assert job.captcha_answer == "AB12"
    assert job.captcha_event.is_set()

    job.answer_captcha("")
    assert job.captcha_answer is None


def test_answer_captcha_can_be_waited_on_by_the_worker():
    params = {"angebotsart": 4, "locations": [], "was": "", "umkreis": 0,
              "seit": None, "year": "Alle", "max_results": 0, "headed": False}
    job = ExtractionJob(params)
    job.captcha_png = b"captcha-image"
    job.captcha_event.clear()
    job.answer_captcha("XY99")

    assert job.captcha_event.wait(0.1)
    assert job.captcha_answer == "XY99"


def test_search_offers_uses_verified_v6_connector():
    result = SearchResult(
        reported_total=2,
        unique_count=2,
        status="complete",
        listings=[
            {"reference_number": "1", "name": "A", "title": "Ausbildung", "street": "Main 1", "zip": "10115", "city": "Berlin", "start_date": "2026-09-01", "url": "https://example.test/1"},
            {"reference_number": "2", "name": "B", "title": "Ausbildung", "street": "", "zip": "20095", "city": "Hamburg", "start_date": "", "url": "https://example.test/2"},
        ],
    )
    totals = []
    with patch("scraper.aa_live.verified_search_all", return_value=result) as search:
        out = list(search_offers(4, wo="Berlin", was="IT", page_size=25, on_total=totals.append))

    search.assert_called_once_with("IT", "Berlin", angebotsart=4, radius=None,
                                   veroeffentlichtseit=None, page_size=25, log=ANY)
    assert totals == [2]
    assert [offer["refnr"] for offer in out] == ["1", "2"]
    assert out[0]["adresse"] == "Main 1 | 10115 Berlin"
    assert out[0]["beginn"] == "01.09.2026"


def test_job_state_done():
    job = ExtractionJob({"angebotsart": 4, "locations": [], "was": "", "umkreis": 0, "seit": None, "year": "Alle", "max_results": 0, "headed": False})
    # don't run thread; manually set
    job.status = "done"
    assert job.status == "done"
    job2 = ExtractionJob({"angebotsart": 4, "locations": [], "was": "", "umkreis": 0, "seit": None, "year": "Alle", "max_results": 0, "headed": False})
    job2.status = "stopped"
    assert job2.status == "stopped"


def test_jobboerse_excel_matches_the_application_template_order():
    workbook = load_workbook(io.BytesIO(export_xlsx([{
        "firma": "ACME GmbH",
        "person": "Frau Beispiel",
        "adresse": "Musterstraße 1 | 10115 Berlin",
        "beginn": "01.09.2027",
        "email": "bewerbung@acme.de; jobs@acme.de",
        "telefonnummer": "+49 30 123456",
        "link": "https://www.arbeitsagentur.de/jobsuche/jobdetail/123",
    }])))
    sheet = workbook["Sheet1"]

    assert [cell.value for cell in sheet[1]] == [
        "firma", "person", "adresse", "beginn", "email", "telefonnummer", "link",
    ]
    assert [cell.value for cell in sheet[2]] == [
        "ACME GmbH", "Frau Beispiel", "Musterstraße 1 | 10115 Berlin",
        "01.09.2027", "bewerbung@acme.de; jobs@acme.de", "+49 30 123456",
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/123",
    ]


def test_website_fallback_keeps_all_company_emails_and_uses_city_in_cache_key():
    params = {"angebotsart": 4, "locations": [], "was": "", "umkreis": 0,
              "seit": None, "year": "Alle", "max_results": 0, "headed": False}
    job = ExtractionJob(params)
    website_finder = Mock()
    website_finder.find_website.return_value = "https://acme.de"
    email_finder = Mock()
    email_finder.find.return_value = {
        "emails": ["bewerbung@acme.de", "jobs@acme.de"],
        "person": "Frau Beispiel",
    }
    job._contact_finders = Mock(return_value=(website_finder, email_finder))

    offer = {"firma": "ACME GmbH", "city": "Berlin", "email": "", "person": ""}
    job._fill_from_website(offer)

    assert offer["emails"] == ["bewerbung@acme.de", "jobs@acme.de"]
    assert offer["email"] == "bewerbung@acme.de; jobs@acme.de"
    assert offer["person"] == "Frau Beispiel"

    job._fill_from_website({"firma": "ACME GmbH", "city": "Hamburg", "email": "", "person": ""})

    assert website_finder.find_website.call_args_list == [
        call("ACME GmbH", "Berlin"), call("ACME GmbH", "Hamburg"),
    ]
