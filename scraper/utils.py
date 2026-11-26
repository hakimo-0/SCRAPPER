# -*- coding: utf-8 -*-
"""Utilities: HTTP, regex, extraction, export."""
import re, time, random, unicodedata, json, os
from urllib.parse import urljoin, urlparse, quote_plus
from datetime import datetime

import requests
from bs4 import BeautifulSoup

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]

AGENTUR_USER_AGENT = (
    "Jobsuche/2.9.2 (de.arbeitsagentur.jobboerse; build:1077; iOS 15.1.0) "
    "Alamofire/5.4.4"
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", re.IGNORECASE)

JUNK_EMAIL_RE = re.compile(
    r"(example|yourname|yourcompany|user@|test|sample|name@|email@|@email|mail@mail|"
    r"@domain|@sentry|@wixpress|@site|\d\.\d\d@|no-?reply|donotreply|noreply|"
    r"2x@|@2x|\.png$|\.jpg$|\.jpeg$|\.gif$|\.webp$|\.svg$|\.css$|\.js$|"
    r"@example\.|@localhost|@\.loc|@test\.|@sentry\.|@wixpress\.|@schema\.)",
    re.IGNORECASE,
)

JUNK_EMAIL_DOMAINS = {
    "arbeitsagentur.de", "jobboerse.de", "stepstone.de", "indeed.com",
    "xing.com", "linkedin.com", "kununu.com", "glassdoor.com", "monster.de",
    "azubiyo.de", "ausbildung.de", "meinpraktikum.de", "trainee.de",
    "google.com", "googlemail.com", "gstatic.com", "w3.org", "schema.org",
    "sentry.io", "wixpress.com", "sentry-next.wixpress.com",
}

PREFERRED_LOCALPARTS = [
    "info", "kontakt", "contact", "office", "service", "bewerbung",
    "karriere", "jobs", "personal", "hr", "mail", "hallo", "team",
    "ausbildung", "recruiting",
]

GENERIC_LOCALPARTS = {
    "info", "kontakt", "contact", "office", "service", "mail", "hallo",
    "care", "kundenservice", "firmenkontaktdaten-bk",
}

BLACKLIST_URL_WORDS = (
    "facebook.com", "linkedin.com", "xing.com", "twitter.com",
    "instagram.com", "youtube.com", "tiktok.com", "snapchat.com",
    "pinterest.com", "whatsapp.com", "azubiyo", "ausbildung.de",
    "ausbildungsmarkt", "w3.org", "schema.org", "google.", "doubleclick",
    "arbeitsagentur.de", "jobboerse", "stepstone", "indeed", "kununu",
    "glassdoor", "meinpraktikum", "trainee.de", "yumpu", "scribd",
    "bing.com", "duckduckgo.com", "microsoft.com", "msn.com", "ecosia.org",
    "brave.com", "search.marcaria", "wikipedia.org", "wiktionary.org",
    "dict.cc", "dict.leo.org", "leo.org", "linguee.", "dastelefonbuch",
    "gelbeseiten", "11880.com", "cylex", "firmenabc", "northdata",
)

# Portals must be matched by hostname, not as a substring: "hotels.com"
# would otherwise also block a real company domain such as
# "kenzi-hotels.com".  The same applies to "x.com" and "box.com".
BLOCKED_PORTAL_HOSTS = {
    "x.com",
    "booking.com", "tripadvisor.com", "tripadvisor.de", "tripadvisor.fr",
    "tripadvisor.es", "tripadvisor.it", "expedia.com", "expedia.de",
    "hotels.com", "agoda.com", "trivago.com", "trivago.de", "kayak.com",
    "kayak.de", "airbnb.com", "airbnb.de", "hostelworld.com", "hrs.com",
    "hrs.de",
}


def is_blocked_url(url):
    low = (url or "").lower()
    if any(w in low for w in BLACKLIST_URL_WORDS):
        return True
    host = get_domain(url)
    labels = host.split(".")
    return any(".".join(labels[i:]) in BLOCKED_PORTAL_HOSTS for i in range(len(labels) - 1))


def normalize(value):
    if not value:
        return ""
    v = unicodedata.normalize("NFKD", str(value))
    v = "".join(c for c in v if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", v.strip().lower())


def get_domain(url):
    try:
        host = urlparse(url if "//" in url else "//" + url).netloc
    except Exception:
        host = url
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_junk_email(email):
    parts = email.split("@")
    if len(parts) != 2:
        return True
    local, dom = parts
    local = local.lower()
    dom = dom.lower()
    if JUNK_EMAIL_RE.search(email):
        return True
    if dom in JUNK_EMAIL_DOMAINS:
        return True
    if any(dom.endswith("." + d) for d in JUNK_EMAIL_DOMAINS):
        return True
    if local.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        return True
    if dom.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        return True
    if len(dom) < 4 or "." not in dom:
        return True
    return False


def clean_emails(raw_emails):
    out = []
    for e in raw_emails:
        e = (e or "").strip()
        if not e:
            continue
        e = e.split("?")[0].split("#")[0].strip(".")
        if len(e) > 254:
            continue
        e = re.sub(r"\s", "", e)
        if is_junk_email(e):
            continue
        if e not in out:
            out.append(e)
    return out


def rank_email(email):
    local = email.split("@")[0].lower()
    if local in GENERIC_LOCALPARTS:
        return 0
    if any(local.startswith(p) for p in PREFERRED_LOCALPARTS):
        return 1
    if local in ("webmaster", "admin"):
        return 4
    return 2


def best_email(emails):
    if not emails:
        return None
    return sorted(emails, key=rank_email)[0]


class Throttle:
    def __init__(self, delay=1.0):
        self.delay = delay
        self._last = {}

    def wait(self, host):
        if self.delay <= 0:
            return
        now = time.time()
        since = self._last.get(host, 0)
        wait = (since + self.delay) - now
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.25))
        self._last[host] = time.time()


class SafeSession:
    def __init__(self, throttle=None, base_headers=None):
        self.session = requests.Session()
        self.session.headers.update({"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
        self.rotate_ua()
        if base_headers:
            self.session.headers.update(base_headers)
        self.throttle = throttle or Throttle(delay=0.5)

    def rotate_ua(self, ua=None):
        self.session.headers["User-Agent"] = ua or random.choice(USER_AGENTS)

    def get(self, url, params=None, timeout=20, host=None, attempts=3, **kw):
        """GET with a bounded number of retries.

        ``attempts=1`` is used by the e-mail crawler: a dead sub-page must not
        cost three timeouts before the crawler can move on to the next page.
        """
        h = host or get_domain(url)
        self.throttle.wait(h)
        attempts = max(1, int(attempts))
        for attempt in range(attempts):
            try:
                r = self.session.get(url, params=params, timeout=timeout, allow_redirects=True, **kw)
                if r.status_code in (429, 503) and attempt < attempts - 1:
                    time.sleep(2.0 + attempt * 2)
                    continue
                return r
            except requests.RequestException:
                if attempt == attempts - 1:
                    return None
                time.sleep(1.5 * (attempt + 1))
        return None

    def get_text(self, url, params=None, **kw):
        r = self.get(url, params=params, **kw)
        if r is None or r.status_code != 200:
            return None
        try:
            return r.text
        except Exception:
            return None

    def post(self, url, data=None, json=None, timeout=30, **kw):
        return self.session.post(url, data=data, json=json, timeout=timeout, **kw)


def soup_of(html):
    return BeautifulSoup(html or "", "lxml")


def extract_emails_from_text(text):
    return clean_emails(EMAIL_RE.findall(text or ""))


def extract_emails_from_html(html, base_url=""):
    soup = soup_of(html)
    emails = []
    contact_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0]
            if addr and "@" in addr and addr not in emails:
                emails.append(addr)
        if base_url and re.search(
            r"(impressum|kontakt|contact|über.?uns|ueber.?uns|about|impress|anbieter)",
            href.lower(),
        ):
            url = urljoin(base_url, href)
            if url.startswith("http") and url not in contact_links:
                contact_links.append(url)
    emails += extract_emails_from_text(soup.get_text(" ", strip=True))
    return {"emails": clean_emails(emails), "contact_links": contact_links}


def is_company_like_url(url, company_keywords):
    if not url:
        return False
    low = url.lower()
    if any(w in low for w in BLACKLIST_URL_WORDS):
        return False
    if any(w in low for w in ("/search?", "?q=", "t.co", "bit.ly")):
        return False
    if not company_keywords:
        return True
    domain = get_domain(url)
    for kw in company_keywords:
        if kw and len(kw) >= 3 and kw in domain:
            return True
    return False


def export_excel(leads, path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    headers = [
        "Source", "Unternehmen", "Straße", "PLZ", "Stadt", "Titel/Stelle",
        "Website", "Emails", "Telefon", "Profil/Anzeige",
    ]
    ws.append(headers)
    header_font = Font(bold=True, color="FFFFFF")
    for col, name in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col)
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
        cell.fill = _header_fill()

    for le in leads:
        emails = le.get("emails") or ([le.get("email")] if le.get("email") else [])
        email_cell = ", ".join(emails) if emails else ""
        ws.append([
            le.get("source", ""),
            le.get("name", ""),
            le.get("street", ""),
            le.get("zip", ""),
            le.get("city", ""),
            le.get("title", ""),
            le.get("website", ""),
            email_cell,
            le.get("phone", ""),
            le.get("url", ""),
        ])

    widths = [16, 34, 26, 8, 20, 46, 40, 36, 18, 50]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)


def _header_fill():
    from openpyxl.styles import PatternFill
    return PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")


def default_output_path():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"output/leads_{ts}.xlsx"


def ensure_output_dir():
    os.makedirs("output", exist_ok=True)