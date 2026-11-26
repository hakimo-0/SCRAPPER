# -*- coding: utf-8 -*-
"""Search configuration and query expansion for the BA Jobsuche source."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Optional


# These are intentionally just convenient starting points, not a restriction:
# every search field still accepts any city, PLZ or country typed by the user.
# Keeping the catalogue here lets every connector expose the same city picker.
LOCATION_CATALOG: tuple[str, ...] = (
    "Berlin", "Hamburg", "München", "Köln", "Frankfurt am Main", "Stuttgart",
    "Düsseldorf", "Leipzig", "Dortmund", "Essen", "Bremen", "Dresden",
    "Hannover", "Nürnberg", "Duisburg", "Bochum", "Wuppertal", "Bielefeld",
    "Bonn", "Münster", "Mannheim", "Karlsruhe", "Augsburg", "Wiesbaden",
    "Mönchengladbach", "Gelsenkirchen", "Braunschweig", "Chemnitz", "Kiel",
    "Aachen", "Halle (Saale)", "Magdeburg", "Freiburg im Breisgau", "Krefeld",
    "Lübeck", "Oberhausen", "Erfurt", "Mainz", "Rostock", "Kassel",
    "Hagen", "Hamm", "Saarbrücken", "Mülheim an der Ruhr", "Potsdam",
    "Ludwigshafen am Rhein", "Oldenburg", "Leverkusen", "Osnabrück", "Solingen",
    "Heidelberg", "Herne", "Neuss", "Darmstadt", "Paderborn", "Regensburg",
    "Ingolstadt", "Würzburg", "Fürth", "Wolfsburg", "Offenbach am Main",
    "Ulm", "Heilbronn", "Pforzheim", "Göttingen", "Bottrop", "Trier",
    "Recklinghausen", "Reutlingen", "Bremerhaven", "Koblenz", "Bergisch Gladbach",
    "Jena", "Remscheid", "Erlangen", "Moers", "Siegen", "Hildesheim",
    "Salzgitter", "Cottbus", "Gütersloh", "Kaiserslautern", "Schwerin",
    "Witten", "Iserlohn", "Düren", "Esslingen am Neckar", "Tübingen",
)


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


def _split_batch_values(raw_value: str) -> list[str]:
    """Split pasted batch values while preserving terms such as ``C++``."""
    return re.split(r"[\n,]|(?<=\+\+)\+|(?<!\+)\+(?!\+)", raw_value or "")


def split_locations(raw_value: str) -> tuple[str, ...]:
    """Turn comma/newline/plus-separated locations into unique queries."""
    locations: list[str] = []
    for item in _split_batch_values(raw_value):
        cleaned = item.strip()
        if cleaned and cleaned.casefold() not in {value.casefold() for value in locations}:
            locations.append(cleaned)
    return tuple(locations)


def split_terms(raw_value: str) -> tuple[str, ...]:
    """Split a pasted company/keyword list, keeping each search term once.

    Lists copied from Excel or a web page frequently start each line with a
    number (``1 McDonald's`` or ``2. Burger King``); the number is a list
    marker, not part of the Google Maps query.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for item in _split_batch_values(raw_value):
        cleaned = re.sub(r"^\s*\d+\s*[.)\-:]?\s*", "", item).strip()
        if cleaned and cleaned.casefold() not in seen:
            terms.append(cleaned)
            seen.add(cleaned.casefold())
    return tuple(terms)


def merge_locations(*groups) -> tuple[str, ...]:
    """Merge manually entered and picked locations without duplicate searches."""
    merged: list[str] = []
    known: set[str] = set()
    for group in groups:
        for location in group or ():
            cleaned = str(location or "").strip()
            if cleaned and cleaned.casefold() not in known:
                merged.append(cleaned)
                known.add(cleaned.casefold())
    return tuple(merged)
