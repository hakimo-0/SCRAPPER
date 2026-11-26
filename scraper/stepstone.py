# -*- coding: utf-8 -*-
"""Stepstone connector.

Stepstone answers direct requests with an Akamai "Access Denied" page for
some countries (including the user's current network).  The connector first
tries the public search pages directly and then falls back to the Jina Reader
proxy, which renders the same HTML for parsing.  No login or partner key is
required for the public result list.

Search URL shapes used by Stepstone itself:

* ``/jobs/{beruf}``
* ``/jobs/in-{stadt}``
* ``/jobs/{beruf}/in-{stadt}``
* pagination: ``?page=N`` (25 cards per page)

Only fields actually present on the card are exported.  A detail lookup is
optional and uses the public ``-inline.html`` page with its JSON-LD record.
"""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests

from .utils import SafeSession, soup_of

BASE = "https://www.stepstone.de"
READER = "https://r.jina.ai/"
DEFAULT_MAX_PAGES = 10
DEFAULT_DELAY = 2.0

# Direct access is usually blocked outside Germany.  Remember the outcome so
# one export run does not repeat the same failed request for every page.
_direct_available = True


def _slug(value: str) -> str:
    """Build a Stepstone path segment: ``Frankfurt am Main`` -> ``frankfurt-am-main``."""
    value = (value or "").strip().lower()
    for source, target in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        value = value.replace(source, target)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def search_url(keyword: str = "", city: str = "", page: int = 1) -> str:
    """Return the public Stepstone result URL for a keyword/city query."""
    keyword_slug = _slug(keyword)
    city_slug = _slug(city)
    if keyword_slug and city_slug:
        path = f"/jobs/{keyword_slug}/in-{city_slug}"
    elif keyword_slug:
        path = f"/jobs/{keyword_slug}"
    elif city_slug:
        path = f"/jobs/in-{city_slug}"
    else:
        path = "/jobs"
    url = urljoin(BASE, path)
    if page > 1:
        url += "?" + urlencode({"page": page})
    return url


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


def _job_id(card, url: str) -> str:
    card_id = card.get("id", "")
    match = re.search(r"job-item-(\d+)", card_id)
    if match:
        return match.group(1)
    match = re.search(r"--(\d+)(?:-inline\.html)?(?:$|[?#])", url or "")
    return match.group(1) if match else ""


def parse_cards(html: str) -> list[dict]:
    """Parse the current Stepstone result-card layout."""
    soup = soup_of(html)
    leads: list[dict] = []
    seen: set[str] = set()
    for card in soup.select("article[data-testid=job-item]"):
        anchor = card.select_one("a[data-testid=job-item-title]")
        if anchor is None:
            anchor = card.select_one("a[data-at=job-item-title]")
        url = urljoin(BASE, anchor.get("href", "")) if anchor else ""
        title = _text(card.select_one("[data-testid=job-item-title]"))
        company = _text(card.select_one("[data-at=job-item-company-name]"))
        if not title or not company:
            continue
        if url and url in seen:
            continue

        location = _text(card.select_one("[data-at=job-item-location]"))
        zip_code = ""
        match = re.match(r"^(\d{5})\s+(.+)$", location)
        if match:
            zip_code, location = match.group(1), match.group(2)
        elif "," in location:
            location = location.split(",")[0].strip()

        if url:
            seen.add(url)
        leads.append({
            "source": "Stepstone",
            "reference_number": _job_id(card, url),
            "name": company,
            "street": "",
            "zip": zip_code,
            "city": location,
            "title": title,
            "publication_date": _text(card.select_one("[data-at=job-item-timeago]")),
            "start_date": "",
            "url": url,
            "website": "",
            "email": "",
            "phone": "",
        })
    return leads


def _reader_headers() -> dict[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
        "X-Return-Format": "html",
    }
    api_key = os.getenv("JINA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _reader_fetch(url: str, log=print) -> str | None:
    """Fetch a Stepstone page through the public Jina Reader proxy."""
    for attempt in range(2):
        try:
            response = requests.get(
                READER + url,
                headers=_reader_headers(),
                timeout=120,
            )
        except requests.RequestException as exc:
            log(f"  [!] Stepstone Reader: {type(exc).__name__}.")
            return None
        if response.status_code == 200:
            text = response.text or ""
            return text if text.strip() else None
        if response.status_code in (429, 503) and attempt == 0:
            log("  · Stepstone Reader: Rate-Limit, warte 5s …")
            time.sleep(5)
            continue
        log(f"  [!] Stepstone Reader: HTTP {response.status_code}.")
        return None
    return None


def _fetch(url: str, http=None, log=print) -> str | None:
    """Try Stepstone directly, then fall back to the reader proxy."""
    global _direct_available
    if _direct_available:
        http = http or SafeSession()
        response = http.get(url, timeout=30)
        if response is not None and response.status_code == 200:
            text = response.text or ""
            # Some networks get a consent/empty shell without cards.  Let the
            # reader try as well instead of reporting a false empty result.
            if "job-item" in text:
                return text
        if response is not None and response.status_code in (401, 403, 429):
            log("  · Stepstone: direkter Zugriff blockiert, nutze Reader-Fallback.")
            _direct_available = False
        elif response is None:
            _direct_available = False
    return _reader_fetch(url, log=log)


def has_next_page(html: str, current_page: int) -> bool:
    """Return True when the result page links to a higher page number."""
    soup = soup_of(html)
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "page=" not in href:
            continue
        try:
            page = int(parse_qs(urlparse(href).query).get("page", ["0"])[0])
        except ValueError:
            continue
        if page > current_page:
            return True
    return False


def search(
    keyword: str = "",
    city: str = "",
    *,
    max_results: int | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    delay: float = DEFAULT_DELAY,
    http=None,
    log=print,
) -> list[dict]:
    """Search the public Stepstone result list.

    ``max_results`` caps the returned leads; ``max_pages`` caps the number of
    result pages so one export cannot run for an unbounded amount of time.
    """
    keyword = (keyword or "").strip()
    city = (city or "").strip()
    if not keyword and not city:
        log("  [!] Stepstone: Beruf oder Ort angeben.")
        return []

    http = http or SafeSession()
    leads: list[dict] = []
    seen: set[str] = set()
    page = 1
    while page <= max(1, max_pages):
        url = search_url(keyword, city, page)
        html = _fetch(url, http, log=log)
        if not html:
            log(f"  [!] Stepstone: Seite {page} nicht erreichbar.")
            break
        cards = parse_cards(html)
        fresh = [card for card in cards if card["url"] not in seen]
        if not fresh:
            if page == 1:
                log(
                    "  [!] Stepstone: keine Treffer (evtl. blockiert die Seite "
                    "auch den Reader)."
                )
            break
        for card in fresh:
            seen.add(card["url"])
            leads.append(card)
            if max_results is not None and len(leads) >= max_results:
                break
        log(f"  · Stepstone: Seite {page} → {len(leads)} Angebote")
        if max_results is not None and len(leads) >= max_results:
            break
        if not has_next_page(html, page):
            break
        page += 1
        if delay:
            time.sleep(delay)

    if not leads:
        return []
    log(
        f"  · Stepstone: {len(leads)} Angebote für "
        f"«{keyword or 'alle'}» / «{city or 'deutschlandweit'}»."
    )
    return leads[:max_results] if max_results is not None else leads


def detail(url: str, http=None, log=print) -> dict:
    """Read one Stepstone offer through its public JSON-LD record.

    Detail pages are requested through the reader as well; Stepstone blocks
    the plain request for the same networks as the search list.  The returned
    address is used only when the source actually publishes it.
    """
    if not url:
        return {}
    inline = re.sub(r"-inline\.html$", "", url.rstrip("/"))
    if not inline.endswith(".html"):
        inline += "-inline.html"
    html = _fetch(inline, http=http, log=log)
    if not html:
        return {}
    soup = soup_of(html)
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw or '"JobPosting"' not in raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(data, list):
            data = next(
                (item for item in data if isinstance(item, dict) and item.get("@type") == "JobPosting"),
                {},
            )
        if not isinstance(data, dict):
            continue
        location = data.get("jobLocation") or {}
        if isinstance(location, list):
            location = location[0] if location else {}
        address = location.get("address") or {} if isinstance(location, dict) else {}
        organization = data.get("hiringOrganization") or {}
        return {
            "title": str(data.get("title") or ""),
            "name": str(organization.get("name") or "") if isinstance(organization, dict) else "",
            "street": str(address.get("streetAddress") or ""),
            "zip": str(address.get("postalCode") or ""),
            "city": str(address.get("addressLocality") or ""),
            "publication_date": str(data.get("datePosted") or ""),
            "valid_until": str(data.get("validThrough") or ""),
        }
    return {}
