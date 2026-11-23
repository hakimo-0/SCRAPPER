from unittest.mock import patch

from scraper import ausbildung_de


SEARCH_CARD = """
<article data-testid="jp-card">
  <a href="/stellen/ausbildung-elektroniker-2027-bei-acme-in-berlin-123/">
    <span data-testid="jp-title">Ausbildung Elektroniker:in 2027</span>
  </a>
  <div data-testid="jp-customer">bei ACME GmbH</div>
  <div data-testid="jp-branches">10115 Berlin</div>
  <div data-testid="jp-starting-at">01.09.2027</div>
</article>
"""

COMPANY_CARD = """
<a href="/unternehmen/acme-gmbh/stellen/">
  <h3 data-testid="corporation-name">ACME GmbH</h3>
</a>
"""


def test_parse_search_cards_cleans_company_prefix_and_place():
    leads = ausbildung_de._parse_search_cards(SEARCH_CARD)

    assert len(leads) == 1
    lead = leads[0]
    assert lead["name"] == "ACME GmbH"
    assert lead["title"] == "Ausbildung Elektroniker:in 2027"
    assert lead["zip"] == "10115"
    assert lead["city"] == "Berlin"
    assert lead["start_date"] == "01.09.2027"
    assert lead["url"].startswith("https://www.ausbildung.de/stellen/")


def test_parse_directory_cards_keeps_only_company_pages():
    leads = ausbildung_de._parse_directory_cards(COMPANY_CARD)

    assert [lead["name"] for lead in leads] == ["ACME GmbH"]
    assert leads[0]["url"].endswith("/unternehmen/acme-gmbh/stellen/")


def test_search_uses_keyword_and_city():
    class Http:
        def __init__(self):
            self.urls = []

        def get_text(self, url, **kwargs):
            self.urls.append(url)
            return SEARCH_CARD

    http = Http()
    leads = ausbildung_de.search("Elektroniker", "Berlin", http=http, log=lambda _m: None)

    assert len(leads) == 1
    assert "form_main_search%5Bwhat%5D=Elektroniker" in http.urls[0]
    assert "form_main_search%5Bwhere%5D=Berlin" in http.urls[0]


def test_attach_websites_uses_detail_address():
    leads = [{"name": "ACME GmbH", "url": "https://www.ausbildung.de/stellen/1/", "website": ""}]

    with patch(
        "scraper.ausbildung_de.job_detail",
        return_value={"website": "https://acme.de", "street": "Hauptstr. 1", "zip": "10115", "city": "Berlin"},
    ):
        enriched = ausbildung_de.attach_websites(leads, log=lambda _m: None)

    assert enriched[0]["website"] == "https://acme.de"
    assert enriched[0]["zip"] == "10115"
    assert enriched[0]["city"] == "Berlin"
