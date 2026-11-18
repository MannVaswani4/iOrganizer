"""Resumable mailbox backfill and recent-message scanner."""
from __future__ import annotations

from datetime import datetime, timezone
from email.header import decode_header, make_header
from typing import Callable

from google.auth.transport.requests import AuthorizedSession

from .domain import Candidate, ItemKind
from .email_agent import LocalEmailTriageAgent
from .email_store import GmailTriageStore
from .google_integrations import (GMAIL_SCOPE, GoogleIntegrationError, GoogleNotConnected,
                                 GmailMessagesConnector, _decode_gmail_body, load_credentials)


def _header_text(value: str, limit: int) -> str:
    try:
        return str(make_header(decode_header(value)))[:limit]
    except (ValueError, TypeError):
        return value[:limit]


class GmailMailboxScanner:
    def __init__(self, store: GmailTriageStore, agent: LocalEmailTriageAgent,
                 connector: GmailMessagesConnector | None = None) -> None:
        self.store = store
        self.agent = agent
        self.connector = connector or GmailMessagesConnector()

    def scan(self, on_important: Callable[[list[Candidate]], None]) -> None:
        credentials = load_credentials(required_scope=GMAIL_SCOPE)
        session = AuthorizedSession(credentials)
        state = self.store.scan_state()
        initial_complete = state["initial_complete"]
        recent_only = initial_complete
        page_token = state["page_token"] if state["scan_kind"] == ("recent" if recent_only else "initial") else None
        seen = state["messages_seen"]
        classified = state["messages_classified"]
        important_total = state["important_found"]
        try:
            while True:
                result = self.connector.list_page(session, page_token=page_token, recent_only=recent_only)
                references = result.get("messages", [])
                message_ids = [item.get("id", "") for item in references if item.get("id")]
                known = self.store.processed_ids(message_ids)
                seen += len(message_ids)
                records: list[dict] = []
                candidates: list[Candidate] = []
                for message_id in message_ids:
                    if message_id in known:
                        continue
                    message = self.connector.get_message(session, message_id)
                    payload = message.get("payload", {})
                    headers = {str(h.get("name", "")).lower(): h.get("value", "")
                               for h in payload.get("headers", [])}
                    subject = _header_text(headers.get("subject", "Important email") or "Important email", 300).strip()
                    sender = _header_text(headers.get("from", ""), 300).strip()
                    body = _decode_gmail_body(payload) or str(message.get("snippet", ""))
                    internal_ms = message.get("internalDate")
                    received_at = (datetime.fromtimestamp(int(internal_ms) / 1000, timezone.utc)
                                   if internal_ms else None)
                    triage = self.agent.classify(
                        subject=subject, sender=sender,
                        received_at=received_at.isoformat() if received_at else "",
                        body=body,
                    )
                    records.append({
                        "message_id": message_id,
                        "is_important": triage.important,
                        "title": subject if triage.important else None,
                        "summary": triage.summary if triage.important else None,
                        "category": triage.category if triage.important else None,
                        "received_at": received_at,
                    })
                    classified += 1
                    if triage.important:
                        important_total += 1
                        candidates.append(Candidate(title=subject[:200], body=triage.summary,
                                                    kind=ItemKind.NOTE, category=triage.category,
                                                    source_ref=f"gmail:{message_id}"))
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

                self.store.record_many(records)
                if candidates:
                    on_important(candidates)
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
