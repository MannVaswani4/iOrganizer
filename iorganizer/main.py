from __future__ import annotations

import asyncio
from email.utils import parseaddr
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import FastAPI, File, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from fastapi.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse
from pydantic import BaseModel, Field
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token
from google_auth_oauthlib.flow import Flow

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
    due_at: str | None = None
    event_start_at: str | None = None
    event_end_at: str | None = None
    is_pinned: bool = False
    sender_email: str | None = None


class CalendarEventRequest(BaseModel):
    start_at: datetime | None = None
    end_at: datetime | None = None


class GmailScanSettingsRequest(BaseModel):
    interval_days: int = Field(ge=1, le=30)


class SavedEmailResponse(BaseModel):
    message_id: str
    title: str
    summary: str
    category: str
    sender_email: str | None = None
    received_at: str | None


class ImportantSenderRequest(BaseModel):
    sender_email: str = Field(min_length=3, max_length=320)


sessions, _engine = make_database()
service = OrganizerService(SqlAlchemyItemRepository(sessions), AgentPipeline(RuleBasedExtractor()))
pipeline = AgentPipeline(RuleBasedExtractor())
gmail_store = GmailTriageStore(sessions)
email_agent = LocalEmailTriageAgent()
gmail_scanner = GmailMailboxScanner(gmail_store, email_agent)
oauth_states: dict[str, tuple[float, str, str]] = {}
photo_sessions: dict[str, tuple[str, float]] = {}
login_oauth_states: dict[str, tuple[float, str, str]] = {}
last_google_sync: str | None = None
google_sync_error: str | None = None
suggestion_lock = threading.Lock()
gmail_scan_lock = threading.Lock()
gmail_scan_running = False
gmail_scan_error: str | None = None
authenticated_session_active = False
active_user_email: str | None = None
calendar_event_lock = threading.Lock()


def suggestion_response(suggestion_id: int, candidate: Candidate) -> SuggestionResponse:
    return SuggestionResponse(
        id=suggestion_id, title=candidate.title, body=candidate.body, kind=candidate.kind,
        category=candidate.category, source_ref=candidate.source_ref,
        due_at=candidate.due_at.isoformat() if candidate.due_at else None,
        event_start_at=candidate.event_start_at.isoformat() if candidate.event_start_at else None,
        event_end_at=candidate.event_end_at.isoformat() if candidate.event_end_at else None,
        is_pinned=candidate.is_pinned,
        sender_email=candidate.sender_email,
    )


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
            status = google.connection_status() if authenticated_session_active else {"connected": False}
            signed_in_email = active_user_email or ""
            if (status.get("connected") and status.get("identity_verified")
                    and status.get("account_email") == signed_in_email):
                now = datetime.now().astimezone()
                last_sync = datetime.fromisoformat(last_google_sync) if last_google_sync else None
                last_sync = (last_sync.replace(tzinfo=now.tzinfo) if last_sync and last_sync.tzinfo is None
                             else last_sync)
                if status.get("calendar") and (last_sync is None or now - last_sync >= timedelta(minutes=30)):
                    await run_in_threadpool(collect_google_suggestions)
                scan_state = gmail_store.scan_state()
                last_scan = datetime.fromisoformat(scan_state["last_scan_at"]) if scan_state["last_scan_at"] else None
                if last_scan and last_scan.tzinfo is None:
                    last_scan = last_scan.replace(tzinfo=timezone.utc)
                retry_after = (timedelta(minutes=30) if scan_state["last_error"] else
                               timedelta(days=scan_state.get("scan_interval_days", 3)))
                scan_due = (not scan_state["initial_complete"] or last_scan is None or
                            now - last_scan >= retry_after)
                if (status.get("gmail") and email_agent.status()["model_ready"] and
                        not gmail_scan_running and scan_due):
                    start_gmail_scan()
        except Exception as exc:
            google_sync_error = str(exc)[:300]
        await asyncio.sleep(300)


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
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SESSION_SECRET_FILE = DATA_DIR / "session_secret"
OWNER_FILE = DATA_DIR / "google_owner.json"


def session_secret() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    configured = os.getenv("IORGANIZER_SESSION_SECRET")
    if configured:
        return configured
    try:
        return SESSION_SECRET_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = secrets.token_urlsafe(48)
        try:
            SESSION_SECRET_FILE.write_text(value, encoding="utf-8")
            SESSION_SECRET_FILE.chmod(0o600)
            return value
        except FileExistsError:
            return SESSION_SECRET_FILE.read_text(encoding="utf-8").strip()


def bind_google_owner(subject: str, email: str) -> bool:
    """Keep this local single-user database bound to its first verified Google identity."""
    owner = {"sub": subject, "email": email.casefold()}
    try:
        saved = json.loads(OWNER_FILE.read_text(encoding="utf-8"))
        return saved.get("sub") == subject
    except FileNotFoundError:
        try:
            OWNER_FILE.write_text(json.dumps(owner), encoding="utf-8")
            OWNER_FILE.chmod(0o600)
            return True
        except FileExistsError:
            saved = json.loads(OWNER_FILE.read_text(encoding="utf-8"))
            return saved.get("sub") == subject


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def require_google_login(request: Request, call_next):
    global authenticated_session_active, active_user_email
    path = request.url.path
    public = (path == "/" or (path.startswith("/static/") and path != "/static/index.html") or path in {
        "/auth/google", "/auth/logout", "/oauth/google/callback", "/api/v1/health"
    })
    if public or request.session.get("google_user"):
        if request.session.get("google_user"):
            authenticated_session_active = True
            active_user_email = request.session["google_user"].get("email", "").casefold()
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Sign in with Google to use iOrganizer."}, status_code=401)
    return RedirectResponse("/", status_code=303)


app.add_middleware(SessionMiddleware, secret_key=session_secret(), session_cookie="iorganizer_session",
                   same_site="lax", https_only=False, max_age=60 * 60 * 24 * 30)


@app.get("/", include_in_schema=False)
def frontend(request: Request) -> FileResponse:
    page = "index.html" if request.session.get("google_user") else "landing.html"
    return FileResponse(STATIC_DIR / page)


@app.get("/auth/google", include_in_schema=False)
def google_login(request: Request):
    if not google.CLIENT_FILE.is_file():
        raise HTTPException(503, "Google OAuth client configuration is missing.")
    try:
        flow = Flow.from_client_secrets_file(str(google.CLIENT_FILE), scopes=list(google.IDENTITY_SCOPES))
        flow.redirect_uri = google.REDIRECT_URI
        nonce = secrets.token_urlsafe(24)
        request.session["login_state"] = nonce
        url, state = flow.authorization_url(access_type="offline", prompt="select_account")
    except Exception as exc:
        raise HTTPException(503, "Could not start Google sign-in.") from exc
    login_oauth_states[state] = (time.time() + 600, flow.code_verifier or "", nonce)
    return RedirectResponse(url)


@app.get("/auth/logout", include_in_schema=False)
def google_logout(request: Request):
    global authenticated_session_active, active_user_email
    request.session.clear()
    authenticated_session_active = False
    active_user_email = None
    return RedirectResponse("/", status_code=303)


suggestions: dict[int, Candidate] = {}
next_suggestion_id = 1


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/auth/me")
def auth_me(request: Request) -> dict:
    return request.session["google_user"]


@app.get("/api/v1/google/status")
def google_status() -> dict:
    scan_state = gmail_store.scan_state()
    return {**google.connection_status(), "last_sync": last_google_sync,
            "sync_error": google_sync_error, "sync_interval_minutes": 30,
            "gmail_scan_interval_days": scan_state.get("scan_interval_days", 3)}


def require_linked_google_account() -> dict:
    status = google.connection_status()
    if (not status.get("identity_verified") or
            status.get("account_email") != (active_user_email or "")):
        raise HTTPException(403, "Reconnect Google and link the same account you used to sign in.")
    return status


@app.patch("/api/v1/google/gmail/scan/settings")
def update_gmail_scan_settings(request: GmailScanSettingsRequest) -> dict:
    gmail_store.set_scan_interval_days(request.interval_days)
    return {"interval_days": request.interval_days}


@app.get("/api/v1/google/gmail/saved", response_model=list[SavedEmailResponse])
def list_saved_emails() -> list[SavedEmailResponse]:
    return [SavedEmailResponse(**record) for record in gmail_store.saved_emails()]


@app.get("/api/v1/google/gmail/important-senders")
def list_important_senders() -> dict:
    return {"senders": gmail_store.important_senders()}


@app.post("/api/v1/google/gmail/important-senders", status_code=201)
def add_important_sender(request: ImportantSenderRequest) -> dict:
    email = request.sender_email.strip().casefold()
    parsed = parseaddr(email)[1]
    if parsed != email or email.count("@") != 1 or any(ch.isspace() for ch in email):
        raise HTTPException(422, "Enter one valid sender email address.")
    gmail_store.add_important_sender(email)
    with suggestion_lock:
        for suggestion_id, candidate in list(suggestions.items()):
            if candidate.sender_email and candidate.sender_email.casefold() == email:
                suggestions.pop(suggestion_id, None)
    restore_gmail_suggestions()
    started = False
    linked = google.connection_status()
    if (linked.get("identity_verified") and linked.get("account_email") == (active_user_email or "")
            and linked.get("gmail") and email_agent.status()["model_ready"]):
        started = start_gmail_scan()
    return {"sender_email": email, "senders": gmail_store.important_senders(), "scan_started": started}


@app.delete("/api/v1/google/gmail/important-senders/{sender_email}", status_code=204)
def remove_important_sender(sender_email: str) -> None:
    gmail_store.remove_important_sender(sender_email)


@app.delete("/api/v1/google/gmail/saved/{message_id}", status_code=204)
def unpin_saved_email(message_id: str) -> None:
    try:
        gmail_store.set_pinned(message_id, False)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/v1/suggestions/{suggestion_id}/pin")
def pin_important_email(suggestion_id: int) -> dict:
    with suggestion_lock:
        candidate = suggestions.get(suggestion_id)
    if candidate is None or not candidate.source_ref or not candidate.source_ref.startswith("gmail:"):
        raise HTTPException(404, "Gmail suggestion not found")
    message_id = candidate.source_ref.removeprefix("gmail:")
    try:
        gmail_store.set_pinned(message_id, True)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    with suggestion_lock:
        current = suggestions.get(suggestion_id)
        if current:
            suggestions[suggestion_id] = replace(current, is_pinned=True)
    return {"pinned": True, "message_id": message_id}


@app.delete("/api/v1/suggestions/{suggestion_id}/pin", status_code=204)
def unpin_important_email(suggestion_id: int) -> None:
    with suggestion_lock:
        candidate = suggestions.get(suggestion_id)
    if candidate is None or not candidate.source_ref or not candidate.source_ref.startswith("gmail:"):
        raise HTTPException(404, "Gmail suggestion not found")
    message_id = candidate.source_ref.removeprefix("gmail:")
    try:
        gmail_store.set_pinned(message_id, False)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    with suggestion_lock:
        current = suggestions.get(suggestion_id)
        if current:
            suggestions[suggestion_id] = replace(current, is_pinned=False)


@app.post("/api/v1/suggestions/{suggestion_id}/calendar-event")
def create_suggestion_calendar_event(suggestion_id: int, request: CalendarEventRequest) -> dict:
    with suggestion_lock:
        candidate = suggestions.get(suggestion_id)
    if (candidate is None or candidate.category != "meeting" or not candidate.source_ref
            or not candidate.source_ref.startswith("gmail:")):
        raise HTTPException(404, "Meeting suggestion not found")
    if not require_linked_google_account().get("calendar_write"):
        raise HTTPException(403, "Enable Google Calendar event creation, then retry.")
    message_id = candidate.source_ref.removeprefix("gmail:")
    start_at = request.start_at or candidate.event_start_at
    if start_at is None:
        raise HTTPException(422, "Add the meeting date and time before creating a calendar event.")
    end_at = request.end_at or candidate.event_end_at
    if start_at.tzinfo is None:
        start_at = start_at.astimezone()
    if end_at and end_at.tzinfo is None:
        end_at = end_at.astimezone()
    if end_at and end_at <= start_at:
        raise HTTPException(422, "Meeting end time must be after its start time.")

    with calendar_event_lock:
        row = gmail_store.get_message(message_id)
        event_id = row.calendar_event_id if row else None
        if not event_id:
            event_id = hashlib.sha256(f"iorganizer:{message_id}".encode()).hexdigest()
            original_email_url = f"https://mail.google.com/mail/u/0/#all/{message_id}"
            event_description = f"{candidate.body}\n\nOriginal source email: {original_email_url}"
            try:
                google.create_calendar_event(event_id=event_id, title=candidate.title,
                                             description=event_description, start_at=start_at, end_at=end_at)
            except google.GoogleNotConnected as exc:
                raise HTTPException(401, str(exc)) from exc
            except google.GoogleIntegrationError as exc:
                raise HTTPException(502, str(exc)) from exc
        source_ref = f"google-calendar:{event_id}"
        item = next((saved for saved in service.list_items() if saved.source_ref == source_ref), None)
        if item is None:
            original_email_url = f"https://mail.google.com/mail/u/0/#all/{message_id}"
            body = f"{candidate.body}\n\nOriginal source email: {original_email_url}"
            calendar_candidate = replace(candidate, body=body, kind=ItemKind.EVENT, due_at=start_at,
                                         event_start_at=start_at, event_end_at=end_at,
                                         source_ref=source_ref)
            item = accept_candidate(service, calendar_candidate)
        gmail_store.set_calendar_event_id(message_id, event_id)
        with suggestion_lock:
            suggestions.pop(suggestion_id, None)
    return {"item": ItemResponse.from_item(item), "calendar_event_id": event_id}


@app.get("/api/v1/google/gmail/scan/status")
def gmail_scan_status() -> dict:
    state = gmail_store.scan_state()
    local_model = email_agent.status()
    return {**state, "running": gmail_scan_running,
            "error": gmail_scan_error or state.get("last_error"), "local_model": local_model}


@app.post("/api/v1/google/gmail/scan")
def gmail_scan_start() -> dict:
    connection = require_linked_google_account()
    if not connection.get("gmail"):
        raise HTTPException(401, "Connect Google and grant Gmail read access first.")
    try:
        started = start_gmail_scan()
    except EmailAgentError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"started": started, **gmail_scan_status()}


@app.get("/api/v1/google/authorize")
def google_authorize(request: Request):
    try:
        url, state, code_verifier = google.authorization_url()
    except google.GoogleIntegrationError as exc:
        raise HTTPException(503, str(exc)) from exc
    oauth_states[state] = (time.time() + 600, code_verifier,
                           (request.session.get("google_user") or {}).get("email", "").casefold())
    return RedirectResponse(url)


@app.get("/oauth/google/callback", include_in_schema=False)
async def google_callback(request: Request):
    global authenticated_session_active, active_user_email
    state = request.query_params.get("state", "")
    login_oauth_state = login_oauth_states.pop(state, None)
    if login_oauth_state:
        expires, code_verifier, expected_state = login_oauth_state
        if expires < time.time():
            raise HTTPException(400, "Google sign-in expired. Return to iOrganizer and try again.")
        if request.session.get("login_state") != expected_state:
            raise HTTPException(400, "Google sign-in state did not match. Start again from iOrganizer.")
        if request.query_params.get("error"):
            return HTMLResponse("<h2>Google sign-in was not completed.</h2><p>Return to iOrganizer and try again.</p>", status_code=400)
        try:
            flow = Flow.from_client_secrets_file(str(google.CLIENT_FILE), scopes=list(google.IDENTITY_SCOPES), state=state)
            flow.redirect_uri = google.REDIRECT_URI
            flow.code_verifier = code_verifier
            google.exchange_oauth_code(flow, str(request.url))
            claims = google_id_token.verify_oauth2_token(
                flow.credentials.id_token, GoogleAuthRequest(), flow.client_config["client_id"]
            )
            if not claims.get("email_verified") or not claims.get("email") or not claims.get("sub"):
                raise ValueError("Google did not return a verified account email.")
            if not bind_google_owner(claims["sub"], claims["email"]):
                return HTMLResponse("<h2>This iOrganizer workspace belongs to a different Google account.</h2>",
                                    status_code=403)
            request.session.clear()
            request.session["google_user"] = {
                "sub": claims["sub"], "email": claims["email"],
                "name": claims.get("name") or claims["email"],
                "picture": claims.get("picture", ""),
            }
            authenticated_session_active = True
            active_user_email = claims["email"].casefold()
            return RedirectResponse("/")
        except Exception as exc:
            logger.exception("Google identity sign-in failed (%s)", type(exc).__name__)
            raise HTTPException(502, "Google sign-in could not be verified. Start again from iOrganizer.") from exc
    oauth_state = oauth_states.pop(state, None)
    expires, code_verifier, expected_email = oauth_state if oauth_state else (0, "", "")
    if not state or expires < time.time():
        raise HTTPException(400, "Google sign-in expired. Return to iOrganizer and connect again.")
    if expected_email != (request.session.get("google_user") or {}).get("email", "").casefold():
        raise HTTPException(400, "Google connection state did not match your signed-in account. Start again.")
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
        try:
            linked_account = google_id_token.verify_oauth2_token(
                flow.credentials.id_token, GoogleAuthRequest(), flow.client_config["client_id"]
            )
        except Exception as exc:
            raise HTTPException(403, "Reconnect Google and grant account identity access to link sources safely.") from exc
        signed_in_email = (request.session.get("google_user") or {}).get("email", "").casefold()
        if linked_account.get("email", "").casefold() != signed_in_email:
            raise HTTPException(403, "Connect the same Google account you used to sign in to iOrganizer.")
        google.save_credentials(flow.credentials, account_email=linked_account.get("email", ""))
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
        require_linked_google_account()
        added = await run_in_threadpool(collect_google_suggestions)
        return {"added": added, "last_sync": last_google_sync}
    except google.GoogleNotConnected as exc:
        raise HTTPException(401, str(exc)) from exc
    except google.GoogleIntegrationError as exc:
        google_sync_error = str(exc)
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/v1/google/photos/sessions")
def google_photos_create() -> dict:
    require_linked_google_account()
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
    require_linked_google_account()
    try:
        result = google.get_photos_session(get_photo_session(session_key))
        return {"ready": result["ready"], "polling_config": result.get("polling_config", {})}
    except google.GoogleNotConnected as exc:
        raise HTTPException(401, str(exc)) from exc
    except google.GoogleIntegrationError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/v1/google/photos/sessions/{session_key}/import")
async def google_photos_import(session_key: str) -> dict:
    require_linked_google_account()
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
        output.append(suggestion_response(suggestion_id, candidate))
    return {"processed": processed, "suggestions": output}


@app.get("/api/v1/suggestions", response_model=list[SuggestionResponse])
def list_suggestions() -> list[SuggestionResponse]:
    return [suggestion_response(key, value)
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
        output.append(suggestion_response(suggestion_id, candidate))
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
