from unittest.mock import patch

from scraper.generic_job import GenericExtractionJob, _generic_collect


def test_generic_job_normalizes_company_name_and_saves_output():
    job = GenericExtractionJob({"source": "azubiyo", "delay": 0})
    leads = [{
        "name": "  ACME   GmbH  ",
        "street": "Hauptstrasse 1",
        "zip": "10115",
        "city": "Berlin",
        "website": "https://example.test",
        "phone": "",
        "url": "https://example.test/job",
        "source": "Azubiyo",
    }]

    with patch("scraper.generic_job._generic_collect", return_value=(leads, "Azubiyo")), patch(
        "scraper.leads.WebsiteEmailFinder.find", return_value={
            "email": "info@acme.de",
            "emails": ["info@acme.de", "kontakt@acme.de", "jobs@acme.de"],
            "person": "",
        }
    ), patch("scraper.leads.export_leads_xlsx") as export:
        job._run()

    assert job.status == "done"
    assert job.rows[0]["firma"] == "ACME GmbH"
    assert job.rows[0]["emails"] == ["info@acme.de", "kontakt@acme.de", "jobs@acme.de"]
    assert job.rows[0]["email"] == "info@acme.de; kontakt@acme.de; jobs@acme.de"
    assert export.called


def test_google_maps_collects_each_selected_city_separately():
    job = GenericExtractionJob({"source": "google_maps"})
    berlin = [{"name": "Berlin GmbH"}]
    hamburg = [{"name": "Hamburg GmbH"}, {"name": "Nord GmbH"}]

    with patch("scraper.google_maps.places_text_search", side_effect=[berlin, hamburg]) as search:
        leads, label = _generic_collect(
            {
                "source": "google_maps",
                "keyword": "IT Unternehmen",
                "locations": ["Berlin", "Hamburg"],
                "api_key": "test-key",
            },
            job,
        )

    assert label == "Google Maps"
    assert leads == berlin + hamburg
    assert [call.args[1] for call in search.call_args_list] == ["Berlin", "Hamburg"]
    progress = job.snapshot_progress()
    assert progress["locations_total"] == 2
    assert progress["locations_completed"] == 2


def test_azubiyo_collect_uses_keyword_city_search_and_page_limit():
    job = GenericExtractionJob({"source": "azubiyo"})
    result = [{"name": "ACME GmbH"}]

    with patch("scraper.azubiyo.search", return_value=result) as search:
        leads, label = _generic_collect(
            {
                "source": "azubiyo",
                "keyword": "Elektroniker",
                "locations": ["Berlin"],
                "max_pages": 3,
            },
            job,
        )

    assert label == "Azubiyo"
    assert leads == result
    assert search.call_args.args[:2] == ("Elektroniker", "Berlin")
    assert search.call_args.kwargs["max_pages"] == 3


def test_ausbildung_collect_uses_search_then_attaches_websites():
    job = GenericExtractionJob({"source": "ausbildung"})
    berlin = [{"name": "Berlin GmbH"}]
    hamburg = [{"name": "Hamburg GmbH"}]

    with patch("scraper.ausbildung_de.search", side_effect=[berlin, hamburg]) as search, patch(
        "scraper.ausbildung_de.attach_websites"
    ) as attach:
        leads, label = _generic_collect(
            {
                "source": "ausbildung",
                "keyword": "Elektroniker",
                "locations": ["Berlin", "Hamburg"],
            },
            job,
        )

    assert label == "Ausbildung.de"
    assert leads == berlin + hamburg
    assert [call.args[:2] for call in search.call_args_list] == [
        ("Elektroniker", "Berlin"),
        ("Elektroniker", "Hamburg"),
    ]
    assert attach.called


def test_stepstone_collect_uses_public_search():
    job = GenericExtractionJob({"source": "stepstone"})
    result = [{"name": "ACME GmbH", "source": "Stepstone"}]

    with patch("scraper.stepstone.search", return_value=result) as search:
        leads, label = _generic_collect(
            {
                "source": "stepstone",
                "keyword": "Elektroniker",
                "locations": ["Berlin"],
                "max_pages": 2,
            },
            job,
        )

    assert label == "Stepstone"
    assert leads == result
    assert search.call_args.args[:2] == ("Elektroniker", "Berlin")
    assert search.call_args.kwargs["max_pages"] == 2


def test_generic_job_uses_each_row_city_for_website_lookup():
    job = GenericExtractionJob({"source": "azubiyo", "delay": 0})
    leads = [
        {"name": "Berlin GmbH", "city": "Berlin", "url": "https://example.test/1", "source": "AzubiYo"},
        {"name": "Hamburg GmbH", "city": "Hamburg", "url": "https://example.test/2", "source": "AzubiYo"},
    ]
    calls = []

    def fake_find_website(company, city=""):
        calls.append((company, city))
        return ""

    with patch("scraper.generic_job._generic_collect", return_value=(leads, "Azubiyo")), patch(
        "scraper.email_finder.EmailFinder.find_website", side_effect=fake_find_website
    ), patch("scraper.leads.WebsiteEmailFinder.find", return_value={"emails": [], "person": ""}), patch(
        "scraper.leads.export_leads_xlsx"
    ):
        job._run()

    assert calls == [("Berlin GmbH", "Berlin"), ("Hamburg GmbH", "Hamburg")]
    assert job.status == "done"


def test_google_maps_skips_domain_guessing_for_places_without_website():
    job = GenericExtractionJob({"source": "google_maps", "delay": 0})
    leads = [
        {
            "name": "Hotel mit Website", "city": "Tangier",
            "website": "https://hotel.example", "url": "https://maps/1",
        },
        {
            "name": "Hotel ohne Website", "city": "Tangier",
            "website": "", "url": "https://maps/2",
        },
        {
            "name": "Hotel nur auf Portal", "city": "Tangier",
            "website": "https://www.booking.com/hotel/ma/test.html", "url": "https://maps/3",
        },
    ]

    with patch("scraper.generic_job._generic_collect", return_value=(leads, "Google Maps")), patch(
        "scraper.email_finder.EmailFinder.find_website"
    ) as guess, patch(
        "scraper.leads.WebsiteEmailFinder.find",
        return_value={"emails": ["info@hotel-tanger.ma"], "person": ""},
    ) as find, patch("scraper.leads.export_leads_xlsx"):
        job._run()

    assert job.status == "done"
    assert not guess.called
    assert find.call_count == 1
    assert job.rows[0]["email"] == "info@hotel-tanger.ma"
    assert job.rows[1]["website"] == ""
    assert job.rows[2]["website"] == ""
    assert any("ohne Website" in text for _kind, text in job.snapshot_log())


def test_google_maps_parallel_email_extraction_keeps_row_order():
    job = GenericExtractionJob({"source": "google_maps", "delay": 0, "email_workers": 4})
    leads = [
        {
            "name": f"Hotel {index}",
            "city": "Tangier",
            "website": f"https://hotel{index}.ma" if index % 2 == 0 else "",
            "url": f"https://maps/{index}",
        }
        for index in range(12)
    ]

    def fake_find(website):
        return {"emails": [f"info@{website.split('//', 1)[1]}"], "person": ""}

    with patch("scraper.generic_job._generic_collect", return_value=(leads, "Google Maps")), patch(
        "scraper.email_finder.EmailFinder.find_website"
    ) as guess, patch("scraper.leads.WebsiteEmailFinder.find", side_effect=fake_find), patch(
        "scraper.leads.export_leads_xlsx"
    ):
        job._run()

    assert job.status == "done"
    assert job.processed == 12
    assert [row["firma"] for row in job.rows] == [f"Hotel {index}" for index in range(12)]
    assert job.rows[0]["email"] == "info@hotel0.ma"
    assert job.rows[1]["email"] == ""
    assert not guess.called
