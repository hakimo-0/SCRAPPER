# -*- coding: utf-8 -*-
"""Multi-source research dashboard with a verified Jobsuche workflow."""
from __future__ import annotations

from dataclasses import asdict
from dataclasses import replace
from datetime import datetime, time
import hashlib
import json
import os
from pathlib import Path
import random
import secrets
import sqlite3
import sys
from typing import Callable

import pandas as pd
import requests
import streamlit as st

from scraper import applications as application_mail
from scraper import google_workspace
from scraper import (
    ad_search,
    az_search,
    crm,
    gm_search,
    google_sender_schedule,
    local_schedule,
    stepstone_search,
)
from scraper.arbeitsagentur import RADIUS_OPTIONS, SearchResult, search_all
from scraper.aa_live import ANGEBOTSART_MAP
from scraper.database import save_run
from scraper.deduplication import unique_listings
from scraper.exporter import csv_bytes, excel_bytes, json_bytes
from scraper.history import RETENTION_DAYS, recent_results, save_result
from scraper.i18n import DEFAULT_LANGUAGE, LANGUAGES, LANGUAGE_KEY, layout_css, translate, visual_css
from scraper.search import LOCATION_CATALOG, SearchConfig, merge_locations, split_locations, split_terms
from scraper.aa_live_ui import get_job as aa_live_job, job_running as aa_live_running, render_captcha_dialog as render_aa_captcha, render_job as render_aa_live, start_job as start_aa_live, has_saved_state as aa_has_saved_state
from scraper.generic_live_ui import (
    get_job as generic_live_job,
    job_running as generic_live_running,
    render_job as render_generic_live,
    start_job as start_generic_live,
    has_saved_state as generic_has_saved_state,
)


st.set_page_config(
    page_title="Ausbildung Lead Finder",
    page_icon=":material/manage_search:",
    layout="wide",
)


def tr(text: str) -> str:
    return translate(text, str(st.session_state.get(LANGUAGE_KEY, DEFAULT_LANGUAGE)))


st.session_state.setdefault(LANGUAGE_KEY, DEFAULT_LANGUAGE)


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
CRM_DATABASE_PATH = PROJECT_DIR / "output" / "crm.sqlite"
SCHEDULE_CONFIG_PATH = PROJECT_DIR / "output" / "scheduled_search.json"
SCHEDULE_RUNS_PATH = PROJECT_DIR / "output" / "scheduled_search.sqlite"
SCHEDULE_LAUNCHER_PATH = PROJECT_DIR / "output" / "run_scheduled_search.cmd"
GOOGLE_SEND_SCHEDULE_PATH = PROJECT_DIR / "output" / "google_send_schedule.json"
GOOGLE_SEND_LAUNCHER_PATH = PROJECT_DIR / "output" / "run_google_send_schedule.cmd"
GOOGLE_SEND_RUNS_PATH = PROJECT_DIR / "output" / "google_send_runs.sqlite"


def maps_key() -> str:
    typed = str(st.session_state.get("google_maps_api_key", "")).strip()
    if typed:
        return typed
    try:
        secret = str(st.secrets.get("SERPAPI_KEY", "") or st.secrets.get("GOOGLE_MAPS_API_KEY", "")).strip()
    except Exception:
        secret = ""
    return secret or os.getenv("SERPAPI_KEY", "").strip() or os.getenv("GOOGLE_MAPS_API_KEY", "").strip()


def google_workspace_config() -> google_workspace.GoogleWorkspaceConfig | None:
    try:
        return google_workspace.load_config(st.secrets, os.environ)
    except Exception:
        return google_workspace.load_config(None, os.environ)


def initialize_google_workspace(
    config: google_workspace.GoogleWorkspaceConfig,
    tokens: dict,
) -> dict:
    """Create or discover the user's application resources after OAuth consent."""
    resources = st.session_state.get("google_workspace_resources")
    if resources:
        return resources
    spreadsheet = google_workspace.find_or_create_spreadsheet(config, tokens)
    spreadsheet_config = replace(
        config,
        spreadsheet_id=spreadsheet["spreadsheet_id"],
        sender_name=str(st.session_state.get("application_sender_name", config.sender_name)).strip(),
    )
    template_id = google_workspace.find_or_create_template_document(
        spreadsheet_config, tokens,
    )
    assets = google_workspace.list_dossier_pdfs(spreadsheet_config, tokens)
    selected_pdf = str(st.session_state.get("google_static_pdf_id") or "")
    available_pdf_ids = {item["id"] for item in assets}
    if selected_pdf not in available_pdf_ids:
        selected_pdf = assets[0]["id"] if len(assets) == 1 else ""
    resources = {
        "spreadsheet_id": spreadsheet["spreadsheet_id"],
        "spreadsheet_url": spreadsheet["spreadsheet_url"],
        "sheet_name": config.sheet_name,
        "template_doc_id": template_id,
        "pdfs": assets,
        "static_pdf_id": selected_pdf,
    }
    st.session_state["google_workspace_resources"] = resources
    st.session_state["google_spreadsheet_url"] = spreadsheet["spreadsheet_url"]
    st.session_state["google_spreadsheet_id"] = spreadsheet["spreadsheet_id"]
    st.session_state["google_sheet_name"] = config.sheet_name
    st.session_state["google_template_doc_id"] = template_id
    st.session_state["google_static_pdf_id"] = selected_pdf
    return resources


def handle_google_oauth_callback() -> None:
    """Consume the one-time authorization response and keep tokens only in session state."""
    config = google_workspace_config()
    if config is None:
        return
    code = str(st.query_params.get("code", ""))
    provider_error = str(st.query_params.get("error", ""))
    if not (code or provider_error):
        if not st.session_state.get(google_workspace.TOKENS_KEY):
            try:
                restored = google_workspace.restore_persisted_session(config)
                if restored:
                    tokens, email = restored
                    st.session_state[google_workspace.TOKENS_KEY] = tokens
                    st.session_state[google_workspace.ACCOUNT_KEY] = email
            except google_workspace.GoogleWorkspaceError as exc:
                st.session_state["google_workspace_error"] = tr(str(exc))
        return
    try:
        returned_state = str(st.query_params.get("state", ""))
        expected_state = str(st.session_state.pop(google_workspace.OAUTH_STATE_KEY, ""))
        verifier = str(st.session_state.pop(google_workspace.OAUTH_VERIFIER_KEY, ""))
        st.query_params.clear()
        if provider_error:
            st.session_state["google_workspace_error"] = tr("Google-Anmeldung wurde abgebrochen.")
            return
        if not verifier or not google_workspace.oauth_state_matches(expected_state, returned_state):
            st.session_state["google_workspace_error"] = tr("Google-Anmeldung fehlgeschlagen: ungültige Sitzung.")
            return
        tokens = google_workspace.exchange_code(config, code, verifier)
        email = google_workspace.account_email(config, tokens)
        google_workspace.persist_refresh_token(tokens, email)
        st.session_state[google_workspace.TOKENS_KEY] = tokens
        st.session_state[google_workspace.ACCOUNT_KEY] = email
        st.session_state.pop("google_workspace_error", None)
    except google_workspace.GoogleWorkspaceError as exc:
        st.session_state.pop(google_workspace.TOKENS_KEY, None)
        st.session_state.pop(google_workspace.ACCOUNT_KEY, None)
        st.session_state["google_workspace_error"] = tr(str(exc))


handle_google_oauth_callback()


def show_log(logs: list[str], message: str, placeholder) -> None:
    logs.append(message)
    placeholder.code("\n".join(logs[-12:]))


def provider_card(provider: dict) -> None:
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        st.image(provider["logo"], width=24)
        st.toggle(provider["name"], key=f'source_{provider["id"]}')


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


def _initialize_google_maps_terms() -> None:
    """Migrate the previous single/textarea search controls into + entries."""
    if isinstance(st.session_state.get("maps_terms"), list):
        return
    terms = merge_locations(
        split_terms(str(st.session_state.get("maps_keyword", ""))),
        split_terms(str(st.session_state.get("maps_company_list", ""))),
    )
    st.session_state["maps_terms"] = list(terms)
    st.session_state["maps_selected_terms"] = list(terms)


def _add_google_maps_terms() -> None:
    """Add one or many pasted company/industry terms to the selected run."""
    terms = list(st.session_state.get("maps_terms", []))
    selected = list(st.session_state.get("maps_selected_terms", []))
    known = {term.casefold() for term in terms}
    selected_known = {term.casefold() for term in selected}
    for term in split_terms(str(st.session_state.get("maps_term_draft", ""))):
        if term.casefold() not in known:
            terms.append(term)
            known.add(term.casefold())
        if term.casefold() not in selected_known:
            selected.append(term)
            selected_known.add(term.casefold())
    st.session_state["maps_terms"] = terms
    st.session_state["maps_selected_terms"] = selected
    st.session_state["maps_term_draft"] = ""


def _catalog_cities(source_id: str) -> tuple[str, ...]:
    """Offer optional German city suggestions without limiting typed cities/PLZ."""
    return tuple(st.multiselect(
        tr("Deutsche Städte aus der Liste hinzufügen"),
        options=LOCATION_CATALOG,
        key=f"{source_id}_catalog_cities",
        placeholder=tr("Städte auswählen (beliebig viele)"),
        help=tr("Mehrere Orte werden als getrennte, vollständige Suchen ausgeführt."),
    ))


def source_settings(source_id: str) -> dict:
    """Show only the configuration panel for a source the user selected."""
    if source_id == "arbeitsagentur":
        first, second, third = st.columns([3, 3, 2])
        with first:
            keyword = st.text_input(
                tr("Beruf, Stichwort oder Referenznummer"), key="aa_keyword",
                placeholder=tr("z. B. Fachinformatiker für Anwendungsentwicklung"),
            )
        with second:
            locations = st.text_input(
                tr("Ort/PLZ (optional)"), key="aa_locations",
                placeholder=tr("Leer lassen für deutschlandweit; Köln, Dortmund, Duisburg"),
                help=tr("Mehrere Orte werden als getrennte, vollständige Suchen ausgeführt."),
            )
        with third:
            offer_label = st.selectbox(
                tr("Angebotsart"), list(ANGEBOTSART_MAP), key="aa_offer", format_func=tr,
            )
        fourth, fifth = st.columns(2)
        with fourth:
            radius = st.selectbox(
                tr("Umkreis"), RADIUS_OPTIONS, format_func=lambda value: tr("Ganzer Ort") if value is None else f"{value} km",
                key="aa_radius",
            )
        with fifth:
            # Migrate values stored before the labels were aligned with the
            # official Jobsuche filter.
            if st.session_state.get("aa_published") not in PUBLISHED_OPTIONS:
                st.session_state["aa_published"] = "Alle anzeigen"
            published = st.radio(
                tr("Veröffentlicht seit"), list(PUBLISHED_OPTIONS), key="aa_published", format_func=tr,
            )
        target_year = st.selectbox(
            tr("Ausbildungsjahr / Startjahr (optional)"),
            ["Alle"] + list(range(2026, 2032)),
            key="aa_year",
            format_func=lambda value: tr(str(value)),
        )
        catalog_locations = _catalog_cities("aa")
        website_fallback = st.checkbox(
            tr("Website suchen, wenn die Anzeige keine E-Mail enthält"),
            value=True,
            key="aa_website_fallback",
            help=tr("Viele Anzeigen externer Arbeitgeber enthalten nur den Firmennamen. Dann wird die Firmenwebsite gesucht und nach E-Mail-Adressen durchsucht."),
        )
        website_max_seconds = 45
        website_max_pages = 8
        website_search_key = ""
        if website_fallback:
            website_max_seconds = st.number_input(
                tr("Max. Sekunden pro Firmenwebsite"),
                min_value=10, max_value=180, value=45, step=5,
                key="aa_website_max_seconds",
                help=tr("Zeitlimit für Websuche und E-Mail-Suche pro Firma. Firmen ohne E-Mail werden danach übersprungen."),
            )
            website_max_pages = st.number_input(
                tr("Kontaktseiten pro Firmenwebsite prüfen"),
                min_value=2, max_value=20, value=8, step=1,
                key="aa_website_max_pages",
                help=tr("Prüft gezielt Kontakt, Impressum, Karriere, Jobs und Ausbildung sowie relevante interne Links und JavaScript-Dateien."),
            )
            website_search_key = st.text_input(
                tr("SerpAPI key für offizielle Website-Suche (optional)"),
                type="password", key="aa_website_search_key",
                placeholder=tr("Erhöht Trefferquote bei Firmen ohne Website in der Anzeige"),
                help=tr("Mit einem SerpAPI-Key kann die offizielle Website über eine Google-Suche zuverlässiger gefunden werden."),
            ).strip()
        return {
            "keyword": keyword.strip(), "locations": merge_locations(split_locations(locations), catalog_locations),
            "offer_type": ANGEBOTSART_MAP[offer_label], "radius": radius,
            "published_since": PUBLISHED_OPTIONS[published], "target_year": target_year,
            "website_fallback": website_fallback,
            "website_max_seconds": int(website_max_seconds),
            "website_max_pages": int(website_max_pages),
            "website_search_api_key": website_search_key,
        }

    if source_id == "google_maps":
        _initialize_google_maps_cities()
        _initialize_google_maps_terms()
        st.caption(tr("1 · Suchbegriffe hinzufügen  →  2 · Städte hinzufügen  →  Start. Jede Kombination wird automatisch gesucht."))
        with st.form("maps_add_term", border=False):
            term_input, term_button = st.columns([4, 1], vertical_alignment="bottom")
            with term_input:
                st.text_input(
                    tr("Suchbegriff / Branche hinzufügen"),
                    key="maps_term_draft",
                    placeholder=tr("z. B. IT, Altenheim, ambulante Pflege, Hotel, Restaurant"),
                    help=tr("Eine Eingabe oder mehrere Begriffe mit Komma/Zeilenumbruch einfügen."),
                )
            with term_button:
                st.form_submit_button(
                    tr("Hinzufügen"), icon=":material/add:", type="secondary", width="stretch",
                    on_click=_add_google_maps_terms,
                )
        keywords = tuple(st.multiselect(
            tr("Suchbegriffe in diesem Lauf"),
            options=st.session_state["maps_terms"],
            key="maps_selected_terms",
            placeholder=tr("Oben mit + Suchbegriffe hinzufügen"),
            help=tr("Mit × entfernst du einen Begriff nur aus diesem Lauf. Er bleibt für die nächste Suche verfügbar."),
            wrap=True,
        ))
        with st.form("maps_add_city", border=False):
            city_input, add_button = st.columns([4, 1], vertical_alignment="bottom")
            with city_input:
                st.text_input(
                    tr("Stadt hinzufügen"),
                    key="maps_city_draft",
                    placeholder=tr("z. B. Berlin oder Berlin, Hamburg"),
                )
            with add_button:
                st.form_submit_button(
                    tr("Hinzufügen"),
                    icon=":material/add:",
                    type="secondary",
                    width="stretch",
                    on_click=_add_google_maps_cities,
                )
        selected_cities = list(st.session_state.get("maps_cities", []))
        map_city_options = list(merge_locations(selected_cities, LOCATION_CATALOG))
        locations = st.multiselect(
            tr("Städte in dieser Suche"),
            options=map_city_options,
            key="maps_cities",
            placeholder=tr("Mit + Städte hinzufügen"),
            select_all=False,
            help=tr("Jede Stadt wird nacheinander mit demselben Suchbegriff durchsucht. Mit × entfernst du eine Stadt."),
            wrap=True,
        )
        if keywords:
            st.caption(
                f"{len(keywords)} {tr('Suchbegriffe')} × {len(locations) or 1} {tr('Stadt/Städte')} = "
                f"{len(keywords) * (len(locations) or 1)} getrennte Google-Maps-Suchen."
            )
        max_pages = st.number_input(
            tr("Seiten pro Website prüfen"), min_value=1, max_value=20, value=4, step=1,
            key="maps_max_pages",
            help=tr("Durchsucht die Startseite sowie Kontakt-, Impressum-, Karriere- und verlinkte Kontaktseiten."),
        )
        max_seconds = st.number_input(
            tr("Max. Sekunden pro Firma"), min_value=10, max_value=240, value=60, step=10,
            key="maps_max_seconds",
            help=tr("Nach Ablauf dieses Zeitlimits wird die Firma übersprungen und die nächste geprüft."),
        )
        guess_websites = st.checkbox(
            tr("Offizielle Firmenwebsite suchen, wenn Google Maps keine Website zeigt"),
            value=True,
            key="maps_guess_websites",
            help=tr("Die App prüft Firmenname plus Stadt und akzeptiert nur eine plausible offizielle Domain."),
        )
        st.caption(tr(
            "Alle E-Mail-Adressen werden auf der offiziellen Firmenwebsite gesucht: Startseite, Kontakt, Impressum, Karriere und Bewerbung."
        ))
        st.warning(tr(
            "Google Maps/Places APIs haben Kontingente und Kosten; Ergebnisse sind nicht unbegrenzt. "
            "Diese Suche nutzt einen konfigurierten SerpAPI-Key. Playwright-Scraping von Google Maps "
            "kann gegen Plattformbedingungen verstoßen und ist unzuverlässig."
        ))
        key_input = st.text_input(tr("SerpAPI key"), type="password", key="maps_key_input", placeholder=tr("SerpAPI private key"))
        if key_input.strip():
            st.session_state["google_maps_api_key"] = key_input.strip()
        if maps_key():
            st.success(tr("SerpAPI key erkannt"))
        else:
            st.warning(tr("Füge SERPAPI_KEY ein, um diese Quelle zu starten."))
        settings = {
            "keyword": keywords[0] if keywords else "",
            "keywords": keywords,
            "locations": tuple(locations),
            "max_pages": int(max_pages),
            "email_max_seconds": int(max_seconds),
            "guess_websites": guess_websites,
        }
        maps_running = live_extraction_running("google_maps")
        if st.button(
            tr("Google Maps E-Mail-Extraktion starten"),
            type="primary",
            width="stretch",
            icon=":material/play_arrow:",
            key="google_maps_settings_live_start",
            disabled=maps_running,
        ):
            error = live_extraction_error("google_maps", settings)
            if error:
                st.error(tr(error))
            else:
                start_live_extraction("google_maps", settings)
                st.session_state["dashboard_tabs"] = tr(":material/monitoring: Live-Extraktionen")
                st.rerun()
        if maps_running:
            st.info(tr("Google Maps läuft bereits. Öffne den Tab Live-Extraktionen für Fortschritt und Stop."))
        else:
            st.caption(tr("Fortschritt, Pause und Stop findest du im Tab Live-Extraktionen."))
        return settings

    if source_id == "azubiyo":
        first, second, third = st.columns([2, 2, 1])
        with first:
            keyword = st.text_input(tr("Beruf / Keyword"), key="az_keyword", placeholder=tr("z. B. Elektroniker"))
        with second:
            locations = st.text_input(tr("Ort/PLZ (optional)"), key="az_locations", placeholder=tr("z. B. Köln, Dortmund"))
        with third:
            max_pages = st.number_input(
                tr("Max. Seiten je Ort"), min_value=1, max_value=20, value=4, step=1,
                key="az_max_pages", help=tr("15 Angebote pro Seite."),
            )
        st.caption(tr("Azubiyo liefert 15 Angebote pro Seite. Weitere Seiten werden über die öffentliche ResultsMoreByUrl-Schnittstelle der Seite geladen."))
        catalog_locations = _catalog_cities("az")
        return {
            "keyword": keyword.strip(), "locations": merge_locations(split_locations(locations), catalog_locations),
            "max_pages": int(max_pages),
        }

    if source_id == "ausbildung":
        first, second = st.columns(2)
        with first:
            keyword = st.text_input(tr("Beruf / Keyword (optional)"), key="ad_keyword", placeholder=tr("z. B. Elektroniker"))
        with second:
            locations = st.text_input(tr("Ort/PLZ (optional)"), key="ad_locations", placeholder=tr("z. B. Berlin"))
        max_results = st.number_input(
            tr("Max. Unternehmen (nur Verzeichnis)"), min_value=10, max_value=2000, value=100, step=10,
            key="ad_max_results",
            help=tr("Wird nur verwendet, wenn Beruf und Ort leer bleiben."),
        )
        st.caption(tr("Mit Beruf oder Ort wird die öffentliche Ausbildung.de-Suche verwendet (20 Treffer pro Suche). Ohne Eingabe wird das Unternehmensverzeichnis bis zur gewählten Grenze gelesen."))
        catalog_locations = _catalog_cities("ad")
        return {
            "keyword": keyword.strip(), "locations": merge_locations(split_locations(locations), catalog_locations),
            "max_results": int(max_results),
        }

    if source_id == "stepstone":
        first, second, third = st.columns([2, 2, 1])
        with first:
            keyword = st.text_input(tr("Beruf / Keyword (optional)"), key="ss_keyword", placeholder=tr("z. B. Elektroniker"))
        with second:
            locations = st.text_input(tr("Ort/PLZ (optional)"), key="ss_locations", placeholder=tr("z. B. Berlin"))
        with third:
            max_pages = st.number_input(
                tr("Max. Seiten je Ort"), min_value=1, max_value=20, value=4, step=1,
                key="ss_max_pages", help=tr("25 Angebote pro Seite."),
            )
        st.caption(tr("Stepstone blockiert direkte Zugriffe aus einigen Ländern. Die App versucht zuerst den Direktzugriff und nutzt dann automatisch den öffentlichen Reader-Fallback."))
        catalog_locations = _catalog_cities("ss")
        return {
            "keyword": keyword.strip(), "locations": merge_locations(split_locations(locations), catalog_locations),
            "max_pages": int(max_pages),
        }

    st.warning(f"{provider['name']} {tr('ist ausgewählt, aber noch nicht verbunden. Für eine echte Suche brauchst du einen offiziellen API-/Partnerzugang dieser Plattform.')}")
    return {"unsupported": True}


LIVE_EMAIL_SOURCE_IDS = ("arbeitsagentur", "google_maps", "azubiyo", "ausbildung", "stepstone")
GENERIC_LIVE_SOURCE_IDS = ("google_maps", "azubiyo", "ausbildung", "stepstone")


def live_extraction_error(source_id: str, settings: dict) -> str | None:
    """Validate every selected source before a combined live start."""
    if source_id == "arbeitsagentur" and not settings["keyword"] and not settings["locations"]:
        return f"{tr('Jobbörse')}: {tr('gib Beruf oder Ort ein.')}"
    if source_id == "google_maps":
        if not settings.get("keywords") and not settings["keyword"]:
            return f"Google Maps: {tr('gib mindestens eine Firma oder einen Suchbegriff ein.')}"
        if not maps_key():
            return f"Google Maps: {tr('SerpAPI key fehlt.')}"
    if source_id in ("azubiyo", "stepstone") and not settings["keyword"] and not settings["locations"]:
        return f"{PROVIDER_BY_ID[source_id]['name']}: {tr('gib Beruf oder mindestens einen Ort ein.')}"
    return None


def start_live_extraction(source_id: str, settings: dict) -> None:
    """Start one source in its own job; source panels and captcha remain separate."""
    if source_id == "arbeitsagentur":
        start_aa_live({
            "angebotsart": settings["offer_type"], "locations": list(settings["locations"]),
            "was": settings["keyword"], "umkreis": settings["radius"], "seit": settings["published_since"],
            "year": settings["target_year"], "max_results": None, "headed": False,
            "website_max_pages": settings.get("website_max_pages", 8),
            "website_search_api_key": settings.get("website_search_api_key", "") or maps_key(),
        })
    elif source_id == "google_maps":
        start_generic_live("google_maps", {
            "source": "google_maps",
            "keyword": settings["keyword"],
            "keywords": list(settings.get("keywords") or [settings["keyword"]]),
            "locations": list(settings["locations"]) or [""],
            "api_key": maps_key(),
            "max_pages": settings["max_pages"],
            "email_max_seconds": settings.get("email_max_seconds", 60),
            "guess_websites": settings.get("guess_websites", True),
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
        terms = tuple(settings.get("keywords") or ([settings["keyword"]] if settings["keyword"] else ()))
        if not terms or not maps_key():
            message = "Keyword or Google Maps key missing."
            show_log(logs, f"  ! Google Maps: {message}", log_view)
            return [], {"source": provider["name"], "status": "not_started", "message": message}
        results: list[dict] = []
        query_statuses: list[dict] = []
        for term in terms:
            for location in settings["locations"] or ("",):
                show_log(logs, f"→ Google Maps: {term} — {location or 'ohne Ort'}", log_view)
                results.extend(gm_search(
                    term,
                    location,
                    api_key=maps_key(),
                    max_results=None,
                    log=lambda message: show_log(logs, message, log_view),
                    status_callback=lambda status, message, query=term, city=location: query_statuses.append({
                        "query": f"{query} — {city or 'Deutschlandweit'}",
                        "status": status,
                        "message": message,
                    }),
                ))
        statuses = [item["status"] for item in query_statuses]
        result_status = "failed" if not statuses or all(status == "failed" for status in statuses) else (
            "complete" if all(status == "complete" for status in statuses) else "incomplete"
        )
        return results, {
            "source": provider["name"],
            "status": result_status,
            "message": f"{len(statuses)} query status(es); provider quotas/limits may apply.",
            "queries": query_statuses,
        }
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
        ("source", tr("Quelle")), ("reference_number", tr("Referenznummer")), ("title", tr("Stellentitel")),
        ("name", tr("Arbeitgeber")), ("city", tr("Ort")), ("publication_date", tr("Veröffentlicht")),
        ("start_date", tr("Beginn")), ("working_hours", tr("Arbeitszeit")), ("salary", tr("Vergütung")),
        ("url", tr("Original-URL")),
    )
    return pd.DataFrame([{label: listing.get(key, "") for key, label in columns} for listing in listings])


def localize_table(rows: list[dict]) -> pd.DataFrame:
    labels = {
        "id": "ID",
        "lead_id": "Lead ID",
        "source": "Quelle",
        "reference_number": "Referenznummer",
        "title": "Stellentitel",
        "name": "Arbeitgeber",
        "firma": "Firma",
        "person": "Kontakt",
        "email": "E-Mail",
        "phone": "Telefon",
        "telefonnummer": "Telefon",
        "city": "Ort",
        "street": "Straße",
        "zip": "PLZ",
        "website": "Website",
        "url": "Original-URL",
        "source_url": "Original-URL",
        "status": "Status",
        "message": "Meldung",
        "query": "Suchanfrage",
        "started_at": "Startzeit",
        "searched_count": "Gesuchte Datensätze",
        "new_count": "Neue Datensätze",
        "imported_count": "Importierte Leads",
        "reference_hash": "Referenz-Hash",
        "action": "Aktion",
        "reason": "Anlass",
        "created_at": "Zeitpunkt",
        "activity_type": "Interaktionstyp",
        "details": "Details",
        "publication_date": "Veröffentlicht",
        "start_date": "Beginn",
        "working_hours": "Arbeitszeit",
        "salary": "Vergütung",
    }
    return pd.DataFrame(rows).rename(columns={
        column: tr(label) for column, label in labels.items()
    })


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
        if job.status not in ("done", "stopped", "error") or not rows or archive_key in saved:
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


def _crm_import_candidates() -> list[dict]:
    candidates: list[dict] = []
    aa_job = aa_live_job()
    if aa_job is not None:
        for row in aa_job.rows:
            lead = dict(row)
            if not lead.get("source"):
                lead["source"] = PROVIDER_BY_ID["arbeitsagentur"]["name"]
            candidates.append(lead)
    for source_id in GENERIC_LIVE_SOURCE_IDS:
        job = generic_live_job(source_id)
        if job is None:
            continue
        for row in job.snapshot_rows():
            lead = dict(row)
            if not lead.get("source"):
                lead["source"] = PROVIDER_BY_ID[source_id]["name"]
            candidates.append(lead)

    for entry in recent_results(HISTORY_DATABASE_PATH):
        source_id = entry["source_ids"][0] if len(entry["source_ids"]) == 1 else ""
        source_name = PROVIDER_BY_ID.get(source_id, {}).get("name", "")
        for row in entry["rows"]:
            lead = dict(row)
            if not lead.get("source") and source_name:
                lead["source"] = source_name
            candidates.append(lead)
    return candidates


def _crm_status_label(value: str) -> str:
    return tr({
        "new": "Neu",
        "contacted": "Kontaktiert",
        "sent": "Gesendet",
        "replied": "Geantwortet",
        "interview": "Vorstellungsgespräch",
        "rejected": "Abgelehnt",
        "follow_up": "Nachfassen",
        "closed": "Abgeschlossen",
        "opt_out": "Widerspruch / Kontakt gesperrt",
    }.get(value, value))


def render_applications() -> None:
    st.header(tr("Bewerbungen · E-Mail-Versand"), icon=":material/mail:")
    st.caption(tr("Wähle CRM-Leads aus, prüfe die Zeilen und übertrage sie per Google Workspace oder Apps Script in dein Tabellenblatt."))
    st.warning(tr("Google-Zugriff und E-Mail-Versand erfolgen nur nach OAuth-Einwilligung und ausdrücklichem Start. Gmail-Kontingente, Einwilligungen und geltendes Recht beachten."))
    st.caption(tr("Das Tageslimit ist eine Sicherheitsgrenze, keine Gmail-Garantie; beachte die aktuellen Kontingente deines Google-Kontos."))

    workspace_config = google_workspace_config()
    tokens = st.session_state.get(google_workspace.TOKENS_KEY)
    resources = None
    if "google_spreadsheet_id" not in st.session_state:
        st.session_state["google_spreadsheet_id"] = workspace_config.spreadsheet_id if workspace_config else ""
    if "google_sheet_name" not in st.session_state:
        st.session_state["google_sheet_name"] = workspace_config.sheet_name if workspace_config else "BEWERBUNG"
    if "google_template_doc_id" not in st.session_state:
        st.session_state["google_template_doc_id"] = workspace_config.template_doc_id if workspace_config else ""
    if "google_static_pdf_id" not in st.session_state:
        st.session_state["google_static_pdf_id"] = workspace_config.static_pdf_id if workspace_config else ""
    created_spreadsheet_id = st.session_state.pop("google_spreadsheet_created", None)
    if created_spreadsheet_id:
        st.session_state["google_spreadsheet_id"] = created_spreadsheet_id
    if st.session_state.pop("google_workspace_disconnected", False):
        try:
            google_workspace.clear_persisted_session()
        except google_workspace.GoogleWorkspaceError as exc:
            st.session_state["google_workspace_error"] = tr(str(exc))
        st.session_state.pop(google_workspace.TOKENS_KEY, None)
        st.session_state.pop(google_workspace.ACCOUNT_KEY, None)
        st.session_state.pop("google_send_job", None)
        st.session_state.pop("google_resources_initialized", None)
        st.session_state.pop("google_workspace_resources", None)
        tokens = None

    with st.container(border=True):
        st.subheader(tr("Google Workspace verbinden"), icon=":material/account_circle:")
        if workspace_config is None:
            st.warning(tr("Google OAuth ist nicht aktiviert: es fehlen Client-ID, Client-Secret oder eine gültige localhost-Redirect-URI."))
            st.caption(tr("Füge die Google OAuth-Webclient-Werte in [google] in .streamlit/secrets.toml ein. Der Redirect muss exakt mit der Google Console übereinstimmen."))
        else:
            st.caption(tr("Scopes: Gmail senden, Sheets lesen/bearbeiten, Docs lesen/bearbeiten, Drive-Dateien lesen/verwalten und Konto-E-Mail lesen."))
            if tokens and st.session_state.get(google_workspace.ACCOUNT_KEY):
                st.success(f"{tr('Verbunden als')} {st.session_state[google_workspace.ACCOUNT_KEY]}")
                if st.button(tr("Google trennen und Sitzungstoken löschen"), key="google_workspace_disconnect"):
                    st.session_state["google_workspace_disconnected"] = True
                    st.rerun()
                try:
                    resources = initialize_google_workspace(workspace_config, tokens)
                    st.success(tr("Google-Arbeitsbereich automatisch vorbereitet."))
                    st.link_button(
                        tr("Google Sheet öffnen"),
                        resources["spreadsheet_url"],
                        icon=":material/open_in_new:",
                    )
                    pdfs = resources["pdfs"]
                    if pdfs:
                        pdf_ids = [item["id"] for item in pdfs]
                        selected_pdf = st.selectbox(
                            tr("Lebenslauf / Bewerbungs-PDF in Drive"),
                            options=pdf_ids,
                            index=(
                                pdf_ids.index(resources["static_pdf_id"])
                                if resources["static_pdf_id"] in pdf_ids else 0
                            ),
                            format_func=lambda pdf_id: next(
                                item["name"] for item in pdfs if item["id"] == pdf_id
                            ),
                            key="google_dossier_choice",
                        )
                        resources["static_pdf_id"] = selected_pdf
                        st.session_state["google_static_pdf_id"] = selected_pdf
                    else:
                        st.warning(tr("Kein PDF in Google Drive gefunden. Lade dein Bewerbungs-PDF in Drive hoch, damit der Versand es anhängen kann."))
                except google_workspace.GoogleWorkspaceError as exc:
                    st.error(tr(str(exc)))
            else:
                oauth_state = secrets.token_urlsafe(32)
                verifier = google_workspace.make_pkce_verifier()
                st.session_state[google_workspace.OAUTH_STATE_KEY] = oauth_state
                st.session_state[google_workspace.OAUTH_VERIFIER_KEY] = verifier
                st.link_button(
                    tr("Mit Google anmelden"),
                    google_workspace.authorization_url(workspace_config, oauth_state, verifier),
                    type="primary",
                    icon=":material/login:",
                )
                st.caption(tr("Nach der einmaligen Google-Einwilligung speichert Windows das Refresh-Token verschlüsselt im Benutzer-Tresor; Trennen löscht es."))
        if st.session_state.get("google_workspace_error"):
            st.error(st.session_state["google_workspace_error"])

    with st.expander(tr("Notfall: Manuelle Apps-Script-Konfiguration"), expanded=False):
        with st.form("application_script_config_form"):
            spreadsheet_url = st.text_input(
                tr("Google Sheets URL oder ID"),
                key="application_spreadsheet_url",
            )
            sheet_name = st.text_input(tr("Name des Tabellenblatts"), value="BEWERBUNG", key="application_sheet_name")
            template_doc_id = st.text_input(
                tr("Google-Docs-ID oder URL des Anschreiben-Musters"),
                key="application_template_doc_id",
            )
            static_pdf_id = st.text_input(
                tr("Google-Drive-ID oder URL des Lebenslaufs / Anlagen-PDF"),
                key="application_static_pdf_id",
            )
            sender_name = st.text_input(tr("Absendername"), key="application_sender_name")
            timezone = st.text_input(tr("Zeitzone"), value="Africa/Casablanca", key="application_timezone")
            limit_col, min_col, max_col = st.columns(3)
            daily_limit = limit_col.number_input(
                tr("Maximaler Versand pro Tag"), min_value=0, max_value=1000, value=159, step=1,
                key="application_daily_limit",
            )
            min_delay = min_col.number_input(
                tr("Minimale Verzögerung (Minuten)"), min_value=1, max_value=1440, value=1, step=1,
                key="application_min_delay",
            )
            max_delay = max_col.number_input(
                tr("Maximale Verzögerung (Minuten)"), min_value=1, max_value=1440, value=3, step=1,
                key="application_max_delay",
            )
            max_attempts = st.number_input(
                tr("Maximale Versuche je E-Mail"),
                min_value=1,
                max_value=5,
                value=3,
                key="application_max_attempts",
            )
            continue_on_error = st.checkbox(
                tr("Bei dauerhaftem Fehler mit der nächsten E-Mail fortfahren"),
                value=True,
                key="application_continue_on_error",
            )
            generate = st.form_submit_button(
                tr("Apps-Script-Konfiguration erstellen"),
                type="primary",
                key="application_script_generate",
            )
        if generate:
            config = {
                "spreadsheet_url": spreadsheet_url,
                "sheet_name": sheet_name,
                "template_doc_id": template_doc_id,
                "static_pdf_id": static_pdf_id,
                "sender_name": sender_name,
                "timezone": timezone,
                "max_per_day": daily_limit,
                "min_delay_minutes": min_delay,
                "max_delay_minutes": max_delay,
                "max_attempts": max_attempts,
                "continue_on_error": continue_on_error,
            }
            try:
                config = application_mail.normalize_script_config(config)
                application_mail.validate_script_config(config)
                st.session_state["application_script_config"] = config
            except ValueError as exc:
                st.error(tr(str(exc)))
        st.caption(tr("Der tägliche Trigger startet um 04:03 Uhr an Werktagen; Zufallsabstand und Tageslimit gelten zusätzlich. Die tatsächlichen Gmail-Quoten hängen von deinem Google-Konto ab."))

    config = st.session_state.get("application_script_config")
    if config:
        try:
            config = application_mail.normalize_script_config(config)
            script = application_mail.apps_script_source(config)
        except ValueError as exc:
            st.error(tr(str(exc)))
        else:
            st.success(tr("Konfiguration bereit. Der Versand bleibt in deinem Google-Konto und startet nicht automatisch."))
            st.markdown(
                tr("Lade den Script-Code herunter und füge ihn im Google Apps Script Editor des Tabellenblatts ein. "
                   "Lade anschließend die CSV in Google Drive hoch und nutze im Tabellenmenü „Bewerbungen“ → "
                   "„CSV aus Drive importieren“. Der Import hängt nur neue E-Mail-Adressen an. "
                   "Danach kannst du den täglichen Trigger ausdrücklich einrichten oder eine einzelne E-Mail "
                   "nach Bestätigung starten. „Versand pausieren / stoppen“ entfernt die Versand-Trigger. "
                   "Gesendet-Status und Versandverlauf liegen in Google Sheets; CRM-Status bei Bedarf separat aktualisieren.")
            )
            st.download_button(
                tr("Google Apps Script herunterladen"),
                data=script.encode("utf-8"),
                file_name="bewerbungen_apps_script.gs",
                mime="text/plain",
                icon=":material/download:",
                key="application_script_download",
            )
            st.code(script, language="javascript")

    st.subheader(tr("CRM-Leads für das Tabellenblatt auswählen"))
    leads = crm.list_leads(CRM_DATABASE_PATH, limit=5000)
    eligible = [
        row for row in leads
        if row["status"] != "opt_out" and row["email"].strip() and row["firma"].strip()
    ]
    lead_by_id = {int(row["id"]): row for row in eligible}
    if not eligible:
        st.info(tr("Keine versandfähigen CRM-Leads gefunden. Importiere zuerst Ergebnisse ins CRM."))
        return

    selected_ids = st.multiselect(
        tr("Leads auswählen"),
        options=list(lead_by_id),
        format_func=lambda lead_id: (
            f"{lead_by_id[lead_id]['firma']} · {lead_by_id[lead_id]['email']} · "
            f"{_crm_status_label(lead_by_id[lead_id]['status'])}"
        ),
        key="application_selected_leads",
        help=tr("Opt-out-Einträge werden ausgeschlossen; dieselbe E-Mail-Adresse wird nur einmal exportiert."),
    )
    subject_template = st.text_input(
        tr("Betreff-Vorlage"),
        value="Bewerbung bei {COMPANY}",
        key="application_subject_template",
    )
    message_template = st.text_area(
        tr("Nachrichten-Vorlage"),
        value="Guten Tag,\n\nanbei sende ich Ihnen meine Bewerbung.\n\nMit freundlichen Grüßen",
        key="application_message_template",
        height=120,
    )
    st.caption(tr("Platzhalter: {SALUTATION}, {COMPANY}, {PERSON}. Anschreiben-PDF: {COMPANY}, {SALUTATION}, {DATE}."))
    rows = application_mail.build_sheet_rows(
        eligible,
        selected_ids,
        subject_template=subject_template,
        message_template=message_template,
    )
    all_crm_rows = application_mail.build_sheet_rows(
        eligible,
        list(lead_by_id),
        subject_template=subject_template,
        message_template=message_template,
    )
    if resources and workspace_config and tokens and all_crm_rows:
        auto_config = replace(
            workspace_config,
            spreadsheet_id=resources["spreadsheet_id"],
            sheet_name=resources["sheet_name"],
            template_doc_id=resources["template_doc_id"],
            static_pdf_id=resources["static_pdf_id"],
            sender_name=str(st.session_state.get("application_sender_name", workspace_config.sender_name)).strip(),
        )
        digest = hashlib.sha256(
            json.dumps(all_crm_rows, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        if st.session_state.get("google_auto_sync_attempted_hash") != digest:
            st.session_state["google_auto_sync_attempted_hash"] = digest
            try:
                sync_result = google_workspace.sync_rows_to_sheet(
                    auto_config,
                    tokens,
                    auto_config.spreadsheet_id,
                    auto_config.sheet_name,
                    all_crm_rows,
                )
                st.session_state["google_auto_sync_error"] = ""
                st.session_state["google_auto_sync_hash"] = digest
                if sync_result["inserted"]:
                    crm.record_export(CRM_DATABASE_PATH, "google_sheets_api", sync_result["inserted"])
                    st.success(
                        f"{sync_result['inserted']} {tr('neue Zeilen hinzugefügt;')} "
                        f"{sync_result['skipped']} {tr('bereits vorhanden oder ohne Adresse übersprungen.')}"
                    )
                lead_id_by_email = {
                    str(lead["email"]).casefold(): int(lead["id"])
                    for lead in eligible
                    if lead.get("email")
                }
                for ready in sync_result["ready_rows"] + sync_result.get("sent_rows", []):
                    ledger = crm.google_send_ledger(CRM_DATABASE_PATH, ready["email"])
                    if ledger and ledger["state"] == "SENT":
                        crm.record_confirmed_google_contact(
                            CRM_DATABASE_PATH,
                            ledger.get("lead_id") or lead_id_by_email.get(ready["email"].casefold()),
                            str(ledger.get("message_id") or ""),
                            str(ledger.get("sent_at") or ""),
                        )
                        if ready.get("status") != "SENT":
                            google_workspace.update_sheet_status(
                                auto_config, tokens, auto_config.spreadsheet_id,
                                auto_config.sheet_name, int(ready["row_number"]),
                                "SENT", str(ledger.get("sent_at") or ""),
                            )
            except google_workspace.GoogleWorkspaceError as exc:
                st.session_state["google_auto_sync_error"] = tr(str(exc))
        if st.session_state.get("google_auto_sync_error"):
            st.error(st.session_state["google_auto_sync_error"])
            if st.button(tr("Google-Synchronisierung erneut versuchen"), key="google_sync_retry"):
                st.session_state.pop("google_auto_sync_attempted_hash", None)
                st.rerun()
    st.caption(f"{len(rows)} {tr('eindeutige Zeilen bereit; Vorschau und Export. Versand ist nur separat nach Bestätigung möglich.')}")
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        st.download_button(
            tr("CSV für Google Sheets herunterladen"),
            data=application_mail.csv_bytes(rows),
            file_name="bewerbungen.csv",
            mime="text/csv",
            on_click=crm.record_export,
            args=(CRM_DATABASE_PATH, "google_sheets_csv", len(rows)),
            key="application_csv_download",
        )
        if workspace_config and tokens and st.session_state.get(google_workspace.ACCOUNT_KEY) and resources:
            st.subheader(tr("Google Workspace ist automatisch synchronisiert"), icon=":material/sync:")
            direct_config = replace(
                workspace_config,
                spreadsheet_id=resources["spreadsheet_id"],
                sheet_name=resources["sheet_name"],
                template_doc_id=resources["template_doc_id"],
                static_pdf_id=resources["static_pdf_id"],
                sender_name=str(st.session_state.get("google_sender_name", "")).strip(),
            )
            st.text_input(
                tr("Absendername"),
                value=direct_config.sender_name,
                key="google_sender_name",
            )
            send_ready = bool(
                direct_config.spreadsheet_id
                and direct_config.template_doc_id
                and direct_config.static_pdf_id
            )
            limit_col, min_col, max_col = st.columns(3)
            daily_limit = int(limit_col.number_input(
                tr("Tageslimit für Gmail"), min_value=1, max_value=100, value=10,
                key="google_send_daily_limit",
            ))
            min_delay = int(min_col.number_input(
                tr("Minimale Verzögerung (Minuten)"), min_value=1, max_value=1440,
                value=1, key="google_send_min_delay",
            ))
            max_delay = int(max_col.number_input(
                tr("Maximale Verzögerung (Minuten)"), min_value=1, max_value=1440,
                value=3, key="google_send_max_delay",
            ))
            max_attempts = int(st.number_input(
                tr("Maximale Versuche je E-Mail"), min_value=1, max_value=5,
                value=3, key="google_send_max_attempts",
            ))
            continue_on_error = st.checkbox(
                tr("Bei dauerhaftem Fehler mit der nächsten E-Mail fortfahren"),
                value=True,
                key="google_send_continue_on_error",
            )
            pacing_ready = min_delay <= max_delay
            st.caption(tr("Der Start sendet höchstens eine E-Mail pro Schritt; Stop hält vor der nächsten E-Mail an."))
            if not pacing_ready:
                st.warning(tr("Die maximale Verzögerung muss mindestens so groß wie die minimale sein."))
            confirm_send = st.checkbox(
                tr("Ich habe Empfänger und Nachricht geprüft und bestätige den Versand."),
                key="google_send_confirm",
            )
            if st.button(
                tr("Google-Mailversand starten"),
                key="google_send_start",
                type="primary",
                icon=":material/send:",
                disabled=not confirm_send or not send_ready or not pacing_ready,
            ):
                try:
                    result = google_workspace.sync_rows_to_sheet(
                        direct_config, tokens, direct_config.spreadsheet_id,
                        direct_config.sheet_name, rows,
                    )
                    crm.record_export(CRM_DATABASE_PATH, "google_sheets_api", result["inserted"])
                    row_by_email = {
                        str(row["Email"]).casefold(): row
                        for row in rows
                    }
                    lead_by_email = {
                        str(lead["email"]).casefold(): int(lead["id"])
                        for lead in eligible
                    }
                    queued = []
                    for ready in result["ready_rows"]:
                        email = ready["email"].casefold()
                        ledger = crm.google_send_ledger(CRM_DATABASE_PATH, email)
                        if ledger and ledger["state"] == "SENT":
                            google_workspace.update_sheet_status(
                                direct_config, tokens, direct_config.spreadsheet_id,
                                direct_config.sheet_name, int(ready["row_number"]),
                                "SENT", str(ledger.get("sent_at") or ""),
                            )
                            continue
                        if ledger and ledger["state"] in {"SENDING", "VERIFY_REQUIRED"}:
                            google_workspace.update_sheet_status(
                                direct_config, tokens, direct_config.spreadsheet_id,
                                direct_config.sheet_name, int(ready["row_number"]),
                                "VERIFY_REQUIRED", "",
                            )
                            continue
                        if ledger and ledger["state"] == "ERROR":
                            google_workspace.update_sheet_status(
                                direct_config, tokens, direct_config.spreadsheet_id,
                                direct_config.sheet_name, int(ready["row_number"]),
                                "ERROR", "",
                            )
                            continue
                        if email in row_by_email:
                            queued.append({
                                **row_by_email[email],
                                "sheet_row": ready["row_number"],
                                "lead_id": lead_by_email.get(email),
                            })
                    if not queued:
                        st.info(tr("Keine neuen, noch nicht versendeten Zeilen gefunden."))
                    else:
                        local_today = datetime.now().astimezone().date().isoformat()
                        st.session_state["google_send_job"] = {
                            "rows": queued,
                            "cursor": 0,
                            "status": "running",
                            "spreadsheet_id": direct_config.spreadsheet_id,
                            "sheet_name": direct_config.sheet_name,
                            "sender_name": direct_config.sender_name,
                            "template_doc_id": direct_config.template_doc_id,
                            "static_pdf_id": direct_config.static_pdf_id,
                            "daily_limit": daily_limit,
                            "min_delay_minutes": min_delay,
                            "max_delay_minutes": max_delay,
                            "max_attempts": max_attempts,
                            "continue_on_error": continue_on_error,
                            "next_send_at": 0.0,
                            "sent_today_at_start": crm.google_sends_today(
                                CRM_DATABASE_PATH, local_today,
                            ),
                            "local_today": local_today,
                            "current_attempt": 0,
                            "confirmed_message_id": "",
                            "sent_at": "",
                        }
                        st.rerun()
                except google_workspace.GoogleWorkspaceError as exc:
                    st.error(tr(str(exc)))
            if not send_ready:
                st.info(tr("Für Gmail-Versand muss ein Bewerbungs-PDF in Drive verfügbar sein."))

            @st.fragment(
                run_every=2 if (
                    st.session_state.get("google_send_job", {}).get("status")
                    in {"running", "retry", "finalize"}
                ) else None,
                key="google_email_sender",
            )
            def google_email_sender_panel() -> None:
                job = st.session_state.get("google_send_job")
                if not job:
                    return
                total = len(job["rows"])
                cursor = int(job["cursor"])
                st.progress(min(cursor / max(total, 1), 1.0))
                st.caption(f"{tr('Versandfortschritt')}: {cursor}/{total}")
                if job["status"] in {"running", "retry", "finalize"}:
                    if job["status"] != "finalize" and st.button(
                        tr("Versand stoppen"), key="google_send_stop",
                        icon=":material/stop_circle:",
                    ):
                        job["status"] = "stopped"
                        st.info(tr("Versand gestoppt; die aktuelle Anfrage ist abgeschlossen."))
                        return
                    if job["status"] == "retry":
                        next_send_at = float(job.get("next_send_at", 0))
                        if next_send_at > datetime.now().timestamp():
                            next_send = datetime.fromtimestamp(next_send_at).astimezone().strftime("%H:%M")
                            st.caption(
                                f"{tr('Nächster Versand frühestens um')} {next_send} · "
                                f"{job.get('last_error', '')}"
                            )
                            return
                        job["status"] = "running"
                    if job["status"] == "finalize":
                        next_send_at = float(job.get("next_send_at", 0))
                        if next_send_at > datetime.now().timestamp():
                            next_send = datetime.fromtimestamp(next_send_at).astimezone().strftime("%H:%M")
                            st.caption(f"{tr('Tabellenstatus wird erneut aktualisiert um')} {next_send}")
                            return
                        current = job["rows"][cursor]
                        config_for_send = replace(
                            workspace_config,
                            spreadsheet_id=job["spreadsheet_id"],
                            sheet_name=job["sheet_name"],
                            template_doc_id=job["template_doc_id"],
                            static_pdf_id=job["static_pdf_id"],
                            sender_name=job["sender_name"],
                        )
                        current_tokens = st.session_state.get(google_workspace.TOKENS_KEY)
                        if not current_tokens:
                            job["status"] = "verify"
                            job["error"] = tr("Google-Sitzung getrennt; die E-Mail wurde bestätigt, Status bitte prüfen.")
                            return
                        crm.record_confirmed_google_contact(
                            CRM_DATABASE_PATH,
                            current.get("lead_id"),
                            job["confirmed_message_id"],
                            job["sent_at"],
                        )
                        try:
                            google_workspace.update_sheet_status(
                                config_for_send, current_tokens, job["spreadsheet_id"],
                                job["sheet_name"], current["sheet_row"], "SENT",
                                job["sent_at"],
                            )
                        except google_workspace.GoogleWorkspaceError as exc:
                            job["finalize_attempts"] = int(job.get("finalize_attempts", 0)) + 1
                            if job["finalize_attempts"] >= int(job["max_attempts"]):
                                job["status"] = "verify"
                                job["error"] = (
                                    tr("Gmail hat die E-Mail bestätigt, aber der Tabellenstatus konnte nicht aktualisiert werden. ")
                                    + tr(str(exc))
                                )
                            else:
                                job["status"] = "finalize"
                                job["next_send_at"] = datetime.now().timestamp() + google_workspace.retry_delay_seconds(
                                    job["finalize_attempts"],
                                )
                            return
                        job["cursor"] = cursor + 1
                        job["sent_at"] = ""
                        job["confirmed_message_id"] = ""
                        job["finalize_attempts"] = 0
                        job["status"] = "running"
                        if job["cursor"] >= total:
                            job["status"] = "done"
                        else:
                            delay = random.randint(
                                int(job["min_delay_minutes"]),
                                int(job["max_delay_minutes"]),
                            )
                            job["next_send_at"] = datetime.now().timestamp() + delay * 60
                        return
                    if cursor >= total:
                        job["status"] = "done"
                        st.success(tr("Google-Mailversand abgeschlossen."))
                        return
                    sent_today = crm.google_sends_today(
                        CRM_DATABASE_PATH, datetime.now().astimezone().date().isoformat(),
                    )
                    if sent_today >= int(job.get("daily_limit", 0)):
                        job["status"] = "limit"
                        st.warning(tr("Tageslimit erreicht; die verbleibenden E-Mails wurden nicht gesendet."))
                        return
                    next_send_at = float(job.get("next_send_at", 0))
                    if next_send_at > datetime.now().timestamp():
                        next_send = datetime.fromtimestamp(next_send_at).astimezone().strftime("%H:%M")
                        st.caption(f"{tr('Nächster Versand frühestens um')} {next_send}")
                        return

                    current = job["rows"][cursor]
                    sent_at = datetime.now().astimezone().isoformat(timespec="seconds")
                    config_for_send = replace(
                        workspace_config,
                        spreadsheet_id=job["spreadsheet_id"],
                        sheet_name=job["sheet_name"],
                        template_doc_id=job["template_doc_id"],
                        static_pdf_id=job["static_pdf_id"],
                        sender_name=job["sender_name"],
                    )
                    current_tokens = st.session_state.get(google_workspace.TOKENS_KEY)
                    if not current_tokens:
                        job["status"] = "error"
                        job["error"] = tr("Google-Sitzung getrennt; bitte erneut anmelden.")
                        return
                    try:
                        salutation, person = application_mail._person_fields(
                            current.get("Person"), current.get("Anrede"),
                        )
                        greeting = "Sehr geehrte Damen und Herren,"
                        if person and salutation == "Herr":
                            greeting = f"Sehr geehrter Herr {person},"
                        elif person and salutation == "Frau":
                            greeting = f"Sehr geehrte Frau {person},"
                        elif person:
                            greeting = f"Sehr geehrte/r Frau/Herr {person},"
                        company = current["Firma"]
                        subject = current["Betreff"].replace("{COMPANY}", company).replace("{PERSON}", person)
                        message = (
                            current["Nachricht"].replace("{SALUTATION}", greeting)
                            .replace("{COMPANY}", company).replace("{PERSON}", person)
                        )
                        attempt_number = crm.reserve_google_send(
                            CRM_DATABASE_PATH,
                            current["Email"],
                            lead_id=current.get("lead_id"),
                            spreadsheet_id=job["spreadsheet_id"],
                            sheet_row=int(current["sheet_row"]),
                            max_attempts=int(job["max_attempts"]),
                        )
                        if attempt_number is None:
                            ledger = crm.google_send_ledger(CRM_DATABASE_PATH, current["Email"])
                            if ledger and ledger["state"] == "SENT":
                                job["confirmed_message_id"] = ledger["message_id"]
                                job["sent_at"] = str(ledger.get("sent_at") or sent_at)
                                job["status"] = "finalize"
                                return
                            if ledger and ledger["state"] == "RETRY" and ledger.get("next_retry_at"):
                                job["next_send_at"] = datetime.fromisoformat(
                                    ledger["next_retry_at"],
                                ).timestamp()
                                job["status"] = "retry"
                                return
                            job["status"] = "verify"
                            job["error"] = tr("Vorheriger Sendeversuch muss geprüft werden; es wurde nichts erneut gesendet.")
                            return
                        job["current_attempt"] = attempt_number
                        job["last_error"] = ""
                        job["mail_attempted"] = False
                        google_workspace.update_sheet_status(
                            config_for_send, current_tokens, job["spreadsheet_id"],
                            job["sheet_name"], current["sheet_row"], "SENDING", "",
                        )
                        cover_letter = google_workspace.personalized_cover_pdf(
                            config_for_send, current_tokens,
                            company=company,
                            salutation=greeting,
                            today=datetime.now().strftime("%d.%m.%Y"),
                            person=job["sender_name"],
                        )
                        dossier = google_workspace._drive_file_bytes(
                            config_for_send, current_tokens, job["static_pdf_id"],
                        )
                        job["mail_attempted"] = True
                        message_id = google_workspace.send_gmail(
                            config_for_send,
                            current_tokens,
                            sender_email=st.session_state[google_workspace.ACCOUNT_KEY],
                            to=current["Email"],
                            subject=subject,
                            body=message,
                            attachments=[
                                ("Anschreiben.pdf", cover_letter),
                                ("Lebenslauf_und_Anlagen.pdf", dossier),
                            ],
                            sender_name=job["sender_name"],
                        )
                        job["confirmed_message_id"] = message_id
                        job["sent_at"] = sent_at
                        recorded = crm.record_google_send(
                            CRM_DATABASE_PATH,
                            current["Email"],
                            message_id,
                            sent_at,
                            lead_id=current.get("lead_id"),
                            spreadsheet_id=job["spreadsheet_id"],
                            sheet_row=int(current["sheet_row"]),
                        )
                        if not recorded:
                            ledger = crm.google_send_ledger(CRM_DATABASE_PATH, current["Email"])
                            if not ledger or ledger["state"] != "SENT":
                                raise sqlite3.IntegrityError("Confirmed Gmail send could not be recorded.")
                        job["sent_at"] = sent_at
                        job["status"] = "finalize"
                        job["finalize_attempts"] = 0
                    except google_workspace.GoogleWorkspaceError as exc:
                        attempt_number = max(1, int(job.get("current_attempt", 1)))
                        job["last_error"] = tr(str(exc))
                        try:
                            if exc.ambiguous:
                                crm.update_google_send_state(
                                    CRM_DATABASE_PATH, current["Email"], "VERIFY_REQUIRED",
                                    last_error=str(exc),
                                )
                                google_workspace.update_sheet_status(
                                    config_for_send, current_tokens, job["spreadsheet_id"],
                                    job["sheet_name"], current["sheet_row"], "VERIFY_REQUIRED", "",
                                )
                                job["status"] = "verify"
                                job["error"] = tr("Gmail-Sendeergebnis unklar; vor erneutem Versand den Ordner Gesendet prüfen.")
                            elif exc.transient and attempt_number < int(job["max_attempts"]):
                                delay = google_workspace.retry_delay_seconds(attempt_number)
                                next_retry = datetime.fromtimestamp(
                                    datetime.now().timestamp() + delay,
                                ).astimezone().isoformat(timespec="seconds")
                                crm.update_google_send_state(
                                    CRM_DATABASE_PATH, current["Email"], "RETRY",
                                    last_error=str(exc), next_retry_at=next_retry,
                                )
                                google_workspace.update_sheet_status(
                                    config_for_send, current_tokens, job["spreadsheet_id"],
                                    job["sheet_name"], current["sheet_row"], "RETRY", "",
                                )
                                job["status"] = "retry"
                                job["next_send_at"] = datetime.now().timestamp() + delay
                            else:
                                crm.update_google_send_state(
                                    CRM_DATABASE_PATH, current["Email"], "ERROR",
                                    last_error=str(exc),
                                )
                                google_workspace.update_sheet_status(
                                    config_for_send, current_tokens, job["spreadsheet_id"],
                                    job["sheet_name"], current["sheet_row"], "ERROR", "",
                                )
                                if job["continue_on_error"]:
                                    job["cursor"] = cursor + 1
                                    job["status"] = "running"
                                    delay = random.randint(
                                        int(job["min_delay_minutes"]),
                                        int(job["max_delay_minutes"]),
                                    )
                                    job["next_send_at"] = datetime.now().timestamp() + delay * 60
                                else:
                                    job["status"] = "error"
                        except (google_workspace.GoogleWorkspaceError, sqlite3.Error) as status_exc:
                            job["status"] = "verify"
                            job["error"] = (
                                tr("Der Versandstatus konnte nicht sicher aktualisiert werden. ")
                                + tr(str(status_exc))
                            )
                    except (ValueError, sqlite3.Error) as exc:
                        job["status"] = "verify"
                        job["error"] = (
                            tr("Gmail hat möglicherweise gesendet, aber der lokale Versandnachweis fehlt. ")
                            + tr(str(exc))
                        )
                        try:
                            crm.update_google_send_state(
                                CRM_DATABASE_PATH, current["Email"], "VERIFY_REQUIRED",
                                last_error=str(exc),
                            )
                        except (ValueError, LookupError, sqlite3.Error):
                            pass
                    if job["status"] == "done":
                        st.success(tr("Google-Mailversand abgeschlossen."))
                    elif job["status"] == "verify":
                        st.error(job.get("error", tr("Sendeergebnis bitte manuell prüfen.")))
                    elif job.get("last_error") and job["status"] == "running":
                        st.warning(f"{tr('E-Mail übersprungen; fortgesetzt')}: {job['last_error']}")
                    elif job["status"] == "finalize":
                        st.success(tr("Gmail hat den Versand bestätigt; Tabellenstatus wird aktualisiert."))
                    elif job["status"] in {"retry", "running"}:
                        st.info(f"{tr('Versendet')}: {current['Firma']}" if job["confirmed_message_id"] else tr("Versand wird sicher vorbereitet."))
                elif job["status"] == "stopped":
                    st.warning(tr("Versand gestoppt; der Fortschritt bleibt erhalten."))
                    if st.button(tr("Versand fortsetzen"), key="google_send_resume"):
                        job["status"] = "running"
                        st.rerun(scope="fragment")
                elif job["status"] == "error":
                    st.error(job.get("error", tr("Versand fehlgeschlagen; Tabellenstatus manuell prüfen.")))
                elif job["status"] == "verify":
                    st.error(job.get("error", tr("Sendeergebnis bitte manuell prüfen.")))
                elif job["status"] == "limit":
                    st.warning(tr("Tageslimit erreicht; die verbleibenden E-Mails wurden nicht gesendet."))
                else:
                    st.success(tr("Google-Mailversand abgeschlossen."))

            google_email_sender_panel()

            with st.expander(tr("Täglichen Google-Mailversand planen"), expanded=False):
                try:
                    saved_schedule = google_sender_schedule.load_config(
                        GOOGLE_SEND_SCHEDULE_PATH,
                    )
                except ValueError as exc:
                    saved_schedule = {"enabled": False}
                    st.error(tr(str(exc)))
                if saved_schedule.get("enabled"):
                    st.success(
                        f"{tr('Täglicher Versand aktiv ab')} "
                        f"{saved_schedule.get('schedule_time', '')}"
                    )
                schedule_time = st.time_input(
                    tr("Tägliche Startzeit (lokale Windows-Zeit)"),
                    value=time(9, 0),
                    key="google_schedule_time",
                )
                st.caption(tr("Der Windows-Aufgabenplaner läuft nur, wenn du in deinem Windows-Konto angemeldet bist. Der Auftrag nutzt den verschlüsselten Token-Tresor; Versand ist täglich begrenzt und stoppbar."))
                schedule_confirm = st.checkbox(
                    tr("Ich bestätige den täglichen automatischen Versand mit diesen Empfängern und Vorlagen."),
                    key="google_schedule_confirm",
                )
                st.caption(
                    f"{len(rows)} {tr('Empfänger für den geplanten Versand')}; "
                    f"{tr('nur diese bestätigten CRM-Leads werden berücksichtigt.')}"
                )
                schedule_col, stop_col = st.columns(2)
                if schedule_col.button(
                    tr("Täglichen Versand aktivieren"),
                    key="google_schedule_enable",
                    type="primary",
                    disabled=not schedule_confirm or not send_ready,
                ):
                    try:
                        selected_recipient_by_email = {}
                        for lead in eligible:
                            if int(lead["id"]) in selected_ids:
                                selected_recipient_by_email.setdefault(
                                    str(lead["email"]).casefold(), lead,
                                )
                        schedule_config = {
                            "enabled": True,
                            "schedule_time": schedule_time.strftime("%H:%M"),
                            "spreadsheet_id": direct_config.spreadsheet_id,
                            "sheet_name": direct_config.sheet_name,
                            "template_doc_id": direct_config.template_doc_id,
                            "static_pdf_id": direct_config.static_pdf_id,
                            "sender_name": direct_config.sender_name,
                            "recipients": [{
                                "lead_id": int(selected_recipient_by_email[row["Email"].casefold()]["id"]),
                                "email": row["Email"].casefold(),
                            } for row in rows],
                            "subject_template": subject_template,
                            "message_template": message_template,
                            "daily_limit": daily_limit,
                            "min_delay_minutes": min_delay,
                            "max_delay_minutes": max_delay,
                            "max_attempts": max_attempts,
                            "continue_on_error": continue_on_error,
                        }
                        google_sender_schedule.save_config(
                            GOOGLE_SEND_SCHEDULE_PATH, schedule_config,
                        )
                        google_sender_schedule.write_launcher(
                            GOOGLE_SEND_SCHEDULE_PATH,
                            GOOGLE_SEND_LAUNCHER_PATH,
                            project_dir=PROJECT_DIR,
                        )
                        google_sender_schedule.register_task(
                            GOOGLE_SEND_SCHEDULE_PATH,
                            GOOGLE_SEND_LAUNCHER_PATH,
                            schedule_time.strftime("%H:%M"),
                        )
                        st.success(tr("Täglicher Versand wurde im Windows-Aufgabenplaner aktiviert."))
                        st.rerun()
                    except (ValueError, RuntimeError, OSError) as exc:
                        st.error(tr(str(exc)))
                if saved_schedule.get("enabled") and stop_col.button(
                    tr("Geplanten Google-Versand stoppen"),
                    key="google_schedule_disable",
                    icon=":material/stop_circle:",
                ):
                    try:
                        saved_schedule["enabled"] = False
                        google_sender_schedule.save_config(
                            GOOGLE_SEND_SCHEDULE_PATH, saved_schedule,
                        )
                        google_sender_schedule.unregister_task()
                        st.success(tr("Geplanter Versand wurde gestoppt."))
                        st.rerun()
                    except (ValueError, RuntimeError, OSError) as exc:
                        st.error(tr(str(exc)))
                send_runs = google_sender_schedule.recent_runs(GOOGLE_SEND_RUNS_PATH)
                if send_runs:
                    st.caption(tr("Letzte geplante Google-Versandläufe"))
                    run_status_labels = {
                        "complete": tr("Abgeschlossen"),
                        "needs_review": tr("Prüfung erforderlich"),
                        "sync_pending": tr("Synchronisierung ausstehend"),
                        "daily_limit": tr("Tageslimit erreicht"),
                        "stopped": tr("Gestoppt"),
                        "error": tr("Fehler"),
                    }
                    st.dataframe(
                        pd.DataFrame([{
                            tr("Startzeit"): run["started_at"],
                            tr("Status"): run_status_labels.get(run["status"], run["status"]),
                            tr("Versendete E-Mails"): run["sent_count"],
                            tr("Ergebnis"): run["message"],
                        } for run in send_runs]),
                        width="stretch",
                        hide_index=True,
                    )
    else:
        st.info(tr("Wähle mindestens einen Lead mit gültiger E-Mail-Adresse aus."))


def render_crm() -> None:
    st.header(tr("Lead CRM"), icon=":material/contacts:")
    st.caption(tr("Leads, Kontaktverlauf und Wiedervorlagen werden lokal in SQLite gespeichert."))
    records = crm.list_leads(CRM_DATABASE_PATH, limit=5000)
    active = [row for row in records if row["status"] != "opt_out"]
    due = crm.due_follow_ups(CRM_DATABASE_PATH)
    total, follow_ups, coverage, average = st.columns(4)
    total.metric(tr("Aktive Leads"), len(active))
    follow_ups.metric(tr("Fällige Wiedervorlagen"), len(due))
    coverage.metric(tr("Mit E-Mail"), sum(1 for row in active if row["email"]))
    average.metric(tr("Durchschnittlicher Score"), f"{sum(row['lead_score'] for row in active) / len(active):.1f}" if active else "0")

    with st.form("crm_follow_up_settings"):
        settings_col, city_col = st.columns(2)
        follow_up_days = settings_col.selectbox(
            tr("Relance-Intervall für neue gesendete Leads"),
            options=crm.FOLLOW_UP_SCHEDULES,
            index=crm.FOLLOW_UP_SCHEDULES.index(crm.get_follow_up_days(CRM_DATABASE_PATH)),
            format_func=lambda days: f"{days} {tr('Tage')}",
        )
        target_city = city_col.text_input(
            tr("Zielstadt für Score-Abgleich"),
            value=crm.get_target_city(CRM_DATABASE_PATH),
        )
        save_settings = st.form_submit_button(tr("Follow-up-Einstellungen speichern"))
    if save_settings:
        crm.set_follow_up_days(CRM_DATABASE_PATH, follow_up_days)
        crm.set_target_city(CRM_DATABASE_PATH, target_city)
        st.success(tr("Follow-up-Einstellungen gespeichert. Neue gesendete Leads erhalten automatisch ein Wiedervorlagedatum."))
        st.rerun()

    st.caption(tr(
        "Lead-Score (0–100): E-Mail 25, Kontaktperson 10, Telefon 10, Quelle 15, "
        "Stadt 15, Aktualität 15, Workflow 10 Punkte."
    ))
    st.subheader(tr("Fällige Wiedervorlagen"))
    if due:
        st.dataframe(pd.DataFrame([{
            tr("Firma"): row["firma"],
            tr("Ort"): row["city"],
            tr("Status"): _crm_status_label(row["status"]),
            tr("Wiedervorlage"): row["follow_up_at"],
            tr("Score"): row["lead_score"],
        } for row in due]), width="stretch", hide_index=True, height=180)
    else:
        st.info(tr("Keine fälligen Wiedervorlagen."))

    with st.expander(tr("Ergebnisse in das CRM übernehmen"), expanded=not records):
        st.caption(tr("Importiert aktuelle Live-Ergebnisse und das lokale 10-Tage-Archiv; vorhandene CRM-Notizen und Status bleiben erhalten."))
        with st.form("crm_import_form"):
            purpose = st.text_input(tr("Dokumentierter Verarbeitungszweck"), placeholder=tr("z. B. Kontaktaufnahme zu Ausbildungsstellen"))
            legal_basis = st.text_input(
                tr("Von deiner Organisation bestätigte Rechtsgrundlage"),
                placeholder=tr("Bitte die intern geprüfte Grundlage eintragen"),
                help=tr("Kein Rechtsrat. Import erst nach dokumentierter Prüfung deiner Organisation."),
            )
            candidates = _crm_import_candidates()
            st.caption(f"{len(candidates)} {tr('Datensätze aus Live-Jobs und Archiv verfügbar.')}")
            submitted = st.form_submit_button(
                tr("Leads übernehmen"),
                type="primary",
                disabled=not candidates or not purpose.strip() or not legal_basis.strip(),
            )
        if submitted:
            result = crm.import_leads(
                CRM_DATABASE_PATH,
                candidates,
                processing_purpose=purpose.strip(),
                legal_basis=legal_basis.strip(),
                retention_days=crm.get_retention_days(CRM_DATABASE_PATH),
            )
            st.success(
                f"{result['inserted']} {tr('neu ·')} {result['updated']} {tr('aktualisiert ·')} "
                f"{result['suppressed']} {tr('gesperrt ·')} {result['skipped']} {tr('ohne Firmennamen übersprungen.')}"
            )
            st.rerun()

    search_col, status_col, score_col, sort_col = st.columns([2, 1, 1, 1])
    query = search_col.text_input(
        tr("CRM durchsuchen"),
        placeholder=tr("Firma, Person, E-Mail, Telefon, Stadt oder Quelle"),
        key="crm_query",
    )
    status_filter = status_col.selectbox(
        tr("Status"),
        options=["all", *crm.STATUSES],
        format_func=lambda value: tr("Alle") if value == "all" else _crm_status_label(value),
        key="crm_status_filter",
    )
    min_score = score_col.number_input(
        tr("Mindest-Score"), min_value=0, max_value=100, value=0, step=5, key="crm_min_score",
    )
    score_sort = sort_col.selectbox(
        tr("Sortierung"),
        options=("score_desc", "score_asc", "updated_desc"),
        format_func=lambda value: tr({
            "score_desc": "Höchster Score zuerst",
            "score_asc": "Niedrigster Score zuerst",
            "updated_desc": "Zuletzt aktualisiert",
        }[value]),
        key="crm_score_sort",
    )
    filtered = crm.list_leads(
        CRM_DATABASE_PATH,
        status=None if status_filter == "all" else status_filter,
        query=query,
        limit=5000,
    )
    filtered = [row for row in filtered if row["lead_score"] >= min_score]
    if score_sort == "score_desc":
        filtered.sort(key=lambda row: (row["lead_score"], row["updated_at"]), reverse=True)
    elif score_sort == "score_asc":
        filtered.sort(key=lambda row: (row["lead_score"], row["updated_at"]))
    table_rows = [{
        tr("Firma"): row["firma"] or f"({tr('Kontakt gesperrt')})",
        tr("Kontakt"): row["person"],
        tr("E-Mail"): row["email"],
        tr("Telefon"): row["telefonnummer"],
        tr("Ort"): row["city"],
        tr("Quelle"): row["source"],
        tr("Status"): _crm_status_label(row["status"]),
        tr("Score"): row["lead_score"],
        tr("Wiedervorlage"): row["follow_up_at"] or "",
        tr("Frist"): row["retention_until"][:10],
    } for row in filtered]
    if table_rows:
        st.dataframe(pd.DataFrame(table_rows), width="stretch", hide_index=True, height=300)
        record_by_id = {row["id"]: row for row in filtered}
        selected_id = st.selectbox(
            tr("Lead bearbeiten"),
            options=list(record_by_id),
            format_func=lambda lead_id: (
                f"{record_by_id[lead_id]['firma'] or tr('Kontakt gesperrt')} · "
                f"{record_by_id[lead_id]['city']} · {record_by_id[lead_id]['source']}"
            ),
            key="crm_selected_lead",
        )
        selected = record_by_id[selected_id]
        score_parts = selected["score_breakdown"]
        st.caption(
            f"{tr('Score-Aufschlüsselung')}: "
            f"{tr('E-Mail')} {score_parts['email']}/25 · "
            f"{tr('Kontaktperson')} {score_parts['person']}/10 · "
            f"{tr('Telefon')} {score_parts['phone']}/10 · "
            f"{tr('Quelle')} {score_parts['source']}/15 · "
            f"{tr('Stadt')} {score_parts['city']}/15 · "
            f"{tr('Aktualität')} {score_parts['recentness']}/15 · "
            f"{tr('Workflow')} {score_parts['workflow']}/10"
        )
        st.caption(
            f"{tr('Quelle')}: {selected['source']} · {tr('Zweck')}: {selected['processing_purpose'] or tr('nicht dokumentiert')} · "
            f"{tr('Rechtsgrundlage')}: {selected['legal_basis'] or tr('nicht dokumentiert')}"
        )
        if selected["status"] == "opt_out":
            st.info(tr("Kontaktangaben wurden nach dem Widerspruch entfernt; die Sperre bleibt zur Vermeidung eines erneuten Imports bestehen."))
        else:
            editable_statuses = tuple(status for status in crm.STATUSES if status != "opt_out")
            with st.form(f"crm_lead_form_{selected_id}"):
                status = st.selectbox(
                    tr("Bearbeitungsstatus"),
                    options=editable_statuses,
                    index=editable_statuses.index(selected["status"]),
                    format_func=_crm_status_label,
                )
                follow_up = st.text_input(
                    tr("Wiedervorlage (YYYY-MM-DD, optional)"),
                    value=selected["follow_up_at"] or "",
                )
                notes = st.text_area(tr("Notizen"), value=selected["notes"], height=90)
                save = st.form_submit_button(tr("Änderungen speichern"), type="primary")
            if save:
                try:
                    crm.update_lead(
                        CRM_DATABASE_PATH, selected_id, status=status,
                        follow_up_at=follow_up.strip(), notes=notes,
                    )
                    st.success(tr("CRM-Eintrag gespeichert."))
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

            with st.form(f"crm_activity_form_{selected_id}"):
                activity_type = st.selectbox(
                    tr("Interaktionstyp"),
                    options=crm.ACTIVITY_TYPES,
                    format_func=lambda value: tr({
                        "note": "Notiz", "call": "Anruf", "email": "E-Mail", "meeting": "Termin",
                    }[value]),
                )
                details = st.text_input(tr("Kontaktverlauf"))
                added = st.form_submit_button(tr("Interaktion protokollieren"))
            if added:
                try:
                    crm.add_activity(CRM_DATABASE_PATH, selected_id, activity_type, details)
                    st.success(tr("Interaktion protokolliert."))
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            activities = crm.list_activities(CRM_DATABASE_PATH, selected_id)
            if activities:
                st.markdown(f"**{tr('Kontaktverlauf')}**")
                activity_frame = localize_table(activities)
                activity_frame[tr("Interaktionstyp")] = activity_frame[tr("Interaktionstyp")].map(
                    lambda value: tr({
                        "note": "Notiz", "call": "Anruf", "email": "E-Mail", "meeting": "Termin",
                    }.get(value, value))
                )
                st.dataframe(activity_frame, width="stretch", hide_index=True, height=180)
    else:
        st.info(tr("Keine Leads gefunden. Importiere zuerst Live- oder Archiv-Ergebnisse."))

    exportable = [row for row in active if row["firma"]]
    if exportable:
        export_frame = pd.DataFrame([{
            tr("Firma"): row["firma"],
            tr("Kontakt"): row["person"],
            tr("E-Mail"): row["email"],
            tr("Telefon"): row["telefonnummer"],
            tr("Ort"): row["city"],
            tr("Quelle"): row["source"],
            tr("Website"): row["website"],
            tr("Link"): row["source_url"],
            tr("Status"): _crm_status_label(row["status"]),
            tr("Wiedervorlage"): row["follow_up_at"],
        } for row in exportable])
        st.download_button(
            tr("CRM als CSV exportieren"),
            data=export_frame.to_csv(index=False).encode("utf-8-sig"),
            file_name="crm_leads.csv",
            mime="text/csv",
            on_click=crm.record_export,
            args=(CRM_DATABASE_PATH, "csv", len(exportable)),
            key="crm_export_csv",
        )


def render_schedule() -> None:
    st.subheader(tr("Geplante Suche · Windows-Aufgabenplanung"), icon=":material/event_repeat:")
    if os.name != "nt":
        st.info(tr("Geplante Suchen benötigen die Windows-Aufgabenplanung; diese Funktion ist auf diesem System nicht verfügbar."))
        return
    config = local_schedule.load_config(SCHEDULE_CONFIG_PATH)
    if config["enabled"]:
        st.success(f"{tr('Tägliche Jobsuche um')} {config['schedule_time']} {tr('(lokale Zeit); Task läuft nur, wenn du angemeldet bist.')}")
    else:
        st.caption(tr("Registriert einen täglichen Job unter deinem Windows-Benutzerkonto. Zugangsdaten werden nicht in der Aufgabe gespeichert."))
    try:
        configured_time = time.fromisoformat(config["schedule_time"])
    except ValueError:
        configured_time = time(9, 0)
    with st.form("scheduled_search_form"):
        keyword = st.text_input(tr("Jobbörse-Suchbegriff"), value=config["keyword"], placeholder=tr("z. B. Fachinformatiker"))
        locations = st.text_input(
            tr("Städte (optional, Komma oder + getrennt)"),
            value=", ".join(config["locations"]),
            placeholder="Bonn + Köln",
        )
        run_time = st.time_input(tr("Täglich um"), value=configured_time)
        purpose = st.text_input(
            tr("Verarbeitungszweck"),
            value=config["processing_purpose"],
            placeholder=tr("Dein dokumentierter Zweck"),
        )
        legal_basis = st.text_input(
            tr("Bestätigte Rechtsgrundlage"),
            value=config["legal_basis"],
            placeholder=tr("Intern geprüfte Rechtsgrundlage"),
        )
        retention_days = st.number_input(
            tr("CRM-Aufbewahrung (Tage)"),
            min_value=1,
            max_value=3650,
            value=int(config["retention_days"]),
        )
        register = st.form_submit_button(
            tr("Tägliche Aufgabe registrieren/aktualisieren"),
            type="primary",
            disabled=not keyword.strip() or not purpose.strip() or not legal_basis.strip(),
        )
    if register:
        updated = {
            **config,
            "enabled": True,
            "source": "arbeitsagentur",
            "keyword": keyword.strip(),
            "locations": list(split_locations(locations)),
            "schedule_time": run_time.strftime("%H:%M"),
            "processing_purpose": purpose.strip(),
            "legal_basis": legal_basis.strip(),
            "retention_days": int(retention_days),
        }
        try:
            local_schedule.save_config(SCHEDULE_CONFIG_PATH, updated)
            local_schedule.write_launcher(
                SCHEDULE_CONFIG_PATH,
                SCHEDULE_LAUNCHER_PATH,
                project_dir=PROJECT_DIR,
                python_executable=sys.executable,
            )
            local_schedule.register_windows_task(
                SCHEDULE_CONFIG_PATH, SCHEDULE_LAUNCHER_PATH, updated["schedule_time"],
            )
            crm.set_retention_days(CRM_DATABASE_PATH, updated["retention_days"])
            st.success(tr("Tägliche Aufgabe wurde in der Windows-Aufgabenplanung registriert."))
            st.rerun()
        except (OSError, RuntimeError, ValueError) as exc:
            st.error(f"{tr('Aufgabe konnte nicht registriert werden:')} {exc}")
    if config["enabled"] and st.button(tr("Geplante Suche deaktivieren"), key="disable_scheduled_search"):
        try:
            local_schedule.unregister_windows_task()
            config["enabled"] = False
            local_schedule.save_config(SCHEDULE_CONFIG_PATH, config)
            st.success(tr("Windows-Aufgabe entfernt."))
            st.rerun()
        except (OSError, RuntimeError, ValueError) as exc:
            st.error(f"{tr('Aufgabe konnte nicht entfernt werden:')} {exc}")
    runs = local_schedule.recent_runs(SCHEDULE_RUNS_PATH)
    if runs:
        st.markdown(f"**{tr('Letzte geplante Läufe')}**")
        run_frame = localize_table(runs)
        if tr("Status") in run_frame:
            run_frame[tr("Status")] = run_frame[tr("Status")].map(tr)
        st.dataframe(run_frame, width="stretch", hide_index=True, height=220)


def render_analytics() -> None:
    st.header(tr("Analytics"), icon=":material/analytics:")
    latest_run = local_schedule.recent_runs(SCHEDULE_RUNS_PATH, limit=1)
    if latest_run:
        run = latest_run[0]
        if run["status"] == "failed":
            st.error(f"{tr('Die letzte geplante Suche ist fehlgeschlagen:')} {run['message']}")
        elif run["new_count"]:
            st.success(
                f"{tr('Die letzte geplante Suche hat')} {run['new_count']} "
                f"{tr('neue Ergebnisse gefunden. Sie wurden im Archiv und CRM erfasst.')}"
            )
    data = crm.metrics(CRM_DATABASE_PATH)
    columns = st.columns(4)
    columns[0].metric(tr("Leads"), data["total"])
    columns[1].metric(tr("Mit E-Mail"), data["with_email"])
    columns[2].metric(tr("E-Mail-Abdeckung"), f"{data['email_coverage']:.1f}%")
    columns[3].metric(tr("Neu · letzte 7 Tage"), data["new_last_7_days"])
    coverage = st.columns(5)
    coverage[0].metric(tr("Mit Telefon"), data["with_phone"])
    coverage[1].metric(tr("Benannte Kontakte"), data["with_person"])
    coverage[2].metric(tr("Durchschnittlicher Score"), f"{data['average_score']:.1f}")
    coverage[3].metric(tr("Fällige Wiedervorlagen"), data["due_follow_ups"])
    coverage[4].metric(tr("CRM-Exporte"), data["exports"])
    left, right = st.columns(2)
    with left:
        st.subheader(tr("Quelle"))
        if data["source_counts"]:
            source_frame = pd.DataFrame(data["source_counts"]).rename(columns={
                "source": tr("Quelle"), "count": tr("Anzahl"),
            })
            st.bar_chart(source_frame.set_index(tr("Quelle")))
        else:
            st.info(tr("Noch keine CRM-Importe."))
    with right:
        st.subheader(tr("Stadt"))
        if data["city_counts"]:
            city_frame = pd.DataFrame(data["city_counts"]).rename(columns={
                "city": tr("Stadt"), "count": tr("Anzahl"),
            })
            st.bar_chart(city_frame.set_index(tr("Stadt")))
        else:
            st.info(tr("Stadtangaben erscheinen nach dem CRM-Import."))
    st.subheader(tr("Pipeline-Status"))
    if data["pipeline_counts"]:
        pipeline_frame = pd.DataFrame(data["pipeline_counts"]).rename(columns={
            "status": tr("Status"), "count": tr("Anzahl"),
        })
        pipeline_frame[tr("Status")] = pipeline_frame[tr("Status")].map(_crm_status_label)
        st.bar_chart(pipeline_frame.set_index(tr("Status")))
    else:
        st.info(tr("Noch keine Pipeline-Daten."))
    due = crm.due_follow_ups(CRM_DATABASE_PATH)
    st.subheader(tr("Fällige Wiedervorlagen"))
    if due:
        st.dataframe(pd.DataFrame([{
            tr("Firma"): row["firma"],
            tr("Ort"): row["city"],
            tr("Status"): _crm_status_label(row["status"]),
            tr("Wiedervorlage"): row["follow_up_at"],
            tr("Score"): row["lead_score"],
        } for row in due]), width="stretch", hide_index=True, height=180)
    else:
        st.info(tr("Keine fälligen Wiedervorlagen."))
    recent = sorted(
        crm.list_leads(CRM_DATABASE_PATH, limit=5000),
        key=lambda row: row["first_seen_at"],
        reverse=True,
    )[:10]
    st.subheader(tr("Kürzlich gefundene Leads"))
    if recent:
        st.dataframe(pd.DataFrame([{
            tr("Firma"): row["firma"] or f"({tr('Kontakt gesperrt')})",
            tr("Quelle"): row["source"],
            tr("Ort"): row["city"],
            tr("Status"): _crm_status_label(row["status"]),
            tr("Gefunden"): row["first_seen_at"],
        } for row in recent]), width="stretch", hide_index=True)
    else:
        st.info(tr("Nach dem Import erscheinen hier neue Leads."))
    render_schedule()


def render_gdpr() -> None:
    st.header(tr("Datenschutz & Aufbewahrung"), icon=":material/privacy_tip:")
    st.caption(tr(
        "Lokale Verwaltungsfunktionen, keine Rechtsberatung. Bestätige Zweck und Rechtsgrundlage "
        "mit deiner Organisation und beachte die für dich geltenden Anforderungen."
    ))
    retention = crm.get_retention_days(CRM_DATABASE_PATH)
    with st.form("crm_retention_form"):
        days = st.number_input(
            tr("Aufbewahrungsfrist für CRM-Leads (Tage)"),
            min_value=1, max_value=3650, value=retention,
            help=tr("Die Frist wird beim nächsten Import auf aktive Leads angewendet."),
        )
        save_retention = st.form_submit_button(tr("Aufbewahrungsfrist speichern"))
    if save_retention:
        crm.set_retention_days(CRM_DATABASE_PATH, int(days))
        st.success(tr("Aufbewahrungsfrist gespeichert."))
        st.rerun()

    expired = crm.expired_count(CRM_DATABASE_PATH)
    st.write(f"{tr('Abgelaufene CRM-Datensätze:')} **{expired}**")
    if expired:
        with st.form("crm_purge_form"):
            confirm_purge = st.checkbox(tr("Ich bestätige das Löschen aller abgelaufenen CRM-Datensätze."))
            purge = st.form_submit_button(tr("Abgelaufene Daten löschen"), disabled=not confirm_purge)
        if purge:
            st.success(f"{crm.prune_expired(CRM_DATABASE_PATH)} {tr('abgelaufene Datensätze gelöscht.')}")
            st.rerun()

    eligible = [row for row in crm.list_leads(CRM_DATABASE_PATH, limit=5000) if row["status"] != "opt_out"]
    if eligible:
        by_id = {row["id"]: row for row in eligible}
        selected_id = st.selectbox(
            tr("Datenschutzanfrage für Lead"),
            options=list(by_id),
            format_func=lambda lead_id: (
                f"{by_id[lead_id]['firma']} · {by_id[lead_id]['source']} · {by_id[lead_id]['city']}"
            ),
            key="gdpr_selected_lead",
        )
        with st.form("gdpr_request_form"):
            action = st.selectbox(
                tr("Aktion"),
                options=("opt_out", "delete"),
                format_func=lambda value: (
                    tr("Widerspruch / Kontakt sperren und Kontaktdaten bereinigen")
                    if value == "opt_out" else tr("Datensatz und Kontaktverlauf endgültig löschen")
                ),
            )
            reason = st.text_input(tr("Anlass (ohne unnötige personenbezogene Details)"))
            confirmed = st.checkbox(tr("Ich bestätige diese Datenschutzaktion."))
            apply_action = st.form_submit_button(tr("Datenschutzaktion ausführen"), disabled=not confirmed)
        if apply_action:
            if action == "opt_out":
                crm.opt_out_lead(CRM_DATABASE_PATH, selected_id, reason)
                st.success(tr("Widerspruch gespeichert; Kontaktangaben wurden bereinigt."))
            else:
                crm.delete_lead(CRM_DATABASE_PATH, selected_id, reason)
                st.success(tr("CRM-Datensatz und Kontaktverlauf wurden gelöscht."))
            st.rerun()
    actions = crm.list_gdpr_actions(CRM_DATABASE_PATH)
    if actions:
        st.subheader(tr("Datenschutz-Auditprotokoll"))
        st.caption(tr("Das Protokoll enthält gehashte Referenzen, Aktion, Anlass und Zeitstempel – keine Kontaktfelder."))
        action_frame = localize_table(actions)
        action_frame[tr("Aktion")] = action_frame[tr("Aktion")].map(lambda value: {
            "opt_out": tr("Widerspruch / Kontakt sperren und Kontaktdaten bereinigen"),
            "delete": tr("Datensatz und Kontaktverlauf endgültig löschen"),
        }.get(value, value))
        st.dataframe(action_frame, width="stretch", hide_index=True, height=220)


def render_history() -> None:
    """Show locally saved results that have not reached their ten-day expiry."""
    entries = recent_results(HISTORY_DATABASE_PATH)
    st.header(f"{tr('Archiv ·')} {RETENTION_DAYS} {tr('Tage')}", icon=":material/inventory_2:")
    st.caption(tr("Abgeschlossene Suchen und Live-Extraktionen bleiben lokal auf diesem PC gespeichert und werden danach automatisch gelöscht."))
    if not entries:
        st.info(tr("Noch keine gespeicherten Ergebnisse. Starte eine Recherche oder eine Live-Extraktion."))
        return

    entry_by_id = {entry["id"]: entry for entry in entries}
    selected_id = st.selectbox(
        tr("Gespeicherte Suche öffnen"),
        options=list(entry_by_id),
        format_func=lambda entry_id: (
            f"{entry_by_id[entry_id]['title']} · "
            f"{entry_by_id[entry_id]['created_at'][:16].replace('T', ' ')}"
        ),
        key="history_entry",
    )
    entry = entry_by_id[selected_id]
    first, second, third = st.columns(3)
    first.metric(tr("Datensätze"), len(entry["rows"]))
    second.metric(tr("Quellen"), len(entry["source_ids"]))
    third.metric(tr("Automatisch gelöscht"), entry["expires_at"][:10])
    if entry["rows"]:
        st.dataframe(localize_table(entry["rows"]), width="stretch", hide_index=True, height=420)
        st.download_button(
            tr("Archiv als JSON herunterladen"),
            data=json_bytes(entry["rows"]),
            file_name=f"archiv_{entry['id']}.json",
            mime="application/json",
            icon=":material/download:",
        )


def _set_dashboard_tab(label: str) -> None:
    st.session_state["dashboard_tabs"] = label


dashboard_tabs = [
    (tr(":material/search: Recherche"), "search"),
    (tr(":material/monitoring: Live-Extraktionen"), "live"),
    (tr(":material/contacts: CRM"), "crm"),
    (tr(":material/mail: Bewerbungen"), "applications"),
    (tr(":material/analytics: Analytics"), "analytics"),
    (f"{tr(':material/inventory_2: Archiv')} · {RETENTION_DAYS} {tr('Tage')}", "archive"),
    (tr(":material/privacy_tip: Datenschutz"), "privacy"),
]
tab_labels = [label for label, _ in dashboard_tabs]
if st.session_state.get("dashboard_tabs") not in tab_labels:
    st.session_state["dashboard_tabs"] = tab_labels[0]

title_col, status_col, language_col = st.columns([3, 1, 1.25], vertical_alignment="center")
with title_col:
    st.title(tr("Ausbildung Lead Finder"), icon=":material/manage_search:")
with status_col:
    extraction_running = aa_live_running() or any(
        generic_live_running(source_id) for source_id in GENERIC_LIVE_SOURCE_IDS
    )
    st.badge(
        tr("Live-Extraktion läuft") if extraction_running else tr("Bereit · lokale App"),
        icon=":material/monitoring:" if extraction_running else ":material/check_circle:",
        color="orange" if extraction_running else "green",
    )
with language_col:
    st.selectbox(
        tr("Sprache / اللغة / Language"),
        options=LANGUAGES,
        key=LANGUAGE_KEY,
        label_visibility="collapsed",
    )
language_css = layout_css(str(st.session_state.get(LANGUAGE_KEY, DEFAULT_LANGUAGE)))
st.html(language_css + visual_css(str(st.session_state.get(LANGUAGE_KEY, DEFAULT_LANGUAGE))))

with st.sidebar:
    st.subheader(tr("Quellen"), icon=":material/source:")
    st.caption(tr("Aktiviere Quellen; die Sucheinstellungen erscheinen im Hauptbereich."))
    supported_providers = [provider for provider in PROVIDERS if provider["supported"]]
    for provider in supported_providers:
        provider_card(provider)
    with st.expander(tr("Weitere Quellen (offizieller Zugang erforderlich)")):
        for provider in (item for item in PROVIDERS if not item["supported"]):
            provider_card(provider)

    st.divider()
    st.subheader(tr("Schnellnavigation"), icon=":material/arrow_forward:")
    for label, key in dashboard_tabs:
        translated_label = tr(label)
        st.button(
            translated_label,
            key=f"navigate_{key}",
            icon=label.split(" ", 1)[0] if label.startswith(":material/") else None,
            type="tertiary",
            width="stretch",
            on_click=_set_dashboard_tab,
            args=(translated_label,),
        )

selected = active_source_ids()
settings_by_source: dict[str, dict] = {}
search_tab, live_tab, crm_tab, application_tab, analytics_tab, archive_tab, gdpr_tab = st.tabs([
    *tab_labels,
], key="dashboard_tabs", on_change="rerun")

with search_tab:
    st.header(tr("Recherche einrichten"), icon=":material/search:")
    st.caption(tr("Aktiviere Quellen links und richte die Suchbegriffe und Orte hier ein."))
    if not selected:
        st.info(tr("Aktiviere mindestens eine Quelle in der Seitenleiste, um die Suche einzurichten."))
    else:
        source_tab_labels = [PROVIDER_BY_ID[source_id]["name"] for source_id in selected]
        source_tabs = st.tabs(
            source_tab_labels,
            default=PROVIDER_BY_ID["google_maps"]["name"] if "google_maps" in selected else None,
        )
        for tab, source_id in zip(source_tabs, selected):
            with tab:
                settings_by_source[source_id] = source_settings(source_id)

        st.subheader(tr("Recherche ohne Kontaktanreicherung"), icon=":material/search:")
        st.caption(tr("Sucht die ausgewählten Quellen und speichert das Ergebnis im lokalen Archiv. E-Mail-/Telefonprüfung bleibt ein eigener Live-Schritt."))
        if st.button(
            tr("Ausgewählte Quellen suchen"),
            type="primary",
            width="stretch",
            icon=":material/search:",
            key="search_selected_sources",
        ):
            logs: list[str] = []
            statuses: list[dict] = []
            listings: list[dict] = []
            log_view = st.empty()
            with st.status(tr("Recherche läuft …"), expanded=True) as search_status:
                for source_id in selected:
                    try:
                        if source_id == "arbeitsagentur":
                            for result in run_arbeitsagentur(settings_by_source[source_id], logs, log_view):
                                listings.extend(result.listings)
                                statuses.append({
                                    "source": PROVIDER_BY_ID[source_id]["name"],
                                    "status": result.status,
                                    "message": (
                                        f"{result.unique_count} eindeutige Treffer; "
                                        f"{result.message}"
                                    ),
                                })
                        else:
                            source_rows, source_status = run_other_source(
                                source_id, settings_by_source[source_id], logs, log_view,
                            )
                            listings.extend(source_rows)
                            queries = source_status.get("queries", [])
                            if queries:
                                statuses.extend({
                                    "source": source_status["source"],
                                    **query_status,
                                } for query_status in queries)
                                statuses.append({
                                    "source": source_status["source"],
                                    "query": "Gesamtergebnis",
                                    "status": source_status["status"],
                                    "message": source_status["message"],
                                })
                            else:
                                statuses.append({
                                    "source": source_status["source"],
                                    "status": source_status["status"],
                                    "message": source_status["message"],
                                })
                    except (requests.RequestException, OSError, RuntimeError, ValueError) as exc:
                        message = f"{PROVIDER_BY_ID[source_id]['name']}: {type(exc).__name__}: {exc}"
                        show_log(logs, f"  ! {message}", log_view)
                        statuses.append({
                            "source": PROVIDER_BY_ID[source_id]["name"],
                            "status": "failed",
                            "message": message,
                        })
                listings = unique_listings(listings)
                st.session_state["listings"] = listings
                st.session_state["statuses"] = statuses
                st.session_state["logs"] = logs
                save_result(
                    HISTORY_DATABASE_PATH,
                    title="Recherche · " + ", ".join(PROVIDER_BY_ID[source_id]["name"] for source_id in selected),
                    source_ids=selected,
                    rows=listings,
                    metadata={"type": "search", "statuses": statuses},
                )
                search_status.update(
                    label=tr("Recherche abgeschlossen"),
                    state="complete",
                    expanded=False,
                )
            st.rerun()

    if "listings" in st.session_state or "statuses" in st.session_state:
        listings = st.session_state.get("listings", [])
        statuses = st.session_state.get("statuses", [])
        st.header(tr("Ergebnis und Vollständigkeit"), icon=":material/table_chart:")
        if statuses:
            status_frame = pd.DataFrame(statuses).rename(columns={
                "source": tr("Quelle"), "query": tr("Suchanfrage"),
                "status": tr("Status"), "message": tr("Meldung"),
            })
            if tr("Status") in status_frame:
                status_frame[tr("Status")] = status_frame[tr("Status")].map(tr)
            st.dataframe(status_frame, width="stretch", hide_index=True)
        if not listings:
            st.warning(tr("Keine Listings wurden abgerufen. Prüfe den Status und die Einstellungen jeder Quelle."))
        else:
            frame = display_frame(listings)
            first, second, third = st.columns(3)
            first.metric(tr("Eindeutige Listings"), len(frame))
            second.metric(tr("Mit Referenznummer"), int(frame["Referenznummer"].astype(bool).sum()))
            third.metric(tr("Mit Original-URL"), int(frame["Original-URL"].astype(bool).sum()))
            st.dataframe(
                frame, width="stretch", hide_index=True, height=450,
                column_config={tr("Original-URL"): st.column_config.LinkColumn(tr("Original-URL"), display_text=tr("Öffnen"))},
            )
            export_one, export_two, export_three = st.columns(3)
            with export_one:
                st.download_button(tr("Excel herunterladen"), excel_bytes(listings), "jobsuche.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", icon=":material/download:")
            with export_two:
                st.download_button(tr("CSV herunterladen"), csv_bytes(listings), "jobsuche.csv", "text/csv", icon=":material/download:")
            with export_three:
                st.download_button(tr("JSON herunterladen"), json_bytes(listings), "jobsuche.json", "application/json", icon=":material/download:")
        with st.expander(tr("Rechercheprotokoll")):
            st.code("\n".join(st.session_state.get("logs", [])))

live_jobs = []
if aa_live_job() is not None or aa_has_saved_state():
    live_jobs.append(("Jobbörse", "arbeitsagentur"))
for source_id in GENERIC_LIVE_SOURCE_IDS:
    if generic_live_job(source_id) is not None or generic_has_saved_state(source_id):
        live_jobs.append((PROVIDER_BY_ID[source_id]["name"], source_id))
_archive_completed_live_jobs()

with live_tab:
    st.header(tr("Live-Extraktionen"), icon=":material/monitoring:")
    st.caption(tr("Extraktion starten, Fortschritt prüfen und Ergebnisse je Quelle ansehen."))
    live_sources = [source_id for source_id in selected if source_id in LIVE_EMAIL_SOURCE_IDS]
    if live_sources:
        with st.container(border=True):
            st.subheader(tr("E-Mail-Extraktion"), icon=":material/mail:")
            st.caption(tr("Jede Quelle läuft separat. Ein Captcha kann nur bei der Jobbörse erscheinen – nie bei Google Maps."))
            if len(live_sources) > 1:
                labels = ", ".join(PROVIDER_BY_ID[source_id]["name"] for source_id in live_sources)
                combined_clicked = st.button(
                    f"{tr('E-Mail-Extraktion für alle starten')} ({len(live_sources)})",
                    type="primary",
                    width="stretch",
                    icon=":material/play_arrow:",
                    key="all_live_start",
                    disabled=all(live_extraction_running(source_id) for source_id in live_sources),
                    help=f"{tr('Startet gleichzeitig:')} {labels}",
                )
                if combined_clicked:
                    errors = [
                        error for source_id in live_sources
                        if (error := live_extraction_error(source_id, settings_by_source[source_id]))
                    ]
                    if errors:
                        for error in errors:
                            st.error(tr(error))
                    else:
                        for source_id in live_sources:
                            if not live_extraction_running(source_id):
                                start_live_extraction(source_id, settings_by_source[source_id])
                        st.rerun()
            else:
                source_id = live_sources[0]
                provider = PROVIDER_BY_ID[source_id]
                st.markdown(f"**{provider['name']}**")
                if source_id == "arbeitsagentur":
                    st.caption(tr("E-Mail, Ansprechperson und Telefon · Captcha kann erforderlich sein"))
                else:
                    st.caption(tr("Alle E-Mails auf jeder Firmenwebsite · kein Captcha"))
                live_clicked = st.button(
                    f"{provider['name']} {tr('extrahieren')}",
                    type="primary",
                    width="stretch",
                    icon=":material/play_arrow:",
                    key=f"{source_id}_live_start",
                    disabled=live_extraction_running(source_id),
                )
                if live_clicked:
                    error = live_extraction_error(source_id, settings_by_source[source_id])
                    if error:
                        st.error(tr(error))
                    else:
                        start_live_extraction(source_id, settings_by_source[source_id])
                        st.rerun()
    elif not live_jobs:
        st.info(tr("Aktiviere eine unterstützte Quelle in der Seitenleiste, um die Live-Extraktion zu starten."))

    if live_jobs:
        live_source_tabs = st.tabs([name for name, _ in live_jobs])
        for tab, (name, source_id) in zip(live_source_tabs, live_jobs):
            with tab:
                if source_id == "arbeitsagentur":
                    render_aa_live()
                else:
                    render_generic_live(source_id, name)
    elif live_sources:
        st.info(tr("Noch keine Live-Extraktion. Starte sie mit der Schaltfläche oben."))

with crm_tab:
    render_crm()

with application_tab:
    render_applications()

with analytics_tab:
    render_analytics()

with archive_tab:
    render_history()

with gdpr_tab:
    render_gdpr()

# A captcha blocks the Arbeitsagentur worker.  Render it outside the tabs so
# it pops up even while the user is still on the search/settings view.
render_aa_captcha()
