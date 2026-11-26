# Ausbildung Lead Finder

Streamlit dashboard for researching Ausbildung offers and businesses from
explicitly selected sources. It supports the public Jobbörse connector,
Azubiyo, Ausbildung.de, Stepstone, and Google Maps data through SerpAPI
(google_maps engine). It does not substitute data from one source and label
it as another.

## Start

1. Install dependencies: `pip install -r requirements.txt`
2. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`.
3. Add your `SERPAPI_KEY` to that new file (or set the env var).
4. On Windows, double-click `start_app.bat`. It starts the web dashboard and opens it in your default browser.

You can also start it from a terminal with `python -m streamlit run streamlit_app.py`.
Do not run `python streamlit_app.py` directly: that only executes the file and does not start the web server. `app.py` is the separate legacy command-line tool.

## Docker on Windows

Install Docker Desktop and make sure it is running. Then double-click
`start_docker.bat`; Docker builds the app and opens it at
`http://127.0.0.1:8501`. The app and Chromium browser run in a container.

The existing `output/` directory and `.streamlit/` settings stay on this PC
through Docker bind mounts. Keep `.streamlit/secrets.toml` private. To rebuild
after changing files in this folder, run `update_docker.bat`.

The source for this app is hosted on the public GitHub branch
`d-old-version`. On another PC, install Docker Desktop and run:

```bat
git clone --branch d-old-version https://github.com/hakimo-0/Web-Scraping-System.git
cd Web-Scraping-System
copy .streamlit\secrets.toml.example .streamlit\secrets.toml
```

Add that PC's own API key to `.streamlit\secrets.toml`, then run
`start_docker.bat`. Later, run `update_docker.bat` to pull the latest code from
`d-old-version` and rebuild. The `output/` data and secrets are local to each
PC and are not uploaded or synchronized by GitHub.

The Google key is never placed in exported spreadsheets. Do not commit
`.streamlit/secrets.toml`, `.env`, or `config.json`.

## Source status

- **Jobbörse (Arbeitsagentur):** public job-search connector with offer type,
  publication date, place, and radius filters.
- **Google Maps:** SerpAPI `google_maps` engine; provides name, address, phone,
  website and Maps URL. E-mails come from the company website.
- **Google Maps live extraction:** keeps every valid e-mail found on the company
  website in one Excel cell (separated by `;`), not only the best-ranked one.
  Add any number of cities with the `+` button; they are searched one after the
  other under the same keyword. The live panel reports the current city and is
  fully separate from the Jobbörse captcha panel.
  The interface also lets you choose how many website pages to inspect per company.
  Only the website listed by Google Maps is used for e-mail extraction. Places
  without a website (common for small hotels outside Germany) are exported with
  address and phone but no invented domain, so the run does not stall on slow
  domain guessing. Booking portals and social pages are not crawled as if they
  were the company's own contact page. Company websites are checked in parallel
  (six at a time), so a full city finishes in minutes rather than hours.
- **Combined live start:** when two or more supported sources are selected, one
  button starts all their e-mail extractions together. Each source still keeps
  its own progress, captcha (Jobbörse only), result panel, and Excel file.
- **Azubiyo:** public `/stellenmarkt/` search by profession, city, and radius.
  Further result pages come from Azubiyo's own `ResultsMoreByUrl` endpoint
  (15 offers per page), so a search is not limited to the first screen.
- **Ausbildung.de:** public `/suche/` search by profession and city (20 cards
  per query). With no keyword/city the connector reads the A–Z company
  directory instead. Employer websites are taken from the company/job pages
  before e-mail extraction; nothing is guessed at this stage.
- **Stepstone:** public result pages (`/jobs/{beruf}/in-{stadt}`), 25 cards per
  page, up to the page limit selected in the UI. Stepstone blocks direct
  requests from some countries (for example Morocco); the connector then
  automatically uses the public Jina Reader proxy with the same HTML. Set
  `JINA_API_KEY` to raise the reader's rate limit. The reader can also be
  blocked; in that case the source reports no results instead of inventing data.
- **Aubi-plus / Indeed / LinkedIn / Xing:** displayed in the dashboard but not
  queried until you provide an approved partner API or written authorization.

Only collect and process public business contact data where your intended use
is lawful and permitted by the relevant platform terms.

## Legal note

Only process business contact data where lawful (GDPR; German UWG §7 restricts
unsolicited e-mail marketing, also B2B). Prefer generic company addresses
(info@, kontakt@, ...), keep the source URL of each record, and provide a way
to delete a contact on request. Never send any e-mail from this tool.
