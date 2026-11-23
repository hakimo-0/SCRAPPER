# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the project

Install dependencies:
```bash
pip install -r requirements.txt
```

For the live Jobbörse extraction feature (Playwright/browser contacts), also install:
```bash
pip install playwright
playwright install chromium
```

Start the Streamlit dashboard:
```bash
python -m streamlit run streamlit_app.py
```

Run the CLI tool (interactive menu):
```bash
python app.py
```

Run the CLI with arguments:
```bash
python app.py --sources 1,2 --keyword "Fachinformatiker" --city "Berlin" --limit 100
python app.py --sources 5 --keyword "Elektroniker" --city "Berlin" --limit 50
python app.py --from-file leads.xlsx --no-enrich
```

## Configuration

- Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and add `SERPAPI_KEY` (used for Google Maps) — never commit this file.
- Optional: set `JINA_API_KEY` in the environment to raise the Stepstone reader fallback's rate limit.
- `config.json` holds `google_maps_api_key`, `email_delay` (seconds), and `max_pages_per_site` — never commit this either.
- Outputs go to `output/` as `.xlsx` files; the SQLite database is at `output/arbeitsagentur.sqlite`.

## Architecture

There are two separate entry points that share the `scraper/` package but are otherwise independent:

**`streamlit_app.py`** — the primary UI. Renders source cards, per-source settings tabs, runs searches, shows results in a dataframe, and offers CSV/JSON/Excel downloads. Calls `scraper.arbeitsagentur.search_all` (the verified connector) directly, not through `engine.py`. Also exposes a "Live-Extraktion" mode backed by `scraper/aa_live_ui.py` + `scraper/aa_live.py` for the Jobbörse and by `scraper/generic_live_ui.py` + `scraper/generic_job.py` for Google Maps, Azubiyo, Ausbildung.de, and Stepstone.

**`app.py`** — legacy CLI / interactive menu. Orchestrates via `scraper/engine.py`, which is a higher-level facade over the same scrapers.

### `scraper/` package

| Module | Role |
|---|---|
| `arbeitsagentur.py` | Public Jobsuche REST API connector (v6). `search_all()` paginates without a cap, returns a typed `SearchResult` dataclass, and reports `status` as `complete`/`incomplete`/`failed`. `fetch_contact()` is intentionally stubbed (returns empty). |
| `aa_live.py` | Browser-based contact extractor using Playwright. `ExtractionJob` runs in a background thread, uses `BrowserContacts` to open each offer in Chromium, handles captcha relay via `ask_captcha` / `answer_captcha`, and autosaves to Excel every 10 rows. |
| `aa_live_ui.py` | Thin Streamlit adapter for `ExtractionJob`: manages the singleton job in `st.session_state`, renders progress, captcha image, and download button. |
| `azubiyo.py` | HTML connector for azubiyo.de. `search(keyword, city)` uses `/stellenmarkt/` and follows `ResultsMoreByUrl` for further pages. `detail()` reads the offer's JSON-LD address. |
| `ausbildung_de.py` | Ausbildung.de connector. `search(keyword, city)` uses `/suche/`; `directory()` reads the A–Z company directory; `attach_websites()` resolves employer websites and addresses from company/job pages. |
| `stepstone.py` | Stepstone connector. Public result cards (`article[data-testid=job-item]`, 25/page), optional page cap, and automatic Jina Reader fallback when Stepstone returns HTTP 403. `detail()` reads the public JSON-LD record. |
| `google_maps.py` | Google Places Text Search API (New) connector. Reads key from `.streamlit/secrets.toml` or env `SERPAPI_KEY` / `GOOGLE_MAPS_API_KEY`. |
| `generic_job.py` | Background extraction job for Google Maps, Azubiyo, Ausbildung.de, and Stepstone: collects leads, resolves missing websites (except Google Maps, which only uses the website it publishes), extracts every valid e-mail, and autosaves Excel rows. |
| `generic_live_ui.py` | Source-specific Streamlit panel for the generic extraction jobs (one job per source, no shared captcha state). |
| `engine.py` | Used only by `app.py`. Wraps collect → dedupe → enrich (email scraping) → export into one pipeline. |
| `deduplication.py` | `unique_listings()` deduplicates by reference number > URL > (title+name+city+date). Never merges separate vacancies at the same employer. |
| `database.py` | SQLite persistence for Streamlit runs: `save_run()` upserts listings and writes one `search_runs` audit row per call. DB lives at `output/arbeitsagentur.sqlite`. |
| `exporter.py` | `csv_bytes()`, `json_bytes()`, `excel_bytes()` — all return `bytes` for Streamlit download buttons. |
| `search.py` | `SearchConfig` dataclass + `split_locations()` helper (comma/newline → independent per-city queries). |
| `utils.py` | `SafeSession` (requests with throttle), `normalize()` (lowercase + strip accents), Excel export helpers. |
| `email_finder.py` | Domain-guessing website finder used by `engine.enrich()` and `generic_job` when a source does not publish the employer website. It rejects unrelated search results (dictionaries, directories, other companies). |

### Key design rules encoded in the project

- **Source integrity**: results must never be relabelled from one provider to another. Aubi-plus, Indeed, LinkedIn, and Xing connectors are intentionally absent; the UI displays them as "Offizieller Zugang erforderlich."
- **Completeness reporting**: `SearchResult.status` is always set to `complete`, `incomplete`, or `failed` — never silently truncated.
- **No artificial cap on Jobsuche**: `search_all()` pages until the API stops returning results or until `reported_total` is reached.
- **Captcha relay**: the live extraction path relays captcha images to the Streamlit UI thread via `ExtractionJob.ask_captcha` / `answer_captcha` (blocking the worker thread until the user responds).
