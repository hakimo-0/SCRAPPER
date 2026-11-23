# -*- coding: utf-8 -*-
"""
Arbeitsagentur – Extraktion (version autonome, même moteur que dans streamlit_app.py)

Lancer :  streamlit run agentur_app.py
Prérequis :  pip install -r requirements_agentur.txt && playwright install chromium
"""
import streamlit as st

from scraper.aa_live import ANGEBOTSART_MAP, PUBLISHED_OPTIONS, RADIUS_OPTIONS
from scraper.aa_live_ui import FULL, get_job, render_captcha_dialog, render_job, start_job

st.set_page_config(page_title="Arbeitsagentur Extraktion", page_icon="🔎", layout="centered")


def render_settings():
    st.markdown("## 🔎 Arbeitsagentur Einstellungen")
    with st.form("settings"):
        art = st.selectbox("Angebotsart", list(ANGEBOTSART_MAP), index=0)
        seit = st.selectbox("Veröffentlicht", list(PUBLISHED_OPTIONS), index=0)
        wo = st.text_input("Ort", placeholder="z. B. Berlin, Hamburg…")
        umkreis = st.selectbox("Umkreis", list(RADIUS_OPTIONS), index=0)
        was = st.text_input("Beruf", placeholder="z. B. Pflegefachmann…")
        c1, c2, c3 = st.columns(3)
        max_results = c1.number_input("Max. Angebote (0 = alle)", min_value=0, value=0, step=50)
        year = c2.selectbox("Ausbildungsjahr", ["Alle"] + list(range(2026, 2032)))
        headed = c3.checkbox("Browser anzeigen (Debug)", value=False)
        go = st.form_submit_button("Extraktion", type="primary", **FULL)
    if go:
        if not wo.strip() and not was.strip():
            st.warning("Bitte mindestens Ort oder Beruf angeben.")
            return
        start_job({
            "angebotsart": ANGEBOTSART_MAP[art],
            "locations": [x.strip() for x in wo.split(",") if x.strip()],
            "was": was.strip(), "umkreis": RADIUS_OPTIONS[umkreis], "seit": PUBLISHED_OPTIONS[seit],
            "max_results": int(max_results) or None, "headed": headed,
            "year": year,
        })
        st.rerun()


if get_job() is None:
    render_settings()
else:
    render_job()
    render_captcha_dialog()
