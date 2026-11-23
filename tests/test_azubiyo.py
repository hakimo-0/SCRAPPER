from unittest.mock import patch

from scraper import azubiyo


CARD = """
<div class="job-offer-teaser">
  <a href="/stellenanzeigen/ausbildung-elektroniker_acme_abc123/">
    <span class="h3 fw-bold">Ausbildung Elektroniker (m/w/d)</span>
  </a>
  <span class="text-secondary font-size-14">ACME GmbH</span>
  <ul class="list-unstyled">
    <li><span class="text-placeholder">01.08.2027</span></li>
    <li><span class="text-placeholder">10115 Berlin</span></li>
  </ul>
</div>
"""


def test_parse_cards_extracts_company_location_and_start():
    leads = azubiyo._parse_cards(CARD)

    assert len(leads) == 1
    lead = leads[0]
    assert lead["name"] == "ACME GmbH"
    assert lead["title"] == "Ausbildung Elektroniker (m/w/d)"
    assert lead["zip"] == "10115"
    assert lead["city"] == "Berlin"
    assert lead["start_date"] == "01.08.2027"
    assert lead["url"].endswith("/stellenanzeigen/ausbildung-elektroniker_acme_abc123/")


class _Response:
    status_code = 200

    def __init__(self, text):
        self.text = text


class _Session:
    def __init__(self, pages):
        self.pages = list(pages)

    def post(self, url, data=None, headers=None, timeout=30):
        return _Response(self.pages.pop(0)) if self.pages else _Response("")


class _Http:
    def __init__(self, first_page, more_pages=()):
        self.first_page = first_page
        self.session = _Session(list(more_pages))

    def get_text(self, url, **kwargs):
        return self.first_page


def test_search_follows_more_results_endpoint():
    second_page = CARD.replace("abc123", "def456").replace(
        "Ausbildung Elektroniker (m/w/d)", "Ausbildung Mechatroniker (m/w/d)"
    )
    http = _Http(CARD, [second_page])

    with patch("scraper.azubiyo.time.sleep"):
        leads = azubiyo.search(
            "Elektroniker", "Berlin", max_results=5, max_pages=2, http=http, log=lambda _m: None
        )

    assert [lead["name"] for lead in leads] == ["ACME GmbH", "ACME GmbH"]
    assert leads[1]["title"] == "Ausbildung Mechatroniker (m/w/d)"
    assert leads[1]["url"].endswith("def456/")


def test_city_slug_handles_umlauts():
    assert azubiyo.city_slug("München") == "muenchen"
    assert azubiyo.city_slug("Frankfurt am Main") == "frankfurt"
