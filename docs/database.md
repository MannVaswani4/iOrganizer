# Database schema

Database: local SQLite (`./data/iorganizer.db`), created automatically. SQLAlchemy persists organizer items, Gmail summaries and scan state, and sender inclusion rules. Import suggestions live in memory for the single-process demo and should move into these tables before multi-worker deployment.

## `items`

| Column | Type | Constraints / meaning |
|---|---|---|
| `id` | VARCHAR(36) | Primary key, UUID |
| `title` | VARCHAR(200) | Not null |
| `kind` | VARCHAR(20) | Not null; note/task/event/link |
| `body` | TEXT | Not null, default empty |
| `due_at` | DATETIME | Optional UTC due/event timestamp |
| `source` | VARCHAR(40) | Not null; manual/import/connector identifier |
| `source_ref` | VARCHAR(300) | Optional original file/event reference |
| `category` | VARCHAR(80) | Not null |
| `status` | VARCHAR(20) | Not null; open/done/archived |
| `created_at` | DATETIME | Not null UTC |
| `updated_at` | DATETIME | Not null UTC |

The class `OrganizerItem` maps one-to-one to `items`. Google identity is held in a signed local session cookie; its signing key and the first verified workspace owner are stored in ignored files under `data/`. The API receives UUID primary keys; imports carry provenance in candidate `source_ref`, which is retained when accepted.

## `gmail_message_triage`

One row per processed Gmail message. The primary key is the Gmail message ID for deduplication and restart safety. `sender_email` supports user-approved sender rules. `is_important` is true for selected security, meeting/evaluation, deadline, and payment actions or always-included sender messages. `title`, `summary`, extracted `due_at`, `event_start_at`, and `event_end_at` contain selected information only; raw body is not stored. `review_state` tracks pending/accepted/dismissed/ignored; `is_pinned` preserves a chosen summary in Important emails. `calendar_event_id` prevents duplicate Calendar creation, and `triage_version` allows rule/model changes to reprocess messages.

## `important_senders`

Stores sender email addresses explicitly chosen by the user for automatic inclusion. Matching messages are summarized locally and appear in Important emails without importance filtering.

## `gmail_scan_state`

Single-row local checkpoint with Gmail `page_token`, whether the initial scan is complete, active scan kind, counters, triage version, configurable interval, last scan timestamp, and last error. A changed classifier version restarts pagination within the configured scan window. Scan state is single-user and keyed by ID 1.

## Indexes and relationships

SQLite creates primary-key indexes on item IDs and Gmail message IDs. Add indexes on `(status, due_at)` and `created_at` when list volume warrants them; current item search uses title/body `LIKE` and is suitable only for small personal datasets. A production extension should add `import_batches(id, source_type, filename, status, created_at)` and `suggestions(id, batch_id FK, candidate_json, accepted_item_id FK nullable, reviewed_at)` with an index on `suggestions.batch_id`. This creates batch composition (one batch has many suggestions) and optional accepted-item association.
