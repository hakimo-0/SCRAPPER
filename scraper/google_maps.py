# -*- coding: utf-8 -*-
"""Google Maps connector backed by SerpAPI's ``google_maps`` engine.

It uses the official SerpAPI endpoint (https://serpapi.com) with the account's
private API key. Each page returns up to 20 local business results, and pagination
is followed through ``serpapi_pagination.next`` until the provider stops or the
configured limit is reached.
"""
from __future__ import annotations

import re
from typing import Callable

import requests


SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"
SERPAPI_PAGE_SIZE = 20
SERPAPI_MAX_PAGES = 10
_COUNTRY_TOKENS = {"germany", "deutschland", "de", "österreich", "austria", "at", "schweiz", "switzerland", "ch"}


def _split_address(address: str, fallback_city: str = "") -> tuple[str, str, str]:
    """Split a Google Maps address into street, postal code and city."""
    parts = [piece.strip() for piece in (address or "").split(",") if piece.strip()]
    if parts and parts[-1].lower() in _COUNTRY_TOKENS:
        parts = parts[:-1]

    street = ""
    postal_code = ""
    city = ""
    for index, part in enumerate(parts):
        match = re.search(r"\b(\d{4,5})\b", part)
        if match:
            postal_code = match.group(1)
            city = re.sub(r"\s+", " ", part[: match.start()] + " " + part[match.end():]).strip(" ,")
            street = ", ".join(parts[:index])
            break

    if not street and parts:
        street = parts[0]
    if not city:
        city = parts[1] if len(parts) > 1 else fallback_city
    return street.strip(), postal_code, (city or fallback_city).strip()


def places_text_search(
    query: str,
    city: str = "",
    *,
    api_key: str,
    max_results: int | None = None,
    log: Callable[[str], None] = print,
) -> list[dict]:
    """Search businesses using the SerpAPI Google Maps engine.

    With ``max_results=None`` every page exposed by the provider is followed; a
    caller can pass a number only when it explicitly wants a smaller retrieval.
    """
    if not api_key:
        log("  [!] Google Maps: SERPAPI_KEY manquante.")
        return []
    if not query.strip():
        log("  [!] Google Maps: entrez un métier ou un mot-clé.")
        return []

    limit = None if max_results is None else max(1, int(max_results))
    text_query = " ".join(piece.strip() for piece in (query, city) if piece.strip())
    leads: list[dict] = []
    start = 0
    page = 0

    while (limit is None or len(leads) < limit) and page < SERPAPI_MAX_PAGES:
        params = {
            "engine": "google_maps",
            "type": "search",
            "q": text_query,
            "hl": "de",
            "gl": "de",
            "start": start,
            "api_key": api_key,
        }
        try:
            response = requests.get(SERPAPI_SEARCH_URL, params=params, timeout=30)
        except requests.RequestException as exc:
            log(f"  [!] Google Maps: erreur réseau ({exc}).")
            break

        try:
            data = response.json()
        except ValueError:
            if response.status_code != 200:
                log(f"  [!] Google Maps HTTP {response.status_code}: vérifiez la clé SerpAPI.")
            else:
                log("  [!] Google Maps: réponse JSON invalide.")
            break

        if data.get("error"):
            log(f"  [!] Google Maps: {data['error']}")
            break
        if response.status_code != 200:
            log(f"  [!] Google Maps HTTP {response.status_code}: vérifiez la clé SerpAPI.")
            break

        results = data.get("local_results") or []
        for place in results:
            name = (place.get("title") or "").strip()
            if not name:
                continue
            street, postal_code, result_city = _split_address(place.get("address", ""), city)
            place_id = place.get("place_id") or place.get("data_id") or ""
            leads.append(
                {
                    "source": "Google Maps",
                    "name": name,
                    "street": street,
                    "zip": postal_code,
                    "city": result_city,
                    "title": "",
                    "website": place.get("website", "") or "",
                    "email": "",
                    "phone": place.get("phone", "") or "",
                    "url": f"https://www.google.com/maps/place/?q=place_id:{place_id}" if place_id else "",
                }
            )
            if limit is not None and len(leads) >= limit:
                break

        next_page = (data.get("serpapi_pagination") or {}).get("next")
        if not results or not next_page:
            break
        start += SERPAPI_PAGE_SIZE
        page += 1

    log(f"  · Google Maps: {len(leads)} résultat(s) pour « {text_query} ».")
    return leads
