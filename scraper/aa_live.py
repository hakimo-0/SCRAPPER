# -*- coding: utf-8 -*-
"""
Arbeitsagentur-Extraktion (Jobsuche) : recherche d'offres + lecture des contacts.

- Recherche   : API publique de la Jobbörse (rapide, pas de captcha).
- Contacts    : page de l'offre dans un vrai navigateur (Playwright). Le site demande
                un captcha pour afficher l'e-mail : on le relaie à l'utilisateur.

Rien ici ne dépend de Streamlit : le module peut être testé seul.
"""
import base64
import html as html_lib
import io
import os
import re
import sys
import threading
import time
from datetime import datetime

import requests

try:
    from .leads import WebsiteEmailFinder, normalise_firma, valid_emails
    from .utils import SafeSession, is_blocked_url
    from .arbeitsagentur import search_all as verified_search_all
    from .job_state import save as save_state, load as load_state
except Exception:
    from leads import WebsiteEmailFinder, normalise_firma, valid_emails
    from utils import SafeSession, is_blocked_url
    from arbeitsagentur import search_all as verified_search_all
    from job_state import save as save_state, load as load_state

API_KEY = "jobboerse-jobsuche"  # clé publique utilisée par le site lui-même
JOB_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"

# --- Valeurs des listes déroulantes (mêmes libellés que le site) -----------------
ANGEBOTSART_MAP = {
    "Ausbildung / Duales Studium": 4,
    "Arbeit": 1,
    "Selbstständigkeit": 2,
    "Praktikum / Trainee": 34,
}
RADIUS_OPTIONS = {
    "Ganzer Ort": 0, "5 km": 5, "10 km": 10, "15 km": 15, "20 km": 20,
    "25 km": 25, "50 km": 50, "100 km": 100, "200 km": 200,
}
PUBLISHED_OPTIONS = {          # veröffentlichtseit = nombre de jours
    "Heute": 0, "Seit gestern": 1, "Letzte 7 Tage": 7, "Letzte 14 Tage": 14,
    "Letzte 30 Tage": 30, "Letzte 60 Tage": 60, "Alle": None,
}

# Keep the Jobbörse workbook compatible with the established application
# template.  This is deliberately separate from the Google Maps export.
EXCEL_COLUMNS = ["firma", "person", "adresse", "beginn", "email", "telefonnummer", "link"]
EXCEL_WIDTHS = {"A": 55, "B": 30, "C": 55, "D": 18, "E": 55, "F": 30, "G": 55}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+49|0049|0)[\s ]?\(?\d{1,5}\)?[\d\s /\-]{4,}\d")
PERSON_RE = re.compile(r"\b(Herr|Frau)[  ]+((?:Dr\.|Prof\.)?[ ]?[A-ZÄÖÜ][\w\-äöüß]+(?:[ ][A-ZÄÖÜ][\w\-äöüß]+){0,2})")
BAD_EMAIL_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
BAD_EMAIL_DOMAINS = ("arbeitsagentur.de", "sentry.io", "example.com", "w3.org")


class Stopped(Exception):
    """Levée quand l'utilisateur appuie sur Stop."""


# =============================================================================
# 1) Recherche des offres
# =============================================================================
def _session():
    s = requests.Session()
    s.headers.update({
        "X-API-Key": API_KEY,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json",
    })
    return s


def _get_json(session, url, params, retries=4, stop=None):
    last = None
    for attempt in range(retries):
        if stop is not None and stop.is_set():
            raise Stopped()
        try:
            r = session.get(url, params=params, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                last = RuntimeError(f"HTTP {r.status_code}")
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 404:
                return {}
            raise RuntimeError(f"HTTP {r.status_code} : {r.text[:200]}")
        except requests.RequestException as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"API injoignable : {last}")


def format_date_de(iso):
    """'2026-09-01' -> '01.09.2026' (format du modèle Excel)."""
    if not iso:
        return ""
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return iso


def parse_offer(item):
    """Transforme un élément 'stellenangebote' de l'API en dict propre."""
    ort = item.get("arbeitsort") or {}
    street = (ort.get("strasse") or "").strip()
    town = " ".join(x for x in [(ort.get("plz") or "").strip(), (ort.get("ort") or "").strip()] if x)
    region = (ort.get("region") or "").strip()
    if region and region.lower() not in town.lower():
        town = f"{town}, {region}" if town else region
    adresse = f"{street} | {town}" if street and town else (town or street)
    refnr = item.get("refnr", "")
    return {
        "refnr": refnr,
        "firma": (item.get("arbeitgeber") or "").strip(),
        "titel": (item.get("titel") or item.get("beruf") or "").strip(),
        "adresse": adresse,
        "beginn": format_date_de(item.get("eintrittsdatum")),
        "link": JOB_URL.format(refnr=refnr) if refnr else "",
        "person": "", "email": "", "telefonnummer": "",
    }


def _live_offer_from_listing(listing):
    """Adapt the verified v6 connector's listing to the live extractor format."""
    street = (listing.get("street") or "").strip()
    place = " ".join(part for part in (listing.get("zip"), listing.get("city")) if part).strip()
    return {
        "refnr": (listing.get("reference_number") or "").strip(),
        "firma": (listing.get("name") or "").strip(),
        "titel": (listing.get("title") or "").strip(),
        "adresse": f"{street} | {place}" if street and place else (street or place),
        "beginn": format_date_de(listing.get("start_date") or ""),
        "link": listing.get("url") or "",
        "person": "", "email": "", "telefonnummer": "",
        # Used by the website fallback when the offer has no contact data.
        "city": (listing.get("city") or "").strip(),
    }


def search_offers(angebotsart, wo="", was="", umkreis=0, veroeffentlicht_seit=None,
                  max_results=None, stop=None, on_total=None, page_size=50):
    """Yield offers via the maintained Jobsuche v6 connector.

    The former live path queried the discontinued v4 endpoint, which now
    returns HTTP 403.  Contacts are still read later in Chromium (where a
    captcha may appear); only search results come from the verified v6 API.
    """
    if stop is not None and stop.is_set():
        raise Stopped()

    result = verified_search_all(
        was, wo, angebotsart=angebotsart, radius=umkreis or None,
        veroeffentlichtseit=veroeffentlicht_seit, page_size=page_size,
        log=lambda _message: None,
    )
    if result.status == "failed":
        raise RuntimeError(result.message or "Jobsuche search failed.")

    total = result.reported_total if result.reported_total is not None else result.unique_count
    if max_results:
        total = min(total, int(max_results))
    if on_total:
        on_total(total)

    yielded = 0
    for listing in result.listings:
        if stop is not None and stop.is_set():
            raise Stopped()
        offer = _live_offer_from_listing(listing)
        if not offer["refnr"]:
            continue
        yield offer
        yielded += 1
        if max_results and yielded >= int(max_results):
            return


# =============================================================================
# 2) Contacts via le navigateur
# =============================================================================
def clean_emails(text):
    out = []
    for m in EMAIL_RE.findall(text or ""):
        m = m.strip(".,;:()<>[]\"'")
        low = m.lower()
        local = low.split("@", 1)[0] if "@" in low else low
        if local.endswith(BAD_EMAIL_SUFFIX) or low.endswith(BAD_EMAIL_SUFFIX):
            continue
        if any(low.endswith("@" + d) or low.endswith("." + d) for d in BAD_EMAIL_DOMAINS):
            continue
        if m not in out:
            out.append(m)
    return out


def parse_contact_text(text):
    """Extrait (person, email, telefon) d'un texte de page. Tolérant, jamais d'exception."""
    emails = clean_emails(text)
    person = ""
    # on cherche près du mot-clé 'Ansprechperson / Ansprechpartner / Kontakt'
    ctx = text or ""
    k = re.search(r"Ansprech\w*|Kontaktperson", ctx, re.I)
    if k:
        ctx = ctx[k.start(): k.start() + 400]
    pm = PERSON_RE.search(ctx)
    if pm:
        person = f"{pm.group(1)} {pm.group(2).strip()}"
    phone = ""
    tk = re.search(r"Telefon|Tel\.", text or "", re.I)
    if tk:
        pm2 = PHONE_RE.search((text or "")[tk.start(): tk.start() + 120])
        if pm2:
            phone = pm2.group(0).strip()
    return person, (emails[0] if emails else ""), phone


class BrowserContacts:
    """
    Ouvre chaque offre dans Chromium, déclenche l'affichage des contacts et relaie le
    captcha via `ask_captcha(png_bytes) -> str | None`.
    Playwright est importé ici pour que le reste de l'app marche même sans lui.
    """
    # "anzeigen" alone was too broad: it also matched "Alle Stellen anzeigen"
    # and could navigate away from the offer before the contact panel loaded.
    REVEAL_RE = re.compile(r"kontakt|e-?mail|bewerbung", re.I)
    SUBMIT_RE = re.compile(r"senden|absenden|bestätigen|weiter|prüfen|ok", re.I)
    COOKIE_RE = re.compile(r"alle.*(zulassen|akzeptieren)|akzeptieren|zustimmen", re.I)
    # The Arbeitsagentur cookie banner is a web component with an open shadow
    # root.  get_by_role() does not find its buttons, and the modal then
    # covers the captcha image and intercepts clicks on "Absenden".
    COOKIE_REJECT_SELECTOR = '[data-testid="bahf-cookie-disclaimer-btn-ablehnen"]'
    COOKIE_ACCEPT_SELECTOR = '[data-testid="bahf-cookie-disclaimer-btn-alle"]'
    CONTACT_READY_SELECTOR = (
        "#jobdetails-kontaktdaten-container, "
        "#kontaktdaten-captcha-image, "
        "jb-job-detail-kontaktdaten"
    )
    CONTACT_PANEL_SELECTOR = "#jobdetails-kontaktdaten-container, jb-job-detail-kontaktdaten"
    CAPTCHA_IMAGE_SELECTOR = "#kontaktdaten-captcha-image"
    CAPTCHA_INPUT_SELECTOR = "#kontaktdaten-captcha-input"
    CAPTCHA_SUBMIT_SELECTOR = "#kontaktdaten-captcha-absenden-button"
    CAPTCHA_ERROR_SELECTOR = "#kontaktdaten-captcha-input-fehler-container"
    CAPTCHA_SELECTORS = (
        '#kontaktdaten-captcha-image',
        'img[src*="captcha" i]',
        '[class*="captcha" i] img',
        '[id*="captcha" i] img',
        'img[alt*="sicherheitsabfrage" i]',
        'img[alt*="captcha" i]',
        '[role="dialog"] img',
        'mat-dialog-container img',
        'dialog img',
        '.modal img',
        'img[src*="challenge" i]',
    )
    CAPTCHA_INPUT_SELECTORS = (
        CAPTCHA_INPUT_SELECTOR,
        'input[formcontrolname*="captcha" i]',
        'input[name*="captcha" i]',
        'input[id*="captcha" i]',
        'input[type="text"]',
        'input:not([type])',
    )
    CAPTCHA_SUBMIT_SELECTORS = (
        CAPTCHA_SUBMIT_SELECTOR,
        'button:has-text("Absenden")',
        'button:has-text("Senden")',
        'button:has-text("Bestätigen")',
    )

    def __init__(self, ask_captcha, log=lambda kind, msg: None, headed=False,
                 debug_dir="debug", stop=None):
        self.ask_captcha, self.log, self.headed = ask_captcha, log, headed
        self.debug_dir, self.stop = debug_dir, stop
        self._pw = self._browser = self._ctx = self.page = None
        self._debug_count = 0
        self._captcha_cancelled = False
        self._cookies_done = False

    # -- cycle de vie ---------------------------------------------------------
    def start(self):
        if sys.platform == "win32":  # Streamlit force un loop incompatible avec Playwright
            import asyncio
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=not self.headed)
        self._ctx = self._browser.new_context(locale="de-DE", viewport={"width": 1280, "height": 900})
        self.page = self._ctx.new_page()
        self.page.set_default_timeout(15000)

    def close(self):
        for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:
                pass

    # -- aides ----------------------------------------------------------------
    def _check_stop(self):
        if self.stop is not None and self.stop.is_set():
            raise Stopped()

    def _dump(self, refnr, tag):
        """Sauve page + capture pour que le sélecteur puisse être ajusté (3 max)."""
        if self._debug_count >= 3:
            return
        self._debug_count += 1
        try:
            os.makedirs(self.debug_dir, exist_ok=True)
            base = os.path.join(self.debug_dir, f"{tag}_{refnr}")
            self.page.screenshot(path=base + ".png", full_page=True)
            with open(base + ".html", "w", encoding="utf-8") as f:
                f.write(self.page.content())
        except Exception:
            pass

    def _click_first(self, pattern, roles=("button", "link")):
        for role in roles:
            try:
                loc = self.page.get_by_role(role, name=pattern)
                if loc.count():
                    loc.first.click(timeout=3000)
                    return True
            except Exception:
                continue
        return False

    def _dismiss_cookies(self):
        """Close the BA cookie modal, which lives in an open shadow root.

        Rejecting the optional cookies is enough for scraping and avoids
        leaving a tracking consent behind.  The generic role-based fallback
        keeps this working if the portal changes its banner again.
        """
        if self._cookies_done:
            return True
        try:
            self.page.wait_for_selector(
                self.COOKIE_REJECT_SELECTOR, state="visible", timeout=2500
            )
        except Exception:
            # No banner at all (consent already stored) or it is still
            # hydrating.  The next page will retry.
            return False
        for sel in (self.COOKIE_REJECT_SELECTOR, self.COOKIE_ACCEPT_SELECTOR):
            try:
                loc = self.page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=4000)
                    self.page.wait_for_timeout(250)
                    self._cookies_done = True
                    return True
            except Exception:
                continue
        clicked = self._click_first(self.COOKIE_RE, roles=("button",))
        self._cookies_done = clicked
        return clicked

    def _wait_for_contact_section(self, timeout_ms=4500):
        """Wait for the client-rendered application/contact panel to appear."""
        try:
            self.page.locator(self.CONTACT_READY_SELECTOR).first.wait_for(
                state="visible", timeout=timeout_ms
            )
        except Exception:
            # Some offers have no contact panel. The normal extraction path
            # below still checks the rendered page text and exits safely.
            pass

    def _captcha_image(self, wait_ms=0):
        """Return a visible captcha image, waiting briefly for Angular to render it."""
        deadline = time.monotonic() + max(0, wait_ms) / 1000
        while True:
            for sel in self.CAPTCHA_SELECTORS:
                try:
                    loc = self.page.locator(sel)
                    if loc.count() and loc.first.is_visible():
                        return loc.first
                except Exception:
                    continue
            if time.monotonic() >= deadline:
                break
            self.page.wait_for_timeout(200)
        # fallback: largest visible image inside a dialog/modal
        try:
            imgs = self.page.locator('dialog img, [role="dialog"] img, .modal img')
            best = None
            best_area = 0
            for i in range(imgs.count()):
                img = imgs.nth(i)
                if not img.is_visible():
                    continue
                box = img.bounding_box()
                if box and box["width"] * box["height"] > best_area:
                    best_area = box["width"] * box["height"]
                    best = img
            if best is not None:
                return best
        except Exception:
            pass
        return None

    def _captcha_visible(self) -> bool:
        try:
            loc = self.page.locator(self.CAPTCHA_IMAGE_SELECTOR)
            return bool(loc.count()) and loc.first.is_visible()
        except Exception:
            return False

    def _captcha_error(self) -> str:
        """Return the site's error text after a wrong captcha, if any."""
        try:
            loc = self.page.locator(self.CAPTCHA_ERROR_SELECTOR)
            if loc.count():
                text = (loc.first.inner_text() or "").strip()
                if text:
                    return text
        except Exception:
            pass
        return ""

    def _captcha_input(self):
        for sel in self.CAPTCHA_INPUT_SELECTORS:
            try:
                loc = self.page.locator(sel)
                for i in range(loc.count()):
                    item = loc.nth(i)
                    if item.is_visible():
                        return item
            except Exception:
                continue
        return None

    def _captcha_submit(self):
        for sel in self.CAPTCHA_SUBMIT_SELECTORS:
            try:
                loc = self.page.locator(sel)
                for i in range(loc.count()):
                    item = loc.nth(i)
                    if item.is_visible():
                        return item
            except Exception:
                continue
        return None

    def _has_contact_panel(self) -> bool:
        try:
            return bool(self.page.locator(self.CONTACT_PANEL_SELECTOR).count())
        except Exception:
            return False

    def _solve_captcha(self, img, refnr):
        """Relaye l'image à l'utilisateur et envoie sa réponse.

        Retourne True si le serveur a accepté la réponse, False sinon.
        """
        try:
            png = img.screenshot()
        except Exception:
            png = b""
        if not png:
            self.log("warn", f"Captcha für {refnr} konnte nicht gelesen werden.")
            return False
        self._captcha_cancelled = False
        self.log("warn", f"Captcha required for {refnr}; waiting for your answer.")
        answer = self.ask_captcha(png)
        self._check_stop()
        if not answer:
            self._captcha_cancelled = True
            return False

        box = self._captcha_input()
        if box is None:
            self.log("warn", f"Captcha-Eingabefeld für {refnr} nicht gefunden.")
            return False
        try:
            box.click()
            box.fill("")
            # `type` (instead of only `fill`) makes Angular's form control
            # valid, which enables the "Absenden" button.
            box.type(answer, delay=25)
        except Exception:
            try:
                box.fill(answer)
            except Exception:
                return False

        submitted = False
        button = self._captcha_submit()
        if button is not None:
            try:
                if button.is_enabled():
                    button.click()
                    submitted = True
            except Exception:
                pass
        if not submitted:
            try:
                box.press("Enter")
            except Exception:
                return False

        # The answer is checked asynchronously.  A wrong answer renders an
        # error plus a new image; a correct one removes the captcha and loads
        # the employer contact card.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self._check_stop()
            error = self._captcha_error()
            if error:
                self.log("warn", f"Captcha für {refnr} nicht akzeptiert: {error[:120]}")
                return False
            if not self._captcha_visible():
                # Let Angular settle before declaring success: a wrong answer
                # can briefly hide the old image while the new one loads.
                self.page.wait_for_timeout(1200)
                error = self._captcha_error()
                if error:
                    self.log("warn", f"Captcha für {refnr} nicht akzeptiert: {error[:120]}")
                    return False
                if not self._captcha_visible():
                    return True
            self.page.wait_for_timeout(350)
        self.log("warn", f"Captcha für {refnr}: keine Antwort vom Server.")
        return False

    def _captcha_loop(self, refnr, max_attempts=8):
        """Ask for a captcha until the contact card is unlocked or the user cancels."""
        for attempt in range(1, max_attempts + 1):
            self._check_stop()
            # Dismiss the cookie modal before taking the captcha screenshot:
            # otherwise its overlay hides half of the characters.
            self._dismiss_cookies()
            # Wait long enough for Angular to render the first challenge; on
            # later attempts a new image is already on its way.
            img = self._captcha_image(wait_ms=8000 if attempt == 1 else 4000)
            if img is None:
                return
            self.log("info", f"Captcha {attempt}/{max_attempts} für {refnr}.")
            if not self._solve_captcha(img, refnr):
                if self._captcha_cancelled:
                    return
                # Wrong answer: the site normally shows a fresh image.
                self.page.wait_for_timeout(1000)
                continue
            self._wait_for_contact_section(timeout_ms=6000)
            self.page.wait_for_timeout(800)
            if clean_emails(self._body()):
                return
            if not self._captcha_visible():
                return

    # -- API principale ---------------------------------------------------------
    def fetch(self, refnr):
        """Retourne dict(person, email, telefonnummer). Lève Stopped si Stop."""
        self._check_stop()
        self._captcha_cancelled = False
        p = self.page
        p.goto(JOB_URL.format(refnr=refnr), wait_until="domcontentloaded")
        p.wait_for_timeout(1200)
        self._click_first(self.COOKIE_RE, roles=("button",))
        self._wait_for_contact_section(timeout_ms=3000)

        text = self._body()
        person, email, phone = parse_contact_text(text)
        if not email:
            lowered = text.lower()
            if self._captcha_visible():
                # The challenge is already on the offer page; no extra click
                # is needed before asking the user.
                self._captcha_loop(refnr)
            elif "externe seite öffnen" in lowered and not self._has_contact_panel():
                # Employer-hosted offer: this page has no BA contact panel and
                # will never show a captcha.  Skipping the wait keeps the run
                # fast; name/address/link are still exported.
                pass
            else:
                self._click_first(self.REVEAL_RE)
                self._wait_for_contact_section(timeout_ms=2500)
                if self._has_contact_panel() or self._captcha_visible():
                    self._captcha_loop(refnr)
            text = self._body()
            person2, email2, phone2 = parse_contact_text(text)
            person, phone = person or person2, phone or phone2
            email = email or email2
            if not email:
                self._dump(refnr, "no_email")
        return {"person": person, "email": email, "telefonnummer": phone}

    def _body(self):
        try:
            txt = self.page.inner_text("body")
            # les liens mailto: ne sont pas toujours visibles dans le texte
            hrefs = self.page.eval_on_selector_all(
                'a[href^="mailto:"]', "els => els.map(e => e.getAttribute('href'))")
            return txt + "\n" + "\n".join(h.replace("mailto:", " ").split("?")[0] for h in hrefs)
        except Exception:
            return ""


# =============================================================================
# 3) Export Excel (même format que le modèle : 7 colonnes, sans mise en forme)
# =============================================================================
def export_xlsx(rows, path=None):
    """Export Jobbörse rows in the user's seven-column application layout."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(EXCEL_COLUMNS)
    for r in rows:
        ws.append([
            r.get("firma", ""),
            r.get("person") or "Damen und Herren",
            r.get("adresse", ""),
            r.get("beginn") or None,
            r.get("email") or None,
            r.get("telefonnummer") or None,
            r.get("link", ""),
        ])
    for col, w in EXCEL_WIDTHS.items():
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    if path:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return data


def esc(s):
    return html_lib.escape(str(s or ""))


# =============================================================================
# 4) Job d'extraction (thread d'arrière-plan, sans dépendance Streamlit)
# =============================================================================
class ExtractionJob:
    """
    params: angebotsart, locations (liste, vide = toute l'Allemagne), was, umkreis,
            seit, year ("Alle"/None ou année), max_results, headed.
    L'état (log, rows, captcha…) est lu par l'interface toutes les secondes.
    """

    def __init__(self, params, output_dir="output", resume=False):
        self.params = params
        self.status = "running"          # running | done | stopped | error
        self.total = 0
        self.processed = 0
        self.log = []                    # (kind, text) kind: info | ok | ko | warn | err
        self.rows = []
        self.stop = threading.Event()
        self.captcha_png = None
        self.captcha_answer = None
        self.captcha_event = threading.Event()
        self.lock = threading.Lock()
        self._web_finder = None
        self._site_finder = None
        self._website_cache = {}
        self.paused = threading.Event()
        self.resume = bool(resume)
        self.state_path = os.path.join(output_dir, "job_states", "arbeitsagentur.json")
        # Search results form a durable queue.  This is the important part of
        # resume: after a restart we continue with the next offer, instead of
        # asking every selected city and every already-completed offer again.
        self.offers = []
        self.next_offer_index = 0
        self.next_location_index = 0
        self.locations_total = 0
        self.locations_completed = 0
        self.current_location = ""
        self._collection_complete = False
        self._state_restored = False
        self._legacy_seen_refs = set()
        self.autosave_path = os.path.join(
            output_dir, f"arbeitsagentur_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    # -- appelé par le worker / le navigateur --------------------------------------
    def add_log(self, kind, text):
        with self.lock:
            if kind == "warn" and self.log and self.log[-1] == (kind, text):
                return                   # un seul "Captcha detected!" tant qu'il n'est pas résolu
            self.log.append((kind, text))

    def ask_captcha(self, png):
        """Bloque le worker jusqu'à ce que l'utilisateur réponde / annule / stoppe."""
        self.captcha_answer = None
        self.captcha_event.clear()
        self.captcha_png = png
        while not self.captcha_event.wait(0.3):
            if self.stop.is_set() or self.paused.is_set():
                break
        self.captcha_png = None
        return self.captcha_answer

    # -- appelé par l'interface ---------------------------------------------------------
    def answer_captcha(self, text):
        self.captcha_answer = (text or "").strip() or None
        # Clear the image before waking the worker so the Streamlit dialog
        # closes immediately instead of briefly showing a stale captcha.
        self.captcha_png = None
        self.captcha_event.set()

    def request_stop(self):
        self.stop.set()
        self.paused.set()
        self.captcha_event.set()
        self._persist("stopped")

    def request_pause(self):
        self.paused.set()
        # A captcha wait has no regular polling loop; wake it so the current
        # offer can be checkpointed before the worker switches to paused.
        self.captcha_event.set()
        self._persist("pausing")
        self.add_log("info", "Extraktion pausée – état sauvegardé, reprise possible plus tard.")

    def _persist(self, status: str | None = None):
        with self.lock:
            status = status or self.status
            data = {
                "source": "arbeitsagentur",
                "status": status,
                "params": self.params,
                "total": self.total,
                "processed": self.processed,
                "rows": list(self.rows),
                "offers": list(self.offers),
                "next_offer_index": self.next_offer_index,
                "next_location_index": self.next_location_index,
                "locations_total": self.locations_total,
                "locations_completed": self.locations_completed,
                "current_location": self.current_location,
                "collection_complete": self._collection_complete,
                "autosave_path": self.autosave_path,
            }
        try:
            save_state("arbeitsagentur", data)
        except Exception:
            pass

    def _restore_state(self):
        """Restore a paused/interrupted extraction without re-searching it."""
        state = load_state("arbeitsagentur")
        if not state or state.get("status") == "done":
            return False
        self.rows = state.get("rows") or []
        self.processed = int(state.get("processed", len(self.rows)) or 0)
        self.offers = state.get("offers") or []
        self.next_offer_index = int(state.get("next_offer_index", 0) or 0)
        self.next_location_index = int(state.get("next_location_index", 0) or 0)
        self.locations_total = int(state.get("locations_total", 0) or 0)
        self.locations_completed = int(state.get("locations_completed", 0) or 0)
        self.current_location = state.get("current_location", "") or ""
        self._collection_complete = bool(state.get("collection_complete", False))
        self.autosave_path = state.get("autosave_path", self.autosave_path)
        self.total = int(state.get("total", len(self.offers) or len(self.rows)) or 0)
        # Saved states created before the durable queue existed can still be
        # resumed safely once: search again, but never process the saved refs.
        if not self.offers:
            self._legacy_seen_refs = {
                str(row.get("refnr", "")) for row in self.rows if row.get("refnr")
            }
        self.status = "running"
        self.add_log(
            "info",
            f"Reprise AA : {self.processed}/{self.total or '?'} offres déjà traitées.",
        )
        return True

    def snapshot_log(self):
        with self.lock:
            return list(self.log)

    def excel_bytes(self):
        return export_xlsx(list(self.rows))

    def _save(self):
        try:
            export_xlsx(list(self.rows), self.autosave_path)
        except Exception:
            pass

    # -- fallback: website when the offer has no contact data --------------------
    def _contact_finders(self):
        """Lazily build one website finder pair for the whole run."""
        if self._site_finder is not None:
            return self._web_finder, self._site_finder
        from .email_finder import EmailFinder

        limit = max(10.0, min(180.0, float(self.params.get("website_max_seconds", 45))))
        max_pages = max(2, min(20, int(self.params.get("website_max_pages", 8))))
        http = SafeSession()
        http.throttle.delay = 0.5
        self._web_finder = EmailFinder(
            session=http, max_pages=max_pages, log=lambda _message: None,
            api_key=(self.params.get("website_search_api_key") or "").strip(),
            website_timeout=max(10.0, min(45.0, limit * 0.35)),
        )
        self._site_finder = WebsiteEmailFinder(
            session=http, max_pages=max_pages, max_scripts=8, log=lambda _message: None,
            stop=self.stop, pause=self.paused, timeout=10,
            max_seconds=max(10.0, limit * 0.70),
        )
        return self._web_finder, self._site_finder

    def _fill_from_website(self, offer):
        """Find the employer website and read every e-mail published there.

        Some Jobbörse offers only contain a company name (external employer,
        no contact panel).  These rows must still get useful contact data, so
        the company is searched once; results are cached per employer name.
        """
        company = (offer.get("firma") or "").strip()
        if not company:
            return
        # Large employers can have different local recruiting contacts.  Do
        # not reuse a Berlin result for a company with the same name in
        # Hamburg: cache by the employer and its advertised location.
        city = (offer.get("city") or "").strip()
        if not city:
            city = (offer.get("adresse") or "").split("|")[-1].strip()
            city = re.sub(r"^\d{4,5}\s*", "", city).split(",")[0].strip()
        key = (normalise_firma(company).lower(), city.lower())
        cached = self._website_cache.get(key)
        if cached is None:
            web_finder, site_finder = self._contact_finders()
            website = ""
            try:
                website = web_finder.find_website(company, city) or ""
            except Stopped:
                raise
            except Exception:
                website = ""
            emails, person = [], ""
            if website and not is_blocked_url(website):
                try:
                    result = site_finder.find(website)
                    emails = valid_emails(result.get("emails") or result.get("email"))
                    person = (result.get("person") or "").strip()
                except Stopped:
                    raise
                except Exception:
                    pass
            cached = (website, emails, person)
            self._website_cache[key] = cached
            if website:
                self.add_log("info", f"· Website gesucht: {company} → {website}")
            if emails:
                self.add_log("ok", f"· E-Mail auf Website: {company} · {len(emails)}")
        website, emails, person = cached
        if website:
            offer["website"] = website
        if emails:
            # Keep the complete set for the live job state while retaining a
            # semicolon-separated Excel cell, as in the supplied template.
            offer["emails"] = list(emails)
            offer["email"] = "; ".join(emails)
        if not offer.get("person") and person:
            offer["person"] = person

    def _run(self):
        p = self.params
        locations = [l for l in (p.get("locations") or []) if l] or [""]
        year = p.get("year")
        year = None if year in (None, "", "Alle") else str(year)
        contacts, final = None, "done"
        try:
            if self.resume and not self._state_restored:
                restored = self._restore_state()
                self._state_restored = True
            else:
                restored = False

            self.locations_total = len(locations)
            if not self._collection_complete:
                if not restored:
                    self.add_log("info", "Loading jobs from all pages ...")

                # Keep both queued and already-exported refs out of a resumed
                # city search.  The latter is only needed for legacy states
                # saved before the durable offer queue was introduced.
                seen = {
                    str(offer.get("refnr", ""))
                    for offer in self.offers
                    if offer.get("refnr")
                } | self._legacy_seen_refs

                for location_index in range(self.next_location_index, len(locations)):
                    if self.stop.is_set() or self.paused.is_set():
                        break
                    loc = locations[location_index]
                    self.current_location = loc or "Deutschlandweit"
                    self.locations_completed = location_index
                    if len(locations) > 1:
                        self.add_log("info", f"→ {loc or 'deutschlandweit'}")

                    reported = []

                    def on_total(n):
                        reported.append(n)

                    # The public API returns this city's complete result list.
                    # Checkpoint it before the slow browser/email phase.
                    city_offers = list(search_offers(
                        p["angebotsart"], loc, p.get("was", ""), p.get("umkreis"), p.get("seit"),
                        max_results=p.get("max_results"), stop=self.stop, on_total=on_total,
                    ))
                    added = 0
                    for offer in city_offers:
                        refnr = str(offer.get("refnr", ""))
                        if not refnr or refnr in seen:
                            continue
                        seen.add(refnr)
                        if year and year not in offer.get("beginn", ""):
                            continue
                        self.offers.append(offer)
                        added += 1
                    self.next_location_index = location_index + 1
                    self.locations_completed = location_index + 1
                    queued_total = len(self.offers) + (len(self.rows) if self._legacy_seen_refs else 0)
                    self.total = max(self.total, queued_total, len(self.rows))
                    count = reported[-1] if reported else len(city_offers)
                    self.add_log(
                        "info",
                        f"✓ {loc or 'deutschlandweit'} · {added}/{count} offres mises en file",
                    )
                    self._persist("collecting")

                if self.stop.is_set():
                    final = "stopped"
                    return
                if self.paused.is_set():
                    final = "paused"
                    return
                self._collection_complete = True
                queued_total = len(self.offers) + (len(self.rows) if self._legacy_seen_refs else 0)
                self.total = max(self.total, queued_total, len(self.rows))
                self._persist("extracting")

            if not self.offers and not self.rows:
                self.add_log("info", "Keine passenden Angebote gefunden.")
                return

            # Start Chromium only after all selected cities have a durable
            # queue.  A pause/restart therefore never repeats city searches.
            try:
                contacts = BrowserContacts(self.ask_captcha, self.add_log,
                                           headed=p.get("headed", False), stop=self.stop)
                contacts.start()
            except Exception as e:
                contacts = None
                self.add_log("err", f"Navigateur indisponible ({e}). Installez : pip install "
                                    "playwright && playwright install chromium. "
                                    "Les offres sont exportées sans e-mail.")

            for offer_index in range(self.next_offer_index, len(self.offers)):
                if self.stop.is_set() or self.paused.is_set():
                    break
                offer = dict(self.offers[offer_index])
                if contacts:
                    try:
                        contacts_result = contacts.fetch(offer["refnr"])
                        offer.update(contacts_result)
                    except Stopped:
                        raise
                    except Exception as e:
                        self.add_log("warn", f"{offer['firma']} : erreur de lecture ({type(e).__name__})")
                if not offer.get("email") and p.get("website_fallback", True):
                    try:
                        self._fill_from_website(offer)
                    except Stopped:
                        raise
                    except Exception as e:
                        self.add_log(
                            "warn",
                            f"{offer['firma']} : Website-Suche fehlgeschlagen "
                            f"({type(e).__name__})",
                        )
                self.rows.append(offer)
                self.next_offer_index = offer_index + 1
                self.processed += 1
                if offer.get("email"):
                    emails = offer["email"]
                    if len(emails) > 100:
                        emails = emails[:97] + "…"
                    self.add_log("ok", f"({self.processed}) {offer['firma']} · {emails}")
                elif offer.get("website"):
                    self.add_log(
                        "warn",
                        f"({self.processed}) {offer['firma']} · keine veröffentlichte E-Mail nach Website-Prüfung",
                    )
                else:
                    self.add_log("info", f"({self.processed}) {offer['firma']} · keine offizielle Website gefunden")
                self._persist("extracting")
                if self.processed % 10 == 0:
                    self._save()
                time.sleep(0.4)

            if self.stop.is_set():
                final = "stopped"
            elif self.paused.is_set():
                final = "paused"
            elif self.next_offer_index < len(self.offers):
                final = "paused"
            else:
                final = "done"
        except Stopped:
            self.add_log("err", "Process stopped by user")
            final = "stopped"
        except Exception as e:
            self.add_log("err", f"Erreur : {e}")
            final = "error"
        finally:
            if contacts:
                contacts.close()
            self._save()
            self.status = final          # only completed after the file/state write
            self._persist(final)
