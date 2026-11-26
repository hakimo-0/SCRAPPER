# Ausbildung Lead Finder

Streamlit dashboard for researching Ausbildung offers and businesses from
explicitly selected sources. It supports the public Jobbörse connector,
Azubiyo, Ausbildung.de, Stepstone, and Google Maps data through SerpAPI
(google_maps engine). It does not substitute data from one source and label
it as another. The dashboard also includes a local CRM, analytics, archive,
privacy controls, and an opt-in daily Jobbörse schedule for Windows.
The dashboard language can be selected as Deutsch, العربية, or English at the
top of the page; Arabic uses a right-to-left layout. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the application flow and Google Maps
setup.

## Start

1. Install dependencies: `pip install -r requirements.txt`
2. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`.
3. Add your `SERPAPI_KEY` to that new file (or set the env var).
4. On Windows, double-click `start_app.bat`. It starts the web dashboard and opens it in your default browser.

You can also start it from a terminal with `python -m streamlit run streamlit_app.py`.
Do not run `python streamlit_app.py` directly: that only executes the file and does not start the web server. `app.py` is the separate legacy command-line tool.

The Google key is never placed in exported spreadsheets. Do not commit
`.streamlit/secrets.toml`, `.env`, or `config.json`.

## Source status

- **Jobbörse (Arbeitsagentur):** public job-search connector with offer type,
  publication date, place, and radius filters.
- **Google Maps:** SerpAPI `google_maps` engine; provides name, address, phone,
  website and Maps URL. E-mails come from the company website.
- **Google Maps live extraction:** keeps every valid e-mail found on the company
  website in one Excel cell (separated by `;`), not only the best-ranked one.
  Add multiple terms and cities with `+`; every term/city combination runs in
  one resumable job and duplicate places are removed across searches. The live
  panel reports per-query completeness and is separate from the Jobbörse
  captcha panel.
  The interface also lets you choose how many website pages to inspect per company.
  Website lookup can be disabled; when enabled, the app only accepts a plausible
  company website and does not treat booking portals or social pages as the
  company's contact page. Company websites are checked in parallel (six at a
  time). Google Maps/Places APIs have quotas and costs; this integration uses
  one configured SerpAPI key and does not rotate accounts or bypass provider
  limits. Playwright scraping of Google Maps may violate platform terms and is
  fragile; it is not used by this connector.
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

## CRM, analytics, and privacy

- Import current live results and the local archive into the CRM after entering
  your documented purpose and organization-approved legal basis. Leads, notes,
  statuses, follow-ups, activities, exports, and privacy actions are stored in
  `output/crm.sqlite`.
- CRM statuses include new, contacted, replied, follow-up, closed, and opted
  out. Email syntax and phone plausibility are checked at import; named
  contacts are only extracted when supported by explicit public contact labels.
- Analytics reports source and city counts, email/phone/contact coverage,
  exports, and recently found leads. The existing archive expires after ten
  days; CRM retention defaults to 90 days and can be changed or purged from the
  Privacy tab. Expired CRM records are deleted only after an explicit action.
- The Analytics tab can register a daily Jobbörse search in Windows Task
  Scheduler. It runs under the current Windows user only while that user is
  signed in, imports results using the purpose/legal basis you entered, and
  reports newly found listings. Registration is explicit; the app does not
  store credentials in the scheduled task. Other providers are not scheduled.

## Bewerbungen / email workflow

- The Bewerbungen tab automatically discovers or creates the application
  spreadsheet and Docs template, identifies an application PDF in Drive, and
  syncs eligible CRM leads to the `BEWERBUNG` sheet. Review the selected
  recipients and message, then explicitly confirm either a one-time send or a
  daily Windows Task Scheduler job. CSV and Apps Script remain recovery
  options, not prerequisites for normal use.
- For direct Google Workspace access, configure the optional `[google]` section
  in `.streamlit/secrets.toml` from `.streamlit/secrets.toml.example`. Create an
  OAuth 2.0 Web application client in Google Cloud, authorize the exact local
  redirect URI (`http://localhost:8501/` by default), configure the consent
  screen/test users, and enable the Sheets, Docs, Drive, and Gmail APIs. The app
  requests Google account identity, Sheets/Docs/Drive access, and Gmail send
  permission. These scopes can require additional Google consent-screen review;
  review the requested access before authorizing it. OAuth credentials and
  must not be committed or copied into exports. A refresh token is stored in
  the operating system's credential store (Windows Credential Manager on
  Windows); disconnect removes it.
- The direct integration uses formula-safe Sheets writes, generates a
  personalized Docs PDF, attaches the selected Drive PDF, and sends via Gmail.
  A local CRM ledger tracks `SENDING`, `RETRY`, `SENT`, `ERROR`, and
  `VERIFY_REQUIRED`; transient failures get bounded exponential retries.
  Gmail acceptance is recorded before remote Sheet/CRM finalization so a
  confirmed message is never resent just because status synchronization failed.
  Ambiguous outcomes require verification rather than a blind retry.
- The optional daily Windows task runs as the interactive Windows user, only
  while that user is signed in. It syncs selected CRM leads before sending,
  and sends only the CRM leads and exact email addresses selected when you
  enabled the schedule. The local schedule file stores this recipient snapshot;
  changed, deleted, or opted-out leads are skipped for review. The task follows
  the configured daily limit and delay, and checks its enabled state between
  messages so it can be stopped. Run history is stored locally in
  `output/google_send_runs.sqlite`; send state is in `output/crm.sqlite`.
  Gmail quotas and lawful-contact requirements still apply—daily settings do
  not increase provider quotas.
- A generated Apps Script is available in the collapsed recovery section. It
  preserves explicit start/stop controls, weekday scheduling, duplicate
  prevention, retry states, and PDF attachments. It is an alternative path;
  it does not share the direct app's local CRM send ledger.
- Google actions stay disabled until valid local OAuth credentials, redirect
  URI, enabled APIs, and consent-screen configuration are supplied. No live
  Google operation is claimed or run before the user connects an account.

Only collect and process public business contact data where your intended use
is lawful and permitted by the relevant platform terms. The privacy controls
are operational tools, not legal advice; confirm purpose, legal basis, and
retention with your organization.

## Legal note

Only process business contact data where lawful (GDPR; German UWG §7 restricts
unsolicited e-mail marketing, also B2B). Prefer generic company addresses
(info@, kontakt@, ...), keep the source URL of each record, record opt-outs,
and delete data on request. Before confirming any app or Apps Script send,
review every recipient and message, confirm the contact is lawful, and stay
within the current Gmail quota.
