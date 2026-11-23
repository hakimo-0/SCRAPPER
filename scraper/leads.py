# -*- coding: utf-8 -*-
"""Common lead model: normalise, best email, merge/dedupe, Excel export, website finder."""
from __future__ import annotations

import io
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urljoin

from .utils import (
    SafeSession, soup_of, normalize, get_domain, clean_emails, is_junk_email,
    extract_emails_from_html,
)

EXCEL_COLUMNS = ["firma", "person", "adresse", "email", "telefonnummer", "link", "beginn"]
EXCEL_WIDTHS = {"A": 55, "B": 30, "C": 55, "D": 55, "E": 55, "F": 55, "G": 55}

EMAIL_PRIORITY = ["bewerbung", "karriere", "ausbildung", "jobs", "personal", "hr", "info", "kontakt"]

PERSON_RE = re.compile(r"\b(Herr|Frau)\s+((?:Dr\.|Prof\.)?\s?[A-ZÄÖÜ][\w\-ÄÖÜäöüß]+(?:\s+[A-ZÄÖÜ][\w\-ÄÖÜäöüß]+){0,2})")
PATHS = ["/", "/impressum", "/kontakt", "/karriere", "/jobs", "/ausbildung", "/bewerbung"]

# Obfuscation patterns -> real characters
OBFUSCATION = [
    (re.compile(r"\s*\(\s*at\s*\)\s*", re.I), "@"),
    (re.compile(r"\s*\[\s*at\s*\]\s*", re.I), "@"),
    (re.compile(r"\s+at\s+", re.I), "@"),  # weak, applied last-ish
    (re.compile(r"\s*\(\s*dot\s*\)\s*", re.I), "."),
    (re.compile(r"\s*\[\s*dot\s*\]\s*", re.I), "."),
]


class Stopped(Exception):
    pass


def normalise_firma(value: Any) -> str:
    return " ".join(str(value or "").split())


def _to_list(emails: Any) -> List[str]:
    if emails is None:
        return []
    if isinstance(emails, str):
        parts = re.split(r"[\s,;]+", emails)
        return [p for p in parts if p]
    if isinstance(emails, (list, tuple, set)):
        return [str(x) for x in emails if x]
    return [str(emails)]


def _email_score(email: str) -> int:
    local = email.split("@")[0].lower()
    for i, p in enumerate(EMAIL_PRIORITY):
        if local == p:
            return i
    for i, p in enumerate(EMAIL_PRIORITY):
        if local.startswith(p):
            return i + 0.5 if False else (i + len(EMAIL_PRIORITY))
    return len(EMAIL_PRIORITY) + 1


def best_email(emails: Any) -> Optional[str]:
    candidates = []
    for e in _to_list(emails):
        e = e.strip().strip("<>").strip()
        if not e:
            continue
        if is_junk_email(e):
            continue
        candidates.append(e.lower())
    if not candidates:
        return None
    # dedupe preserving
    seen = set()
    uniq = []
    for e in candidates:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return min(uniq, key=_email_score)


def valid_emails(value: Any) -> List[str]:
    """Return every usable email address, preserving its discovery order.

    ``email`` was historically a single value.  Live website extraction also
    returns an ``emails`` list, so this accepts either form (or a semicolon-
    separated Excel value) and keeps all addresses for export.
    """
    cleaned: List[str] = []
    for email in _to_list(value):
        email = email.strip().strip("<>").lower()
        if not email or is_junk_email(email) or email in cleaned:
            continue
        cleaned.append(email)
    return cleaned


def decode_cfemail(hexstr: str) -> str:
    try:
        r = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ r) for i in range(2, len(hexstr), 2))
    except (ValueError, TypeError):
        return ""


def deobfuscate_text(text: str) -> str:
    if not text:
        return ""
    out = text
    for pat, repl in OBFUSCATION:
        out = pat.sub(repl, out)
    return out


def deobfuscate_emails(text: str) -> List[str]:
    return clean_emails(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}", deobfuscate_text(text)))


def _registrable_domain(host: str) -> str:
    host = get_domain(host or "")
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def normalize_website(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    qs = [(k, v) for k, v in parse_qsl(parsed.query) if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref", "source"))]
    new = parsed._replace(query=urlencode(qs), fragment="")
    out = new.geturl()
    return out.rstrip("/")


def _extract_cfemails(html: str) -> List[str]:
    out = []
    for m in re.finditer(r'data-cfemail="([0-9a-fA-F]+)"', html or ""):
        dec = decode_cfemail(m.group(1))
        if dec and "@" in dec:
            out.append(dec)
    return out


def _extract_jsonld_emails(html: str) -> List[str]:
    out = []
    for m in re.finditer(r'"email"\s*:\s*"([^"]+)"', html or "", re.I):
        out.append(m.group(1))
    return out


def _extract_person(text: str) -> str:
    # Try near keywords
    ctx = text or ""
    for kw in ("ansprechpartner", "ansprechperson", "kontaktperson", "personal", "geschäftsführer", "geschaeftsfuehrer"):
        idx = ctx.lower().find(kw)
        if idx >= 0:
            window = ctx[idx: idx + 500]
            pm = PERSON_RE.search(window)
            if pm:
                return f"{pm.group(1)} {pm.group(2).strip()}"
    pm = PERSON_RE.search(ctx)
    if pm:
        # only accept if near a contact keyword? keep conservative: require keyword
        pass
    return ""


def _same_domain(email: str, website: str) -> bool:
    try:
        email_domain = email.split("@", 1)[1]
    except IndexError:
        return False
    return _registrable_domain(email_domain) == _registrable_domain(urlparse(website).netloc or website)


def _has_mx(domain: str) -> bool:
    try:
        import dns.resolver
        dns.resolver.resolve(domain, "MX")
        return True
    except Exception:
        return False


class WebsiteEmailFinder:
    def __init__(self, session=None, delay: float = 0.5, max_pages: int = 4, log: Callable = print,
                 verify_mx: bool = False, stop: Optional[threading.Event] = None,
                 timeout: float = 10.0, max_seconds: float = 60.0):
        self.http = session or SafeSession()
        if session is None:
            self.http.throttle.delay = delay
        self.max_pages = max_pages
        self.log = log
        self.verify_mx = verify_mx
        self.stop = stop
        self.timeout = timeout
        self.max_seconds = max(5.0, float(max_seconds or 60.0))
        self.cache: Dict[str, Dict[str, Any]] = {}

    def _check_stop(self):
        if self.stop is not None and self.stop.is_set():
            raise Stopped()

    def find(self, website_url: str) -> Dict[str, Any]:
        url = normalize_website(website_url)
        if not url:
            return {"website": "", "email": None, "emails": [], "person": "Damen und Herren"}
        # Cache by full starting URL, not only by domain: two hotels of the
        # same chain (e.g. ibis and Mövenpick on accor.com) have different
        # contact pages and must not inherit each other's addresses.
        cache_key = url
        if cache_key in self.cache:
            return dict(self.cache[cache_key])
        self._check_stop()
        result = self._fetch(url)
        self.cache[cache_key] = result
        return dict(result)

    def _fetch(self, start_url: str) -> Dict[str, Any]:
        deadline = time.monotonic() + self.max_seconds
        timed_out = False
        visited: List[str] = []
        to_visit: List[str] = [start_url]
        found_emails: List[str] = []
        person = ""
        base_host = urlparse(start_url).netloc
        # Only queue the two most useful fallbacks.  Blindly adding every
        # known path made dead domains cost one timeout per path.
        for p in ("/kontakt", "/impressum"):
            cand = urljoin(start_url + "/", p.lstrip("/"))
            if cand not in to_visit:
                to_visit.append(cand)
        # also homepage links containing keywords
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                html0 = None
            else:
                html0 = self.http.get_text(
                    start_url, timeout=max(1.0, min(self.timeout, remaining)), attempts=1
                )
            if html0:
                soup = soup_of(html0)
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    txt = (a.get_text(" ", strip=True) or "").lower()
                    if any(k in (href.lower() + " " + txt) for k in ("impressum", "kontakt", "contact", "karriere", "jobs", "ausbildung", "bewerbung")):
                        u = urljoin(start_url, href)
                        if u.startswith("http") and _registrable_domain(urlparse(u).netloc) == _registrable_domain(base_host):
                            if u not in to_visit:
                                to_visit.append(u)
        except Exception:
            pass

        pages_attempted = 0
        while to_visit and pages_attempted < self.max_pages:
            self._check_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            page = to_visit.pop(0).split("#")[0]
            if page in visited:
                continue
            visited.append(page)
            if _registrable_domain(urlparse(page).netloc) != _registrable_domain(base_host):
                continue
            # A failed /kontakt page counts as an attempt too; otherwise a dead
            # site can keep the loop busy for every queued candidate.
            pages_attempted += 1
            html = self.http.get_text(
                page, timeout=max(1.0, min(self.timeout, remaining)), attempts=1
            )
            if not html:
                continue
            # mailto + text
            data = extract_emails_from_html(html, base_url=page)
            for e in data.get("emails", []):
                found_emails.append(e)
            # obfuscated + cloudflare + jsonld
            found_emails += deobfuscate_emails(html)
            found_emails += _extract_cfemails(html)
            found_emails += _extract_jsonld_emails(html)
            # also plain text regex on deobfuscated
            found_emails += clean_emails(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}", deobfuscate_text(html)))
            # person
            if not person:
                person = _extract_person(soup_of(html).get_text(" ", strip=True))
            # contact links
            for link in data.get("contact_links", []):
                if link not in visited and link not in to_visit:
                    to_visit.append(link)

        # filter same domain
        filtered = []
        for e in found_emails:
            e = e.strip().lower()
            if is_junk_email(e):
                continue
            if not _same_domain(e, start_url):
                continue
            if e not in filtered:
                filtered.append(e)

        if self.verify_mx:
            mx_ok = set()
            for e in filtered:
                dom = e.split("@", 1)[1]
                if _has_mx(dom):
                    mx_ok.add(e)
            filtered = [e for e in filtered if e in mx_ok]

        email = best_email(filtered)
        if not person:
            person = "Damen und Herren"
        result = {
            "website": start_url,
            "email": email,
            "emails": filtered,
            "person": person,
            "timed_out": timed_out,
        }
        return result


def find_website_emails(website_url: str, max_pages: int = 4, verify_mx: bool = False,
                        stop: Optional[threading.Event] = None) -> Dict[str, Any]:
    return WebsiteEmailFinder(max_pages=max_pages, verify_mx=verify_mx, stop=stop).find(website_url)


def _plz(adresse):
    m = re.search(r"\b(\d{5})\b", adresse or "")
    return m.group(1) if m else ""


def merge_leads(leads: Iterable[Dict[str, Any]], keep_all_offers: bool = False) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for le in leads:
        lead = dict(le or {})
        lead["firma"] = normalise_firma(lead.get("firma") or lead.get("name") or "")
        for c in EXCEL_COLUMNS:
            lead.setdefault(c, "")
        lead.setdefault("source", lead.get("source", ""))
        if lead.get("person") == "Damen und Herren":
            lead["person"] = ""
        emails = valid_emails(lead.get("emails") or lead.get("email"))
        lead["emails"] = emails
        lead["email"] = "; ".join(emails)

        # find existing duplicate
        existing_idx = None
        if not keep_all_offers:
            for i, ex in enumerate(out):
                same_email = bool(set(lead["emails"]) & set(valid_emails(ex.get("emails") or ex.get("email"))))
                same_firma_plz = (normalise_firma(lead["firma"]).lower() == normalise_firma(ex.get("firma", "")).lower()
                                  and _plz(lead.get("adresse", "")) and _plz(lead.get("adresse", "")) == _plz(ex.get("adresse", "")))
                if same_email or same_firma_plz:
                    existing_idx = i
                    break
        if existing_idx is not None:
            existing = out[existing_idx]
            merged_emails = valid_emails(existing.get("emails") or existing.get("email")) + lead["emails"]
            merged_emails = valid_emails(merged_emails)
            existing["emails"] = merged_emails
            existing["email"] = "; ".join(merged_emails)
            for c in EXCEL_COLUMNS + ["source"]:
                if (not existing.get(c) or existing.get(c) in ("", "Damen und Herren")) and lead.get(c):
                    existing[c] = lead[c]
        else:
            out.append(lead)
    # default person
    for lead in out:
        if not lead.get("person"):
            lead["person"] = "Damen und Herren"
    return out


def _merge_key(lead: Dict[str, Any], keep_all_offers: bool) -> str:
    email = (lead.get("email") or "").strip().lower()
    firma = normalise_firma(lead.get("firma", "")).lower()
    adresse = lead.get("adresse", "") or ""
    m = re.search(r"\b(\d{5})\b", adresse)
    plz = m.group(1) if m else ""
    if keep_all_offers:
        link = (lead.get("link") or "").strip()
        if link:
            return f"link:{link}"
        return f"offer:{firma}|{plz}|{lead.get('beginn','')}|{lead.get('source','')}"
    if email:
        return f"email:{email}"
    return f"firma:{firma}|{plz}"


def export_leads_xlsx(rows: Iterable[Dict[str, Any]], path: Optional[str] = None) -> bytes:
    data = []
    for r in rows:
        lead = dict(r or {})
        lead["firma"] = normalise_firma(lead.get("firma") or lead.get("name") or "")
        if not lead.get("person"):
            lead["person"] = "Damen und Herren"
        emails = valid_emails(lead.get("emails") or lead.get("email"))
        lead["emails"] = emails
        lead["email"] = "; ".join(emails)
        data.append(lead)
    data.sort(key=lambda r: 0 if r.get("email") else 1)
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(EXCEL_COLUMNS)
    for r in data:
        ws.append([
            r.get("firma", ""),
            r.get("person") or "Damen und Herren",
            r.get("adresse", ""),
            r.get("email") or None,
            r.get("telefonnummer") or None,
            r.get("link", ""),
            r.get("beginn") or None,
        ])
    for col, w in EXCEL_WIDTHS.items():
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    out = buf.getvalue()
    if path:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(out)
    return out
