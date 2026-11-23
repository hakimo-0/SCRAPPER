from unittest.mock import patch

from scraper import stepstone


CARD = """
<article data-testid="job-item" id="job-item-14742369">
  <a data-testid="job-item-title"
     href="https://www.stepstone.de/stellenangebote--Ausbildung-Elektroniker-Berlin-ACME-GmbH--14742369">
     Ausbildung Elektroniker (m/w/d)
  </a>
  <span data-at="job-item-company-name">ACME GmbH</span>
  <span data-at="job-item-location">Berlin-Mariendorf</span>
  <span data-at="job-item-timeago">vor 2 Tagen</span>
</article>
"""

PAGE_ONE = CARD + '<a href="/jobs/elektroniker/in-berlin?page=2">2</a>'
PAGE_TWO = CARD.replace("14742369", "99999999").replace(
    "Ausbildung Elektroniker (m/w/d)", "Ausbildung Mechatroniker (m/w/d)"
)


def test_search_url_variants():
    assert stepstone.search_url("Elektroniker", "Berlin") == (
        "https://www.stepstone.de/jobs/elektroniker/in-berlin"
    )
    assert stepstone.search_url("Elektroniker") == "https://www.stepstone.de/jobs/elektroniker"
    assert stepstone.search_url("", "München") == "https://www.stepstone.de/jobs/in-muenchen"
    assert stepstone.search_url("", "", 2) == "https://www.stepstone.de/jobs?page=2"


def test_parse_cards_extracts_fields():
    leads = stepstone.parse_cards(CARD)

    assert len(leads) == 1
    lead = leads[0]
    assert lead["source"] == "Stepstone"
    assert lead["reference_number"] == "14742369"
    assert lead["name"] == "ACME GmbH"
    assert lead["title"] == "Ausbildung Elektroniker (m/w/d)"
    assert lead["city"] == "Berlin-Mariendorf"
    assert lead["publication_date"] == "vor 2 Tagen"


def test_search_follows_pagination_and_stops_on_duplicates():
    with patch("scraper.stepstone._fetch", side_effect=[PAGE_ONE, PAGE_TWO, PAGE_TWO]) as fetch:
        leads = stepstone.search(
            "Elektroniker", "Berlin", max_pages=5, delay=0, log=lambda _m: None
        )

    assert [lead["reference_number"] for lead in leads] == ["14742369", "99999999"]
    assert fetch.call_count == 2


def test_fetch_falls_back_to_reader_when_stepstone_blocks(monkeypatch):
    monkeypatch.setattr(stepstone, "_direct_available", True)

    class Response:
        status_code = 403
        text = "<html>Access Denied</html>"

    class Http:
        def get(self, url, timeout=30):
            return Response()

    with patch("scraper.stepstone._reader_fetch", return_value=PAGE_ONE) as reader:
        html = stepstone._fetch("https://www.stepstone.de/jobs/elektroniker", Http(), log=lambda _m: None)

    assert html == PAGE_ONE
    assert reader.called
    assert stepstone._direct_available is False
