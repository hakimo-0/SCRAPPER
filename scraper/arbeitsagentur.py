# -*- coding: utf-8 -*-
"""Verified public-search connector for Bundesagentur für Arbeit Jobsuche.

The connector follows the public Jobsuche result stream. It reports the
portal's ``maxErgebnisse`` separately from the records retrieved and never
labels a partial retrieval as complete.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Callable, Optional
from urllib.parse import quote

import requests

from .deduplication import unique_listings
from .utils import AGENTUR_USER_AGENT


API_JOBS = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
PUBLIC_DETAIL_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/"
DEFAULT_PAGE_SIZE = 25

HEADERS = {
    "X-API-Key": "jobboerse-jobsuche",
    "User-Agent": AGENTUR_USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "de",
}

ANGEBOTSART_MAP = {
    "Arbeit": 1,
    "Ausbildung / Duales Studium": 4,
}
RADIUS_OPTIONS = (None, 10, 15, 25, 50, 100, 200)
WORKING_HOURS = {
    "arbeitszeitVollzeit": "Vollzeit",
    "arbeitszeitTeilzeitVormittag": "Teilzeit vormittags",
    "arbeitszeitTeilzeitNachmittag": "Teilzeit nachmittags",
    "arbeitszeitTeilzeitAbend": "Teilzeit abends",
    "arbeitszeitTeilzeitFlexibel": "Teilzeit flexibel",
    "arbeitszeitSchichtNachtWochenende": "Schicht/Nacht/Wochenende",
}


@dataclass
class SearchResult:
    listings: list[dict] = field(default_factory=list)
    reported_total: Optional[int] = None
    retrieved_count: int = 0
    unique_count: int = 0
    pages_completed: int = 0
    status: str = "incomplete"
    message: str = ""
    query: dict = field(default_factory=dict)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _period(value) -> str:
    """Return a readable date from Jobsuche period objects.

    The v6 API changed ``eintrittszeitraum`` and ``veroeffentlichungszeitraum``
    from plain strings to ``{"von": "...", "bis": "..."}`` objects.  Keeping
    the nested JSON string in Excel made the date column unreadable, so the
    start date is extracted whenever the API supplies it.
    """
    if isinstance(value, dict):
        for key in ("von", "bis", "start", "end"):
            if value.get(key):
                return str(value[key])
        return json.dumps(value, ensure_ascii=False)
    return _text(value)


def _offer_url(reference_number: str) -> str:
    return f"{PUBLIC_DETAIL_URL}{quote(reference_number, safe='-_')}" if reference_number else ""


def parse_offer(offer: dict) -> dict:
    """Normalize fields that are present in a Jobsuche result item."""
    locations = offer.get("stellenlokationen") or []
    address = (locations[0].get("adresse") or {}) if locations else {}
    street = " ".join(
        value for value in (address.get("strasse", ""), address.get("hausnummer", "")) if value
    ).strip()
    working_hours = ", ".join(label for key, label in WORKING_HOURS.items() if offer.get(key))
    reference_number = _text(offer.get("referenznummer", "")).strip()
    return {
        "source": "Jobbörse (Arbeitsagentur)",
        "reference_number": reference_number,
        "offer_category": _text(offer.get("stellenangebotsart", "")),
        "training_type": _text(offer.get("ausbildungsart", "")),
        "title": _text(offer.get("stellenangebotsTitel", "")),
        "name": _text(offer.get("firma", "")),
        "street": street,
        "zip": _text(address.get("plz", "")),
        "city": _text(address.get("ort", "")),
        "locations": "; ".join(
            _text((location.get("adresse") or {}).get("ort", "")) for location in locations
        ),
        "start_date": _period(offer.get("eintrittszeitraum", "")),
        "publication_date": _text(offer.get("datumErsteVeroeffentlichung", "")),
        "last_modified_date": _text(offer.get("aenderungsdatum", "")),
        "publication_period": _period(offer.get("veroeffentlichungszeitraum", "")),
        "working_hours": working_hours,
        "employment_type": "Minijob" if offer.get("istGeringfuegigeBeschaeftigung") else "",
        "contract_duration": _text(offer.get("vertragsdauer", "")),
        "salary": _text(offer.get("verguetungsangabe", "")),
        "occupation": _text(offer.get("hauptberuf", "")),
        "description": "",
        "qualifications": "",
        "language_skills": "",
        "application_instructions": "",
        "public_contact": "",
        "website": _text(offer.get("externeURL", "")),
        "email": "",
        "phone": "",
        "url": _offer_url(reference_number),
    }


def _get_with_retries(session: requests.Session, params: dict, log: Callable[[str], None]) -> requests.Response:
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = session.get(API_JOBS, params=params, headers=HEADERS, timeout=30)
        except requests.RequestException as exc:
            last_error = exc
        else:
            if response.status_code not in (429, 502, 503, 504):
                return response
            last_error = RuntimeError(f"HTTP {response.status_code}")
        delay = 1.0 * (2**attempt)
        log(f"  · nouvelle tentative dans {delay:.0f}s ({last_error})")
        time.sleep(delay)
    raise RuntimeError(f"Jobsuche request failed after 3 attempts: {last_error}")


def search_all(
    keyword: str = "",
    city: str = "",
    *,
    angebotsart: int = 4,
    radius: Optional[int] = None,
    veroeffentlichtseit: Optional[int] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    delay_seconds: float = 0.35,
    session: Optional[requests.Session] = None,
    log: Callable[[str], None] = print,
) -> SearchResult:
    """Retrieve every accessible page for one Jobsuche query without a cap."""
    # aa_live uses 0 for "Ganzer Ort"; normalise to None which the API expects.
    if radius == 0:
        radius = None
    if radius not in RADIUS_OPTIONS:
        raise ValueError("radius must be None, 10, 15, 25, 50, 100 or 200")
    if not keyword.strip() and not city.strip():
        raise ValueError("Provide a keyword/reference number or an optional location.")

    page_size = max(1, min(int(page_size), 100))
    result = SearchResult(
        query={
            "keyword": keyword.strip(), "location": city.strip(), "angebotsart": angebotsart,
            "radius": radius, "veroeffentlichtseit": veroeffentlichtseit,
        }
    )
    http = session or requests.Session()
    page = 1
    previous_page_fingerprint: tuple[str, ...] | None = None
    records: list[dict] = []

    while True:
        params = {
            "page": page, "size": page_size, "angebotsart": angebotsart,
            "zeitarbeit": "false", "pav": "false",
        }
        if keyword.strip():
            params["was"] = keyword.strip()
        if city.strip():
            params["wo"] = city.strip()
            if radius is not None:
                params["umkreis"] = radius
        if veroeffentlichtseit is not None:
            params["veroeffentlichtseit"] = veroeffentlichtseit

        try:
            response = _get_with_retries(http, params, log)
        except RuntimeError as exc:
            result.status = "failed" if not records else "incomplete"
            result.message = str(exc)
            break
        if response.status_code != 200:
            result.status = "failed" if not records else "incomplete"
            result.message = f"Jobsuche HTTP {response.status_code} on page {page}."
            break
        try:
            payload = response.json()
        except ValueError:
            result.status = "failed" if not records else "incomplete"
            result.message = f"Invalid JSON response on page {page}."
            break

        if result.reported_total is None:
            try:
                result.reported_total = int(payload.get("maxErgebnisse"))
            except (TypeError, ValueError):
                result.reported_total = None
        offers = payload.get("ergebnisliste") or []
        if not offers:
            result.status = "complete" if (result.reported_total or 0) == len(unique_listings(records)) else "incomplete"
            result.message = "No further result page was returned."
            break

        fingerprint = tuple(_text(offer.get("referenznummer", "")) for offer in offers)
        if fingerprint == previous_page_fingerprint:
            result.status = "incomplete"
            result.message = f"Repeated result page detected at page {page}; stopped safely."
            break
        previous_page_fingerprint = fingerprint
        records.extend(parse_offer(offer) for offer in offers)
        result.retrieved_count += len(offers)
        result.pages_completed += 1
        result.listings = unique_listings(records)
        result.unique_count = len(result.listings)
        log(
            f"  · page {page}: {len(offers)} reçus; {result.unique_count} uniques"
            + (f" / {result.reported_total} signalés" if result.reported_total is not None else "")
        )

        if result.reported_total is not None and result.retrieved_count >= result.reported_total:
            result.status = "complete"
            result.message = "Retrieved the number of results reported by Jobsuche."
            break
        if len(offers) < page_size:
            result.status = "incomplete"
            result.message = "Jobsuche ended before the reported total was retrieved."
            break
        page += 1
        time.sleep(max(0.0, delay_seconds))

    result.listings = unique_listings(records)
    result.unique_count = len(result.listings)
    if result.status == "incomplete" and not result.message:
        result.message = "Search ended before completeness could be verified."
    return result


def search(
    keyword: str = "", city: str = "", angebotsart: int = 4,
    max_results: Optional[int] = None, radius: Optional[int] = None,
    veroeffentlichtseit: Optional[int] = None, log: Callable[[str], None] = print,
) -> list[dict]:
    """Compatibility wrapper; the Streamlit app uses ``search_all``."""
    result = search_all(keyword, city, angebotsart=angebotsart, radius=radius,
                        veroeffentlichtseit=veroeffentlichtseit, log=log)
    return result.listings if max_results is None else result.listings[:max_results]


def fetch_contact(*_args, **_kwargs) -> dict:
    """Keep fields empty until a detail endpoint is independently verified."""
    return {"emails": [], "email": "", "website": "", "captcha_required": False}
