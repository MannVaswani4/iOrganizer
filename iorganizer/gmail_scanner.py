"""Resumable mailbox backfill and recent-message scanner."""
from __future__ import annotations

from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr
import re
from typing import Callable

from google.auth.transport.requests import AuthorizedSession

from .domain import Candidate, ItemKind
from .email_agent import EmailTriageResult, LocalEmailTriageAgent
from .email_store import TRIAGE_VERSION, GmailTriageStore
from .google_integrations import (GMAIL_SCOPE, GoogleIntegrationError, GoogleNotConnected,
                                 GmailMessagesConnector, _decode_gmail_body, load_credentials)


def _header_text(value: str, limit: int) -> str:
    try:
        return str(make_header(decode_header(value)))[:limit]
    except (ValueError, TypeError):
        return value[:limit]


_MARKETING_PATTERNS = re.compile(
    r"\b(?:weekly deals|new arrivals|browse (?:our|the) latest|subscribe to (?:our )?newsletter|"
    r"coupon code|promo code|shop now|sale ends|\d{1,3}(?:%| percent)\s*(?:off|discount)|"
    r"(?:save|saving)\s+(?:up to\s+)?\d{1,3}(?:%| percent)|limited.time offer)\b",
    re.IGNORECASE,
)


def _is_marketing_message(message: dict, headers: dict[str, str], subject: str, body: str) -> bool:
    if "CATEGORY_PROMOTIONS" in message.get("labelIds", []):
        return True
    if headers.get("list-unsubscribe"):
        return True
    return bool(_MARKETING_PATTERNS.search(f"{subject}\n{body[:4000]}"))


class GmailMailboxScanner:
    def __init__(self, store: GmailTriageStore, agent: LocalEmailTriageAgent,
                 connector: GmailMessagesConnector | None = None) -> None:
        self.store = store
        self.agent = agent
        self.connector = connector or GmailMessagesConnector()

    def scan(self, on_important: Callable[[list[Candidate]], None],
             on_ignored: Callable[[list[str]], None] | None = None) -> None:
        credentials = load_credentials(required_scope=GMAIL_SCOPE)
        session = AuthorizedSession(credentials)
        state = self.store.scan_state()
        days_back = state.get("scan_interval_days", 3)
        sender_emails = self.store.important_senders()
        same_version = state["triage_version"] == TRIAGE_VERSION
        initial_complete = state["initial_complete"] if same_version else False
        recent_only = initial_complete
        page_token = (state["page_token"] if same_version and
                      state["scan_kind"] == ("recent" if recent_only else "initial") else None)
        seen = state["messages_seen"] if same_version else 0
        classified = state["messages_classified"] if same_version else 0
        important_total = state["important_found"] if same_version else 0
        try:
            while True:
                result = self.connector.list_page(session, page_token=page_token,
                                                  recent_only=recent_only, days_back=days_back,
                                                  sender_emails=sender_emails)
                references = result.get("messages", [])
                message_ids = [item.get("id", "") for item in references if item.get("id")]
                known = self.store.processed_ids(message_ids)
                seen += len(message_ids)
                records: list[dict] = []
                candidates: list[Candidate] = []
                ignored_refs: list[str] = []
                for message_id in message_ids:
                    if message_id in known:
                        continue
                    message = self.connector.get_message(session, message_id)
                    payload = message.get("payload", {})
                    headers = {str(h.get("name", "")).lower(): h.get("value", "")
                               for h in payload.get("headers", [])}
                    subject = _header_text(headers.get("subject", "Important email") or "Important email", 300).strip()
                    sender = _header_text(headers.get("from", ""), 300).strip()
                    sender_email = parseaddr(sender)[1].strip().casefold()
                    body = _decode_gmail_body(payload) or str(message.get("snippet", ""))
                    internal_ms = message.get("internalDate")
                    received_at = (datetime.fromtimestamp(int(internal_ms) / 1000, timezone.utc)
                                   if internal_ms else None)
                    always_include = self.store.is_sender_always_included(sender_email)
                    if always_include:
                        triage = self.agent.summarize(
                            subject=subject, sender=sender,
                            received_at=received_at.isoformat() if received_at else "", body=body,
                        )
                    elif _is_marketing_message(message, headers, subject, body):
                        triage = EmailTriageResult(False, "", "none")
                    else:
                        triage = self.agent.classify(
                            subject=subject, sender=sender,
                            received_at=received_at.isoformat() if received_at else "",
                            body=body,
                        )
                    records.append({
                        "message_id": message_id,
                        "sender_email": sender_email,
                        "is_important": triage.important,
                        "title": subject if triage.important else None,
                        "summary": triage.summary if triage.important else None,
                        "category": triage.category if triage.important else None,
                        "received_at": received_at,
                        "due_at": triage.due_at if triage.important else None,
                        "event_start_at": triage.event_start_at if triage.important else None,
                        "event_end_at": triage.event_end_at if triage.important else None,
                        "auto_saved": always_include,
                    })
                    classified += 1
                    if triage.important and not always_include and self.store.should_review(message_id):
                        important_total += 1
                        is_meeting = triage.category == "meeting"
                        candidates.append(Candidate(
                            title=(triage.event_title or subject)[:200], body=triage.summary,
                            kind=ItemKind.EVENT if is_meeting else ItemKind.TASK,
                            category=triage.category,
                            due_at=triage.event_start_at if is_meeting else triage.due_at,
                            source_ref=f"gmail:{message_id}",
                            event_start_at=triage.event_start_at,
                            event_end_at=triage.event_end_at,
                            is_pinned=self.store.is_pinned(message_id),
                            sender_email=sender_email,
                        ))
                    elif not always_include:
                        ignored_refs.append(f"gmail:{message_id}")
                    if len(records) >= 10:
                        self.store.record_many(records)
                        records.clear()
                        self.store.save_scan_state(
                            page_token=page_token, initial_complete=initial_complete,
                            scan_kind="recent" if recent_only else "initial", messages_seen=seen,
                            messages_classified=classified, important_found=important_total,
                        )
                        if candidates:
                            on_important(candidates)
                            candidates.clear()
                        if ignored_refs and on_ignored:
                            on_ignored(ignored_refs)
                            ignored_refs.clear()

                self.store.record_many(records)
                if candidates:
                    on_important(candidates)
                if ignored_refs and on_ignored:
                    on_ignored(ignored_refs)
                page_token = result.get("nextPageToken")
                if not page_token and not recent_only:
                    initial_complete = True
                    recent_only = True
                self.store.save_scan_state(
                    page_token=page_token, initial_complete=initial_complete,
                    scan_kind="recent" if recent_only else "initial", messages_seen=seen,
                    messages_classified=classified, important_found=important_total,
                )
                if not page_token:
                    break
        except Exception as exc:
            self.store.save_scan_state(
                page_token=page_token, initial_complete=initial_complete,
                scan_kind="recent" if recent_only else "initial", messages_seen=seen,
                messages_classified=classified, important_found=important_total,
                last_error=str(exc),
            )
            if isinstance(exc, (GoogleIntegrationError, GoogleNotConnected)):
                raise
            raise GoogleIntegrationError("Gmail mailbox scan stopped. Resume it from the app after resolving the error.") from exc
        finally:
            session.close()
