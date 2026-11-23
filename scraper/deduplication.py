# -*- coding: utf-8 -*-
"""Deduplication rules that preserve separate vacancies at one employer."""
from __future__ import annotations

from .utils import normalize


def listing_key(listing: dict) -> str:
    """Prefer the official reference number; never merge by company alone."""
    reference = str(listing.get("reference_number") or "").strip()
    if reference:
        return f"reference:{reference}"
    url = str(listing.get("url") or "").strip()
    if url:
        return f"url:{url}"
    # For sources that set neither (e.g. Google Maps with no place_id), include
    # the source so that two businesses with the same name+city from different
    # sources are not incorrectly merged, and include street/zip to distinguish
    # branches of the same company in the same city.
    return "fallback:" + "|".join(
        normalize(listing.get(field, ""))
        for field in ("source", "title", "name", "street", "zip", "city", "publication_date")
    )


def unique_listings(listings: list[dict]) -> list[dict]:
    seen: set[str] = set()
    output: list[dict] = []
    for listing in listings:
        key = listing_key(listing)
        if key in seen:
            continue
        seen.add(key)
        output.append(listing)
    return output
