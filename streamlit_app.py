# -*- coding: utf-8 -*-
"""Multi-source research dashboard with a verified Jobsuche workflow."""
from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
from typing import Callable

import pandas as pd
import streamlit as st

from scraper import ad_search, az_search, gm_search, stepstone_search
from scraper.arbeitsagentur import RADIUS_OPTIONS, SearchResult, search_all
from scraper.aa_live import ANGEBOTSART_MAP
from scraper.database import save_run
from scraper.deduplication import unique_listings
from scraper.exporter import csv_bytes, excel_bytes, json_bytes
from scraper.history import RETENTION_DAYS, recent_results, save_result
from scraper.search import SearchConfig, split_locations
from scraper.aa_live_ui import get_job as aa_live_job, job_running as aa_live_running, render_captcha_dialog as render_aa_captcha, render_job as render_aa_live, start_job as start_aa_live
from scraper.generic_live_ui import (
    get_job as generic_live_job,
    job_running as generic_live_running,
    render_job as render_generic_live,
    start_job as start_generic_live,
)


st.set_page_config(
    page_title="Ausbildung Lead Finder",
    page_icon=":material/manage_search:",
    layout="wide",
)


DEFAULT_SESSION_STATE = {
    # Make the first visit immediately useful for the app's primary purpose.
    "source_arbeitsagentur": True,
    "aa_offer": "Ausbildung / Duales Studium",
    "aa_radius": None,
    "aa_published": "Alle anzeigen",
    "aa_year": "Alle",
}


def initialize_session_state() -> None:
    """Set first-visit defaults without overwriting a user's selections."""
    for key, value in DEFAULT_SESSION_STATE.items():
        st.session_state.setdefault(key, value)


initialize_session_state()


PROVIDERS = (
    {
        "id": "arbeitsagentur", "name": "Jobbörse",
        "logo": "https://www.arbeitsagentur.de/favicon.ico", "supported": True,
    },
    {
        "id": "google_maps", "name": "Google Maps",
        "logo": "https://www.google.com/images/branding/product/ico/maps15_bnuw3a_32dp.ico", "supported": True,
    },
    {
        "id": "azubiyo", "name": "Azubiyo",
        "logo": "https://www.azubiyo.de/favicon.ico", "supported": True,
    },
    {
        "id": "ausbildung", "name": "Ausbildung.de",
        "logo": "https://www.ausbildung.de/favicon.ico", "supported": True,
    },
    {
        "id": "stepstone", "name": "Stepstone",
        "logo": "https://www.google.com/s2/favicons?domain=stepstone.de&sz=64", "supported": True,
    },
    {
        "id": "aubi", "name": "Aubi-plus",
        "logo": "https://www.aubi-plus.de/favicon.ico", "supported": False,
    },
    {
        "id": "indeed", "name": "Indeed",
        "logo": "https://de.indeed.com/favicon.ico", "supported": False,
    },
    {
        "id": "linkedin", "name": "LinkedIn",
        "logo": "https://www.linkedin.com/favicon.ico", "supported": False,
    },
    {
        "id": "xing", "name": "Xing",
        "logo": "https://www.xing.com/favicon.ico", "supported": False,
    },
)
PROVIDER_BY_ID = {provider["id"]: provider for provider in PROVIDERS}
PUBLISHED_OPTIONS = {
    "Alle anzeigen": None,
    "Heute": 0,
    "Gestern": 1,
    "1 Woche": 7,
    "2 Wochen": 14,
    "4 Wochen": 28,
}
PROJECT_DIR = Path(__file__).resolve().parent
DATABASE_PATH = PROJECT_DIR / "output" / "arbeitsagentur.sqlite"
HISTORY_DATABASE_PATH = PROJECT_DIR / "output" / "search_history.sqlite"


def maps_key() -> str:
    typed = str(st.session_state.get("google_maps_api_key", "")).strip()
    if typed:
        return typed
    try:
        secret = str(st.secrets.get("SERPAPI_KEY", "") or st.secrets.get("GOOGLE_MAPS_API_KEY", "")).strip()
    except Exception:
        secret = ""
    return secret or os.getenv("SERPAPI_KEY", "").strip() or os.getenv("GOOGLE_MAPS_API_KEY", "").strip()


def show_log(logs: list[str], message: str, placeholder) -> None:
    logs.append(message)
    placeholder.code("\n".join(logs[-12:]))


def provider_card(provider: dict, column) -> None:
    with column:
        with st.container(border=True):
            logo, text = st.columns([1, 5], vertical_alignment="center")
            with logo:
                st.image(provider["logo"], width=36)
            with text:
                st.markdown(f"**{provider['name']}**")
            st.toggle("Quelle verwenden", key=f'source_{provider["id"]}')


def active_source_ids() -> list[str]:
    return [provider["id"] for provider in PROVIDERS if st.session_state.get(f'source_{provider["id"]}', False)]


def _add_google_maps_cities() -> None:
    """Add comma/newline-separated city names before the widgets render."""
    cities = list(st.session_state.get("maps_cities", []))
    known = {city.casefold() for city in cities}
    for city in split_locations(str(st.session_state.get("maps_city_draft", ""))):
        if city.casefold() not in known:
            cities.append(city)
            known.add(city.casefold())
    st.session_state["maps_cities"] = cities
    st.session_state["maps_city_draft"] = ""


def _initialize_google_maps_cities() -> None:
    """Migrate the old single text field without losing existing cities."""
    value = st.session_state.get("maps_cities")
    if isinstance(value, list):
        return
    legacy = st.session_state.get("maps_locations", "")
    st.session_state["maps_cities"] = list(split_locations(str(legacy)))


def source_settings(source_id: str) -> dict:
    """Show only the configuration panel for a source the user selected."""
    provider = PROVIDER_BY_ID[source_id]
    logo, title = st.columns([1, 10], vertical_alignment="center")
    with logo:
        st.image(provider["logo"], width=32)
    with title:
        st.subheader(provider["name"])

    if source_id == "arbeitsagentur":
        first, second, third = st.columns([3, 3, 2])
        with first:
            keyword = st.text_input(
                "Beruf, Stichwort oder Referenznummer", key="aa_keyword",
                placeholder="z. B. Fachinformatiker für Anwendungsentwicklung",
            )
        with second:
            locations = st.text_input(
                "Ort/PLZ (optional)", key="aa_locations",
                placeholder="Leer lassen für deutschlandweit; Köln, Dortmund, Duisburg",
                help="Mehrere Orte werden als getrennte, vollständige Suchen ausgeführt.",
            )
        with third:
            offer_label = st.selectbox("Angebotsart", list(ANGEBOTSART_MAP), key="aa_offer")
        fourth, fifth = st.columns(2)
        with fourth:
            radius = st.selectbox(
                "Umkreis", RADIUS_OPTIONS, format_func=lambda value: "Ganzer Ort" if value is None else f"{value} km",
                key="aa_radius",
            )
        with fifth:
            # Migrate values stored before the labels were aligned with the
            # official Jobsuche filter.
            if st.session_state.get("aa_published") not in PUBLISHED_OPTIONS:
                st.session_state["aa_published"] = "Alle anzeigen"
            published = st.radio(
                "Veröffentlicht seit", list(PUBLISHED_OPTIONS), key="aa_published",
            )
        target_year = st.selectbox("Ausbildungsjahr / Startjahr (optional)", ["Alle"] + list(range(2026, 2032)), key="aa_year")
        website_fallback = st.checkbox(
            "Website suchen, wenn die Anzeige keine E-Mail enthält",
            value=True,
            key="aa_website_fallback",
            help="Viele Anzeigen externer Arbeitgeber enthalten nur den Firmennamen. "
                 "Dann wird die Firmenwebsite gesucht und nach E-Mail-Adressen durchsucht.",
        )
        website_max_seconds = 45
        if website_fallback:
            website_max_seconds = st.number_input(
                "Max. Sekunden pro Firmenwebsite",
                min_value=10, max_value=180, value=45, step=5,
                key="aa_website_max_seconds",
                help="Zeitlimit für Websuche und E-Mail-Suche pro Firma. "
                     "Firmen ohne E-Mail werden danach übersprungen.",
            )
        return {
            "keyword": keyword.strip(), "locations": split_locations(locations),
            "offer_type": ANGEBOTSART_MAP[offer_label], "radius": radius,
            "published_since": PUBLISHED_OPTIONS[published], "target_year": target_year,
            "website_fallback": website_fallback,
            "website_max_seconds": int(website_max_seconds),
        }

    if source_id == "google_maps":
        _initialize_google_maps_cities()
        first, second = st.columns([3, 2])
        with first:
            keyword = st.text_input("Suchbegriff", key="maps_keyword", placeholder="z. B. IT Unternehmen")
        with second:
            st.caption("Eine Suche, mehrere Städte")
        with st.form("maps_add_city", border=False):
            city_input, add_button = st.columns([4, 1], vertical_alignment="bottom")
            with city_input:
                st.text_input(
                    "Stadt hinzufügen",
                    key="maps_city_draft",
                    placeholder="z. B. Berlin oder Berlin, Hamburg",
                )
            with add_button:
                st.form_submit_button(
                    "Hinzufügen",
                    icon=":material/add:",
                    type="secondary",
                    width="stretch",
                    on_click=_add_google_maps_cities,
                )
        locations = st.multiselect(
            "Städte in dieser Suche",
            options=st.session_state["maps_cities"],
            key="maps_cities",
            placeholder="Mit + Städte hinzufügen",
            select_all=False,
            help="Jede Stadt wird nacheinander mit demselben Suchbegriff durchsucht. Mit × entfernst du eine Stadt.",
            wrap=True,
        )
        max_pages = st.number_input(
            "Seiten pro Website prüfen", min_value=1, max_value=20, value=4, step=1,
            key="maps_max_pages",
            help="Durchsucht die Startseite sowie Kontakt-, Impressum-, Karriere- und verlinkte Kontaktseiten. "
                 "Mehr Seiten können zusätzliche E-Mail-Adressen finden, dauern aber länger.",
        )
        max_seconds = st.number_input(
            "Max. Sekunden pro Firma", min_value=10, max_value=240, value=60, step=10,
            key="maps_max_seconds",
            help="Nach Ablauf dieses Zeitlimits wird die Firma übersprungen und die nächste geprüft. "
                 "So kostet eine Firma ohne E-Mail nicht mehrere Minuten.",
        )
        st.caption(
            "E-Mails werden nur von der Website gelesen, die bei Google Maps hinterlegt ist. "
            "Firmen ohne Website werden ohne Website-Suche übersprungen — dadurch entstehen "
            "keine falschen Domains und der Lauf bleibt schnell."
        )
        key_input = st.text_input("SerpAPI key", type="password", key="maps_key_input", placeholder="SerpAPI private key")
        if key_input.strip():
            st.session_state["google_maps_api_key"] = key_input.strip()
        if maps_key():
            st.success("SerpAPI key erkannt")
        else:
            st.warning("Füge SERPAPI_KEY ein, um diese Quelle zu starten.")
        return {
            "keyword": keyword.strip(), "locations": tuple(locations),
            "max_pages": int(max_pages),
            "email_max_seconds": int(max_seconds),
        }

    if source_id == "azubiyo":
        first, second, third = st.columns([2, 2, 1])
        with first:
            keyword = st.text_input("Beruf / Keyword", key="az_keyword", placeholder="z. B. Elektroniker")
        with second:
            locations = st.text_input("Ort/PLZ (optional)", key="az_locations", placeholder="z. B. Köln, Dortmund")
        with third:
            max_pages = st.number_input(
                "Max. Seiten je Ort", min_value=1, max_value=20, value=4, step=1,
                key="az_max_pages", help="15 Angebote pro Seite.",
            )
        st.caption("Azubiyo liefert 15 Angebote pro Seite. Weitere Seiten werden über die öffentliche ResultsMoreByUrl-Schnittstelle der Seite geladen.")
        return {
            "keyword": keyword.strip(), "locations": split_locations(locations),
            "max_pages": int(max_pages),
        }

    if source_id == "ausbildung":
        first, second = st.columns(2)
        with first:
            keyword = st.text_input("Beruf / Keyword (optional)", key="ad_keyword", placeholder="z. B. Elektroniker")
        with second:
            locations = st.text_input("Ort/PLZ (optional)", key="ad_locations", placeholder="z. B. Berlin")
        max_results = st.number_input(
            "Max. Unternehmen (nur Verzeichnis)", min_value=10, max_value=2000, value=100, step=10,
            key="ad_max_results",
            help="Wird nur verwendet, wenn Beruf und Ort leer bleiben.",
        )
        st.caption("Mit Beruf oder Ort wird die öffentliche Ausbildung.de-Suche verwendet (20 Treffer pro Suche). Ohne Eingabe wird das Unternehmensverzeichnis bis zur gewählten Grenze gelesen.")
        return {
            "keyword": keyword.strip(), "locations": split_locations(locations),
            "max_results": int(max_results),
        }

    if source_id == "stepstone":
        first, second, third = st.columns([2, 2, 1])
        with first:
            keyword = st.text_input("Beruf / Keyword (optional)", key="ss_keyword", placeholder="z. B. Elektroniker")
        with second:
            locations = st.text_input("Ort/PLZ (optional)", key="ss_locations", placeholder="z. B. Berlin")
        with third:
            max_pages = st.number_input(
                "Max. Seiten je Ort", min_value=1, max_value=20, value=4, step=1,
                key="ss_max_pages", help="25 Angebote pro Seite.",
            )
        st.caption("Stepstone blockiert direkte Zugriffe aus einigen Ländern. Die App versucht zuerst den Direktzugriff und nutzt dann automatisch den öffentlichen Reader-Fallback.")
        return {
            "keyword": keyword.strip(), "locations": split_locations(locations),
            "max_pages": int(max_pages),
        }

    st.warning(f"{provider['name']} ist ausgewählt, aber noch nicht verbunden. Für eine echte Suche brauchst du einen offiziellen API-/Partnerzugang dieser Plattform.")
    return {"unsupported": True}


LIVE_EMAIL_SOURCE_IDS = ("arbeitsagentur", "google_maps", "azubiyo", "ausbildung", "stepstone")
GENERIC_LIVE_SOURCE_IDS = ("google_maps", "azubiyo", "ausbildung", "stepstone")


def live_extraction_error(source_id: str, settings: dict) -> str | None:
    """Validate every selected source before a combined live start."""
    if source_id == "arbeitsagentur" and not settings["keyword"] and not settings["locations"]:
        return "Jobbörse: gib Beruf oder Ort ein."
    if source_id == "google_maps":
        if not settings["keyword"]:
            return "Google Maps: gib einen Suchbegriff ein."
        if not maps_key():
            return "Google Maps: SerpAPI key fehlt."
    if source_id in ("azubiyo", "stepstone") and not settings["keyword"] and not settings["locations"]:
        return f"{PROVIDER_BY_ID[source_id]['name']}: gib Beruf oder mindestens einen Ort ein."
    return None


def start_live_extraction(source_id: str, settings: dict) -> None:
    """Start one source in its own job; source panels and captcha remain separate."""
    if source_id == "arbeitsagentur":
        start_aa_live({
            "angebotsart": settings["offer_type"], "locations": list(settings["locations"]),
            "was": settings["keyword"], "umkreis": settings["radius"], "seit": settings["published_since"],
            "year": settings["target_year"], "max_results": None, "headed": False,
        })
    elif source_id == "google_maps":
        start_generic_live("google_maps", {
            "source": "google_maps",
            "keyword": settings["keyword"],
            "locations": list(settings["locations"]) or [""],
            "api_key": maps_key(),
            "max_pages": settings["max_pages"],
            "email_max_seconds": settings.get("email_max_seconds", 60),
            "delay": 0.5,
        })
    elif source_id == "azubiyo":
        start_generic_live("azubiyo", {
            "source": "azubiyo",
            "keyword": settings["keyword"],
            "locations": list(settings["locations"]) or [""],
            "max_pages": settings.get("max_pages", 4),
            "delay": 0.5,
        })
    elif source_id == "ausbildung":
        start_generic_live("ausbildung", {
            "source": "ausbildung",
            "keyword": settings["keyword"],
            "locations": list(settings["locations"]),
            "max_results": settings.get("max_results", 100),
            "max_pages": 4,
            "delay": 0.5,
        })
    elif source_id == "stepstone":
        start_generic_live("stepstone", {
            "source": "stepstone",
            "keyword": settings["keyword"],
            "locations": list(settings["locations"]) or [""],
            "max_pages": settings.get("max_pages", 4),
            "delay": 0.5,
            "search_delay": 2.0,
        })


def live_extraction_running(source_id: str) -> bool:
    if source_id == "arbeitsagentur":
        return aa_live_running()
    return generic_live_running(source_id)


def run_arbeitsagentur(settings: dict, logs: list[str], log_view) -> list[SearchResult]:
    config = SearchConfig(
        keyword=settings["keyword"], locations=settings["locations"], offer_type=settings["offer_type"],
        radius_km=settings["radius"], published_since_days=settings["published_since"],
    )
    results: list[SearchResult] = []
    for location in config.locations_or_nationwide():
        show_log(logs, f"→ Jobbörse: {config.label(location)}", log_view)
        result = search_all(
            config.keyword, location, angebotsart=config.offer_type, radius=config.radius_km,
            veroeffentlichtseit=config.published_since_days,
            log=lambda message: show_log(logs, message, log_view),
        )
        year = settings["target_year"]
        if year != "Alle":
            result.listings = [listing for listing in result.listings if str(year) in str(listing.get("start_date", ""))]
            result.unique_count = len(result.listings)
            result.message += f" Client filter: start year {year}."
        save_run(DATABASE_PATH, asdict(config), result)
        results.append(result)
        show_log(logs, f"  ✓ {result.status}: {result.unique_count}/{result.reported_total if result.reported_total is not None else '?'} unique", log_view)
    return results


def run_other_source(source_id: str, settings: dict, logs: list[str], log_view) -> tuple[list[dict], dict]:
    provider = PROVIDER_BY_ID[source_id]
    if not provider["supported"]:
        message = "Official API/partner authorization required; no request was sent."
        show_log(logs, f"  ! {provider['name']}: {message}", log_view)
        return [], {"source": provider["name"], "status": "not_connected", "message": message}
    if source_id == "google_maps":
        if not settings["keyword"] or not maps_key():
            message = "Keyword or Google Maps key missing."
            show_log(logs, f"  ! Google Maps: {message}", log_view)
            return [], {"source": provider["name"], "status": "not_started", "message": message}
        results: list[dict] = []
        for location in settings["locations"] or ("",):
            show_log(logs, f"→ Google Maps: {settings['keyword']} — {location or 'ohne Ort'}", log_view)
            results.extend(gm_search(settings["keyword"], location, api_key=maps_key(), max_results=None, log=lambda message: show_log(logs, message, log_view)))
        return results, {"source": provider["name"], "status": "completed", "message": "Google API completed (provider response may have its own limits)."}
    if source_id == "azubiyo":
        if not settings["keyword"] and not settings["locations"]:
            message = "At least a keyword or one location is required."
            show_log(logs, f"  ! Azubiyo: {message}", log_view)
            return [], {"source": provider["name"], "status": "not_started", "message": message}
        results = []
        for location in settings["locations"] or ("",):
            show_log(logs, f"→ Azubiyo: {settings['keyword'] or 'alle'} — {location or 'deutschlandweit'}", log_view)
            results.extend(az_search(
                settings["keyword"], location, max_results=None,
                max_pages=settings.get("max_pages", 4),
                log=lambda message: show_log(logs, message, log_view),
            ))
        return results, {"source": provider["name"], "status": "incomplete", "message": "Paginated public search; provider total not verified."}
    if source_id == "ausbildung":
        if not settings["keyword"] and not settings["locations"]:
            show_log(logs, "→ Ausbildung.de Unternehmensverzeichnis", log_view)
            results = ad_search(
                max_results=settings.get("max_results", 100),
                log=lambda message: show_log(logs, message, log_view),
            )
            return results, {"source": provider["name"], "status": "incomplete", "message": "Directory connector; no verified query total."}
        results = []
        for location in settings["locations"] or ("",):
            show_log(logs, f"→ Ausbildung.de: {settings['keyword'] or 'alle'} — {location or 'deutschlandweit'}", log_view)
            results.extend(ad_search(
                settings["keyword"], location, max_results=None,
                log=lambda message: show_log(logs, message, log_view),
            ))
        return results, {"source": provider["name"], "status": "incomplete", "message": "Public search results; 20 cards per query, no verified total."}
    if source_id == "stepstone":
        if not settings["keyword"] and not settings["locations"]:
            message = "At least a keyword or one location is required."
            show_log(logs, f"  ! Stepstone: {message}", log_view)
            return [], {"source": provider["name"], "status": "not_started", "message": message}
        results = []
        for location in settings["locations"] or ("",):
            show_log(logs, f"→ Stepstone: {settings['keyword'] or 'alle'} — {location or 'deutschlandweit'}", log_view)
            results.extend(stepstone_search(
                settings["keyword"], location, max_results=None,
                max_pages=settings.get("max_pages", 4),
                log=lambda message: show_log(logs, message, log_view),
            ))
        return results, {"source": provider["name"], "status": "incomplete", "message": "Public result list; page limit applied, no verified total."}
    return [], {"source": provider["name"], "status": "not_started", "message": "Unknown source."}


def display_frame(listings: list[dict]) -> pd.DataFrame:
    columns = (
        ("source", "Quelle"), ("reference_number", "Referenznummer"), ("title", "Stellentitel"),
        ("name", "Arbeitgeber"), ("city", "Ort"), ("publication_date", "Veröffentlicht"),
        ("start_date", "Beginn"), ("working_hours", "Arbeitszeit"), ("salary", "Vergütung"),
        ("url", "Original-URL"),
    )
    return pd.DataFrame([{label: listing.get(key, "") for key, label in columns} for listing in listings])


def _archive_completed_live_jobs() -> None:
    """Persist each completed live job once, without storing API keys."""
    saved = st.session_state.setdefault("archived_live_jobs", [])
    jobs: list[tuple[str, object, list[dict]]] = []
    aa_job = aa_live_job()
    if aa_job is not None:
        jobs.append(("arbeitsagentur", aa_job, list(aa_job.rows)))
    for source_id in GENERIC_LIVE_SOURCE_IDS:
        job = generic_live_job(source_id)
        if job is not None:
            jobs.append((source_id, job, job.snapshot_rows()))

    for source_id, job, rows in jobs:
        archive_key = f"{source_id}:{job.autosave_path}"
        if job.status == "running" or not rows or archive_key in saved:
            continue
        provider = PROVIDER_BY_ID[source_id]
        save_result(
            HISTORY_DATABASE_PATH,
            title=f"Live-Extraktion · {provider['name']}",
            source_ids=[source_id],
            rows=rows,
            metadata={
                "type": "live_extraction",
                "status": job.status,
                "saved_file": str(job.autosave_path),
            },
        )
        saved.append(archive_key)


def render_history() -> None:
    """Show locally saved results that have not reached their ten-day expiry."""
    entries = recent_results(HISTORY_DATABASE_PATH)
    st.header(f"Archiv · {RETENTION_DAYS} Tage", icon=":material/inventory_2:")
    st.caption("Abgeschlossene Suchen und Live-Extraktionen bleiben lokal auf diesem PC gespeichert und werden danach automatisch gelöscht.")
    if not entries:
        st.info("Noch keine gespeicherten Ergebnisse. Starte eine Recherche oder eine Live-Extraktion.")
        return

    entry_by_id = {entry["id"]: entry for entry in entries}
    selected_id = st.selectbox(
        "Gespeicherte Suche öffnen",
        options=list(entry_by_id),
        format_func=lambda entry_id: (
            f"{entry_by_id[entry_id]['title']} · "
            f"{entry_by_id[entry_id]['created_at'][:16].replace('T', ' ')}"
        ),
        key="history_entry",
    )
    entry = entry_by_id[selected_id]
    first, second, third = st.columns(3)
    first.metric("Datensätze", len(entry["rows"]))
    second.metric("Quellen", len(entry["source_ids"]))
    third.metric("Automatisch gelöscht", entry["expires_at"][:10])
    if entry["rows"]:
        st.dataframe(pd.DataFrame(entry["rows"]), width="stretch", hide_index=True, height=420)
        st.download_button(
            "Archiv als JSON herunterladen",
            data=json_bytes(entry["rows"]),
            file_name=f"archiv_{entry['id']}.json",
            mime="application/json",
            icon=":material/download:",
        )


st.title("Ausbildung Lead Finder", icon=":material/manage_search:")
st.caption("Unternehmen finden, Kontaktseiten prüfen und alle gefundenen E-Mail-Adressen exportieren.")
research_tab, live_tab, archive_tab = st.tabs([
    ":material/search: Recherche",
    ":material/monitoring: Live-Extraktionen",
    f":material/inventory_2: Archiv · {RETENTION_DAYS} Tage",
])

with research_tab:
    st.subheader("1 · Quellen auswählen", icon=":material/source:")
    columns = st.columns(2)
    supported_providers = [provider for provider in PROVIDERS if provider["supported"]]
    for index, provider in enumerate(supported_providers):
        provider_card(provider, columns[index % 2])

    with st.expander("Weitere Quellen (offizieller Zugang erforderlich)"):
        unavailable_columns = st.columns(2)
        for index, provider in enumerate(provider for provider in PROVIDERS if not provider["supported"]):
            provider_card(provider, unavailable_columns[index % 2])

    selected = active_source_ids()
    if not selected:
        st.info("Wähle eine oder mehrere Quellen aus. Die Einstellungen bleiben verborgen, bis du eine Quelle aktivierst.")
    else:
        st.subheader("2 · Suche konfigurieren", icon=":material/tune:")
        settings_by_source: dict[str, dict] = {}
        source_tabs = st.tabs([PROVIDER_BY_ID[source_id]["name"] for source_id in selected])
        for tab, source_id in zip(source_tabs, selected):
            with tab:
                settings_by_source[source_id] = source_settings(source_id)

        st.subheader("E-Mail-Extraktion", icon=":material/mail:")
        st.caption("Jede Quelle läuft separat. Ein Captcha kann nur bei der Jobbörse erscheinen – nie bei Google Maps.")
        live_sources = [source_id for source_id in selected if source_id in LIVE_EMAIL_SOURCE_IDS]
        if len(live_sources) > 1:
            labels = ", ".join(PROVIDER_BY_ID[source_id]["name"] for source_id in live_sources)
            combined_clicked = st.button(
                f"E-Mail-Extraktion für alle starten ({len(live_sources)})",
                type="primary",
                width="stretch",
                icon=":material/play_arrow:",
                key="all_live_start",
                disabled=all(live_extraction_running(source_id) for source_id in live_sources),
                help=f"Startet gleichzeitig: {labels}",
            )
            if combined_clicked:
                errors = [
                    error for source_id in live_sources
                    if (error := live_extraction_error(source_id, settings_by_source[source_id]))
                ]
                if errors:
                    for error in errors:
                        st.error(error)
                else:
                    for source_id in live_sources:
                        if not live_extraction_running(source_id):
                            start_live_extraction(source_id, settings_by_source[source_id])
                    st.rerun()
        elif live_sources:
            source_id = live_sources[0]
            provider = PROVIDER_BY_ID[source_id]
            with st.container(border=True):
                st.markdown(f"**{provider['name']}**")
                if source_id == "arbeitsagentur":
                    st.caption("E-Mail, Ansprechperson und Telefon · Captcha kann erforderlich sein")
                else:
                    st.caption("Alle E-Mails auf jeder Firmenwebsite · kein Captcha")
                live_clicked = st.button(
                    f"{provider['name']} extrahieren",
                    type="primary",
                    width="stretch",
                    icon=":material/play_arrow:",
                    key=f"{source_id}_live_start",
                    disabled=live_extraction_running(source_id),
                )
                if live_clicked:
                    error = live_extraction_error(source_id, settings_by_source[source_id])
                    if error:
                        st.error(error)
                    else:
                        start_live_extraction(source_id, settings_by_source[source_id])
                        st.rerun()

    if "listings" in st.session_state or "statuses" in st.session_state:
        listings = st.session_state.get("listings", [])
        statuses = st.session_state.get("statuses", [])
        st.header("Ergebnis und Vollständigkeit", icon=":material/table_chart:")
        if statuses:
            st.dataframe(pd.DataFrame(statuses), width="stretch", hide_index=True)
        if not listings:
            st.warning("Keine Listings wurden abgerufen. Prüfe den Status und die Einstellungen jeder Quelle.")
        else:
            frame = display_frame(listings)
            first, second, third = st.columns(3)
            first.metric("Eindeutige Listings", len(frame))
            second.metric("Mit Referenznummer", int(frame["Referenznummer"].astype(bool).sum()))
            third.metric("Mit Original-URL", int(frame["Original-URL"].astype(bool).sum()))
            st.dataframe(
                frame, width="stretch", hide_index=True, height=450,
                column_config={"Original-URL": st.column_config.LinkColumn("Original-URL", display_text="Öffnen")},
            )
            export_one, export_two, export_three = st.columns(3)
            with export_one:
                st.download_button("Excel herunterladen", excel_bytes(listings), "jobsuche.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", icon=":material/download:")
            with export_two:
                st.download_button("CSV herunterladen", csv_bytes(listings), "jobsuche.csv", "text/csv", icon=":material/download:")
            with export_three:
                st.download_button("JSON herunterladen", json_bytes(listings), "jobsuche.json", "application/json", icon=":material/download:")
        with st.expander("Rechercheprotokoll"):
            st.code("\n".join(st.session_state.get("logs", [])))

live_jobs = []
if aa_live_job() is not None:
    live_jobs.append(("Jobbörse", "arbeitsagentur"))
for source_id in GENERIC_LIVE_SOURCE_IDS:
    if generic_live_job(source_id) is not None:
        live_jobs.append((PROVIDER_BY_ID[source_id]["name"], source_id))
_archive_completed_live_jobs()

with live_tab:
    st.header("Live-Extraktionen", icon=":material/monitoring:")
    st.caption("Die Daten, Logs und Aktionen jeder Quelle bleiben getrennt.")
    if not live_jobs:
        st.info("Noch keine Live-Extraktion. Starte sie in der Recherche-Ansicht.")
    else:
        live_source_tabs = st.tabs([name for name, _ in live_jobs])
        for tab, (name, source_id) in zip(live_source_tabs, live_jobs):
            with tab:
                if source_id == "arbeitsagentur":
                    render_aa_live()
                else:
                    render_generic_live(source_id, name)

with archive_tab:
    render_history()

# A captcha blocks the Arbeitsagentur worker.  Render it outside the tabs so
# it pops up even while the user is still on the search/settings view.
render_aa_captcha()
