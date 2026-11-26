from unittest.mock import Mock, patch

from scraper.google_maps import places_text_search


def test_places_text_search_reports_complete_when_provider_has_no_next_page():
    response = Mock(status_code=200)
    response.json.return_value = {"local_results": [{"title": "Bonn Hotel"}]}

    with patch("scraper.google_maps.requests.get", return_value=response):
        leads = []
        results = places_text_search(
            "hotel", "Bonn", api_key="test-key",
            status_callback=lambda status, message: leads.append((status, message)),
        )

    assert len(results) == 1
    assert leads[0][0] == "complete"


def test_places_text_search_reports_failed_for_provider_api_error():
    response = Mock(status_code=401)
    response.json.return_value = {"error": "Invalid API key"}

    with patch("scraper.google_maps.requests.get", return_value=response):
        statuses = []
        results = places_text_search(
            "hotel", "Bonn", api_key="test-key",
            status_callback=lambda status, message: statuses.append((status, message)),
        )

    assert results == []
    assert statuses[0][0] == "failed"


def test_places_text_search_reports_incomplete_when_page_limit_is_reached():
    response = Mock(status_code=200)
    response.json.return_value = {
        "local_results": [{"title": "Bonn Hotel"}],
        "serpapi_pagination": {"next": "https://serpapi.com/next"},
    }

    with patch("scraper.google_maps.SERPAPI_MAX_PAGES", 1), patch(
        "scraper.google_maps.requests.get", return_value=response
    ):
        statuses = []
        places_text_search(
            "hotel", "Bonn", api_key="test-key",
            status_callback=lambda status, message: statuses.append((status, message)),
        )

    assert statuses[0][0] == "incomplete"
