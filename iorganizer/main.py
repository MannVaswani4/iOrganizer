from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from uuid import UUID

from fastapi import FastAPI, File, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .agents import AgentPipeline, RuleBasedExtractor
from .domain import Candidate, ItemKind, ItemStatus, OrganizerItem
from .email_agent import EmailAgentError, LocalEmailTriageAgent
from .email_store import GmailTriageStore
from .gmail_scanner import GmailMailboxScanner
from .persistence import SqlAlchemyItemRepository, make_database
from .photo_ocr import PhotoOcrError, SUPPORTED_PHOTO_EXTENSIONS, extract_text
from .services import OrganizerService, accept_candidate
from . import google_integrations as google

logger = logging.getLogger(__name__)


class ItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    kind: ItemKind = ItemKind.NOTE
    body: str = Field(default="", max_length=20_000)
    category: str = Field(default="uncategorized", max_length=80)
    due_at: datetime | None = None


class ItemUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=20_000)
    category: str | None = Field(default=None, max_length=80)
    due_at: datetime | None = None
    status: ItemStatus | None = None


class ItemResponse(BaseModel):
    id: UUID
    title: str
    kind: ItemKind
    body: str
    source: str
    source_ref: str | None
    category: str
    due_at: str | None
    status: str

    @classmethod
    def from_item(cls, item: OrganizerItem) -> "ItemResponse":
        return cls(id=item.id, title=item.title, kind=item.kind, body=item.body,
                   source=item.source, source_ref=item.source_ref, category=item.category,
                   due_at=item.due_at.isoformat() if item.due_at else None, status=item.status.value)


class SuggestionResponse(BaseModel):
    id: int
    title: str
    body: str
    kind: ItemKind
    category: str
    source_ref: str | None


sessions, _engine = make_database()
service = OrganizerService(SqlAlchemyItemRepository(sessions), AgentPipeline(RuleBasedExtractor()))
pipeline = AgentPipeline(RuleBasedExtractor())
gmail_store = GmailTriageStore(sessions)
email_agent = LocalEmailTriageAgent()
gmail_scanner = GmailMailboxScanner(gmail_store, email_agent)
oauth_states: dict[str, tuple[float, str]] = {}
photo_sessions: dict[str, tuple[str, float]] = {}
last_google_sync: str | None = None
google_sync_error: str | None = None
suggestion_lock = threading.Lock()
gmail_scan_lock = threading.Lock()
gmail_scan_running = False
gmail_scan_error: str | None = None


def add_candidate_suggestions(candidates: list[Candidate]) -> int:
    global next_suggestion_id
    with suggestion_lock:
        known_refs = {item.source_ref for item in service.list_items() if item.source_ref}
        known_refs.update(candidate.source_ref for candidate in suggestions.values() if candidate.source_ref)
        added = 0
        for candidate in candidates:
            if candidate.source_ref and candidate.source_ref in known_refs:
                continue
            suggestion_id = next_suggestion_id
            next_suggestion_id += 1
            suggestions[suggestion_id] = candidate
            if candidate.source_ref:
                known_refs.add(candidate.source_ref)
            added += 1
        return added


def remove_candidate_suggestions(source_refs: list[str]) -> None:
    if not source_refs:
        return
    refs = set(source_refs)
    with suggestion_lock:
        for suggestion_id, candidate in list(suggestions.items()):
            if candidate.source_ref in refs:
                del suggestions[suggestion_id]


def collect_google_suggestions() -> int:
    global last_google_sync, google_sync_error
    candidates = google.sync_google_sources(pipeline)
    added = add_candidate_suggestions(candidates)
    last_google_sync = datetime.now().astimezone().isoformat()
    google_sync_error = None
    return added


def run_gmail_scan() -> None:
    global gmail_scan_running, gmail_scan_error
    try:
        gmail_scanner.scan(add_candidate_suggestions, remove_candidate_suggestions)
        gmail_scan_error = None
    except Exception as exc:
        gmail_scan_error = str(exc)[:500]
    finally:
        gmail_scan_running = False


def start_gmail_scan() -> bool:
    global gmail_scan_running, gmail_scan_error
    model_status = email_agent.status()
    if not model_status["model_ready"]:
        raise EmailAgentError(model_status["detail"])
    with gmail_scan_lock:
        if gmail_scan_running:
            return False
        state = gmail_store.scan_state()
        gmail_store.save_scan_state(
            page_token=state["page_token"], initial_complete=state["initial_complete"],
            scan_kind=state["scan_kind"], messages_seen=state["messages_seen"],
            messages_classified=state["messages_classified"], important_found=state["important_found"],
        )
        gmail_scan_running = True
        gmail_scan_error = None
        threading.Thread(target=run_gmail_scan, name="iorganizer-gmail-scan", daemon=True).start()
        return True


def restore_gmail_suggestions() -> None:
    add_candidate_suggestions(gmail_store.pending_candidates())


async def auto_google_sync() -> None:
    global google_sync_error
    while True:
        try:
            status = google.connection_status()
            if status.get("connected"):
                if status.get("calendar"):
                    await run_in_threadpool(collect_google_suggestions)
                if status.get("gmail") and email_agent.status()["model_ready"]:
                    start_gmail_scan()
        except Exception as exc:
            google_sync_error = str(exc)[:300]
        await asyncio.sleep(1800)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await run_in_threadpool(restore_gmail_suggestions)
    task = asyncio.create_task(auto_google_sync())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="iOrganizer", version="0.1.0", description="Local-first personal information organizer", lifespan=lifespan)
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


suggestions: dict[int, Candidate] = {}
next_suggestion_id = 1


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/google/status")
def google_status() -> dict:
    return {**google.connection_status(), "last_sync": last_google_sync,
            "sync_error": google_sync_error, "sync_interval_minutes": 30}


@app.get("/api/v1/google/gmail/scan/status")
def gmail_scan_status() -> dict:
    state = gmail_store.scan_state()
    local_model = email_agent.status()
    return {**state, "running": gmail_scan_running,
            "error": gmail_scan_error or state.get("last_error"), "local_model": local_model}


@app.post("/api/v1/google/gmail/scan")
def gmail_scan_start() -> dict:
    connection = google.connection_status()
    if not connection.get("gmail"):
        raise HTTPException(401, "Connect Google and grant Gmail read access first.")
    try:
        started = start_gmail_scan()
    except EmailAgentError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"started": started, **gmail_scan_status()}


@app.get("/api/v1/google/authorize")
def google_authorize():
    try:
        url, state, code_verifier = google.authorization_url()
    except google.GoogleIntegrationError as exc:
        raise HTTPException(503, str(exc)) from exc
    oauth_states[state] = (time.time() + 600, code_verifier)
    return RedirectResponse(url)


@app.get("/oauth/google/callback", include_in_schema=False)
async def google_callback(request: Request):
    state = request.query_params.get("state", "")
    oauth_state = oauth_states.pop(state, None)
    expires, code_verifier = oauth_state if oauth_state else (0, "")
    if not state or expires < time.time():
        raise HTTPException(400, "Google sign-in expired. Return to iOrganizer and connect again.")
    if request.query_params.get("error"):
        return HTMLResponse("<h2>Google connection was not completed.</h2><p>Return to iOrganizer and try again.</p>", status_code=400)
    try:
        flow = google.make_oauth_flow(state=state)
        flow.code_verifier = code_verifier
        try:
            google.exchange_oauth_code(flow, str(request.url))
        except Exception as exc:
            logger.exception("Google OAuth token exchange failed (%s)", type(exc).__name__)
            raise HTTPException(
                502,
                f"Google rejected the sign-in token exchange ({type(exc).__name__}). Start a fresh sign-in; if it repeats, share the server log error without sharing the callback URL.",
            ) from exc
        google.save_credentials(flow.credentials)
        await run_in_threadpool(collect_google_suggestions)
    except google.GoogleIntegrationError as exc:
        raise HTTPException(502, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Google sign-in could not be completed. Check the authorized redirect URI and try again.") from exc
    return RedirectResponse("/")


@app.post("/api/v1/google/disconnect", status_code=204)
def google_disconnect() -> None:
    google.disconnect_google()


@app.post("/api/v1/google/sync")
async def google_sync() -> dict:
    global google_sync_error
    try:
        added = await run_in_threadpool(collect_google_suggestions)
        return {"added": added, "last_sync": last_google_sync}
    except google.GoogleNotConnected as exc:
        raise HTTPException(401, str(exc)) from exc
    except google.GoogleIntegrationError as exc:
        google_sync_error = str(exc)
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/v1/google/photos/sessions")
def google_photos_create() -> dict:
    try:
        result = google.create_photos_session()
    except google.GoogleNotConnected as exc:
        raise HTTPException(401, str(exc)) from exc
    except google.GoogleIntegrationError as exc:
        raise HTTPException(502, str(exc)) from exc
    key = secrets.token_urlsafe(24)
    photo_sessions[key] = (result["session_id"], time.time() + 1800)
    return {"session_key": key, "picker_uri": result["picker_uri"],
            "polling_config": result.get("polling_config", {})}


def get_photo_session(session_key: str) -> str:
    record = photo_sessions.get(session_key)
    if not record or record[1] < time.time():
        photo_sessions.pop(session_key, None)
        raise HTTPException(404, "Photo selection expired. Start a new selection.")
    return record[0]


@app.get("/api/v1/google/photos/sessions/{session_key}")
def google_photos_status(session_key: str) -> dict:
    try:
        result = google.get_photos_session(get_photo_session(session_key))
        return {"ready": result["ready"], "polling_config": result.get("polling_config", {})}
    except google.GoogleNotConnected as exc:
        raise HTTPException(401, str(exc)) from exc
    except google.GoogleIntegrationError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/v1/google/photos/sessions/{session_key}/import")
async def google_photos_import(session_key: str) -> dict:
    global next_suggestion_id
    session_id = get_photo_session(session_key)
    try:
        candidates, processed = await run_in_threadpool(google.import_selected_photos, session_id, pipeline)
    except google.GoogleNotConnected as exc:
        raise HTTPException(401, str(exc)) from exc
    except (google.GoogleIntegrationError, PhotoOcrError) as exc:
        raise HTTPException(502, str(exc)) from exc
    photo_sessions.pop(session_key, None)
    output = []
    for candidate in candidates:
        suggestion_id = next_suggestion_id
        next_suggestion_id += 1
        suggestions[suggestion_id] = candidate
        output.append(SuggestionResponse(id=suggestion_id, title=candidate.title, body=candidate.body,
                                         kind=candidate.kind, category=candidate.category,
                                         source_ref=candidate.source_ref))
    return {"processed": processed, "suggestions": output}


@app.get("/api/v1/suggestions", response_model=list[SuggestionResponse])
def list_suggestions() -> list[SuggestionResponse]:
    return [SuggestionResponse(id=key, title=value.title, body=value.body, kind=value.kind,
                               category=value.category, source_ref=value.source_ref)
            for key, value in suggestions.items()]


@app.delete("/api/v1/suggestions/{suggestion_id}", status_code=204)
def dismiss_suggestion(suggestion_id: int) -> None:
    with suggestion_lock:
        candidate = suggestions.pop(suggestion_id, None)
    if candidate is None:
        raise HTTPException(404, "Suggestion not found or already reviewed")
    if candidate.source_ref and candidate.source_ref.startswith("gmail:"):
        gmail_store.set_review_state(candidate.source_ref.removeprefix("gmail:"), "dismissed")


@app.post("/api/v1/items", response_model=ItemResponse, status_code=201)
def create_item(request: ItemCreate) -> ItemResponse:
    try:
        return ItemResponse.from_item(service.create_item(request.title, request.kind,
                                                          request.body, category=request.category,
                                                          due_at=request.due_at))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/v1/items", response_model=list[ItemResponse])
def list_items(q: str | None = None) -> list[ItemResponse]:
    return [ItemResponse.from_item(item) for item in service.list_items(q)]


@app.get("/api/v1/items/{item_id}", response_model=ItemResponse)
def get_item(item_id: UUID) -> ItemResponse:
    item = service.get_item(item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    return ItemResponse.from_item(item)


@app.patch("/api/v1/items/{item_id}", response_model=ItemResponse)
def update_item(item_id: UUID, request: ItemUpdate) -> ItemResponse:
    try:
        item = service.update_item(item_id, **request.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if item is None:
        raise HTTPException(404, "Item not found")
    return ItemResponse.from_item(item)


@app.delete("/api/v1/items/{item_id}", status_code=204)
def delete_item(item_id: UUID) -> None:
    if not service.delete_item(item_id):
        raise HTTPException(404, "Item not found")


@app.post("/api/v1/imports", response_model=list[SuggestionResponse], status_code=201)
async def import_file(file: UploadFile = File(...)) -> list[SuggestionResponse]:
    global next_suggestion_id
    if not file.filename or len(file.filename) > 255:
        raise HTTPException(400, "A valid filename is required")
    suffix = Path(file.filename).suffix.lower()
    is_photo = suffix in SUPPORTED_PHOTO_EXTENSIONS
    if not (is_photo or suffix in {".txt", ".ics"}):
        raise HTTPException(415, "Use a .txt chat export, .ics calendar, or supported photo file")
    max_bytes = 10_000_000 if is_photo else 1_000_000
    raw = await file.read(max_bytes + 1)
    await file.close()
    if len(raw) > max_bytes:
        raise HTTPException(413, f"File must be at most {max_bytes // 1_000_000} MB")
    try:
        if suffix == ".txt":
            candidates = service.suggest(raw.decode("utf-8-sig"), file.filename)
        elif suffix == ".ics":
            candidates = service.suggest_calendar(raw, file.filename)
        else:
            ocr_text = await run_in_threadpool(extract_text, raw, file.filename)
            candidates = service.suggest_photo_text(ocr_text, file.filename)
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "Chat export must be UTF-8 text") from exc
    except PhotoOcrError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "Invalid iCalendar file") from exc
    output = []
    for candidate in candidates:
        suggestion_id = next_suggestion_id
        next_suggestion_id += 1
        suggestions[suggestion_id] = candidate
        output.append(SuggestionResponse(id=suggestion_id, title=candidate.title, body=candidate.body,
                                         kind=candidate.kind, category=candidate.category,
                                         source_ref=candidate.source_ref))
    return output

@app.post("/api/v1/suggestions/{suggestion_id}/accept", response_model=ItemResponse, status_code=201)
def accept_suggestion(suggestion_id: int) -> ItemResponse:
    with suggestion_lock:
        candidate = suggestions.pop(suggestion_id, None)
    if candidate is None:
        raise HTTPException(404, "Suggestion not found or already reviewed")
    item = accept_candidate(service, candidate)
    if candidate.source_ref and candidate.source_ref.startswith("gmail:"):
        gmail_store.set_review_state(candidate.source_ref.removeprefix("gmail:"), "accepted")
    return ItemResponse.from_item(item)
