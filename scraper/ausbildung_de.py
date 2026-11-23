# -*- coding: utf-8 -*-
"""Ausbildung.de connector.

The site offers two useful public entry points:

* ``/suche/`` - keyword + city search (up to 20 cards per query)
* ``/unternehmen/alle/<letter>/`` - the full company directory

Company pages contain the employer's own website, which is passed to the
email finder.  Nothing here invents a website or an email address.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from .utils import SafeSession, soup_of

BASE = "https://www.ausbildung.de"
LETTERS = "abcdefghijklmnopqrstuvwxyz"

SKIP_HOSTS = (
    "ausbildung.de", "meinpraktikum.de", "trainee.de", "embrace.family",
    "facebook.com", "instagram.com", "youtube.com", "linkedin.com", "xing.com",
    "tiktok.com", "twitter.com", "x.com", "stepstone.de", "indeed.com",
    "google.com", "googlemail.com", "gstatic.com", "kununu.com",
)


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _split_place(value: str) -> tuple[str, str]:
    value = re.sub(r"\s+", " ", (value or "").strip())
    match = re.match(r"^(\d{5})\s+(.+)$", value)
    if match:
        return match.group(1), match.group(2).strip()
    return "", value


def _clean_company(value: str) -> str:
    value = re.sub(r"^\s*bei\s+", "", value or "", flags=re.I)
    value = re.sub(r"^\s*\d+(?:[.,]\d+)?\s*\(\s*\d+\s*\)\s*", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _parse_search_cards(html: str) -> list[dict]:
    """Parse the current Ausbildung.de search result card layout."""
    soup = soup_of(html)
    leads: list[dict] = []
    for card in soup.select("article[data-testid=jp-card]"):
        anchor = card.find("a", href=True)
        if not anchor:
            continue
        title = _text(card.select_one("[data-testid=jp-title]"))
        company = _clean_company(_text(card.select_one("[data-testid=jp-customer]")))
        location = _text(card.select_one("[data-testid=jp-branches]"))
        location = re.split(r"\s*\+\s*\d+\s*weitere", location, flags=re.I)[0]
        location = location.split(",")[0].strip()
        zip_code, city = _split_place(location)
        start_date = _text(card.select_one("[data-testid=jp-starting-at]"))
        vacancies = _text(card.select_one("[data-testid=jp-vacancies]"))
        if not company:
            continue
        leads.append({
            "source": "Ausbildung.de",
            "name": company,
            "street": "",
            "zip": zip_code,
            "city": city,
            "title": title,
            "start_date": start_date,
            "vacancies": vacancies,
            "url": urljoin(BASE, anchor["href"]),
            "website": "",
            "email": "",
            "phone": "",
        })
    return leads


def _search_url(keyword: str, city: str) -> str:
    params: dict[str, str] = {}
    if keyword:
        params["form_main_search[what]"] = keyword
    if city:
        params["form_main_search[where]"] = city
    return f"{BASE}/suche/?{urlencode(params)}"


def search(
    keyword: str = "",
    city: str = "",
    *,
    max_results: int | None = None,
    http=None,
    log=print,
) -> list[dict]:
    """Search Ausbildung.de by profession and/or city."""
    keyword = (keyword or "").strip()
    city = (city or "").strip()
    if not keyword and not city:
        return directory(max_results=max_results, http=http, log=log)

    http = http or SafeSession()
    url = _search_url(keyword, city)
    html = http.get_text(url)
    leads = _parse_search_cards(html) if html else []
    if not leads:
        # The compact search parameter is used by the site's own header search.
        compact = "|".join(part for part in (keyword, city) if part)
        fallback = f"{BASE}/suche/?search={compact}"
        fallback_html = http.get_text(fallback)
        leads = _parse_search_cards(fallback_html) if fallback_html else []
    if not leads:
        log(f"  [!] Ausbildung.de: keine Treffer für «{keyword or 'alle'}» / «{city or 'deutschlandweit'}».")
        return []
    log(f"  · Ausbildung.de: {len(leads)} Treffer für «{keyword or 'alle'}» / «{city or 'deutschlandweit'}».")
    return leads[:max_results] if max_results is not None else leads


def ad_search(keyword="", city="", max_results=None, http=None, log=print):
    """Package-level entry point used by the dashboard."""
    if keyword or city:
        return search(keyword, city, max_results=max_results, http=http, log=log)
    return directory(max_results=max_results, http=http, log=log)


def _parse_directory_cards(html: str) -> list[dict]:
    soup = soup_of(html)
    leads: list[dict] = []
    seen: set[str] = set()
    for anchor in soup.select("a[data-testid=corporation-card], a[href^='/unternehmen/']"):
        href = anchor.get("href", "")
        if not re.match(r"^/unternehmen/[a-z0-9\-]+/stellen/?$", href):
            continue
        slug = href.strip("/").split("/")[1]
        if not slug or slug in seen:
            continue
        seen.add(slug)
        name = _text(anchor.select_one("h3[data-testid=corporation-name]"))
        if not name:
            empty = anchor.select_one("h3")
            name = _text(empty)
        name = _clean_company(name) or slug.replace("-", " ").title()
        leads.append({
            "source": "Ausbildung.de (Verzeichnis)",
            "name": name,
            "street": "", "zip": "", "city": "",
            "title": "", "url": urljoin(BASE, href),
            "website": "", "email": "", "phone": "",
        })
    return leads


def _has_next_page(html: str) -> bool:
    soup = soup_of(html)
    for anchor in soup.find_all("a", href=True):
        if "page=" not in anchor["href"]:
            continue
        try:
            page = int(parse_qs(urlparse(anchor["href"]).query).get("page", ["0"])[0])
        except ValueError:
            continue
        if page > 0:
            return True
    return False


def directory(max_results=None, http=None, log=print, start_letter=None):
    """Read the public company directory, letter by letter."""
    http = http or SafeSession()
    leads: list[dict] = []
    if start_letter and start_letter in LETTERS:
        ordered = start_letter + "".join(letter for letter in LETTERS if letter != start_letter)
    else:
        ordered = LETTERS
    seen: set[str] = set()
    for letter in ordered:
        if max_results is not None and len(leads) >= max_results:
            break
        for page in range(3):
            if max_results is not None and len(leads) >= max_results:
                break
            url = f"{BASE}/unternehmen/alle/{letter}/"
            if page:
                url += f"?page={page}"
            html = http.get_text(url)
            if not html:
                break
            found = _parse_directory_cards(html)
            if not found:
                break
            before = len(leads)
            for lead in found:
                if lead["url"] in seen:
                    continue
                seen.add(lead["url"])
                leads.append(lead)
                if max_results is not None and len(leads) >= max_results:
                    break
            if len(leads) == before or not _has_next_page(html):
                break
        log(f"  · Ausbildung.de [{letter}] → {len(leads)} Unternehmen")
    return leads[:max_results] if max_results is not None else leads


def _skip_link(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return not host or any(host == blocked or host.endswith("." + blocked) for blocked in SKIP_HOSTS)


def _website_from_page(html: str) -> str:
    soup = soup_of(html)
    candidates: list[tuple[int, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href.startswith("http") or _skip_link(href):
            continue
        text = anchor.get_text(" ", strip=True).lower()
        score = 0
        if any(word in text for word in ("website", "webseite", "zur seite", "mehr über", "mehr ueber")):
            score = 2
        elif any(word in href.lower() for word in ("karriere", "jobs", "bewerb")):
            score = 1
        candidates.append((score, href))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    parsed = urlparse(candidates[0][1])
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


def _json_ld(soup) -> list[dict]:
    items: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        items.extend(data if isinstance(data, list) else [data])
    return items


def company_page(url: str, http=None) -> dict:
    """Return website and address from one company page."""
    http = http or SafeSession()
    html = http.get_text(url)
    if not html:
        return {}
    soup = soup_of(html)
    result: dict[str, str] = {"website": _website_from_page(html)}
    for item in _json_ld(soup):
        if item.get("@type") != "Corporation":
            continue
        location = item.get("location") or {}
        address = location.get("address") or {}
        result.update({
            "street": str(address.get("streetAddress") or ""),
            "zip": str(address.get("postalCode") or ""),
            "city": str(address.get("addressLocality") or ""),
        })
        break
    return result


def job_detail(url: str, http=None) -> dict:
    """Read a job detail page and return its company + address information."""
    http = http or SafeSession()
    html = http.get_text(url)
    if not html:
        return {}
    soup = soup_of(html)
    result: dict[str, str] = {}
    company_url = ""
    for item in _json_ld(soup):
        if item.get("@type") == "Corporation":
            company_url = str(item.get("url") or "")
        elif item.get("@type") == "JobPosting":
            location = item.get("jobLocation") or {}
            address = (location.get("address") or {}) if isinstance(location, dict) else {}
            organization = item.get("hiringOrganization") or {}
            result.update({
                "name": str(organization.get("name") or ""),
                "title": str(item.get("title") or ""),
                "street": str(address.get("streetAddress") or ""),
                "zip": str(address.get("postalCode") or ""),
                "city": str(address.get("addressLocality") or ""),
                "publication_date": str(item.get("datePosted") or ""),
            })
    if company_url.startswith(BASE):
        result.update({key: value for key, value in company_page(company_url, http).items() if value})
    return result


def attach_websites(leads: list[dict], http=None, log=print, max_lookups: int = 40) -> list[dict]:
    """Resolve employer websites for search results before e-mail extraction.

    Detail pages are only fetched until ``max_lookups`` companies have been
    handled, so a large directory does not turn into hundreds of requests.
    """
    http = http or SafeSession()
    cache_by_company: dict[str, dict] = {}
    remaining = max_lookups
    enriched = 0
    for lead in leads:
        if lead.get("website"):
            continue
        key = (lead.get("name") or "").lower()
        if key and key in cache_by_company:
            info = cache_by_company[key]
        else:
            if remaining <= 0:
                continue
            remaining -= 1
            info = job_detail(lead.get("url", ""), http)
            if key:
                cache_by_company[key] = info
        for field in ("website", "street", "zip", "city", "publication_date"):
            if info.get(field) and not lead.get(field):
                lead[field] = info[field]
        if info.get("website"):
            enriched += 1
    log(f"  · Ausbildung.de: {enriched} Firmen-Websites ermittelt.")
    return leads
