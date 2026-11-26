# -*- coding: utf-8 -*-
import io
import time
from openpyxl import load_workbook

from scraper.leads import (
    best_email, deobfuscate_text, decode_cfemail, normalise_firma, merge_leads,
    export_leads_xlsx, EXCEL_COLUMNS, WebsiteEmailFinder, _extract_person,
)


def test_normalise_firma():
    assert normalise_firma("  ACME   GmbH  ") == "ACME GmbH"


def test_best_email_priority():
    emails = ["info@firma.de", "bewerbung@firma.de", "karriere@firma.de"]
    assert best_email(emails) == "bewerbung@firma.de"


def test_best_email_filtering():
    emails = ["logo.png@x.de", "x@sentry.io", "kontakt@firma.de", "a@example.com"]
    assert best_email(emails) == "kontakt@firma.de"


def test_best_email_none():
    assert best_email([]) is None
    assert best_email(None) is None


def test_deobfuscate():
    text = "mail (at) firma (dot) de and name[at]firma.de"
    out = deobfuscate_text(text)
    assert "mail@firma.de" in out
    assert "name@firma.de" in out


def test_decode_cfemail():
    # encode "test@example.com" with key 0x55
    key = 0x55
    enc = format(key, "02x") + "".join(format(ord(c) ^ key, "02x") for c in "test@example.com")
    assert decode_cfemail(enc) == "test@example.com"


def test_extracts_name_only_when_public_contact_label_precedes_it():
    assert _extract_person("Ansprechperson: Max Mustermann, E-Mail: max@betrieb.de") == "Max Mustermann"
    assert _extract_person("Kontaktperson: Bewerbungs Team") == ""
    assert _extract_person("Über uns: Max Mustermann ist Geschäftsführer") == ""


def test_merge_leads():
    leads = [
        {"firma": "ACME GmbH", "adresse": "Str 1 | 12345 Berlin", "email": "info@acme.de", "person": "", "telefonnummer": "", "link": "u1", "beginn": ""},
        {"firma": "ACME  GmbH", "adresse": "12345 Berlin", "email": "info@acme.de", "person": "Frau X", "telefonnummer": "+49", "link": "u2", "beginn": "01.09.2026"},
        {"firma": "Other", "adresse": "", "email": "", "person": "", "telefonnummer": "", "link": "", "beginn": ""},
    ]
    out = merge_leads(leads)
    assert len(out) == 2
    acme = [l for l in out if l["firma"] == "ACME GmbH"][0]
    assert acme["person"] == "Frau X"
    assert acme["telefonnummer"] == "+49"
    assert acme["beginn"] == "01.09.2026"
    assert acme["adresse"] == "Str 1 | 12345 Berlin"


def test_merge_by_firma_plz():
    leads = [
        {"firma": "ACME", "adresse": "12345 Berlin", "email": "", "person": "", "telefonnummer": "", "link": "", "beginn": ""},
        {"firma": "ACME", "adresse": "12345 Berlin", "email": "a@acme.de", "person": "Herr Y", "telefonnummer": "", "link": "", "beginn": ""},
    ]
    out = merge_leads(leads)
    assert len(out) == 1
    assert out[0]["email"] == "a@acme.de"


def test_export_header_widths_order():
    rows = [
        {"firma": "B Co", "person": "", "adresse": "", "email": "", "telefonnummer": "", "link": "", "beginn": ""},
        {"firma": "A Co", "person": "Herr Z", "adresse": "S | 1 2", "email": "z@a.de", "telefonnummer": "+49", "link": "http://x", "beginn": "01.01.2026"},
    ]
    data = export_leads_xlsx(rows)
    wb = load_workbook(io.BytesIO(data))
    ws = wb["Sheet1"]
    header = [c.value for c in ws[1]]
    assert header == EXCEL_COLUMNS
    assert ws.column_dimensions["A"].width == 55
    assert ws.column_dimensions["B"].width == 30
    for col in "CDEFG":
        assert ws.column_dimensions[col].width == 55
    # email rows first
    assert ws["D2"].value == "z@a.de"
    assert ws["D3"].value is None


def test_export_person_default():
    rows = [{"firma": "X", "person": "", "adresse": "", "email": "", "telefonnummer": "", "link": "", "beginn": ""}]
    data = export_leads_xlsx(rows)
    wb = load_workbook(io.BytesIO(data))
    ws = wb["Sheet1"]
    assert ws["B2"].value == "Damen und Herren"


def test_export_keeps_all_valid_emails_for_one_company():
    rows = [{
        "firma": "ACME",
        "emails": ["Info@acme.de", "kontakt@acme.de", "logo.png@acme.de", "info@acme.de"],
    }]
    data = export_leads_xlsx(rows)
    wb = load_workbook(io.BytesIO(data))

    assert wb["Sheet1"]["D2"].value == "info@acme.de; kontakt@acme.de"


def test_website_finder_counts_failed_pages_against_limit():
    class DeadHttp:
        def __init__(self):
            self.calls = []

        def get_text(self, url, **kwargs):
            self.calls.append(url)
            return None

    http = DeadHttp()
    finder = WebsiteEmailFinder(session=http, max_pages=3, log=lambda _message: None)

    result = finder.find("https://dead.example")

    assert result["emails"] == []
    # One homepage prefetch plus at most max_pages attempts.  Dead paths must
    # not keep the crawler busy through every known candidate.
    assert len(http.calls) <= 4


def test_website_finder_stops_at_company_time_limit():
    class SlowHttp:
        def __init__(self):
            self.calls = 0

        def get_text(self, url, **kwargs):
            self.calls += 1
            time.sleep(0.1)
            return "<html><body>Keine E-Mail hier</body></html>"

    http = SlowHttp()
    finder = WebsiteEmailFinder(session=http, max_pages=20, log=lambda _message: None, timeout=1)
    finder.max_seconds = 0.05

    result = finder.find("https://slow.example")

    assert result["emails"] == []
    assert result["timed_out"] is True
    assert http.calls == 1


def test_website_finder_reads_same_site_javascript_email_obfuscation():
    class Http:
        def get_text(self, url, **_kwargs):
            pages = {
                "https://acme.de": (
                    '<html><a href="/kontakt">Kontakt</a>'
                    '<script src="/assets/contact.js"></script></html>'
                ),
                "https://acme.de/assets/contact.js": (
                    "const jobs = 'jobs' + '@' + 'acme.de'; "
                    'const info = "info\\u0040acme.de";'
                ),
            }
            return pages.get(url)

    finder = WebsiteEmailFinder(session=Http(), max_pages=2, max_scripts=2, log=lambda _message: None)

    result = finder.find("https://acme.de")

    assert result["emails"] == ["jobs@acme.de", "info@acme.de"]
