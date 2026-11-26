"""Source-specific Streamlit panel for live extractors without a captcha.

Google Maps, Azubiyo, Ausbildung.de and Stepstone do not share the
Arbeitsagentur captcha state.  Keeping one job per source prevents a captcha
or progress message from one source being rendered in another source's panel.

Pause / Reprise : l'état est persistant sur disque, donc une reprise est
possible même après fermeture du PC ou redémarrage de l'application.
"""
from __future__ import annotations

import os
import threading

import streamlit as st

from scraper.generic_job import GenericExtractionJob
from scraper.job_state import load as load_state, clear as clear_state
from scraper.i18n import DEFAULT_LANGUAGE, LANGUAGE_KEY, translate


def _t(text: str) -> str:
    return translate(text, str(st.session_state.get(LANGUAGE_KEY, DEFAULT_LANGUAGE)))


def _job_key(source_id: str) -> str:
    return f"generic_live_job_{source_id}"


def get_job(source_id: str):
    return st.session_state.get(_job_key(source_id))


def job_running(source_id: str) -> bool:
    job = get_job(source_id)
    return job is not None and job.status == "running"


def job_paused(source_id: str) -> bool:
    job = get_job(source_id)
    return job is not None and job.status == "paused"


def start_job(source_id: str, params: dict, resume: bool = False) -> GenericExtractionJob:
    """Start one independent job for a non-Jobbörse source."""
    existing = get_job(source_id)
    if existing is not None and existing.status == "running":
        return existing
    job = GenericExtractionJob(params, resume=resume)
    st.session_state[_job_key(source_id)] = job
    job.start()
    return job


def has_saved_state(source_id: str) -> bool:
    state = load_state(source_id)
    return bool(state and state.get("status") in ("paused", "pausing", "stopped", "running", "collecting", "extracting", "error"))


def clear_saved_state(source_id: str) -> None:
    clear_state(source_id)
    if _job_key(source_id) in st.session_state:
        del st.session_state[_job_key(source_id)]


def _render_log(job) -> None:
    lines = []
    for kind, text in job.snapshot_log()[-18:]:
        prefix = {"ok": "✓", "ko": "×", "err": "!", "warn": "!"}.get(kind, "·")
        lines.append(f"{prefix} {text}")
    with st.expander(_t("Aktivität"), expanded=job.status in ("running", "paused")):
        st.code("\n".join(lines) or _t("Noch keine Aktivität."))


def _render_progress(job) -> None:
    progress = job.snapshot_progress()
    rows = job.snapshot_rows()
    with_email = sum(1 for row in rows if row.get("email"))
    first, second, third = st.columns(3)
    first.metric(_t("Unternehmen"), f"{progress['processed']}/{progress['total'] or '…'}")
    second.metric(_t("Mit E-Mail"), with_email)
    third.metric(
        _t("Suchläufe") if job.source_label == "google_maps" and len(job.params.get("keywords") or ()) > 1 else _t("Städte"),
        f"{progress['locations_completed']}/{progress['locations_total'] or '…'}",
    )
    if job.source_label == "google_maps" and progress["search_completeness"] != "pending":
        counts = progress["search_status_counts"]
        st.caption(
            f"{_t('Suchvollständigkeit')}: **{_t(progress['search_completeness'])}** "
            f"({_t('vollständig')} {counts['complete']}, {_t('unvollständig')} {counts['incomplete']}, "
            f"{_t('fehlgeschlagen')} {counts['failed']})."
        )
    if progress["total"]:
        st.progress(min(progress["processed"] / progress["total"], 1.0))
    if progress["current_location"]:
        st.caption(f"{_t('Aktuell:')} **{progress['current_location']}**")
    if progress["paused"]:
        if progress["status"] == "paused":
            st.info(_t("Pause – état sauvegardé. Reprise possible à tout moment."))
        else:
            st.info(_t("Pause demandée – la société en cours est sauvegardée avant l'arrêt."))


def render_job(source_id: str, source_label: str) -> None:
    """Render a dedicated panel for exactly one non-captcha source."""
    job = get_job(source_id)
    if job is None:
        # Aucun job en mémoire : affiche une option de reprise s'il y a un état sauvegardé.
        if has_saved_state(source_id):
            state = load_state(source_id)
            st.warning(
                f"{_t('Eine vorherige Jobbörse-Extraktion ist gespeichert.')} **{source_label}** "
                f"({state.get('processed', 0)}/{state.get('total', '?')} {_t('Unternehmen')})."
            )
            c1, c2, c3 = st.columns(3)
            if c1.button(_t("Reprise"), key=f"{source_id}_resume_saved", type="primary", icon=":material/play_arrow:"):
                start_job(source_id, dict(state.get("params", {})), resume=True)
                st.rerun()
            if c2.button(_t("Neue recherche"), key=f"{source_id}_new_saved"):
                clear_saved_state(source_id)
                st.rerun()
            if c3.button(_t("Supprimer l'état"), key=f"{source_id}_clear_saved"):
                clear_saved_state(source_id)
                st.rerun()
        return

    was_running = job.status == "running"
    was_paused = job.status in ("paused", "error")

    @st.fragment(run_every=1 if job.status == "running" else None, key=f"live_panel_{source_id}")
    def live_panel():
        current = get_job(source_id)
        if current.status == "running":
            st.status(f"{source_label} {_t('wird extrahiert')}", state="running", type="compact")
        elif current.status == "paused":
            st.status(f"{source_label} : {_t('en pause')}", state="running", type="compact")
        elif current.status == "done":
            st.status(f"{source_label}: {_t('Extraktion terminée')}", state="complete", type="compact")
        elif current.status == "stopped":
            st.warning(f"{source_label}: {_t('Extraktion arrêtée.')}")
        else:
            st.error(f"{source_label}: {_t('Extraktion avec erreur.')}")
        _render_progress(current)
        _render_log(current)
        if current.status not in ("running", "paused", "error") and (was_running or was_paused):
            st.rerun()

    live_panel()
    job = get_job(source_id)

    if job.status == "running":
        c1, c2 = st.columns(2)
        if c1.button(_t("Pause"), type="secondary", key=f"{source_id}_pause", icon=":material/pause:"):
            job.request_pause()
            st.rerun()
        if c2.button(_t("Stop"), type="primary", key=f"{source_id}_stop", icon=":material/stop_circle:"):
            job.request_stop()
            st.rerun()
        return

    if job.status in ("paused", "error"):
        c1, c2, c3 = st.columns(3)
        if c1.button(_t("Reprise"), type="primary", key=f"{source_id}_resume", icon=":material/play_arrow:"):
            job.paused.clear()
            job.status = "running"
            job.thread = threading.Thread(target=job._run, daemon=True)
            st.session_state[_job_key(source_id)] = job
            job.start()
            st.rerun()
        if c2.button(_t("Stopper et garder le progrès"), key=f"{source_id}_stop_from_pause", icon=":material/stop_circle:"):
            job.request_stop()
            st.rerun()
        if c3.button(_t("Fermer le résultat"), key=f"{source_id}_close_pause", icon=":material/close:"):
            clear_saved_state(source_id)
            if _job_key(source_id) in st.session_state:
                del st.session_state[_job_key(source_id)]
            st.rerun()
        return

    first, second = st.columns(2)
    first.download_button(
        _t("Excel herunterladen"),
        data=job.excel_bytes(),
        file_name=os.path.basename(job.autosave_path),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{source_id}_download",
        disabled=not job.snapshot_rows(),
        icon=":material/download:",
    )
    if second.button(_t("Ergebnis schließen"), key=f"{source_id}_close", icon=":material/close:"):
        clear_saved_state(source_id)
        del st.session_state[_job_key(source_id)]
        st.rerun()
    st.caption(f"{_t('Kopie gespeichert in:')} {os.path.abspath(job.autosave_path)}")
