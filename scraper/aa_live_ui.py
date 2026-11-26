# -*- coding: utf-8 -*-
"""
Panneau Streamlit pour l'extraction live Arbeitsagentur (log vert/rouge, captcha, stop,
téléchargement Excel). Utilisé par streamlit_app.py ET par agentur_app.py.

    start_job(params)        -> lance l'extraction en arrière-plan
    render_job()             -> affiche le panneau (à appeler à chaque exécution du script)
    render_captcha_dialog()  -> ouvre le captcha en modal, même depuis un autre onglet

Pause / Reprise : l'état est persistant sur disque (output/job_states/arbeitsagentur.json),
donc une reprise est possible même après fermeture du PC ou redémarrage de l'appli.
"""
import inspect
import os
import threading

import streamlit as st

from scraper.aa_live import ExtractionJob, esc
from scraper.job_state import load as load_state, clear as clear_state
from scraper.i18n import DEFAULT_LANGUAGE, LANGUAGE_KEY, translate

JOB_KEY = "aa_live_job"
STATE_KEY = "arbeitsagentur"

# `use_container_width` est déprécié dans les Streamlit récents : on s'adapte à la version.
FULL = ({"width": "stretch"} if "width" in inspect.signature(st.button).parameters
        else {"use_container_width": True})


def _t(text: str) -> str:
    return translate(text, str(st.session_state.get(LANGUAGE_KEY, DEFAULT_LANGUAGE)))


def get_job():
    return st.session_state.get(JOB_KEY)


def job_running():
    job = get_job()
    return job is not None and job.status == "running"


def job_paused():
    job = get_job()
    return job is not None and job.status == "paused"


def has_saved_state() -> bool:
    state = load_state(STATE_KEY)
    return bool(state and state.get("status") in ("paused", "pausing", "stopped", "running", "collecting", "extracting", "error"))


def clear_saved_state():
    clear_state(STATE_KEY)


def start_job(params, resume: bool = False):
    """Démarre une extraction (ignoré si une est déjà en cours)."""
    if job_running():
        return get_job()
    # The job restores its own durable queue.  Pre-loading only the finished
    # rows here used to make it re-run the source search from the beginning.
    job = ExtractionJob(params, resume=resume)
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
    ok = sum(1 for r in rows if r.get("email"))
    c1, c2, c3 = st.columns(3)
    c1.metric(_t("Verarbeitet"), f"{job.processed}/{job.total or '…'}")
    c2.metric(_t("Mit E-Mail"), ok)
    c3.metric(_t("Ohne E-Mail"), len(rows) - ok)
    if job.total:
        st.progress(min(job.processed / job.total, 1.0))
    if job.paused.is_set():
        if job.status == "paused":
            st.info(_t("Pause – état sauvegardé. Reprise possible à tout moment."))
        else:
            st.info(_t("Pause demandée – l'offre en cours est sauvegardée avant l'arrêt."))


def render_captcha_dialog():
    """Affiche le captcha en fenêtre modale, quel que soit l'onglet actif.

    Le worker est bloqué dans ``ask_captcha`` tant que l'utilisateur n'a pas
    répondu : sans cette fenêtre, la recherche semble figée.  Le modal est
    volontairement non-dismissible ; on en sort avec « Senden » ou « Abbrechen ».
    """
    job = get_job()
    if job is None or job.status != "running" or job.captcha_png is None:
        return

    @st.dialog(_t("Captcha bestätigen"), width="large", dismissible=False)
    def _dialog():
        st.markdown(
            _t("Die Bundesagentur für Arbeit schützt die Kontaktdaten mit einer Sicherheitsabfrage.")
        )
        st.caption(
            _t("Bitte die Zeichen aus dem Bild eingeben. Danach läuft die Extraktion automatisch weiter. Ist das Bild unlesbar, „Abbrechen“ drücken und die Firma erneut starten.")
        )
        try:
            st.image(job.captcha_png, width=520)
        except Exception:
            st.error(_t("Captcha-Bild konnte nicht angezeigt werden – bitte 'Abbrechen' drücken."))
        with st.form("aa_captcha_dialog_form", clear_on_submit=True):
            answer = st.text_input(
                _t("Dargestellte Zeichen"),
                placeholder=_t("Zeichen aus dem Bild"),
                label_visibility="collapsed",
            )
            c1, c2 = st.columns(2)
            send = c1.form_submit_button(_t("Senden"), type="primary", **FULL)
            cancel = c2.form_submit_button(_t("Abbrechen"), **FULL)
        if send:
            if not (answer or "").strip():
                st.warning(_t("Bitte die Zeichen aus dem Bild eingeben."))
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
        # Aucun job en mémoire : propose la reprise s'il y a un état sauvegardé.
        if has_saved_state():
            state = load_state(STATE_KEY)
            st.warning(
                f"{_t('Eine vorherige Jobbörse-Extraktion ist gespeichert.')} "
                f"({state.get('processed', 0)}/{state.get('total', '?')} {_t('Verarbeitet')})"
            )
            c1, c2, c3 = st.columns(3)
            if c1.button(_t("Reprise"), key="aa_resume_saved", type="primary", icon=":material/play_arrow:"):
                start_job(state.get("params", {}), resume=True)
                st.rerun()
            if c2.button(_t("Neue recherche"), key="aa_new_saved"):
                clear_saved_state()
                st.rerun()
            if c3.button(_t("Supprimer l'état"), key="aa_clear_saved"):
                clear_saved_state()
                st.rerun()
        return

    was_running = job.status == "running"
    was_paused = job.status in ("paused", "error")

    # Rafraîchissement automatique pendant l'extraction (sauf pendant la saisie du captcha).
    captcha_pending = job.captcha_png is not None

    @st.fragment(run_every=(1 if job.status == "running" and not captcha_pending else None),
                 key="aa_live_panel")
    def live():
        j = get_job()
        if j.status == "running":
            st.markdown(f"#### {_t('Daten werden extrahiert…')}")
            st.caption(f"{_t('Verbinde mit')} **Arbeitsagentur** …")
        elif j.status == "paused":
            st.status(f"Jobbörse : {_t('en pause')}", state="running", type="compact")
        elif j.status == "done":
            st.success(_t("Die Daten wurden erfolgreich extrahiert."))
        elif j.status == "stopped":
            st.warning(_t("Process stopped."))
        else:
            st.error(_t("Extraktion mit Fehler beendet."))
        _render_progress(j)
        _render_log(j)
        if j.status == "running" and j.captcha_png is not None:
            st.warning(
                f"🔐 {_t('Captcha wartet – bitte im Eingabefenster die Zeichen eingeben. Die Extraktion läuft nach dem Senden automatisch weiter.')}"
            )
            if not captcha_pending:
                st.rerun(scope="app")
        if j.status not in ("running", "paused", "error") and (was_running or was_paused):
            st.rerun(scope="app")

    live()
    job = get_job()

    if job.status == "running":
        c1, c2 = st.columns(2)
        if c1.button(_t("Pause"), type="secondary", key="aa_pause", icon=":material/pause:"):
            job.request_pause()
            st.rerun()
        if c2.button(_t("Stop"), type="primary", key="aa_stop", icon=":material/stop_circle:"):
            job.request_stop()
            st.rerun()
        return

    if job.status in ("paused", "error"):
        c1, c2, c3 = st.columns(3)
        if c1.button(_t("Reprise"), type="primary", key="aa_resume", icon=":material/play_arrow:"):
            job.paused.clear()
            job.status = "running"
            job.thread = threading.Thread(target=job._run, daemon=True)
            st.session_state[JOB_KEY] = job
            job.start()
            st.rerun()
        if c2.button(_t("Stopper et garder le progrès"), key="aa_stop_from_pause", icon=":material/stop_circle:"):
            job.request_stop()
            st.rerun()
        if c3.button(_t("Fermer le résultat"), key="aa_close_pause", icon=":material/close:"):
            clear_saved_state()
            if JOB_KEY in st.session_state:
                del st.session_state[JOB_KEY]
            st.rerun()
        return

    c1, c2 = st.columns(2)
    c1.download_button(
        f"⬇ {_t('Excel herunterladen')}", data=job.excel_bytes(),
        file_name=os.path.basename(job.autosave_path),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="aa_download", disabled=not job.rows, **FULL)
    if new_job_button and c2.button(_t("Ergebnis schließen"), key="aa_close", **FULL):
        clear_saved_state()
        del st.session_state[JOB_KEY]
        st.rerun()
    st.caption(f"{_t('Kopie gespeichert in:')} {os.path.abspath(job.autosave_path)}")
