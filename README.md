# iOrganizer

**A private, local-first personal information hub** for collecting useful items from calendars, chat exports, and quick notes, then turning them into searchable, actionable information.

Sign in with the Google account that owns the connected sources. The first verified sign-in binds this local workspace to that Google identity. The app requires a signed Google session before serving organizer APIs; use **Sign out** in the app header to end the session.

This repository is the capstone project for **System Design and Agentic AI**. It includes the requirements, LLD, diagrams, API and database design, and a modular working MVP. The MVP runs locally and uses free, open-source software only.

## Problem and solution

Personal commitments and useful information are scattered across calendars, conversations, and notes. iOrganizer brings user-authorized sources into one searchable inbox. Small agents extract candidate tasks/events, suggest categories and due dates, and let the user review results before saving them. The user remains in control: there is no background scanning of the device; Google sync only accesses APIs the user authorized. Email triage uses free local Ollama by default. A deployment can optionally use Cloudflare Workers AI's free daily allocation; when enabled, email text is sent to Cloudflare for classification.

## MVP scope

- Create, view, update, search, and archive organizer items.
- Add a quick note or task through the API.
- Import `.ics` calendar files, plain-text chat exports, and user-selected photos through explicit upload endpoints. Photo text is extracted locally with Tesseract OCR; source images are temporary and are not saved.
- Optionally connect Google Calendar, Gmail, and Google Photos Picker. Calendar event creation requires the Calendar events write scope; Gmail and Photos Picker remain read-only/user-selected.
- Scan Gmail with local Ollama by default, or an optional configured hosted model, for four actionable categories: account security, meetings/evaluations, deadlines, and payments. Extract dates and concise summaries for review; original email bodies are not stored.
- Add meeting suggestions to the primary Google Calendar from review, and pin important email summaries to a persistent Important emails section.
- Run a deterministic, explainable extraction pipeline; review suggestions before accepting them.
- Store data in local SQLite, or PostgreSQL for deployment. Keep source, provenance, and processing status with every imported item.
- Export/backup can be added without changing the source adapter interfaces.

The app does not connect to phone-operating-system internals or scrape private apps. WhatsApp remains a user-exported `.txt` import. Google integration requires enabling the APIs, adding the redirect URI, and consenting in OAuth; local token data is held in macOS Keychain, while deployment tokens are Fernet-encrypted in PostgreSQL. Keep the OAuth JSON local or in the hosting provider's secret file; never commit it.

## Free stack

- Python 3.11+, FastAPI, Pydantic, SQLAlchemy, SQLite, Pillow, and free local Tesseract OCR
- macOS HEIC conversion uses the built-in `sips` tool; on iPhone, AirDrop a selected photo to the Mac for the local demo
- pytest for tests; Mermaid for diagrams
- Ollama runs Gmail triage locally by default; imported chat/photo extraction remains deterministic and explainable. Deployment may select Cloudflare Workers AI.
- The local app needs no paid APIs or subscriptions. Hosting depends on current free-plan quotas and limitations.
- Google API use is subject to Google's quotas and OAuth consent/testing limits. Cloud email classification is optional; local Ollama keeps message content on the machine running the model.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn iorganizer.main:app --reload --port 8765
```

On macOS, install Tesseract if needed with `brew install tesseract`. Open `http://127.0.0.1:8765/` for the web app or `http://127.0.0.1:8765/docs` for the interactive API. The SQLite database is created at `./data/iorganizer.db` by default. Keep the app bound to localhost; imported calendar/chat content can be sensitive.

To enable Google, place the downloaded **Web application** OAuth JSON at `secrets/client_secret.json`, make sure its authorized redirect URI is exactly `http://127.0.0.1:8765/oauth/google/callback`, enable Calendar API, Gmail API, and Photos Picker API in the Cloud project, and add your Google account as an OAuth test user. Restart the server and click **Connect Google**. Install/start the free local email model once:

```bash
brew install ollama
brew services start ollama
ollama pull qwen2.5:1.5b
```

Gmail scans a configurable recent window (1, 3, 7, or 14 days; default 3), resumes from a local SQLite checkpoint, and automatically refreshes on that cadence while iOrganizer is running. Addresses added under **Important emails** also include matching messages in Gmail search, bypass importance filtering, and receive local summaries. Calendar syncs every 30 minutes. If you already connected Google before enabling meeting creation, click **Reconnect Google to enable Calendar events** and approve the updated permission once. In OAuth Testing mode, Google may expire refresh tokens after seven days, requiring reconnection. Google Photos uses a separate Picker session and imports up to 10 selected images per session for local OCR.

## Repository guide

- [`docs/requirements.md`](docs/requirements.md): actors, use cases, functional and non-functional requirements, edge cases
- [`docs/design.md`](docs/design.md): LLD, object model, SOLID, patterns, trade-offs
- [`docs/api.md`](docs/api.md): endpoints and request/response contracts
- [`docs/database.md`](docs/database.md): relational schema, constraints, indexes
- [`docs/diagrams.md`](docs/diagrams.md): class, sequence, and activity diagrams
- `iorganizer/`: API, domain, service, repository, and agent layers
- `static/`: responsive web frontend inspired by the Google Stitch screens

## Evaluation demo path

1. Start the app and create a task with `POST /api/v1/items`.
2. Upload a small `.ics` file or chat text export to `POST /api/v1/imports`.
3. Inspect the proposed items in the import response.
4. Review a photo OCR note or extracted task suggestion, then accept it with `POST /api/v1/suggestions/{id}/accept`.
5. Search/filter the unified inbox with `GET /api/v1/items`.
6. Scan Gmail and review the extracted Security, Meetings/evaluations, Deadlines, and Payments suggestions. Pin messages to Important emails, or add meeting suggestions to Google Calendar.

See the docs for requirements traceability and design justification.

## Free deployment (Render + Neon)

The repository includes a Render Docker Blueprint, PostgreSQL driver, and GitHub Actions schedule. Local Ollama remains the default for development; the hosted app selects the optional Cloudflare Workers AI provider. Render's free web service sleeps when idle and loses local files on restarts, so the deployment uses Neon PostgreSQL for persistent data. The free Render service can take about a minute to wake. Neon currently offers a no-credit-card free plan; check its dashboard for the current quota. These hobby/demo tiers do not provide production reliability guarantees.

1. Create a free Neon project and copy its PostgreSQL connection string. Use the pooled connection string if offered and retain its `sslmode=require` parameter.
2. Create a free Cloudflare account and an API token scoped to Workers AI, then record the account ID. The configured model is `@cf/google/gemma-4-26b-a4b-it`. Workers AI currently includes 10,000 Neurons/day on the Free plan; requests fail after that allocation unless the account is moved to a paid plan. Do not upgrade or add a payment method if you want to keep this setup free.
3. In Google Cloud Console, add `https://<your-render-service>.onrender.com/oauth/google/callback` as an authorized redirect URI on the same Web OAuth client. Keep your Google account as an OAuth test user and enable Calendar, Gmail, and Photos Picker APIs. Download its OAuth client JSON; do not commit it.
4. In Render, create a Blueprint from this GitHub repository and select the free plan. Add `google-oauth.json` as a Secret File at `/etc/secrets/google-oauth.json`. Fill prompted values with the Neon URL, exact Render callback URI, your Google account email, and Cloudflare account ID/token. Generate a Fernet key locally with `python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`. Render generates the session and cron secrets. Keep credentials in provider dashboards; never paste them into chat or commit them.
5. After deployment, copy the `https://...onrender.com` URL. In GitHub repository **Settings → Secrets and variables → Actions**, add `IORGANIZER_URL` (base URL without trailing slash) and `IORGANIZER_CRON_SECRET` (copy from Render's environment). Run **Actions → iOrganizer scheduled sync → Run workflow** once to verify the scheduled-sync endpoint. The job checks daily, but Gmail is scanned only when its user-selected interval is due.
6. Open the Render URL, sign in with the configured Google account, connect Google sources, grant the requested scopes, and let the initial Gmail scan finish. Try a manual note and review item.

If Cloudflare is not configured, set `IORGANIZER_EMAIL_LLM_PROVIDER=ollama` and supply a reachable Ollama host/model; Gmail scanning remains unavailable until that model can be reached. The rest of the organizer can still run. Email text is sent to Cloudflare only when that provider is selected.
