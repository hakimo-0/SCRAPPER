# -*- coding: utf-8 -*-
"""Azubiyo connector.

Azubiyo renders its search result page on the server and appends further
result pages through a small JSON endpoint.  The connector uses both, so a
keyword/city search no longer depends on guessing URL slugs that have been
removed by the site.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlencode, urljoin

from .utils import SafeSession, normalize, soup_of

BASE = "https://www.azubiyo.de"
MORE_URL = f"{BASE}/umbraco/surface/JobList22/ResultsMoreByUrl/"
DEFAULT_RADIUS = 25
DEFAULT_MAX_PAGES = 20

CITY_ALIASES = {
    "frankfurt am main": "frankfurt", "frankfurt": "frankfurt",
    "münchen": "muenchen", "munich": "muenchen",
    "köln": "koeln", "cologne": "koeln",
    "düsseldorf": "duesseldorf", "nürnberg": "nuernberg",
    "würzburg": "wuerzburg", "münster": "muenster",
    "osnabrück": "osnabrueck", "lübeck": "luebeck",
    "saarbrücken": "saarbruecken", "göttingen": "goettingen",
    "krefeld": "krefeld",
}


def city_slug(city: str) -> str:
    key = normalize(city)
    if key in CITY_ALIASES:
        return CITY_ALIASES[key]
    value = (city or "").strip().lower()
    for source, target in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        value = value.replace(source, target)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _location_parts(value: str, fallback_city: str = "") -> tuple[str, str]:
    """Turn '10115 Berlin' into ('10115', 'Berlin')."""
    value = re.sub(r"\s+", " ", (value or "").strip())
    match = re.match(r"^(\d{5})\s+(.+)$", value)
    if match:
        return match.group(1), match.group(2).strip()
    if value and value.lower() not in ("mehrere standorte", "deutschlandweit"):
        return "", value
    return "", fallback_city


def _parse_cards(html: str, city_hint: str = "") -> list[dict]:
    """Parse the current Azubiyo result card layout."""
    soup = soup_of(html)
    leads: list[dict] = []
    for card in soup.select("div.job-offer-teaser"):
        anchor = card.find("a", href=re.compile(r"^/stellenanzeigen/"))
        if not anchor:
            continue

        title = _text(card.find("span", class_=lambda value: value and "h3" in value))
        if not title:
            title = _text(card.find("span", class_=lambda value: value and "fw-bold" in value))

        company = ""
        for span in card.find_all("span"):
            classes = " ".join(span.get("class", []))
            if "text-secondary" in classes and "font-size-14" in classes:
                company = span.get_text(" ", strip=True)
                break
        if not company:
            image = card.find("img", attrs={"alt": True})
            company = re.sub(r"^Logo\s+", "", (image or {}).get("alt", "")).strip()
        if not company:
            continue

        start_date = ""
        location = ""
        salary = ""
        for item in card.select("ul.list-unstyled li"):
            value = _text(item.find("span", class_=lambda c: c and "text-placeholder" in c))
            if not value:
                continue
            if not start_date and re.match(r"^\d{2}\.\d{2}\.\d{4}$", value):
                start_date = value
            elif not location and re.search(r"\d{5}", value):
                location = value
            elif not location and not re.search(r"€|pro Monat|pro Jahr", value):
                location = value
            elif not salary:
                salary = value

        zip_code, city = _location_parts(location, city_hint)
        leads.append({
            "source": "AzubiYo",
            "name": company,
            "street": "",
            "zip": zip_code,
            "city": city,
            "title": title,
            "start_date": start_date,
            "salary": salary,
            "url": urljoin(BASE, anchor["href"]),
            "website": "",
            "email": "",
            "phone": "",
        })
    return leads


def _session(http=None):
    return http or SafeSession()


def _post_more(http, search_url: str, page: int) -> str | None:
    """Fetch result page ``page`` from Azubiyo's JSON/HTML fragment endpoint."""
    payload = {"url": search_url, "page": page}
    headers = {
        "Accept": "text/html, */*; q=0.01",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": search_url,
    }
    session = getattr(http, "session", http)
    try:
        response = session.post(MORE_URL, data=json.dumps(payload), headers=headers, timeout=30)
        if response.status_code != 200:
            return None
        return response.text
    except Exception:
        return None


def search(
    keyword: str = "",
    city: str = "",
    *,
    max_results: int | None = None,
    radius: int = DEFAULT_RADIUS,
    max_pages: int = DEFAULT_MAX_PAGES,
    http=None,
    log=print,
) -> list[dict]:
    """Search Azubiyo by profession and/or city.

    Azubiyo's own search form calls the same ``/stellenmarkt/`` endpoint with
    ``subject``, ``location`` and ``radius``.  Additional pages are requested
    from the site's ``ResultsMoreByUrl`` endpoint.
    """
    keyword = (keyword or "").strip()
    city = (city or "").strip()
    if not keyword and not city:
        log("  [!] AzubiYo: Beruf oder Ort angeben.")
        return []

    http = _session(http)
    params: dict[str, object] = {"radius": radius}
    if keyword:
        params["subject"] = keyword
    if city:
        params["location"] = city
    search_url = f"{BASE}/stellenmarkt/?{urlencode(params)}"

    html = http.get_text(search_url)
    if not html:
        log(f"  [!] AzubiYo: Suche nicht erreichbar ({search_url}).")
        return []

    leads = _parse_cards(html, city)
    if not leads:
        # Older/alternate URL: city page, then filter locally.  This is only a
        # fallback; the normal search endpoint above is preferred.
        slug = city_slug(city) if city else ""
        if slug:
            fallback_url = f"{BASE}/ausbildung/{slug}/"
            fallback_html = http.get_text(fallback_url)
            if fallback_html:
                leads = _parse_cards(fallback_html, city)
                if keyword and leads:
                    wanted = normalize(keyword)
                    leads = [lead for lead in leads if wanted in normalize(lead.get("title", ""))]

    if not leads:
        log(f"  [!] AzubiYo: keine Angebote für «{keyword or city}».")
        return []

    seen = {lead["url"] for lead in leads}
    page = 1
    while len(leads) < (max_results or 10**9) and page < max_pages:
        page += 1
        fragment = _post_more(http, search_url, page)
        if not fragment:
            break
        new_leads = _parse_cards(fragment, city)
        fresh = [lead for lead in new_leads if lead["url"] not in seen]
        if not fresh:
            break
        for lead in fresh:
            seen.add(lead["url"])
            leads.append(lead)
        log(f"  · AzubiYo: Seite {page} → {len(leads)} Angebote")
        time.sleep(0.25)

    if page >= max_pages and len(leads) < (max_results or 10**9):
        log(f"  · AzubiYo: Seitenlimit ({max_pages}) erreicht.")
    log(f"  · AzubiYo: {len(leads)} Angebote für «{keyword or 'alle'}» / «{city or 'deutschlandweit'}».")
    return leads[:max_results] if max_results is not None else leads


def search_city(city, keyword="", max_results=None, http=None, log=print):
    """Backward-compatible wrapper used by the legacy CLI."""
    return search(keyword, city, max_results=max_results, http=http, log=log)


def detail(url: str, http=None) -> dict:
    """Read one Azubiyo offer and return its JSON-LD address details."""
    http = _session(http)
    html = http.get_text(url)
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
        location = (data.get("jobLocation") or [{}])[0]
        address = (location.get("address") or {}) if isinstance(location, dict) else {}
        organization = data.get("hiringOrganization") or {}
        return {
            "title": str(data.get("title") or ""),
            "name": str(organization.get("name") or ""),
            "street": str(address.get("streetAddress") or ""),
            "zip": str(address.get("postalCode") or ""),
            "city": str(address.get("addressLocality") or ""),
            "publication_date": str(data.get("datePosted") or ""),
        }
    return {}


def directory(max_results=None, http=None, log=print):
    """Return the Azubiyo company directory (A-Z entry points)."""
    http = _session(http)
    html = http.get_text(f"{BASE}/ausbildungsbetriebe/")
    if not html:
        log("  [!] AzubiYo: Verzeichnis nicht erreichbar.")
        return []
    slugs: list[str] = []
    for match in re.finditer(r'href="(/ausbildungsbetriebe/[a-z0-9-]+/)"', html):
        slug = match.group(1)
        if slug not in slugs:
            slugs.append(slug)

    leads = []
    selected = slugs if max_results is None else slugs[:max_results]
    for item in selected:
        slug = item.strip("/").split("/")[-1]
        leads.append({
            "source": "AzubiYo (Verzeichnis)",
            "name": slug.replace("-", " ").title(),
            "street": "", "zip": "", "city": "",
            "title": "", "url": urljoin(BASE, item),
            "website": "", "email": "", "phone": "",
        })
    log(f"  · AzubiYo Verzeichnis → {len(leads)} Unternehmen")
    return leads
