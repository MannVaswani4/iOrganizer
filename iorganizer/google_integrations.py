"""Local Google OAuth and read-only Calendar, Gmail, and Photos Picker adapters."""
from __future__ import annotations

import base64
import json
import os
import re
from abc import ABC, abstractmethod
from threading import Lock
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from urllib.parse import urlsplit

import keyring
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from .agents import AgentPipeline
from .domain import Candidate, ItemKind
from .photo_ocr import PhotoOcrError, extract_text


CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
SCOPES = (
    CALENDAR_WRITE_SCOPE,
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/photospicker.mediaitems.readonly",
)
CALENDAR_SCOPE = CALENDAR_WRITE_SCOPE
GMAIL_SCOPE = SCOPES[1]
PHOTOS_SCOPE = SCOPES[2]
KEYRING_SERVICE = "iorganizer-google-oauth"
KEYRING_ACCOUNT = "local-user"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLIENT_FILE = Path(os.getenv("IORGANIZER_GOOGLE_CLIENT_FILE", str(PROJECT_ROOT / "secrets/client_secret.json")))
REDIRECT_URI = "http://127.0.0.1:8765/oauth/google/callback"
PHOTOS_API = "https://photospicker.googleapis.com/v1/"
CALENDAR_API = "https://www.googleapis.com/calendar/v3/"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/"
MAX_GOOGLE_PHOTOS_PER_SESSION = 10
MAX_PHOTO_BYTES = 10_000_000
_oauth_transport_lock = Lock()


class GoogleNotConnected(RuntimeError):
    pass


class GoogleIntegrationError(RuntimeError):
    pass


class GoogleSource(ABC):
    """Read-only connector contract used by the Google sync coordinator."""

    @abstractmethod
    def fetch(self, pipeline: AgentPipeline, session: AuthorizedSession) -> list[Candidate]:
        raise NotImplementedError


class CalendarConnector(GoogleSource):
    def fetch(self, pipeline: AgentPipeline, session: AuthorizedSession) -> list[Candidate]:
        return sync_calendar(pipeline, session)


class GmailMessagesConnector:
    """Paged Gmail API adapter used by the long-running local triage worker."""

    def list_page(self, session: AuthorizedSession, *, page_token: str | None, recent_only: bool) -> dict:
        params: dict[str, str | int] = {"maxResults": 500, "includeSpamTrash": "false",
                                        "q": "newer_than:3d -in:drafts"}
        if page_token:
            params["pageToken"] = page_token
        return _raise_google_error(
            session.get(urljoin(GMAIL_API, "users/me/messages"), params=params, timeout=30),
            "Gmail message listing",
        )

    def get_message(self, session: AuthorizedSession, message_id: str) -> dict:
        return _raise_google_error(
            session.get(urljoin(GMAIL_API, f"users/me/messages/{message_id}"),
                        params={"format": "full"}, timeout=30),
            "Gmail message fetch",
        )


def make_oauth_flow(state: str | None = None) -> Flow:
    if not CLIENT_FILE.is_file():
        raise GoogleIntegrationError(
            f"OAuth client JSON not found at {CLIENT_FILE}. Add the downloaded file to the ignored secrets folder."
        )
    try:
        data = json.loads(CLIENT_FILE.read_text(encoding="utf-8"))
        if "web" not in data:
            raise GoogleIntegrationError("Create a Web application OAuth client in Google Cloud.")
        flow = Flow.from_client_secrets_file(str(CLIENT_FILE), scopes=list(SCOPES), state=state)
        flow.redirect_uri = REDIRECT_URI
        return flow
    except (OSError, json.JSONDecodeError) as exc:
        raise GoogleIntegrationError("Could not read the Google OAuth client JSON file.") from exc


def authorization_url() -> tuple[str, str, str]:
    flow = make_oauth_flow()
    url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    if not flow.code_verifier:
        raise GoogleIntegrationError("Could not initialize secure Google sign-in.")
    return url, state, flow.code_verifier


def exchange_oauth_code(flow: Flow, callback_url: str) -> None:
    """Exchange a code from our exact loopback callback; Google endpoints remain HTTPS."""
    parsed = urlsplit(callback_url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != 8765
            or parsed.path != "/oauth/google/callback" or parsed.username or parsed.password):
        raise GoogleIntegrationError("Google returned to an unexpected OAuth callback address.")
    token_endpoint = urlsplit(flow.client_config.get("token_uri", ""))
    if token_endpoint.scheme != "https" or token_endpoint.hostname not in {"oauth2.googleapis.com", "accounts.google.com"}:
        raise GoogleIntegrationError("The configured Google token endpoint must use HTTPS.")

    # OAuthlib rejects HTTP callback URLs by default. Permit only this validated
    # loopback response, and restore the process setting immediately afterwards.
    with _oauth_transport_lock:
        old_value = os.environ.get("OAUTHLIB_INSECURE_TRANSPORT")
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        try:
            flow.fetch_token(authorization_response=callback_url)
        finally:
            if old_value is None:
                os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
            else:
                os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = old_value


def save_credentials(credentials: Credentials) -> None:
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, credentials.to_json())
    except Exception as exc:
        raise GoogleIntegrationError(
            "Could not store Google tokens in macOS Keychain. Check that the Keychain is available."
        ) from exc


def load_credentials(*, required_scope: str | None = None) -> Credentials:
    try:
        serialized = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except Exception as exc:
        raise GoogleIntegrationError("Could not read Google tokens from macOS Keychain.") from exc
    if not serialized:
        raise GoogleNotConnected("Connect your Google account first.")
    try:
        # Preserve the scopes actually granted and serialized by Google. Passing the
        # app's current desired scopes here would incorrectly make an old read-only
        # token look as if it already had Calendar write access.
        credentials = Credentials.from_authorized_user_info(json.loads(serialized))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise GoogleIntegrationError("Stored Google authorization is invalid. Disconnect and reconnect.") from exc
    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            save_credentials(credentials)
        except Exception as exc:
            raise GoogleNotConnected("Google authorization expired or was revoked. Reconnect your account.") from exc
    if not credentials.valid:
        raise GoogleNotConnected("Google authorization is no longer valid. Reconnect your account.")
    granted = set(credentials.granted_scopes or credentials.scopes or [])
    if required_scope and required_scope not in granted:
        raise GoogleIntegrationError(f"Google permission was not granted: {required_scope}")
    return credentials


def connection_status() -> dict:
    try:
        credentials = load_credentials()
    except GoogleNotConnected:
        return {"connected": False, "calendar": False, "calendar_write": False,
                "gmail": False, "photos": False, "scopes": []}
    except GoogleIntegrationError as exc:
        return {"connected": False, "calendar": False, "calendar_write": False,
                "gmail": False, "photos": False,
                "scopes": [], "error": str(exc)}
    granted = set(credentials.granted_scopes or credentials.scopes or [])
    return {
        "connected": True,
        "calendar": CALENDAR_SCOPE in granted or CALENDAR_READ_SCOPE in granted,
        "calendar_write": CALENDAR_SCOPE in granted,
        "gmail": GMAIL_SCOPE in granted,
        "photos": PHOTOS_SCOPE in granted,
        "scopes": sorted(granted),
    }


def disconnect_google() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except keyring.errors.PasswordDeleteError:
        pass
    except Exception as exc:
        raise GoogleIntegrationError("Could not remove Google authorization from Keychain.") from exc


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)


def _decode_gmail_body(payload: dict) -> str:
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data:
            try:
                decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
                if mime == "text/plain":
                    plain.append(decoded)
                elif mime == "text/html":
                    html.append(decoded)
            except (ValueError, TypeError):
                pass
        for child in part.get("parts", []):
            walk(child)

    walk(payload)
    if plain:
        body = "\n".join(plain)
    elif html:
        parser = _TextOnly()
        parser.feed("\n".join(html))
        body = "\n".join(parser.parts)
    else:
        body = ""
    return re.sub(r"\n{3,}", "\n\n", body).strip()[:12_000]


def _raise_google_error(response, label: str) -> dict:
    if response.ok:
        try:
            return response.json()
        except ValueError as exc:
            raise GoogleIntegrationError(f"{label} returned an unreadable response.") from exc
    try:
        detail = response.json().get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        detail = ""
    detail = detail[:240] or f"HTTP {response.status_code}"
    raise GoogleIntegrationError(f"{label} failed ({detail}).")


def sync_calendar(pipeline: AgentPipeline, session: AuthorizedSession) -> list[Candidate]:
    credentials = session.credentials
    granted = set(credentials.granted_scopes or credentials.scopes or [])
    if not ({CALENDAR_SCOPE, CALENDAR_READ_SCOPE} & granted):
        return []
    now = datetime.now(timezone.utc)
    params = {
        "timeMin": (now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "timeMax": (now + timedelta(days=60)).isoformat().replace("+00:00", "Z"),
        "singleEvents": "true", "orderBy": "startTime", "maxResults": 250,
    }
    candidates: list[Candidate] = []
    page_token = None
    for _ in range(4):
        if page_token:
            params["pageToken"] = page_token
        result = _raise_google_error(session.get(urljoin(CALENDAR_API, "calendars/primary/events"), params=params, timeout=30), "Google Calendar")
        for event in result.get("items", []):
            if event.get("status") == "cancelled":
                continue
            title = str(event.get("summary", "")).strip()
            event_id = event.get("id")
            if not title or not event_id:
                continue
            start = event.get("start", {})
            due_at = None
            if start.get("dateTime"):
                due_at = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=timezone.utc)
            elif start.get("date"):
                due_at = datetime.combine(date.fromisoformat(start["date"]), time.min, tzinfo=timezone.utc)
            details = [event.get("description", ""), event.get("location", "")]
            body = "\n".join(str(part).strip() for part in details if part and str(part).strip())
            candidates.append(Candidate(title=title[:200], body=body[:20_000], kind=ItemKind.EVENT,
                                        category="google calendar", due_at=due_at,
                                        source_ref=f"google-calendar:{event_id}"))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return candidates


def create_calendar_event(*, event_id: str, title: str, description: str,
                          start_at: datetime, end_at: datetime | None = None) -> dict:
    credentials = load_credentials(required_scope=CALENDAR_WRITE_SCOPE)
    session = AuthorizedSession(credentials)
    start = start_at if start_at.tzinfo else start_at.astimezone()
    end = end_at if end_at and end_at.tzinfo else (end_at.astimezone() if end_at else None)
    if end is None:
        end = start + timedelta(minutes=30)
    if end <= start:
        raise GoogleIntegrationError("Calendar event end time must be after its start time.")
    payload = {
        "id": event_id,
        "summary": title[:200],
        "description": description[:20_000],
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    try:
        response = session.post(urljoin(CALENDAR_API, "calendars/primary/events"), json=payload, timeout=30)
        if response.status_code == 409:
            result = _raise_google_error(
                session.get(urljoin(CALENDAR_API, f"calendars/primary/events/{event_id}"), timeout=30),
                "Google Calendar event lookup",
            )
        else:
            result = _raise_google_error(response, "Google Calendar event creation")
        save_credentials(credentials)
        return result
    finally:
        session.close()


def sync_google_sources(pipeline: AgentPipeline, sources: tuple[GoogleSource, ...] | None = None) -> list[Candidate]:
    credentials = load_credentials()
    session = AuthorizedSession(credentials)
    try:
        selected_sources = sources or (CalendarConnector(),)
        candidates = [candidate for source in selected_sources for candidate in source.fetch(pipeline, session)]
        save_credentials(credentials)
        return candidates
    except GoogleIntegrationError:
        raise
    except Exception as exc:
        raise GoogleIntegrationError("Google sync failed. Check enabled APIs, granted scopes, and network access.") from exc
    finally:
        session.close()


def _photos_request(session: AuthorizedSession, method: str, url: str, *, label: str, **kwargs) -> dict:
    try:
        response = session.request(method, url, timeout=30, **kwargs)
    except Exception as exc:
        raise GoogleIntegrationError(f"{label} could not reach Google Photos.") from exc
    return _raise_google_error(response, label)


def create_photos_session() -> dict:
    credentials = load_credentials(required_scope=PHOTOS_SCOPE)
    session = AuthorizedSession(credentials)
    try:
        result = _photos_request(session, "POST", urljoin(PHOTOS_API, "sessions"), label="Google Photos Picker",
                                 json={"pickingConfig": {"maxItemCount": str(MAX_GOOGLE_PHOTOS_PER_SESSION)}})
        save_credentials(credentials)
        return {"session_id": result["id"], "picker_uri": result["pickerUri"],
                "polling_config": result.get("pollingConfig", {})}
    finally:
        session.close()


def get_photos_session(session_id: str) -> dict:
    credentials = load_credentials(required_scope=PHOTOS_SCOPE)
    session = AuthorizedSession(credentials)
    try:
        result = _photos_request(session, "GET", urljoin(PHOTOS_API, f"sessions/{session_id}"), label="Google Photos Picker")
        save_credentials(credentials)
        return {"ready": bool(result.get("mediaItemsSet")), "picker_uri": result.get("pickerUri"),
                "polling_config": result.get("pollingConfig", {})}
    finally:
        session.close()


def _read_download(response, limit: int) -> bytes:
    if not response.ok:
        _raise_google_error(response, "Google Photos download")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=256_000):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise PhotoOcrError("A selected photo exceeds the 10 MB OCR limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def import_selected_photos(session_id: str, pipeline: AgentPipeline) -> tuple[list[Candidate], int]:
    credentials = load_credentials(required_scope=PHOTOS_SCOPE)
    session = AuthorizedSession(credentials)
    candidates: list[Candidate] = []
    processed = 0
    try:
        session_status = _photos_request(session, "GET", urljoin(PHOTOS_API, f"sessions/{session_id}"), label="Google Photos Picker")
        if not session_status.get("mediaItemsSet"):
            return [], 0
        result = _photos_request(session, "GET", urljoin(PHOTOS_API, "mediaItems"), label="Google Photos Picker",
                                 params={"sessionId": session_id, "pageSize": MAX_GOOGLE_PHOTOS_PER_SESSION})
        for picked in result.get("mediaItems", [])[:MAX_GOOGLE_PHOTOS_PER_SESSION]:
            media_file = picked.get("mediaFile", {})
            mime_type = media_file.get("mimeType", "")
            if not mime_type.startswith("image/"):
                continue
            base_url = media_file.get("baseUrl", "")
            photo_id = picked.get("id", "")
            filename = media_file.get("filename") or f"google-photo-{photo_id}.jpg"
            if not base_url or not photo_id:
                continue
            response = session.get(base_url + "=d", timeout=30, stream=True)
            try:
                photo_bytes = _read_download(response, MAX_PHOTO_BYTES)
            finally:
                response.close()
            try:
                ocr = extract_text(photo_bytes, filename)
            except PhotoOcrError:
                # Some Google Photos selections have uncommon formats; try a JPEG suffix if needed.
                if Path(filename).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}:
                    filename = f"google-photo-{photo_id}.jpg"
                    ocr = extract_text(photo_bytes, filename)
                else:
                    raise
            candidates.extend(pipeline.process_photo_text(ocr, f"google-photos:{photo_id}"))
            processed += 1
        _photos_request(session, "DELETE", urljoin(PHOTOS_API, f"sessions/{session_id}"), label="Google Photos Picker cleanup")
        save_credentials(credentials)
        return candidates, processed
    finally:
        session.close()
