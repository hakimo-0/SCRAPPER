# -*- coding: utf-8 -*-
"""
Ausbildungs-Lead-Scraper
Recherche d'entreprises en Allemagne (Ausbildung) + extraction des emails.

Utilisation :
    python app.py                 -> menu interactif
    python app.py --help          -> options en ligne de commande
"""
import argparse
import csv
import json
import os
import sys

from scraper import engine

CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "google_maps_api_key": "",
    "email_delay": 0.8,
    "max_pages_per_site": 4,
}


def _utf8_console():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def banner():
    print("=" * 62)
    print("   AUSBILDUNGS-LEAD-SCRAPER  —  entreprises + emails (DE)")
    print("=" * 62)


def ask_int(prompt, default):
    try:
        v = input(prompt).strip()
        return int(v) if v else default
    except ValueError:
        return default


def choose_sources():
    print("\nSources disponibles :")
    print("  1. Agentur für Arbeit   (recommandé, riche, gratuit)")
    print("  2. AzubiYo")
    print("  3. Ausbildung.de        (annuaire A-Z)")
    print("  4. Google Maps (Places API)")
    print("  5. Stepstone")
    print("  t. Toutes")
    raw = input("Votre choix (ex: 1,2  ou  t) [1] : ").strip().lower()
    if not raw:
        return ["1"]
    if raw in ("t", "tout", "all"):
        return ["1", "2", "3", "4", "5"]
    return [c for c in raw.replace(" ", "").split(",") if c in {"1", "2", "3", "4", "5"}]


def interactive_collect(cfg):
    sources = choose_sources()
    keyword = input("\nMétier / mot-clé (ex: Elektroniker, Koch, Informatik) [] : ").strip()
    city = input("Ville (ex: Berlin, München, Köln) [] : ").strip()
    if not keyword and not city:
        print("⚠  Donnez au moins un métier ou une ville.")
        return
    limit = ask_int("Nombre max d'entreprises [50] : ", 50)
    enrich_ans = input("Chercher les emails sur les sites web ? (o/n) [o] : ").strip().lower()
    do_enrich = enrich_ans in ("", "o", "oui", "y", "yes")

    print("\n--- Collecte en cours ---")
    leads = engine.collect(sources, keyword, city, limit=limit,
                           google_maps_api_key=cfg.get("google_maps_api_key", ""))
    leads = engine.dedupe(leads)
    print(f"\n{len(leads)} entreprises uniques après dédoublonnage.")

    if do_enrich and leads:
        print("\n--- Recherche des emails (cela peut prendre du temps) ---")
        leads = engine.enrich(leads, delay=cfg.get("email_delay", 0.8),
                              max_pages=cfg.get("max_pages_per_site", 4),
                              serpapi_key="")
        with_mail = sum(1 for l in leads if l.get("email"))
        print(f"\n{with_mail}/{len(leads)} entreprises avec email.")

    engine.export(leads)


def load_leads_from_file(path):
    leads = []
    if path.lower().endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    elif path.lower().endswith(".xls"):
        try:
            import pandas as pd
            df = pd.read_excel(path)
            rows = [list(df.columns)] + df.values.tolist()
            # convert to same handling below by faking workbook path
            header = [str(h or "").strip().lower() for h in rows[0]]
            idx = {name: i for i, name in enumerate(header)}
            leads_local = []
            for row in rows[1:]:
                def g(*names):
                    for n in names:
                        if n in idx and idx[n] < len(row) and row[idx[n]] is not None:
                            return str(row[idx[n]]).strip()
                    return ""
                name = g("entreprise", "name", "firma", "unternehmen")
                if not name:
                    continue
                leads_local.append({
                    "source": "Fichier", "name": name,
                    "city": g("ville", "city", "ort"),
                    "website": g("site web", "website", "url", "webseite"),
                    "street": g("rue", "street", "strasse"),
                    "zip": g("plz", "zip"), "title": "", "email": "",
                    "phone": g("telephone", "phone", "telefon"), "url": "",
                })
            return leads_local
        except Exception:
            print("  [!] .xls requires pandas+xlrd; convert to .xlsx.")
            return []
        if not rows:
            return leads
        header = [str(h or "").strip().lower() for h in rows[0]]
        idx = {name: i for i, name in enumerate(header)}
        for row in rows[1:]:
            def g(*names):
                for n in names:
                    if n in idx and idx[n] < len(row) and row[idx[n]] is not None:
                        return str(row[idx[n]]).strip()
                return ""
            name = g("entreprise", "name", "firma", "unternehmen")
            if not name:
                continue
            leads.append({
                "source": "Fichier", "name": name,
                "city": g("ville", "city", "ort"),
                "website": g("site web", "website", "url", "webseite"),
                "street": g("rue", "street", "strasse"),
                "zip": g("plz", "zip"), "title": "", "email": "",
                "phone": g("telephone", "phone", "telefon"), "url": "",
            })
    else:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=";,")
            except Exception:
                dialect = csv.excel
            reader = csv.DictReader(f, dialect=dialect)
            for row in reader:
                low = {(k or "").strip().lower(): v for k, v in row.items()}
                name = low.get("name") or low.get("entreprise") or low.get("firma")
                if not name:
                    continue
                leads.append({
                    "source": "Fichier", "name": name.strip(),
                    "city": (low.get("city") or low.get("ville") or low.get("ort") or "").strip(),
                    "website": (low.get("website") or low.get("site") or "").strip(),
                    "street": "", "zip": "", "title": "", "email": "", "phone": "", "url": "",
                })
    return leads


def interactive_from_file(cfg):
    path = input("\nChemin du fichier (CSV ou XLSX) : ").strip().strip('"')
    if not os.path.exists(path):
        print("⚠  Fichier introuvable.")
        return
    leads = load_leads_from_file(path)
    if not leads:
        print("⚠  Aucune entreprise reconnue (colonnes attendues: Entreprise, Ville, Site web).")
        return
    print(f"{len(leads)} entreprises chargées. Recherche des emails …")
    leads = engine.enrich(leads, delay=cfg.get("email_delay", 0.8),
                          max_pages=cfg.get("max_pages_per_site", 4),
                          serpapi_key="")
    engine.export(leads)


def interactive_config(cfg):
    print(f"\nGoogle Maps API configurée : {'oui' if cfg.get('google_maps_api_key') else 'non'}")
    key = input("Nouvelle clé Google Maps (laisser vide pour ne pas changer) : ").strip()
    if key:
        cfg["google_maps_api_key"] = key
    cfg["email_delay"] = ask_int(
        f"Délai entre requêtes, en ms [{int(cfg.get('email_delay', 0.8) * 1000)}] : ",
        int(cfg.get("email_delay", 0.8) * 1000)) / 1000.0
    cfg["max_pages_per_site"] = ask_int(
        f"Pages max par site [{cfg.get('max_pages_per_site', 4)}] : ",
        cfg.get("max_pages_per_site", 4))
    save_config(cfg)
    print("✔ Configuration enregistrée.")


def interactive():
    cfg = load_config()
    while True:
        banner()
        print("  1. Nouvelle collecte (scraping + emails)")
        print("  2. Extraire les emails depuis un fichier (CSV/XLSX)")
        print("  3. Configuration (clé SerpAPI, délai)")
        print("  0. Quitter")
        choice = input("\nVotre choix [1] : ").strip() or "1"
        try:
            if choice == "1":
                interactive_collect(cfg)
            elif choice == "2":
                interactive_from_file(cfg)
            elif choice == "3":
                interactive_config(cfg)
            elif choice == "0":
                break
        except KeyboardInterrupt:
            print("\nInterrompu.")
        input("\nAppuyez sur Entrée pour revenir au menu …")
        os.system("cls" if os.name == "nt" else "clear")


def cli():
    p = argparse.ArgumentParser(description="Ausbildungs-Lead-Scraper")
    p.add_argument("--sources", default="1",
                   help="Sources: 1=Agentur, 2=AzubiYo, 3=Ausbildung.de, 4=Maps, 5=Stepstone (ex: 1,2)")
    p.add_argument("--keyword", default="", help="Métier / mot-clé")
    p.add_argument("--city", default="", help="Ville")
    p.add_argument("--limit", type=int, default=50, help="Nombre max d'entreprises")
    p.add_argument("--no-enrich", action="store_true", help="Ne pas chercher les emails")
    p.add_argument("--from-file", default="", help="Fichier CSV/XLSX d'entreprises")
    p.add_argument("--out", default="", help="Chemin du fichier Excel de sortie")
    args = p.parse_args()

    cfg = load_config()
    if args.from_file:
        leads = load_leads_from_file(args.from_file)
        leads = engine.dedupe(leads)
        if not args.no_enrich:
            leads = engine.enrich(leads, delay=cfg.get("email_delay", 0.8),
                                   max_pages=cfg.get("max_pages_per_site", 4),
                                   serpapi_key="")
    else:
        sources = [c for c in args.sources.replace(" ", "").split(",") if c in "12345"]
        leads = engine.collect(sources, args.keyword, args.city, limit=args.limit,
                               google_maps_api_key=cfg.get("google_maps_api_key", ""))
        leads = engine.dedupe(leads)
        if not args.no_enrich:
            leads = engine.enrich(leads, delay=cfg.get("email_delay", 0.8),
                                   max_pages=cfg.get("max_pages_per_site", 4),
                                   serpapi_key="")
    engine.export(leads, path=args.out or None)


if __name__ == "__main__":
    _utf8_console()
    if len(sys.argv) > 1:
        cli()
    else:
        interactive()
