# API contract (v1)

Base URL: `http://127.0.0.1:8765/api/v1`. JSON UTF-8 unless noted. Interactive OpenAPI: `/docs`.

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

`POST /imports` with `multipart/form-data`, field `file`; `.txt` and `.ics` up to 1 MB, or user-selected JPG/JPEG/PNG/WebP/BMP/TIFF/HEIC/HEIF photos up to 10 MB. Text imports must be UTF-8. Photos are OCR-processed locally with Tesseract; the original image is temporary and is not retained. `201` response is an array of suggestions: `[{"id":1,"title":"...","body":"...","kind":"task","category":"personal","source_ref":"export.txt:line:2"}]`. Calendar VEVENTs become `event` suggestions with start time and UID provenance. Unsupported format: 415; too large: 413; invalid encoding, image or calendar: 400; OCR engine missing: 503.

## Google account integrations

`GET /google/status` reports connection, granted Calendar/Gmail/Photos permissions, and last sync time. `GET /google/authorize` starts the local OAuth web-server flow; Google redirects to `http://127.0.0.1:8765/oauth/google/callback`. OAuth state expires after ten minutes. `POST /google/disconnect` deletes the local Keychain credentials.

`POST /google/sync` fetches read-only Calendar events (one day past to 60 days ahead) and up to 25 recent Gmail messages matching reminder/task terms. Results are deduplicated by Google source ID and returned as suggestions, never immediately saved. A background task repeats this every 30 minutes while the local server runs. Gmail is limited to the matching search; this is not a full mailbox import.

`POST /google/photos/sessions` creates a Google Photos Picker session capped at 10 user-selected items and returns `session_key`, `picker_uri`, and Google's recommended polling interval. Open the picker URI in a browser, then poll `GET /google/photos/sessions/{session_key}`. When `ready` is true, `POST /google/photos/sessions/{session_key}/import` downloads the selected images temporarily, runs local OCR, deletes the remote picker session, and returns reviewable suggestions. Google Photo videos are skipped. Sessions expire after 30 minutes in the local app.

Google client JSON belongs at `secrets/client_secret.json` (ignored by git); OAuth tokens are held in macOS Keychain. APIs must be enabled and the redirect URI must match exactly. OAuth Testing mode can expire refresh tokens after seven days. The current Gmail read-only scope is restricted and can require verification if the application is published beyond personal testing.

## List, dismiss, and accept suggestions

`GET /suggestions` lists current process-local suggestions. `DELETE /suggestions/{suggestion_id}` dismisses a proposal. Suggestions are held in memory and cleared when the server restarts.

`POST /suggestions/{suggestion_id}/accept` → `201` item response. Suggestion IDs are single-use and currently process-local; unknown/already accepted IDs return 404.

## Error shape

FastAPI standard error envelope: `{"detail":"..."}` (validation errors may include an array of field issues). Google OAuth credentials are local and ignored by git; no AI API key is required.
