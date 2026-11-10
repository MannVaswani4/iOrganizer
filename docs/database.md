# Database schema

Database: local SQLite (`./data/iorganizer.db`), created automatically. SQLAlchemy model currently persists `items`; import suggestions live in memory for the single-process demo and should move into these tables before multi-worker deployment.

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

The class `OrganizerItem` maps one-to-one to `items`. There is no user table in the local single-user MVP. The API receives UUID primary keys; imports carry provenance in candidate `source_ref`, which is retained when accepted.

## Indexes and relationships

SQLite creates the primary-key index on `id`. Add indexes on `(status, due_at)` and `created_at` when list volume warrants them; current MVP search uses title/body `LIKE` and is suitable only for small personal datasets. A production extension should add `import_batches(id, source_type, filename, status, created_at)` and `suggestions(id, batch_id FK, candidate_json, accepted_item_id FK nullable, reviewed_at)` with an index on `suggestions.batch_id`. This creates batch composition (one batch has many suggestions) and optional accepted-item association.
