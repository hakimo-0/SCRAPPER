# -*- coding: utf-8 -*-
"""
Panneau Streamlit pour l'extraction live Arbeitsagentur (log vert/rouge, captcha, stop,
téléchargement Excel). Utilisé par streamlit_app.py ET par agentur_app.py.

    start_job(params)        -> lance l'extraction en arrière-plan
    render_job()             -> affiche le panneau (à appeler à chaque exécution du script)
    render_captcha_dialog()  -> ouvre le captcha en modal, même depuis un autre onglet
"""
import inspect
import os

import streamlit as st

from scraper.aa_live import ExtractionJob, esc

JOB_KEY = "aa_live_job"

# `use_container_width` est déprécié dans les Streamlit récents : on s'adapte à la version.
FULL = ({"width": "stretch"} if "width" in inspect.signature(st.button).parameters
        else {"use_container_width": True})


def get_job():
    return st.session_state.get(JOB_KEY)


def job_running():
    job = get_job()
    return job is not None and job.status == "running"


def start_job(params):
    """Démarre une extraction (ignoré si une est déjà en cours)."""
    if job_running():
        return get_job()
    job = ExtractionJob(params)
    st.session_state[JOB_KEY] = job
    job.start()
    return job


def _render_log(job):
    icons = {"info": ("ℹ️", "#1e6fd9"), "ok": ("✔", "#16a34a"), "ko": ("✘", "#dc2626"),
             "warn": ("ℹ️", "#d97706"), "err": ("ℹ️", "#dc2626")}
    html = []
    for kind, text in job.snapshot_log():
        icon, color = icons.get(kind, icons["info"])
        weight = 600 if kind in ("ok", "ko", "err") else 500
        html.append(f"<div style='color:{color};font-weight:{weight};margin:3px 0'>"
                    f"{icon} {esc(text)}</div>")
    with st.container(height=380, border=True):
        st.markdown("".join(html) or "…", unsafe_allow_html=True)


def _render_progress(job):
    rows = list(job.rows)
    ok = sum(1 for r in rows if r["email"])
    c1, c2, c3 = st.columns(3)
    c1.metric("Verarbeitet", f"{job.processed}/{job.total or '…'}")
    c2.metric("Mit E-Mail", ok)
    c3.metric("Ohne E-Mail", len(rows) - ok)
    if job.total:
        st.progress(min(job.processed / job.total, 1.0))


def render_captcha_dialog():
    """Affiche le captcha en fenêtre modale, quel que soit l'onglet actif.

    Le worker est bloqué dans ``ask_captcha`` tant que l'utilisateur n'a pas
    répondu : sans cette fenêtre, la recherche semble figée.  Le modal est
    volontairement non-dismissible ; on en sort avec « Senden » ou « Abbrechen ».
    """
    job = get_job()
    if job is None or job.status != "running" or job.captcha_png is None:
        return

    @st.dialog("Captcha bestätigen", width="large", dismissible=False)
    def _dialog():
        st.markdown(
            "Die **Bundesagentur für Arbeit** schützt die Kontaktdaten mit einer "
            "Sicherheitsabfrage."
        )
        st.caption(
            "Bitte die Zeichen aus dem Bild eingeben. Danach läuft die Extraktion "
            "automatisch weiter. Ist das Bild unlesbar, „Abbrechen“ drücken und die "
            "Firma erneut starten."
        )
        try:
            st.image(job.captcha_png, width=520)
        except Exception:
            st.error("Captcha-Bild konnte nicht angezeigt werden – bitte 'Abbrechen' drücken.")
        with st.form("aa_captcha_dialog_form", clear_on_submit=True):
            answer = st.text_input(
                "Dargestellte Zeichen",
                placeholder="Zeichen aus dem Bild",
                label_visibility="collapsed",
            )
            c1, c2 = st.columns(2)
            send = c1.form_submit_button("Senden", type="primary", **FULL)
            cancel = c2.form_submit_button("Abbrechen", **FULL)
        if send:
            if not (answer or "").strip():
                st.warning("Bitte die Zeichen aus dem Bild eingeben.")
            else:
                job.answer_captcha(answer)
                st.rerun()
        if cancel:
            job.answer_captcha("")
            st.rerun()

    _dialog()


def render_job(new_job_button=True):
    """Affiche le panneau du job courant. Ne fait rien s'il n'y a pas de job."""
    job = get_job()
    if job is None:
        return
    # This snapshot controls whether the live fragment should auto-refresh.
    # It must be set before the decorator is evaluated.
    captcha_pending = job.captcha_png is not None
    was_running = job.status == "running"

    # Rafraîchissement automatique pendant l'extraction (sauf pendant la saisie du captcha).
    @st.fragment(run_every=(1 if was_running and not captcha_pending else None))
    def live():
        j = get_job()
        if j.status == "running":
            st.markdown("#### Daten werden extrahiert…")
            st.caption("Verbinde mit **Arbeitsagentur** …")
        elif j.status == "done":
            st.success("Die Daten wurden erfolgreich extrahiert.")
        elif j.status == "stopped":
            st.warning("Process stopped.")
        else:
            st.error("Extraktion mit Fehler beendet.")
        _render_progress(j)
        _render_log(j)
        if j.status == "running" and j.captcha_png is not None:
            st.warning(
                "🔐 Captcha wartet – bitte im Eingabefenster die Zeichen eingeben. "
                "Die Extraktion läuft nach dem Senden automatisch weiter."
            )
            # Only trigger a full-app rerun when the dialog is not open yet.
            # Without this guard every rerun would immediately rerun again
            # and the captcha form would never be displayed.
            if not captcha_pending:
                st.rerun(scope="app")
        if j.status != "running" and was_running:
            st.rerun(scope="app")      # passage en mode « terminé »

    live()

    if job.status == "running":
        if st.button("Stop", type="primary", key="aa_stop", **FULL):
            job.request_stop()
            st.rerun()
    else:
        c1, c2 = st.columns(2)
        c1.download_button(
            "⬇ Download (Excel)", data=job.excel_bytes(),
            file_name=os.path.basename(job.autosave_path),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="aa_download", disabled=not job.rows, **FULL)
        if new_job_button and c2.button("Ergebnis schließen", key="aa_close", **FULL):
            del st.session_state[JOB_KEY]
            st.rerun()
        st.caption(f"Kopie gespeichert in: {os.path.abspath(job.autosave_path)}")
