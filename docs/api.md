# API contract (v1)

Base URL: `http://127.0.0.1:8000/api/v1`. JSON UTF-8 unless noted. Interactive OpenAPI: `/docs`.

## Create item

`POST /items`

```json
{"title":"Submit project proposal","kind":"task","body":"Use the course rubric","category":"study","due_at":"2026-11-12T18:00:00Z"}
```

201 response: `{"id":"uuid","title":"Submit project proposal","kind":"task","body":"Use the course rubric","source":"manual","source_ref":null,"category":"study","due_at":null,"status":"open"}`

`kind` is one of `note|task|event|link`. Empty title / invalid enum / overlong fields return 422.

## List/search

`GET /items?q=proposal` → `200` array of item responses. `q` searches title and body, case-insensitively.

## Get/delete

`GET /items/{uuid}` → `200` item or `404`.

`PATCH /items/{uuid}` accepts any subset of `title`, `body`, `category`, `due_at`, and `status`; status is `open|done|archived`. It returns the updated item or 404. A blank title returns 422.

`DELETE /items/{uuid}` → `204` empty body or `404`.

## Import a file

`POST /imports` with `multipart/form-data`, field `file`; `.txt` and `.ics`, maximum 1 MB. Text imports must be UTF-8. `201` response is an array of suggestions: `[{"id":1,"title":"...","body":"...","kind":"task","category":"personal","source_ref":"export.txt:line:2"}]`. Calendar VEVENTs become `event` suggestions with start time and UID provenance. Unsupported format: 415; too large: 413; invalid encoding/calendar: 400.

## Accept suggestion

`POST /suggestions/{suggestion_id}/accept` → `201` item response. Suggestion IDs are single-use and currently process-local; unknown/already accepted IDs return 404.

## Error shape

FastAPI standard error envelope: `{"detail":"..."}` (validation errors may include an array of field issues). No credentials or third-party API keys are used in the MVP.
