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

`GET /google/status` reports connection, granted Calendar/Gmail/Photos permissions, whether Calendar event creation is authorized, and last sync time. `GET /google/authorize` starts the local OAuth web-server flow; Google redirects to `http://127.0.0.1:8765/oauth/google/callback`. Existing users must re-consent to the Calendar events write scope to create events. OAuth state expires after ten minutes. `POST /google/disconnect` deletes the local Keychain credentials.

The landing page starts Google identity sign-in at `/auth/google`; the existing loopback callback validates a verified Google ID token and creates a signed local session. `/auth/logout` clears that session. Organizer APIs require an authenticated session and return `401` otherwise. The Google account used to connect Gmail and Calendar must match the signed-in identity.

`POST /google/sync` fetches read-only Calendar events (one day past to 60 days ahead). Calendar results are deduplicated by source ID and returned as suggestions.

`GET /google/gmail/scan/status` reports local model readiness and scan progress. `POST /google/gmail/scan` starts or resumes a scan of recent messages, excluding Spam, Trash, and Drafts. The window defaults to three days and can be configured to 1, 3, 7, or 14 days with `PATCH /google/gmail/scan/settings` and `{ "interval_days": 7 }`. That value controls both the Gmail search window and the automatic refresh interval while the app is running; transient failures retry sooner. The local Ollama model selects only security alerts, meetings/evaluations, deadlines, and payments, and extracts summaries and dates. Message IDs and summaries are retained in SQLite; email attachments are not scanned. No email content is sent to a hosted model.

`GET /google/gmail/saved` lists pinned summaries. `POST /suggestions/{id}/pin` and `DELETE /suggestions/{id}/pin` pin/unpin a Gmail summary; `DELETE /google/gmail/saved/{message_id}` unpins from the saved list. `POST /suggestions/{id}/calendar-event` accepts `{"start_at":"2026-11-24T09:00:00-08:00","end_at":null}` for a meeting. If no end is supplied or extracted, the Calendar event defaults to 30 minutes. This action creates a primary-calendar event and a local organizer event with stable deduplication.

`GET /google/gmail/important-senders` lists sender addresses that always appear in Important emails. `POST /google/gmail/important-senders` with `{"sender_email":"person@example.com"}` adds one; matching messages bypass importance filtering and receive local summaries. Gmail search includes all messages from those senders in addition to the regular rolling scan window. `DELETE /google/gmail/important-senders/{sender_email}` removes the rule.

`POST /google/photos/sessions` creates a Google Photos Picker session capped at 10 user-selected items and returns `session_key`, `picker_uri`, and Google's recommended polling interval. Open the picker URI in a browser, then poll `GET /google/photos/sessions/{session_key}`. When `ready` is true, `POST /google/photos/sessions/{session_key}/import` downloads the selected images temporarily, runs local OCR, deletes the remote picker session, and returns reviewable suggestions. Google Photo videos are skipped. Sessions expire after 30 minutes in the local app.

Google client JSON belongs at `secrets/client_secret.json` (ignored by git); OAuth tokens are held in macOS Keychain. APIs must be enabled and the redirect URI must match exactly. OAuth Testing mode can expire refresh tokens after seven days. The current Gmail read-only scope is restricted and can require verification if the application is published beyond personal testing.

## List, dismiss, and accept suggestions

`GET /suggestions` lists current suggestions. `DELETE /suggestions/{suggestion_id}` dismisses a proposal. File, Calendar, and Photos suggestions are process-local; Gmail summaries are backed by SQLite and pending Gmail suggestions are restored on restart.

`POST /suggestions/{suggestion_id}/accept` → `201` item response. Suggestion IDs are single-use; unknown/already accepted IDs return 404.

## Error shape

FastAPI standard error envelope: `{"detail":"..."}` (validation errors may include an array of field issues). Google OAuth credentials are local and ignored by git; no AI API key is required.
