# iOrganizer

**A private, local-first personal information hub** for collecting useful items from calendars, chat exports, and quick notes, then turning them into searchable, actionable information.

This repository is the capstone project for **System Design and Agentic AI**. It includes the requirements, LLD, diagrams, API and database design, and a modular working MVP. The MVP runs locally and uses free, open-source software only.

## Problem and solution

Personal commitments and useful information are scattered across calendars, conversations, and notes. iOrganizer brings user-authorized sources into one searchable inbox. Small agents extract candidate tasks/events, suggest categories and due dates, and let the user review results before saving them. The user remains in control: there is no background scanning of the device; Google sync only accesses the APIs the user authorized, and nothing is sent to a paid hosted AI service.

## MVP scope

- Create, view, update, search, and archive organizer items.
- Add a quick note or task through the API.
- Import `.ics` calendar files, plain-text chat exports, and user-selected photos through explicit upload endpoints. Photo text is extracted locally with Tesseract OCR; source images are temporary and are not saved.
- Optionally connect Google Calendar, Gmail, and Google Photos Picker with read-only OAuth. Calendar/Gmail suggestions sync while the local server is running; Photos only processes pictures selected in Google's picker.
- Run a deterministic, explainable extraction pipeline; review suggestions before accepting them.
- Store data in local SQLite. Keep source, provenance, and processing status with every imported item.
- Export/backup can be added without changing the source adapter interfaces.

The app does not connect to phone-operating-system internals or scrape private apps. WhatsApp remains a user-exported `.txt` import. Google integration requires enabling the APIs, adding the local redirect URI, and consenting in OAuth; token data is held in macOS Keychain. Keep `secrets/client_secret.json` local and never commit it.

## Free stack

- Python 3.11+, FastAPI, Pydantic, SQLAlchemy, SQLite, Pillow, and free local Tesseract OCR
- macOS HEIC conversion uses the built-in `sips` tool; on iPhone, AirDrop a selected photo to the Mac for the local demo
- pytest for tests; Mermaid for diagrams
- Optional local model: Ollama (no hosted model required). The MVP agent strategy is rule-based and works offline without a model.
- No paid APIs, subscriptions, or proprietary cloud services are required.
- Google API use is subject to Google's quotas and OAuth consent/testing limits; no paid AI or hosted extraction service is used.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn iorganizer.main:app --reload --port 8765
```

On macOS, install Tesseract if needed with `brew install tesseract`. Open `http://127.0.0.1:8765/` for the web app or `http://127.0.0.1:8765/docs` for the interactive API. The SQLite database is created at `./data/iorganizer.db` by default. Keep the app bound to localhost; imported calendar/chat content can be sensitive.

To enable Google, place the downloaded **Web application** OAuth JSON at `secrets/client_secret.json`, make sure its authorized redirect URI is exactly `http://127.0.0.1:8765/oauth/google/callback`, enable Calendar API, Gmail API, and Photos Picker API in the Cloud project, and add your Google account as an OAuth test user. Restart the server and click **Connect Google**. The first Calendar/Gmail sync runs after consent; later syncs run every 30 minutes while this server is running. In OAuth Testing mode, Google may expire refresh tokens after seven days, requiring reconnection. Google Photos uses a separate Picker session and imports up to 10 selected images per session for local OCR.

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

See the docs for requirements traceability and design justification.
