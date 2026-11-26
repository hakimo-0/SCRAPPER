# -*- coding: utf-8 -*-
"""Appended generic extraction job for non-AA sources.

Pause / Reprise : l'état est persisté sur disque (``output/job_states/<source>.json``)
après chaque entreprise traitée.  Si le PC est fermé ou que le processus s'arrête,
le prochain démarrage peut reprendre exactement là où ça s'est arrêté sans perdre
les e-mails déjà trouvés.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import os

from .leads import normalise_firma, valid_emails
from .job_state import save as save_state, load as load_state


def _generic_collect(params, job):
    """Collect source results a city at a time and checkpoint after each city.

    Portal searches can contain hundreds of cities.  The checkpoint is kept as
    raw source leads until collection is complete, so an interruption never
    requires re-running the cities that have already finished.
    """
    def collect_locations(label, locations, fetch):
        locations = list(locations or [""])
        start_at = max(0, min(int(job.collection_next_index or 0), len(locations)))
        leads = list(job.collection_leads)
        job.collection_label = label
        job.locations_total = len(locations)
        job.locations_completed = start_at
        for index in range(start_at, len(locations)):
            if job.stop.is_set() or job.paused.is_set():
                job._persist("pausing" if job.paused.is_set() else "stopped")
                break
            location = locations[index]
            job.set_active_location(location, index + 1, len(locations))
            result = fetch(location)
            leads.extend(result)
            job.collection_leads.extend(result)
            job.collection_next_index = index + 1
            job.complete_location(location, len(result))
            job._persist("collecting")
        return leads, label

    source = params.get("source")
    if source == "google_maps":
        from scraper import google_maps as gm
        terms = [str(term).strip() for term in (params.get("keywords") or [params.get("keyword", "")])]
        terms = list(dict.fromkeys(term for term in terms if term))
        locations = list(params.get("locations") or [""])
        # Every company/keyword is intentionally sent as its own Maps query
        # for every selected city: e.g. 10 brands × 30 cities = 300 searches.
        targets = [(term, location) for term in terms for location in locations]
        start_at = max(0, min(int(job.collection_next_index or 0), len(targets)))
        leads = list(job.collection_leads)
        job.collection_label = "Google Maps"
        job.locations_total = len(targets)
        job.locations_completed = start_at
        for index in range(start_at, len(targets)):
            if job.stop.is_set() or job.paused.is_set():
                job._persist("pausing" if job.paused.is_set() else "stopped")
                break
            term, location = targets[index]
            target_label = f"{term} — {location or 'Deutschlandweit'}"
            job.set_active_location(target_label, index + 1, len(targets))
            job.add_log("info", f"→ Google Maps · {target_label}")

            def record_status(status, message):
                job.collection_statuses.append({
                    "query": target_label,
                    "status": status,
                    "message": message,
                })
                kind = "info" if status == "complete" else "warn"
                job.add_log(kind, f"Google Maps {status}: {target_label} — {message}")

            result = gm.places_text_search(
                term, location, api_key=params.get("api_key", ""), max_results=None,
                log=lambda m: job.add_log("info", m),
                status_callback=record_status,
            )
            leads.extend(result)
            job.collection_leads.extend(result)
            job.collection_next_index = index + 1
            job.complete_location(target_label, len(result))
            job._persist("collecting")
        return leads, "Google Maps"
    if source == "azubiyo":
        from scraper import azubiyo as az

        def fetch(loc):
            job.add_log("info", f"→ Azubiyo · {loc or 'Deutschlandweit'}")
            return az.search(
                params.get("keyword", ""), loc, max_results=None,
                max_pages=params.get("max_pages", 4),
                log=lambda m: job.add_log("info", m),
            )

        return collect_locations("Azubiyo", params.get("locations") or [""], fetch)
    if source == "ausbildung":
        from scraper import ausbildung_de as ad
        keyword = params.get("keyword", "")
        locations = params.get("locations") or []
        if keyword or locations:

            def fetch(loc):
                job.add_log("info", f"→ Ausbildung.de · {loc or 'deutschlandweit'}")
                return ad.search(keyword, loc, max_results=None, log=lambda m: job.add_log("info", m))

            leads, label = collect_locations("Ausbildung.de", locations or [""], fetch)
            if (not job.stop.is_set() and not job.paused.is_set()
                    and job.collection_next_index >= len(locations or [""])):
                try:
                    ad.attach_websites(leads, log=lambda m: job.add_log("info", m))
                    job.collection_leads = list(leads)
                    job._persist("collecting")
                except Exception as exc:
                    job.add_log(
                        "warn",
                        f"Ausbildung.de: Website-Ermittlung übersprungen ({type(exc).__name__}).",
                    )
            return leads, label

        def fetch_directory(_loc):
            job.add_log("info", "→ Ausbildung.de · deutschlandweit")
            return ad.directory(max_results=params.get("max_results"), log=lambda m: job.add_log("info", m))

        return collect_locations("Ausbildung.de", [""], fetch_directory)
    if source == "stepstone":
        from scraper import stepstone as ss

        def fetch(loc):
            job.add_log("info", f"→ Stepstone · {loc or 'Deutschlandweit'}")
            return ss.search(
                params.get("keyword", ""), loc, max_results=None,
                max_pages=params.get("max_pages", 4),
                delay=params.get("search_delay", 2.0),
                log=lambda m: job.add_log("info", m),
            )

        return collect_locations("Stepstone", params.get("locations") or [""], fetch)
    return [], source or "Source"


class GenericExtractionJob:
    def __init__(self, params, output_dir="output", resume=False):
        self.params = params
        self.source_label = params.get("source", "source")
        self.status = "running"          # running | paused | done | stopped | error | collecting | extracting
        self.total = 0
        self.processed = 0
        self.log = []
        self.rows = []
        self.stop = threading.Event()
        self.paused = threading.Event()  # quand set, le worker attend en sauvegardant l'état
        self.lock = threading.Lock()
        self.locations_total = 0
        self.locations_completed = 0
        self.current_location = ""
        self.resume = bool(resume)
        self.autosave_path = os.path.join(output_dir, f"{self.source_label}_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
        self.state_path = os.path.join(output_dir, "job_states", f"{self.source_label}.json")
        # Collection is checkpointed separately from email extraction.  Raw
        # portal leads are retained until the city list has been exhausted.
        self.collection_leads = []
        self.collection_label = self.source_label
        self.collection_next_index = 0
        self.collection_statuses = []
        self._collected = False          # True once rows have been built
        self._state_restored = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def add_log(self, kind, text):
        with self.lock:
            if kind == "warn" and self.log and self.log[-1] == (kind, text):
                return
            self.log.append((kind, text))

    def request_stop(self):
        self.stop.set()
        self.paused.set()                # débloque le worker s'il était en pause
        self._persist("stopped")

    def request_pause(self):
        self.paused.set()
        # Do not mark the in-memory job as paused yet: a parallel worker may
        # still be returning.  The UI only exposes Resume once the checkpoint
        # is completely written by the worker.
        self._persist("pausing")
        self.add_log("info", "Extraktion pausée – état sauvegardé, reprise possible plus tard.")

    def _persist(self, status: str | None = None):
        """Écrit l'état actuel sur disque (rows + progression)."""
        with self.lock:
            status = status or self.status
            data = {
                "source": self.source_label,
                "status": status,
                "params": self.params,
                "total": self.total,
                "processed": self.processed,
                "rows": [r for r in self.rows if r is not None],
                "collection_leads": list(self.collection_leads),
                "collection_label": self.collection_label,
                "collection_next_index": self.collection_next_index,
                "collection_statuses": list(self.collection_statuses),
                "phase": "extracting" if self._collected else "collecting",
                "autosave_path": self.autosave_path,
                "locations_total": self.locations_total,
                "locations_completed": self.locations_completed,
                "current_location": self.current_location,
            }
        try:
            save_state(self.source_label, data)
        except Exception:
            pass

    def _load_existing_state(self):
        """Tente de reprendre un état sauvegardé. Retourne True si reprise effectuée."""
        state = load_state(self.source_label)
        if not state:
            return False
        if state.get("status") == "done":
            return False
        rows = state.get("rows") or []
        # Marque les lignes non terminées comme pending (sécurité si _status absent)
        for row in rows:
            if row.get("_status") not in ("done", "pending"):
                row["_status"] = "pending"
        self.rows = rows
        self.processed = int(state.get("processed", 0))
        self.total = int(state.get("total", len(rows)))
        self.locations_total = int(state.get("locations_total", 0))
        self.locations_completed = int(state.get("locations_completed", 0))
        self.current_location = state.get("current_location", "")
        self.autosave_path = state.get("autosave_path", self.autosave_path)
        self.collection_leads = state.get("collection_leads") or []
        self.collection_label = state.get("collection_label") or self.collection_label
        self.collection_next_index = int(state.get("collection_next_index", 0) or 0)
        self.collection_statuses = state.get("collection_statuses") or []
        if self.source_label == "google_maps" and len(self.collection_statuses) < self.collection_next_index:
            terms = [
                str(term).strip()
                for term in (self.params.get("keywords") or [self.params.get("keyword", "")])
            ]
            locations = list(self.params.get("locations") or [""])
            targets = [(term, location) for term in terms if term for location in locations]
            missing_statuses = targets[len(self.collection_statuses):self.collection_next_index]
            for term, location in missing_statuses:
                self.collection_statuses.append({
                    "query": f"{term} — {location or 'Deutschlandweit'}",
                    "status": "incomplete",
                    "message": "Der vorherige Suchstatus wurde im gespeicherten Zustand nicht erfasst.",
                })
            if missing_statuses:
                self.add_log(
                    "warn",
                    "Für frühere Google-Maps-Suchen fehlt der Status; die Vollständigkeit ist daher unbestätigt.",
                )
        self._collected = state.get("phase") == "extracting" or bool(rows and not self.collection_leads)
        self.status = "running"
        if self._collected:
            self.add_log(
                "info",
                f"Reprise : {self.processed}/{self.total} entreprises déjà traitées, "
                f"{max(0, self.total - self.processed)} restantes.",
            )
        else:
            self.add_log(
                "info",
                f"Reprise de la recherche : {self.locations_completed}/{self.locations_total or '?'} villes terminées.",
            )
        return True

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
                "status": self.status,
                "paused": self.paused.is_set(),
                "search_status_counts": {
                    status: sum(1 for item in self.collection_statuses if item.get("status") == status)
                    for status in ("complete", "incomplete", "failed")
                },
                "search_completeness": (
                    "pending" if not self.collection_statuses
                    else "failed" if all(item.get("status") == "failed" for item in self.collection_statuses)
                    else "incomplete" if any(item.get("status") != "complete" for item in self.collection_statuses)
                    else "complete"
                ),
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

    def _save_xlsx(self):
        try:
            from scraper.leads import export_leads_xlsx
            export_leads_xlsx(self.snapshot_rows(), self.autosave_path)
        except Exception:
            pass

    def _run(self):
        p = self.params
        final = "done"
        try:
            # --- Reprise ? -------------------------------------------------
            if self.resume and not self._state_restored:
                resumed = self._load_existing_state()
                self._state_restored = True
            else:
                resumed = False

            if not self._collected:
                if not resumed:
                    self.add_log("info", "Loading items from all pages ...")
                leads, label = _generic_collect(p, self)
                # A pause during collection is a real checkpoint, not an
                # empty half-job.  Rows are built only after all chosen cities
                # have been collected, then email extraction starts.
                if self.stop.is_set():
                    final = "stopped"
                    return
                if self.paused.is_set():
                    final = "paused"
                    return
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
                        "_status": "pending",
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
                    self.rows = rows
                    self.total = len(rows)
                    self.processed = 0
                self.add_log("info", f"Total Items: ({self.total})")
                self._collected = True
                self.status = "running"
                self._persist("extracting")

            # --- e-mail finding (avec reprise ligne par ligne) -------------
            from scraper.leads import WebsiteEmailFinder, Stopped
            from scraper.email_finder import EmailFinder
            from scraper.utils import SafeSession, is_blocked_url

            email_max_pages = int(p.get("email_max_pages", p.get("max_pages", 4)))
            email_timeout = float(p.get("email_timeout", 10))
            email_max_seconds = float(p.get("email_max_seconds", 60))
            email_max_seconds = max(10.0, min(240.0, email_max_seconds))
            delay = float(p.get("delay", 0.5))
            # Google Maps often has the company name and phone but no website.
            # Treat it like the portal sources by default: resolve the company
            # website with its city, then inspect only that company's domain.
            # Users can still opt out for a faster Maps-only run.
            guess_websites = bool(p.get("guess_websites", True))

            def make_finder():
                http = SafeSession()
                http.throttle.delay = delay
                return WebsiteEmailFinder(
                    session=http, max_pages=email_max_pages,
                    log=lambda m: None, stop=self.stop, pause=self.paused,
                    timeout=email_timeout, max_seconds=email_max_seconds,
                )

            def finish_row(index, row, emails, person, timed_out=False):
                row["person"] = person or "Damen und Herren"
                row["emails"] = emails
                row["email"] = "; ".join(emails)
                row["_status"] = "done"
                self.rows[index] = row
                with self.lock:
                    self.processed += 1
                if emails:
                    self.add_log("ok", f"({self.processed}) {row['firma']} · {len(emails)} email(s)")
                elif not row.get("website"):
                    self.add_log("info", f"({self.processed}) {row['firma']} · keine Website")
                elif timed_out:
                    self.add_log(
                        "warn",
                        f"({self.processed}) {row['firma']} · Zeitlimit erreicht, keine E-Mail",
                    )
                else:
                    self.add_log(
                        "warn",
                        f"({self.processed}) {row['firma']} · keine veröffentlichte E-Mail nach Website-Prüfung",
                    )
                self._persist()
                if self.processed % 10 == 0:
                    self._save_xlsx()

            def extract(row, finder, legacy):
                started = time.monotonic()
                website = (row.get("website") or "").strip()
                if website and is_blocked_url(website):
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
                    remaining = max(5.0, email_max_seconds - (time.monotonic() - started))
                    finder.max_seconds = remaining
                    try:
                        res = finder.find(website)
                        emails = valid_emails(res.get("emails") or res.get("email"))
                        timed_out = bool(res.get("timed_out")) and not emails
                        if res.get("person"):
                            person = res["person"]
                    except Stopped:
                        raise
                    except Exception:
                        emails = []
                return emails, person, timed_out

            parallel = (
                len(self.rows) > 8
                and bool(p.get("parallel_emails", True))
            )
            self.rows = list(self.rows)  # s'assurer que c'est une liste mutable
            if parallel:
                work = []
                for index, row in enumerate(self.rows):
                    if row.get("_status") == "done":
                        continue
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
                    pool = ThreadPoolExecutor(max_workers=workers)
                    interrupted = False
                    try:
                        futures = {pool.submit(worker, row): index for index, row in work}
                        for future in as_completed(futures):
                            if self.stop.is_set() or self.paused.is_set():
                                interrupted = True
                                break
                            index = futures[future]
                            row = self.rows[index]
                            try:
                                emails, person, timed_out = future.result()
                                finish_row(index, row, emails, person, timed_out)
                            except Stopped:
                                self.add_log("warn", "Extraktion gestoppt.")
                                interrupted = True
                                break
                            except Exception:
                                finish_row(index, row, [], "Damen und Herren")
                    finally:
                        if interrupted or self.stop.is_set() or self.paused.is_set():
                            for future in locals().get("futures", {}):
                                future.cancel()
                            # A slow website may be in its network timeout.
                            # It does not mutate a row, so checkpoint and let
                            # Resume continue immediately without waiting.
                            pool.shutdown(wait=False, cancel_futures=True)
                        else:
                            pool.shutdown(wait=True)
            else:
                finder = make_finder()
                legacy = EmailFinder(
                    session=finder.http,
                    max_pages=email_max_pages,
                    log=lambda m: None,
                    website_timeout=min(20.0, max(5.0, email_max_seconds / 3)),
                )
                for index, row in enumerate(self.rows):
                    if self.stop.is_set() or self.paused.is_set():
                        break
                    if row.get("_status") == "done":
                        continue
                    emails, person, timed_out = extract(row, finder, legacy)
                    finish_row(index, row, emails, person, timed_out)

            # --- pause / stop après boucle ---------------------------------
            if self.paused.is_set() and not self.stop.is_set():
                self.status = "paused"
                self.add_log("info", "Pause confirmée. Cliquez sur Reprise pour continuer.")
                self._persist("paused")
                final = "paused"
            elif self.stop.is_set():
                self.add_log("err", "Process stopped by user")
                final = "stopped"
            else:
                # Vérifie s'il reste des lignes pending (cas arrêt prématuré)
                pending = [r for r in self.rows if r.get("_status") != "done"]
                if pending:
                    self.status = "paused"
                    self.add_log("info", f"{len(pending)} entreprise(s) restante(s) – reprise possible.")
                    self._persist("paused")
                    final = "paused"
                else:
                    self.status = "done"
                    final = "done"
        except Stopped:
            if self.paused.is_set() and not self.stop.is_set():
                self.add_log("info", "Pause confirmée. Cliquez sur Reprise pour continuer.")
                final = "paused"
            else:
                self.add_log("err", "Process stopped by user")
                final = "stopped"
        except Exception as e:
            self.add_log("err", f"Erreur : {e}")
            final = "error"
        finally:
            self._save_xlsx()
            self._persist(final)
            self.status = final
