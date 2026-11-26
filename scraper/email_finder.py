# -*- coding: utf-8 -*-
"""EmailFinder - finds company website and extracts emails."""
import re
import time
from urllib.parse import quote_plus, urljoin

from .utils import (
    SafeSession, extract_emails_from_html, best_email, clean_emails,
    get_domain, normalize, soup_of, is_blocked_url,
)

PHONE_RE = re.compile(
    r"(?:\+49|0049|0)[\s./\-]?(?:\(?\d{2,5}\)?[\s./\-]?){2,5}\d{2,6}"
)

LEGAL_FORMS = {
    "gmbh", "mbh", "ag", "kg", "kgaa", "co", "co kg", "ohg", "ug", "gbr",
    "se", "ev", "e v", "ek", "e k", "stiftung", "gmbh co kg", "gmbh co",
    "und", "and", "the", "der", "die", "das", "fur", "fuer", "für",
}


class EmailFinder:
    def __init__(self, session=None, delay=0.5, max_pages=3, log=print, api_key="",
                 website_timeout=25.0):
        self.http = session or SafeSession()
        if session is None:
            self.http.throttle.delay = delay
        self.max_pages = max_pages
        self.log = log
        self.api_key = api_key
        self.website_timeout = website_timeout

    def find_website(self, company, city=""):
        company = re.sub(r'["\'`]', "", company or "").strip()
        if not company:
            return None
        deadline = time.monotonic() + max(1.0, float(self.website_timeout or 25))

        if self.api_key:
            url = self._serpapi_search(company, city)
            if url and not is_blocked_url(url):
                return self._homepage(url)

        url = self._guess_website(company, deadline=deadline)
        if url:
            return url

        if time.monotonic() >= deadline:
            return None
        tokens = self._keywords(company)
        fallback = None
        for engine in (self._bing, self._ddg):
            for url in engine(company, city):
                if is_blocked_url(url):
                    continue
                domain = get_domain(url)
                if tokens and any(t in domain for t in tokens):
                    return self._homepage(url)
                # Without a matching token the result is unrelated (dictionary,
                # news, another company).  Only accept it when the company name
                # yields no tokens at all.
                if not tokens and fallback is None:
                    fallback = url
            if fallback or time.monotonic() >= deadline:
                break
        return self._homepage(fallback) if fallback else None

    def _serpapi_search(self, company, city):
        import requests
        q = f'"{company}"' + (f" {city}" if city else "")
        try:
            r = requests.get(
                "https://serpapi.com/search.json",
                params={"engine": "google", "q": q, "hl": "de", "gl": "de",
                        "num": 10, "api_key": self.api_key},
                timeout=30,
            )
            data = r.json()
        except Exception:
            return None
        tokens = self._keywords(company)
        fallback = None
        for item in data.get("organic_results", []):
            link = item.get("link", "")
            if not link or is_blocked_url(link):
                continue
            domain = get_domain(link)
            if tokens and any(token in domain for token in tokens):
                return link
            # Do not blindly accept a directory, review or news result for a
            # company with a distinctive name.  A result without a usable
            # company token is only a last resort for very short names.
            if not tokens and fallback is None:
                fallback = link
        return fallback

    def _guess_website(self, company, deadline=None):
        tokens = self._keywords(company)
        if not tokens:
            return None

        # German domains normally keep the umlaut as ``ae``/``oe``/``ue``
        # (kuechen-aktuell.de), while ``normalize`` strips it (kuchen).
        # Probe the transliterated form first.
        variants = [tokens]
        transliterated = self._keywords(self._transliterate(company))
        if transliterated and transliterated != tokens:
            variants.insert(0, transliterated)

        stem_pairs = []
        for group in variants:
            concat = "".join(group)
            hyphen = "-".join(group) if len(group) > 1 else ""
            for stem in (concat, hyphen, group[0] if group else "", group[-1] if len(group) > 1 and len(group[-1]) >= 4 else ""):
                if stem and all(stem != existing for existing, _ in stem_pairs):
                    stem_pairs.append((stem, concat))

        tlds = [".de", ".com", ".eu", ".net"]
        tried = set()
        full = normalize(company)
        best_url = None
        best_score = 0
        for stem, concat in stem_pairs:
            if deadline is not None and time.monotonic() >= deadline:
                break
            for tld in tlds:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                dom = stem + tld
                if dom in tried or len(stem) < 2:
                    continue
                tried.add(dom)
                if len(tried) > 16:
                    break
                url = f"https://www.{dom}"
                html = self._probe(url)
                if not html:
                    continue
                text_norm = normalize(soup_of(html).get_text(" ", strip=True))
                if concat and (concat in text_norm or full in text_norm):
                    score = 10
                else:
                    score = sum(1 for t in tokens if t in text_norm)
                    if stem in text_norm: score += 0.5
                if score > best_score:
                    best_score = score
                    best_url = url
                    if score >= 10:
                        return self._homepage(best_url)
        if best_url and best_score >= 1:
            return self._homepage(best_url)
        return None

    @staticmethod
    def _transliterate(name):
        """Keep German umlauts as ae/oe/ue for domain guessing."""
        value = (name or "").lower()
        for source, target in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
            value = value.replace(source, target)
        return value

    def _probe(self, url):
        try:
            r = self.http.session.get(url, timeout=6, allow_redirects=True,
                headers={"User-Agent": self.http.session.headers.get("User-Agent", "")})
            if r.status_code != 200:
                return None
            return r.text
        except Exception:
            return None

    def _bing(self, query_company, city):
        q = f'"{query_company}"' + (f" {city}" if city else "")
        html = self.http.get_text(
            f"https://www.bing.com/search?q={quote_plus(q)}&setlang=de",
            timeout=12, attempts=1,
        )
        if not html: return []
        soup = soup_of(html)
        out = []
        for a in soup.select("li.b_algo h2 a"):
            real = self._unwrap_bing(a.get("href", ""))
            if real and real not in out:
                out.append(real)
        return out

    @staticmethod
    def _unwrap_bing(href):
        if not href: return None
        if "bing.com/ck/a" in href:
            from urllib.parse import urlparse, parse_qs
            import base64
            u = parse_qs(urlparse(href).query).get("u", [""])[0]
            if u.startswith("a1"):
                b = u[2:]
                b += "=" * (-len(b) % 4)
                try:
                    return base64.urlsafe_b64decode(b).decode("utf-8", "ignore")
                except Exception:
                    return None
            return None
        return href if href.startswith("http") else None

    def _ddg(self, query_company, city):
        q = f'"{query_company}"' + (f" {city}" if city else "")
        html = self.http.get_text(
            f"https://html.duckduckgo.com/html/?q={quote_plus(q)}",
            timeout=12, attempts=1,
        )
        if not html: return []
        soup = soup_of(html)
        out = []
        for a in soup.select("a.result__a"):
            real = self._unwrap_ddg(a.get("href", ""))
            if real and real not in out:
                out.append(real)
        return out

    @staticmethod
    def _unwrap_ddg(href):
        if not href: return None
        if href.startswith("http") and "duckduckgo.com" not in href:
            return href
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            from urllib.parse import unquote
            return unquote(m.group(1))
        return None

    @staticmethod
    def _homepage(url):
        from urllib.parse import urlparse
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return None
        return f"{p.scheme}://{p.netloc}"

    @staticmethod
    def _keywords(name):
        stop = LEGAL_FORMS | {
            "deutschland", "berlin", "hamburg", "munchen", "muenchen",
            "koeln", "cologne", "frankfurt", "group", "holding", "service",
            "gmbh co", "gmbh co kg", "gmbh co kgaa", "international",
        }
        cleaned = normalize(name).replace(".", " ").replace(",", " ")
        words = [w for w in cleaned.split() if len(w) >= 4]
        return [w for w in words if w not in stop][:5]

    def find_emails(self, company, city="", website=None, phone=False):
        result = {"website": website or "", "email": "", "emails": [], "phone": ""}

        if not website:
            website = self.find_website(company, city)
            if not website:
                return result
            result["website"] = website

        found = []
        visited = set()
        to_visit = [website]

        while to_visit and len(visited) < self.max_pages:
            page = to_visit.pop(0).split("#")[0]
            if page in visited:
                continue
            visited.add(page)
            html = self.http.get_text(page)
            if not html:
                continue

            data = extract_emails_from_html(html, base_url=page)
            found += data["emails"]

            for link in data["contact_links"]:
                if link not in visited and link not in to_visit:
                    to_visit.append(link)

            if not found:
                cont = self._find_contact_link(html, page)
                if cont and cont not in visited and cont not in to_visit:
                    to_visit.append(cont)

        found = clean_emails(found)
        result["emails"] = found
        result["email"] = best_email(found) or ""
        if phone:
            html = self.http.get_text(website) or ""
            result["phone"] = self._extract_phone(html)
        return result

    @staticmethod
    def _find_contact_link(html, base_url):
        soup = soup_of(html)
        for a in soup.find_all("a", href=True):
            txt = (a.get_text(" ", strip=True) or "").lower()
            href = a["href"].lower()
            if any(k in txt or k in href for k in (
                "kontakt", "contact", "impressum", "über uns", "ueber uns"
            )):
                url = urljoin(base_url, a["href"])
                if url.startswith("http"):
                    return url
        return None

    @staticmethod
    def _extract_phone(html):
        soup = soup_of(html)
        text = soup.get_text(" ", strip=True)
        m = PHONE_RE.search(text)
        return m.group(0).strip() if m else ""
