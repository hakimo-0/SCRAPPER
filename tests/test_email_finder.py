from scraper.email_finder import EmailFinder
from scraper.utils import is_blocked_url


def test_wiktionary_is_blocked():
    assert is_blocked_url("https://en.wiktionary.org/wiki/K%C3%BCche")


def test_portal_hosts_are_matched_without_blocking_company_domains():
    assert is_blocked_url("https://www.booking.com/hotel/ma/test.html")
    assert is_blocked_url("https://www.hotels.com/")
    assert is_blocked_url("https://x.com/example")
    assert not is_blocked_url("https://www.kenzi-hotels.com/")
    assert not is_blocked_url("https://www.dropbox.com/")


def test_unrelated_search_result_is_not_returned(monkeypatch):
    finder = EmailFinder(log=lambda _message: None)
    monkeypatch.setattr(finder, "_guess_website", lambda company, deadline=None: None)
    monkeypatch.setattr(finder, "_bing", lambda company, city: ["https://en.wiktionary.org/wiki/K%C3%BCche"])
    monkeypatch.setattr(finder, "_ddg", lambda company, city: [])

    assert finder.find_website("Küchen Aktuell GmbH", "Berlin") is None


def test_matching_search_result_is_used(monkeypatch):
    finder = EmailFinder(log=lambda _message: None)
    monkeypatch.setattr(finder, "_guess_website", lambda company, deadline=None: None)
    monkeypatch.setattr(finder, "_bing", lambda company, city: ["https://www.kuechen-aktuell.de/"])
    monkeypatch.setattr(finder, "_ddg", lambda company, city: [])

    assert finder.find_website("Küchen Aktuell GmbH", "Berlin") == "https://www.kuechen-aktuell.de"
