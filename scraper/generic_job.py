# -*- coding: utf-8 -*-
"""Appended generic extraction job for non-AA sources."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .leads import normalise_firma, valid_emails


def _generic_collect(params, job):
    source = params.get("source")
    if source == "google_maps":
        from scraper import google_maps as gm
        leads = []
        locations = params.get("locations") or [""]
        for position, loc in enumerate(locations, start=1):
            job.set_active_location(loc, position, len(locations))
            job.add_log("info", f"→ Google Maps · {loc or 'Deutschlandweit'}")
            res = gm.places_text_search(params.get("keyword", ""), loc, api_key=params.get("api_key", ""), max_results=None, log=lambda m: job.add_log("info", m))
            leads.extend(res)
            job.complete_location(loc, len(res))
        return leads, "Google Maps"
    if source == "azubiyo":
        from scraper import azubiyo as az
        leads = []
        locations = params.get("locations") or []
        for position, loc in enumerate(locations, start=1):
            job.set_active_location(loc, position, len(locations))
            job.add_log("info", f"→ Azubiyo · {loc}")
            res = az.search(
                params.get("keyword", ""), loc, max_results=None,
                max_pages=params.get("max_pages", 4),
                log=lambda m: job.add_log("info", m),
            )
            leads.extend(res)
            job.complete_location(loc, len(res))
        return leads, "Azubiyo"
    if source == "ausbildung":
        from scraper import ausbildung_de as ad
        keyword = params.get("keyword", "")
        locations = params.get("locations") or []
        if keyword or locations:
            leads = []
            targets = locations or [""]
            for position, loc in enumerate(targets, start=1):
                job.set_active_location(loc, position, len(targets))
                job.add_log("info", f"→ Ausbildung.de · {loc or 'deutschlandweit'}")
                res = ad.search(keyword, loc, max_results=None, log=lambda m: job.add_log("info", m))
                leads.extend(res)
                job.complete_location(loc, len(res))
            try:
                ad.attach_websites(leads, log=lambda m: job.add_log("info", m))
            except Exception as exc:
                job.add_log(
                    "warn",
                    f"Ausbildung.de: Website-Ermittlung übersprungen ({type(exc).__name__}).",
                )
            return leads, "Ausbildung.de"
        job.set_active_location("", 1, 1)
        leads = ad.directory(max_results=params.get("max_results"), log=lambda m: job.add_log("info", m))
        job.complete_location("", len(leads))
        return leads, "Ausbildung.de"
    if source == "stepstone":
        from scraper import stepstone as ss
        leads = []
        locations = params.get("locations") or [""]
        for position, loc in enumerate(locations, start=1):
            job.set_active_location(loc, position, len(locations))
            job.add_log("info", f"→ Stepstone · {loc or 'Deutschlandweit'}")
            res = ss.search(
                params.get("keyword", ""), loc, max_results=None,
                max_pages=params.get("max_pages", 4),
                delay=params.get("search_delay", 2.0),
                log=lambda m: job.add_log("info", m),
            )
            leads.extend(res)
            job.complete_location(loc, len(res))
        return leads, "Stepstone"
    return [], source or "Source"


class GenericExtractionJob:
    def __init__(self, params, output_dir="output"):
        self.params = params
        self.source_label = params.get("source", "source")
        self.status = "running"
        self.total = 0
        self.processed = 0
        self.log = []
        self.rows = []
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.locations_total = 0
        self.locations_completed = 0
        self.current_location = ""
        self.autosave_path = __import__("os").path.join(output_dir, f"{self.source_label}_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def add_log(self, kind, text):
        with self.lock:
            self.log.append((kind, text))

    def request_stop(self):
        self.stop.set()

    def snapshot_log(self):
        with self.lock:
            return list(self.log)

    def snapshot_rows(self):
        with self.lock:
            return [row for row in self.rows if row is not None]

    def snapshot_progress(self):
        """Return a coherent progress snapshot for the source-specific UI."""
        with self.lock:
            return {
                "total": self.total,
                "processed": self.processed,
                "locations_total": self.locations_total,
                "locations_completed": self.locations_completed,
                "current_location": self.current_location,
            }

    def set_active_location(self, location, position, total):
        with self.lock:
            self.current_location = location or "Deutschlandweit"
            self.locations_total = total
            self.locations_completed = max(0, position - 1)

    def complete_location(self, location, result_count):
        with self.lock:
            self.locations_completed += 1
        self.add_log(
            "info",
            f"✓ {location or 'Deutschlandweit'} · {result_count} Unternehmen gefunden",
        )

    def excel_bytes(self):
        from scraper.leads import export_leads_xlsx
        return export_leads_xlsx(self.snapshot_rows())

    def _save(self):
        try:
            from scraper.leads import export_leads_xlsx
            export_leads_xlsx(self.snapshot_rows(), self.autosave_path)
        except Exception:
            pass

    def _run(self):
        p = self.params
        final = "done"
        try:
            self.add_log("info", "Loading items from all pages ...")
            leads, label = _generic_collect(p, self)
            # map to lead rows
            rows = []
            for le in leads:
                name = le.get("name", "")
                street = le.get("street", "")
                zipc = le.get("zip", "")
                city = le.get("city", "")
                adresse = (f"{street} | {zipc} {city}".strip(" |") if street else (f"{zipc} {city}".strip()))
                row = {
                    "firma": normalise_firma(name),
                    "person": "Damen und Herren",
                    "adresse": adresse,
                    "email": "",
                    "emails": [],
                    "telefonnummer": le.get("phone", "") or "",
                    "link": le.get("url", "") or "",
                    "beginn": le.get("start_date", "") or "",
                    "source": le.get("source", label),
                    "website": le.get("website", "") or "",
                    "city": city,
                }
                rows.append(row)
            unique_rows = []
            seen_keys = set()
            for row in rows:
                key = (row.get("link") or "").strip().lower()
                if not key:
                    key = (row["firma"], row["adresse"])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                unique_rows.append(row)
            rows = unique_rows
            with self.lock:
                self.total = len(rows)
            self.add_log("info", f"Total Items: ({self.total})")
            # email finding
            from scraper.leads import WebsiteEmailFinder, Stopped
            from scraper.email_finder import EmailFinder
            from scraper.utils import SafeSession, is_blocked_url

            email_max_pages = int(p.get("email_max_pages", p.get("max_pages", 4)))
            email_timeout = float(p.get("email_timeout", 10))
            email_max_seconds = float(p.get("email_max_seconds", 60))
            email_max_seconds = max(10.0, min(240.0, email_max_seconds))
            delay = float(p.get("delay", 0.5))
            # Google Maps only publishes the website that the business itself
            # registered.  Guessing domains for the rest is slow and produces
            # unrelated sites (e.g. "Hôtel Atlas" -> atlas.com), especially
            # outside Germany where .de guessing does not apply.
            guess_websites = bool(p.get("guess_websites", p.get("source") != "google_maps"))
            missing_websites = sum(1 for row in rows if not (row.get("website") or "").strip())
            if missing_websites:
                if guess_websites:
                    self.add_log("info", f"· {missing_websites} Einträge ohne Website: Website wird gesucht …")
                else:
                    self.add_log(
                        "info",
                        f"· {missing_websites} Einträge ohne Website: keine Website-Suche "
                        "(Google Maps liefert keine Website für diese Firmen).",
                    )

            def make_finder():
                http = SafeSession()
                http.throttle.delay = delay
                return WebsiteEmailFinder(
                    session=http, max_pages=email_max_pages,
                    log=lambda m: None, stop=self.stop,
                    timeout=email_timeout, max_seconds=email_max_seconds,
                )

            def finish_row(index, row, emails, person, timed_out=False):
                row["person"] = person or "Damen und Herren"
                row["emails"] = emails
                row["email"] = "; ".join(emails)
                self.rows[index] = row
                with self.lock:
                    self.processed += 1
                if emails:
                    self.add_log("ok", f"({self.processed}) {row['firma']} · {len(emails)} email(s)")
                elif not row.get("website"):
                    self.add_log("info", f"({self.processed}) {row['firma']} · keine Website")
                elif timed_out:
                    self.add_log(
                        "ko",
                        f"({self.processed}) {row['firma']} · Zeitlimit erreicht, keine E-Mail",
                    )
                else:
                    self.add_log("ko", f"({self.processed}) {row['firma']} · keine E-Mail gefunden")
                if self.processed % 10 == 0:
                    self._save()

            def extract(row, finder, legacy):
                started = time.monotonic()
                website = (row.get("website") or "").strip()
                if website and is_blocked_url(website):
                    # Booking portals and social pages are not the company's
                    # own contact page; crawling them would attribute a
                    # portal address to the hotel.
                    website = ""
                    row["website"] = ""
                if not website and guess_websites and legacy is not None:
                    try:
                        website = legacy.find_website(row["firma"], row.get("city", "")) or ""
                    except Exception:
                        website = ""
                    row["website"] = website
                emails = []
                person = "Damen und Herren"
                timed_out = False
                if website:
                    # The total budget includes the website lookup above, so a
                    # company that cannot be checked is skipped after ~60s
                    # instead of wasting minutes on dead pages.
                    remaining = max(5.0, email_max_seconds - (time.monotonic() - started))
                    finder.max_seconds = remaining
                    try:
                        res = finder.find(website)
                        # Keep every valid address found on the company's own
                        # website: not merely the highest-ranked job email.
                        emails = valid_emails(res.get("emails") or res.get("email"))
                        timed_out = bool(res.get("timed_out")) and not emails
                        if res.get("person"):
                            person = res["person"]
                    except Stopped:
                        raise
                    except Exception:
                        emails = []
                return emails, person, timed_out

            # Large result sets are checked with a small worker pool: one slow
            # company must not hold up the whole run.  Rows without a website
            # still go through the worker when website lookup is enabled, so
            # portal sources keep their previous behaviour.  Rows are
            # preallocated so the export keeps the original source order.
            parallel = (
                len(rows) > 8
                and bool(p.get("parallel_emails", True))
            )
            self.rows = [None] * len(rows)
            if parallel:
                work = []
                for index, row in enumerate(rows):
                    website = (row.get("website") or "").strip()
                    if website and is_blocked_url(website):
                        row["website"] = ""
                        website = ""
                    if website or guess_websites:
                        work.append((index, row))
                    else:
                        finish_row(index, row, [], "Damen und Herren")

                local = threading.local()

                def worker(row):
                    finder = getattr(local, "finder", None)
                    if finder is None:
                        finder = local.finder = make_finder()
                        local.legacy = EmailFinder(
                            session=finder.http,
                            max_pages=email_max_pages,
                            log=lambda m: None,
                            website_timeout=min(20.0, max(5.0, email_max_seconds / 3)),
                        )
                    return extract(row, finder, getattr(local, "legacy", None))

                workers = max(1, min(int(p.get("email_workers", 6)), len(work) or 1))
                if work:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = {pool.submit(worker, row): index for index, row in work}
                        for future in as_completed(futures):
                            index = futures[future]
                            row = rows[index]
                            try:
                                emails, person, timed_out = future.result()
                                finish_row(index, row, emails, person, timed_out)
                            except Stopped:
                                self.add_log("warn", "Extraktion gestoppt.")
                                break
                            except Exception:
                                finish_row(index, row, [], "Damen und Herren")
            else:
                finder = make_finder()
                legacy = EmailFinder(
                    session=finder.http,
                    max_pages=email_max_pages,
                    log=lambda m: None,
                    website_timeout=min(20.0, max(5.0, email_max_seconds / 3)),
                )
                for index, row in enumerate(rows):
                    if self.stop.is_set():
                        raise Exception("stopped")
                    emails, person, timed_out = extract(row, finder, legacy)
                    finish_row(index, row, emails, person, timed_out)
        except Exception as e:
            if "stopped" in str(e).lower() or self.stop.is_set():
                self.add_log("err", "Process stopped by user")
                final = "stopped"
            else:
                self.add_log("err", f"Erreur : {e}")
                final = "error"
        finally:
            self._save()
            self.status = final
