# -*- coding: utf-8 -*-
import io
from unittest.mock import patch, MagicMock

from scraper.arbeitsagentur import parse_offer, search_all


def test_parse_offer_arbeitsagentur():
    offer = {
        "referenznummer": "RN1",
        "firma": "ACME",
        "stellenangebotsTitel": "Ausbildung",
        "stellenlokationen": [{"adresse": {"strasse": "Hauptstr", "hausnummer": "1", "plz": "10115", "ort": "Berlin"}}],
        "eintrittszeitraum": "2026-09-01",
    }
    r = parse_offer(offer)
    assert r["reference_number"] == "RN1"
    assert r["name"] == "ACME"
    assert r["zip"] == "10115"
    assert r["city"] == "Berlin"
    assert r["url"].endswith("RN1")


def test_parse_offer_flattens_period_objects():
    offer = {
        "referenznummer": "RN2",
        "firma": "ACME",
        "stellenangebotsTitel": "Ausbildung",
        "eintrittszeitraum": {"von": "2027-09-01", "bis": "2027-10-01"},
        "veroeffentlichungszeitraum": {"von": "2026-11-13"},
    }
    r = parse_offer(offer)
    assert r["start_date"] == "2027-09-01"
    assert r["publication_period"] == "2026-11-13"
    assert "{" not in r["start_date"]


def test_search_all_pagination():
    page1 = {"maxErgebnisse": 2, "ergebnisliste": [{"referenznummer": "1"}, {"referenznummer": "2"}]}
    page2 = {"maxErgebnisse": 2, "ergebnisliste": []}
    with patch("scraper.arbeitsagentur._get_with_retries") as m:
        r1 = MagicMock()
        r1.status_code = 200
        r1.json.return_value = page1
        r2 = MagicMock()
        r2.status_code = 200
        r2.json.return_value = page2
        m.side_effect = [r1, r2]
        res = search_all(keyword="x", city="Berlin", delay_seconds=0)
    assert res.unique_count == 2
    assert res.status == "complete"