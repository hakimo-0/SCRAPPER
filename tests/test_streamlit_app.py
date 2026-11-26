import ast
from pathlib import Path

from streamlit.testing.v1 import AppTest

from scraper import google_workspace
from scraper.i18n import (
    LANGUAGE_KEY,
    LANGUAGES,
    _TRANSLATIONS,
    is_rtl,
    layout_css,
    translate,
    visual_css,
)

APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


def test_dashboard_exposes_search_controls_without_an_artificial_result_cap():
    source = APP_PATH.read_text(encoding="utf-8")

    assert "Ausgewählte Quellen suchen" in source
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
    assert "source_google_maps" in [toggle.key for toggle in app.toggle]
    assert "Ausgewählte Quellen suchen" in [button.label for button in app.button]


def test_sidebar_navigation_keeps_google_maps_batch_inputs_available():
    app = AppTest.from_file(str(APP_PATH), default_timeout=60).run()
    app.toggle(key="source_google_maps").set_value(True).run()

    assert not app.exception
    assert app.session_state["source_google_maps"] is True
    assert "maps_term_draft" in [widget.key for widget in app.text_input]
    assert "maps_city_draft" in [widget.key for widget in app.text_input]

    app.button(key="navigate_live").click().run()
    assert not app.exception
    assert app.session_state["dashboard_tabs"].endswith("Live-Extraktionen")


def test_google_maps_has_a_clear_start_action_in_search_settings():
    app = AppTest.from_file(str(APP_PATH), default_timeout=60).run()
    app.toggle(key="source_google_maps").set_value(True).run()

    assert not app.exception
    button = next(
        button for button in app.button
        if button.key == "google_maps_settings_live_start"
    )
    assert button.label == "Google Maps E-Mail-Extraktion starten"


def test_applications_tab_renders_user_controlled_script_download():
    app = AppTest.from_file(str(APP_PATH), default_timeout=60)
    app.session_state["application_script_config"] = {
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/sheet_id/edit",
        "sheet_name": "BEWERBUNG",
        "template_doc_id": "doc_template_123",
        "static_pdf_id": "drive_pdf_456",
        "sender_name": "Example Sender",
        "timezone": "Africa/Casablanca",
        "max_per_day": 10,
        "min_delay_minutes": 1,
        "max_delay_minutes": 3,
    }
    app.run()

    assert not app.exception
    assert "Google Apps Script herunterladen" in [
        button.label for button in app.get("download_button")
    ]
    assert any("MailApp.sendEmail" in element.value for element in app.code)


def test_apps_script_form_shows_download_and_copyable_code_after_submit():
    sheet_id = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"
    doc_id = "1FAIpQLSc7g2exampleDocTemplate_ABC-123"
    pdf_id = "1xYz_Example-PDF-FileId1234567890"
    app = AppTest.from_file(str(APP_PATH), default_timeout=60)
    app.session_state["application_spreadsheet_url"] = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    )
    app.session_state["application_template_doc_id"] = (
        f"https://docs.google.com/document/d/{doc_id}/edit"
    )
    app.session_state["application_static_pdf_id"] = (
        f"https://drive.google.com/file/d/{pdf_id}/view"
    )
    app.session_state["application_sender_name"] = "Example Sender"

    app.run()
    assert not app.exception
    app.button(key="application_script_generate").click().run()

    assert not app.exception
    assert "Google Apps Script herunterladen" in [
        button.label for button in app.get("download_button")
    ]
    assert any("Konfiguration bereit" in element.value for element in app.success)
    generated_script = next(
        element.value for element in app.code if "MailApp.sendEmail" in element.value
    )
    assert f'TEMPLATE_DOC_ID: "{doc_id}"' in generated_script
    assert f'STATIC_PDF_ID: "{pdf_id}"' in generated_script


def test_applications_ui_exposes_google_sign_in_without_leaking_client_secret(monkeypatch):
    client_secret = "never-render-this-client-secret"
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", client_secret)
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8501/")
    monkeypatch.setattr(google_workspace, "restore_persisted_session", lambda *_args, **_kwargs: None)

    app = AppTest.from_file(str(APP_PATH), default_timeout=60).run()

    assert not app.exception
    sign_in = [
        element for element in app.get("link_button")
        if element.label == "Mit Google anmelden"
    ]
    assert len(sign_in) == 1
    assert "accounts.google.com" in sign_in[0].url
    assert client_secret not in sign_in[0].url


def test_applications_ui_shows_connected_account_and_logout(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8501/")
    monkeypatch.setattr(google_workspace, "clear_persisted_session", lambda: None)
    app = AppTest.from_file(str(APP_PATH), default_timeout=60)
    app.session_state[google_workspace.TOKENS_KEY] = {"access_token": "test-token"}
    app.session_state[google_workspace.ACCOUNT_KEY] = "connected@example.de"
    app.session_state["google_workspace_resources"] = {
        "spreadsheet_id": "sheet_123",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/sheet_123/edit",
        "sheet_name": "BEWERBUNG",
        "template_doc_id": "template_123",
        "pdfs": [],
        "static_pdf_id": "",
    }

    app.run()
    assert not app.exception
    assert any("Verbunden als connected@example.de" in element.value for element in app.success)
    assert "google_workspace_disconnect" in [button.key for button in app.button]

    app.button(key="google_workspace_disconnect").click().run()
    assert not app.exception
    assert google_workspace.TOKENS_KEY not in app.session_state
    assert any(button.label == "Mit Google anmelden" for button in app.get("link_button"))


def test_language_selector_switches_dashboard_translations():
    app = AppTest.from_file(str(APP_PATH), default_timeout=60).run()
    assert app.session_state[LANGUAGE_KEY] == LANGUAGES[0]

    app.selectbox(key=LANGUAGE_KEY).select("English").run()
    assert not app.exception
    assert app.session_state[LANGUAGE_KEY] == "English"
    assert "Search selected sources" in [button.label for button in app.button]
    assert app.title[0].value == "Apprenticeship Lead Finder"

    arabic_app = AppTest.from_file(str(APP_PATH), default_timeout=60)
    arabic_app.session_state[LANGUAGE_KEY] = LANGUAGES[1]
    arabic_app.run()
    assert not arabic_app.exception
    assert arabic_app.session_state[LANGUAGE_KEY] == LANGUAGES[1]
    assert "البحث في المصادر المحددة" in [button.label for button in arabic_app.button]


def test_arabic_layout_is_right_to_left_and_key_labels_are_translated():
    arabic = LANGUAGES[1]
    assert is_rtl(arabic)
    assert "direction: rtl" in layout_css(arabic)
    assert not is_rtl("English")
    assert layout_css("English") == ""
    assert translate("Ausgewählte Quellen suchen", "English") == "Search selected sources"
    assert translate("Ausgewählte Quellen suchen", arabic) != "Ausgewählte Quellen suchen"


def test_visual_polish_respects_rtl_and_reduced_motion():
    arabic_css = visual_css(LANGUAGES[1])
    english_css = visual_css("English")

    assert "left: 18px" in arabic_css
    assert "right: 18px" in english_css
    assert "prefers-reduced-motion: reduce" in arabic_css
    assert "by Hakim Argui" in english_css


def test_wrapped_dashboard_copy_has_entries_in_both_translation_catalogues():
    files = (
        APP_PATH,
        APP_PATH.parent / "scraper" / "aa_live_ui.py",
        APP_PATH.parent / "scraper" / "generic_live_ui.py",
    )
    wrapped = set()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"tr", "_t"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                wrapped.add(node.args[0].value)

    for language in LANGUAGES[1:]:
        assert wrapped <= _TRANSLATIONS[language].keys()
