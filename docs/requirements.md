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
| FR-4 | Import a user-selected text or iCalendar export | `POST /api/v1/imports` |
| FR-5 | Extract task-like lines into reviewable suggestions | RuleBasedExtractor and import response |
| FR-6 | Accept a suggestion into the inbox | `POST /api/v1/suggestions/{id}/accept` |
| FR-7 | Store accepted items in local SQLite | SQLAlchemy repository |
| FR-8 | Preserve source and provenance for imported suggestions | `source_ref` in suggestion response; `source=import` on accepted item |

## Non-functional requirements

- **Privacy:** local storage, explicit file upload, no network model calls, no background device scanning.
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

## Assumptions and scope

This is a single-user local MVP. Direct phone/calendar/chat integrations are future adapters. They must use official APIs or user-exported data with explicit authorization; scraping or silently scanning personal accounts is out of scope. The initial text extractor recognizes task markers and returns explainable rules-based candidates. The iCalendar adapter converts VEVENT records into event suggestions.

## Edge cases

- Empty or whitespace-only titles: rejected.
- Unknown item/suggestion ID: 404.
- Unsupported extension: 415; oversized upload: 413; invalid UTF-8: 400.
- Empty/non-actionable import: valid empty suggestion list.
- Repeated acceptance: suggestion is consumed once; second request returns 404.
- Concurrent writes: SQLite transactions serialize write commits; conflicting edits need optimistic versioning if multi-device editing is added.
- Malformed `.ics`: rejected with 400; valid calendar files without VEVENT records return no candidates.
