# iOrganizer

**A private, local-first personal information hub** for collecting useful items from calendars, chat exports, and quick notes, then turning them into searchable, actionable information.

This repository is the capstone project for **System Design and Agentic AI**. It includes the requirements, LLD, diagrams, API and database design, and a modular working MVP. The MVP runs locally and uses free, open-source software only.

## Problem and solution

Personal commitments and useful information are scattered across calendars, conversations, and notes. iOrganizer brings user-authorized sources into one searchable inbox. Small agents extract candidate tasks/events, suggest categories and due dates, and let the user review results before saving them. The user remains in control: there is no background scanning of the device, and nothing is sent to a paid hosted AI service.

## MVP scope

- Create, view, update, search, and archive organizer items.
- Add a quick note or task through the API.
- Import `.ics` calendar files and plain-text chat exports through explicit upload endpoints.
- Run a deterministic, explainable extraction pipeline; review suggestions before accepting them.
- Store data in local SQLite. Keep source, provenance, and processing status with every imported item.
- Export/backup can be added without changing the source adapter interfaces.

The initial release does not connect to phone operating-system internals or scrape private apps. Direct integrations require supported public APIs, user-granted authorization, and platform-specific setup. Chat export import is the free, demonstrable path for the capstone.

## Free stack

- Python 3.11+, FastAPI, Pydantic, SQLAlchemy, SQLite
- pytest for tests; Mermaid for diagrams
- Optional local model: Ollama (no hosted model required). The MVP agent strategy is rule-based and works offline without a model.
- No paid APIs, subscriptions, or proprietary cloud services are required.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn iorganizer.main:app --reload --port 8765
```

Open `http://127.0.0.1:8765/` for the web app or `http://127.0.0.1:8765/docs` for the interactive API. The SQLite database is created at `./data/iorganizer.db` by default. Keep the app bound to localhost; imported calendar/chat content can be sensitive.

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
4. Accept a suggestion with `POST /api/v1/suggestions/{id}/accept`.
5. Search/filter the unified inbox with `GET /api/v1/items`.

See the docs for requirements traceability and design justification.
