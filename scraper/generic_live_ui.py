"""Source-specific Streamlit panel for live extractors without a captcha.

Google Maps, Azubiyo, Ausbildung.de and Stepstone do not share the
Arbeitsagentur captcha state.  Keeping one job per source prevents a captcha
or progress message from one source being rendered in another source's panel.
"""
from __future__ import annotations

import os

import streamlit as st

from scraper.generic_job import GenericExtractionJob


def _job_key(source_id: str) -> str:
    return f"generic_live_job_{source_id}"


def get_job(source_id: str):
    return st.session_state.get(_job_key(source_id))


def job_running(source_id: str) -> bool:
    job = get_job(source_id)
    return job is not None and job.status == "running"


def start_job(source_id: str, params: dict) -> GenericExtractionJob:
    """Start one independent job for a non-Jobbörse source."""
    existing = get_job(source_id)
    if existing is not None and existing.status == "running":
        return existing
    job = GenericExtractionJob(params)
    st.session_state[_job_key(source_id)] = job
    job.start()
    return job


def _render_log(job) -> None:
    lines = []
    for kind, text in job.snapshot_log()[-18:]:
        prefix = {"ok": "✓", "ko": "×", "err": "!", "warn": "!"}.get(kind, "·")
        lines.append(f"{prefix} {text}")
    with st.expander("Aktivität", expanded=job.status == "running"):
        st.code("\n".join(lines) or "Noch keine Aktivität.")


def _render_progress(job) -> None:
    progress = job.snapshot_progress()
    rows = job.snapshot_rows()
    with_email = sum(1 for row in rows if row.get("email"))
    first, second, third = st.columns(3)
    first.metric("Unternehmen", f"{progress['processed']}/{progress['total'] or '…'}")
    second.metric("Mit E-Mail", with_email)
    third.metric(
        "Städte",
        f"{progress['locations_completed']}/{progress['locations_total'] or '…'}",
    )
    if progress["total"]:
        st.progress(min(progress["processed"] / progress["total"], 1.0))
    if job.status == "running" and progress["current_location"]:
        st.caption(f"Aktuell: **{progress['current_location']}**")


def render_job(source_id: str, source_label: str) -> None:
    """Render a dedicated panel for exactly one non-captcha source."""
    job = get_job(source_id)
    if job is None:
        return
    was_running = job.status == "running"

    @st.fragment(run_every=1 if was_running else None, key=f"live_panel_{source_id}")
    def live_panel():
        current = get_job(source_id)
        if current.status == "running":
            st.status(f"{source_label} wird extrahiert", state="running", type="compact")
        elif current.status == "done":
            st.status(f"{source_label}: Extraktion abgeschlossen", state="complete", type="compact")
        elif current.status == "stopped":
            st.warning(f"{source_label}: Extraktion gestoppt.")
        else:
            st.error(f"{source_label}: Extraktion mit Fehler beendet.")
        _render_progress(current)
        _render_log(current)
        if current.status != "running" and was_running:
            st.rerun()

    live_panel()
    job = get_job(source_id)
    if job.status == "running":
        if st.button("Extraktion stoppen", key=f"{source_id}_stop", icon=":material/stop_circle:"):
            job.request_stop()
            st.rerun()
        return

    first, second = st.columns(2)
    first.download_button(
        "Excel herunterladen",
        data=job.excel_bytes(),
        file_name=os.path.basename(job.autosave_path),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{source_id}_download",
        disabled=not job.snapshot_rows(),
        icon=":material/download:",
    )
    if second.button("Ergebnis schließen", key=f"{source_id}_close", icon=":material/close:"):
        del st.session_state[_job_key(source_id)]
        st.rerun()
    st.caption(f"Kopie gespeichert in: {os.path.abspath(job.autosave_path)}")
