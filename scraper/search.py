# -*- coding: utf-8 -*-
"""Search configuration and query expansion for the BA Jobsuche source."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class SearchConfig:
    """One independent public Jobsuche search.

    Location is intentionally optional because Jobsuche supports a keyword-only
    search. A comma separated UI value becomes multiple independent searches,
    matching the user's requested city list without inventing a multi-city API
    parameter.
    """

    keyword: str
    locations: tuple[str, ...] = field(default_factory=tuple)
    offer_type: int = 4
    radius_km: Optional[int] = None
    published_since_days: Optional[int] = None

    def locations_or_nationwide(self) -> tuple[str, ...]:
        return self.locations or ("",)

    def label(self, location: str = "") -> str:
        where = location or "Deutschlandweit"
        return f"{self.keyword or 'Alle Berufe'} — {where}"


def split_locations(raw_value: str) -> tuple[str, ...]:
    """Turn comma/newline-separated locations into unique independent queries."""
    locations: list[str] = []
    for item in (raw_value or "").replace("\n", ",").split(","):
        cleaned = item.strip()
        if cleaned and cleaned.casefold() not in {value.casefold() for value in locations}:
            locations.append(cleaned)
    return tuple(locations)
