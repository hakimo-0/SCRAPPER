from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


def test_dashboard_omits_the_plain_search_controls():
    source = APP_PATH.read_text(encoding="utf-8")

    assert "Recherche ohne E-Mail starten" not in source
    assert "Keine künstliche Ergebnisgrenze" not in source


def test_source_cards_omit_provider_subtitles():
    source = APP_PATH.read_text(encoding="utf-8")

    assert '"subtitle"' not in source
    assert "Bundesagentur für Arbeit" not in source
    assert "SerpAPI (google_maps)" not in source


def test_dashboard_exposes_stepstone_and_renders_without_error():
    app = AppTest.from_file(str(APP_PATH), default_timeout=60).run()

    assert not app.exception
    assert "source_stepstone" in [toggle.key for toggle in app.toggle]
