# -*- coding: utf-8 -*-
"""Moteur : collecte multi-sources, dédoublonnage, enrichissement emails, export."""
from . import arbeitsagentur, azubiyo, ausbildung_de, google_maps, stepstone
from .utils import SafeSession, normalize, ensure_output_dir, export_excel, default_output_path
from .email_finder import EmailFinder

SOURCES = {
    "1": "Agentur für Arbeit",
    "2": "AzubiYo",
    "3": "Ausbildung.de",
    "4": "Google Maps",
    "5": "Stepstone",
    "aubi": "Aubi-plus",
    "indeed": "Indeed",
    "linkedin": "Linkedin",
    "xing": "Xing",
}


def collect(sources, keyword="", city="", limit=50, google_maps_api_key="", http=None,
            log=print, veroeffentlichtseit=None, angebotsart=4, radius=25):
    """Lance les scrapers choisis et retourne une liste de leads."""
    http = http or SafeSession()
    leads = []
    per_source = limit

    if "1" in sources:
        log("→ Agentur für Arbeit …")
        leads += arbeitsagentur.search(
            keyword,
            city,
            max_results=per_source,
            log=log,
            veroeffentlichtseit=veroeffentlichtseit,
            angebotsart=angebotsart,
            radius=radius,
        )

    if "2" in sources:
        log("→ AzubiYo …")
        leads += azubiyo.search_city(city, keyword, max_results=min(per_source, 200),
                                     http=http, log=log)

    if "3" in sources:
        log("→ Ausbildung.de (annuaire) …")
        start_letter = None
        if keyword:
            first = normalize(keyword)[:1]
            if first.isalpha():
                start_letter = first
        leads += ausbildung_de.directory(max_results=per_source, http=http, log=log,
                                         start_letter=start_letter)

    if "4" in sources:
        log("→ Google Maps …")
        leads += google_maps.places_text_search(
            keyword, city, api_key=google_maps_api_key, max_results=per_source, log=log
        )

    if "5" in sources:
        log("→ Stepstone …")
        leads += stepstone.search(
            keyword, city, max_results=per_source, http=http, log=log
        )

    # Never relabel another provider's data as Aubi-plus, Indeed, LinkedIn or Xing.
    # These sources require their own approved API/partner integration.
    for source_id in ["aubi", "indeed", "linkedin", "xing"]:
        if source_id in sources:
            log(f"  [!] {SOURCES[source_id]} nécessite une intégration autorisée dédiée.")

    if city and "4" in sources:
        pass  # déjà filtré par ville

    # filtre mot-clé léger (titre/nom) si non vide
    if keyword:
        kw = normalize(keyword)
        keep = []
        for le in leads:
            blob = normalize(le.get("title", "") + " " + le.get("name", ""))
            if kw in blob or le.get("source") == "Jobbörse (Arbeitsagentur)":
                keep.append(le)
        leads = keep or leads

    return leads[:limit] if limit else leads


def dedupe(leads):
    """Fusionne les doublons (même nom + ville)."""
    out = {}
    for le in leads:
        key = (normalize(le.get("name", "")), normalize(le.get("city", "")))
        if key not in out:
            out[key] = dict(le)
            continue
        existing = out[key]
        if le["source"] not in existing["source"]:
            existing["source"] += " + " + le["source"]
        for f in ("street", "zip", "city", "title", "url", "website", "email", "phone"):
            if not existing.get(f) and le.get(f):
                existing[f] = le[f]
    return list(out.values())


def enrich(leads, delay=0.8, max_pages=4, log=print, only_missing=True, serpapi_key=""):
    """Remplit emails (et site/téléphone) pour chaque entreprise.
    Pour Agentur : tente d'abord l'API jobdetails (email réel + captcha), sinon fallback EmailFinder.
    """
    http = SafeSession()
    http.throttle.delay = delay
    finder = EmailFinder(session=http, max_pages=max_pages, log=log, api_key=serpapi_key)

    total = len(leads)
    for i, le in enumerate(leads, 1):
        if only_missing and le.get("email"):
            continue
        name = le.get("name", "")
        city = le.get("city", "")
        log(f"  [{i}/{total}] {name} ({city or '?'}) …")

        # 1) Agentur : email réel via detail API (sans guessing, 100% clean)
        if le.get("source") == "Jobbörse (Arbeitsagentur)" and le.get("url"):
            try:
                b64 = le["url"].rsplit("/",1)[-1]
                contact = arbeitsagentur.fetch_contact(b64, log=log)
                if contact.get("captcha_required"):
                    le["captcha_required"] = True
                    le["captcha_url"] = contact.get("captcha_url","")
                    log(f"      ✦ Captcha requis – veuillez le résoudre dans l'app")
                    continue
                if contact.get("emails"):
                    le["emails"] = contact["emails"]
                    if not le.get("email") and contact["emails"]:
                        le["email"] = contact["emails"][0]
                if contact.get("email") and not le.get("email"):
                    le["email"] = contact["email"]
                if contact.get("website"): le["website"] = contact["website"]
                if contact.get("person"): le["person"] = contact["person"]
                if le.get("email"):
                    log(f"      → {le['email']} (via Jobbörse Detail)")
                    continue
            except Exception as exc:
                log(f"      detail error: {exc}")

        # 2) Fallback : guessing du domaine
        try:
            res = finder.find_emails(name, city, website=le.get("website") or None,
                                     phone=not le.get("phone"))
        except Exception as exc:
            log(f"      erreur : {exc}")
            continue
        if res.get("website") and not le.get("website"):
            le["website"] = res["website"]
        if res.get("emails"):
            le["emails"] = res["emails"]
            if not le.get("email") and res["emails"]:
                le["email"] = res["emails"][0]
        elif res.get("email") and not le.get("email"):
            le["email"] = res["email"]
            le["emails"] = [res["email"]]
        if res.get("phone") and not le.get("phone"):
            le["phone"] = res["phone"]
        allm = ", ".join(le.get("emails", []) or ([le["email"]] if le.get("email") else []))
        log(f"      → {allm or '(pas d email trouvé)'}")
    return leads


def export(leads, path=None, log=print):
    ensure_output_dir()
    path = path or default_output_path()
    export_excel(leads, path)
    log(f"\n✔ Fichier Excel enregistré : {path}")
    return path
