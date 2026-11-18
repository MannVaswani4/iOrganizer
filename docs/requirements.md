# Requirements and use cases

## Product goal

Collect user-selected information from scattered sources into one private, searchable inbox. Agent-generated results remain suggestions until the user accepts them.

## Actors

| Actor | Role |
|---|---|
| Organizer user | Creates items, imports files, searches, reviews and accepts suggestions |
| Source file | User-selected `.txt` chat export or `.ics` calendar file; treated as untrusted input |
| Import agent | Extracts candidate tasks and categories; cannot write items directly |
| Repository | Persists and retrieves organizer items |

## Functional requirements

| ID | Requirement | Acceptance evidence |
|---|---|---|
| FR-1 | Create a note, task, event or link with title, body, category and optional due/event time | `POST /api/v1/items` |
| FR-2 | List and search saved items by title/body | `GET /api/v1/items?q=...` |
| FR-3 | Retrieve and delete an item by ID | Item GET and DELETE endpoints |
| FR-3a | Update item fields and lifecycle status | `PATCH /api/v1/items/{id}` |
| FR-4 | Import a user-selected text, iCalendar, or photo file | `POST /api/v1/imports` |
| FR-5 | Extract task-like lines and OCR text into reviewable suggestions | RuleBasedExtractor, local Tesseract OCR, and import response |
| FR-6 | Accept a suggestion into the inbox | `POST /api/v1/suggestions/{id}/accept` |
| FR-7 | Store accepted items in local SQLite | SQLAlchemy repository |
| FR-8 | Preserve source and provenance for imported suggestions | `source_ref` in suggestion response; `source=import` on accepted item |
| FR-9 | Connect Google; read Calendar, Gmail, and Photos Picker sources; request Calendar event write access only for user-triggered event creation | `/api/v1/google/authorize`, `/api/v1/google/sync` |
| FR-10 | Import only Google Photos explicitly selected through Picker; OCR locally | `/api/v1/google/photos/sessions` |
| FR-11 | Deduplicate Google records; refresh Calendar every 30 minutes and Gmail's rolling three-day window every three days while the app is running | Source IDs, `/api/v1/google/status` |
| FR-12 | Scan Gmail messages received in the last three days, excluding Spam/Trash/Drafts, and keep only security, meeting/evaluation, deadline, and payment actions | `/api/v1/google/gmail/scan` |
| FR-13 | Extract concise summary and dates; add meeting suggestions to Google Calendar after a user action | `LocalEmailTriageAgent`, `/api/v1/suggestions/{id}/calendar-event` |
| FR-14 | Pin/unpin important email summaries in a persistent dedicated section | `/api/v1/google/gmail/saved`, `/api/v1/suggestions/{id}/pin` |
| FR-15 | Persist scan checkpoints and message IDs to resume safely without retaining unimportant email bodies | `GmailTriageStore` in local SQLite |

## Non-functional requirements

- **Privacy:** local storage; Gmail read-only and explicit Photos selection; event writes require a user click and a Calendar write scope; email inference uses Ollama on loopback with no hosted model.
- **Cost:** every required runtime component is free/open-source; optional AI runs locally.
- **Maintainability:** depend on repository and extractor abstractions, not concrete implementations.
- **Usability:** OpenAPI docs at `/docs`; imports return suggestions for review.
- **Validation:** constrain title/body sizes and file type, encoding, and size.
- **Reliability:** one accepted suggestion creates one item; duplicate acceptance returns 404.
- **Extensibility:** new connectors and extraction strategies implement interfaces without changing the core service.

## Use cases

1. **Capture manually:** user submits an item; API validates it; service creates domain entity; repository persists it.
2. **Collect from export:** user uploads a file; import boundary checks size/type/encoding; extractor proposes candidates; user reviews and accepts selected candidate(s).
3. **Find information:** user searches; service delegates to repository; matching items return newest first.
4. **Remove item:** user deletes by identifier; repository reports whether an item existed.
5. **Connect Google:** user consents to Calendar/Gmail/Photos scopes; OAuth tokens are kept in macOS Keychain; Calendar/Gmail records are fetched locally, deduplicated, and presented for review. Calendar writes happen only on an explicit review action.
6. **Select photos:** user chooses images in Google Photos Picker; the app downloads selected images temporarily, extracts text with local OCR, and presents candidates for review.
7. **Triage Gmail:** local background worker pages through the mailbox; local model selects only security, meeting/evaluation, deadline, or payment actions, extracts relevant dates and concise summaries, and routes them to review. Users may pin summaries or create Calendar events from meeting suggestions.

## Assumptions and scope

This is a single-user local MVP. Google Calendar/Gmail are optional authorized integrations; Google Photos is limited to user-selected Picker items. WhatsApp is imported from an explicitly exported `.txt` file; it is not scraped. The initial text extractor recognizes task markers and returns explainable rules-based candidates. AirDrop remains an alternative path for iPhone photos.

## Edge cases

- Empty or whitespace-only titles: rejected.
- Unknown item/suggestion ID: 404.
- Unsupported extension: 415; oversized upload: 413; invalid UTF-8 or unreadable image: 400.
- Empty/non-actionable import: valid empty suggestion list.
- Repeated acceptance: suggestion is consumed once; second request returns 404.
- Concurrent writes: SQLite transactions serialize write commits; conflicting edits need optimistic versioning if multi-device editing is added.
- Malformed `.ics`: rejected with 400; valid calendar files without VEVENT records return no candidates.
- Ollama unavailable: email scan remains paused and resumable; no message is marked processed until a local model result is produced.
- Gmail pagination/network failure: persist the current page checkpoint, expose scan error, and resume from that page.
