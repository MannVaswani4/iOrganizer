"""SQLite checkpoint and minimal-result storage for whole-mailbox triage."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .domain import Candidate, ItemKind
from .persistence import GmailMessageTriageRow, GmailScanStateRow, ImportantSenderRow

TRIAGE_VERSION = 5


class GmailTriageStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def processed_ids(self, message_ids: list[str]) -> set[str]:
        if not message_ids:
            return set()
        with self._sessions() as session:
            return set(session.scalars(select(GmailMessageTriageRow.message_id).where(
                GmailMessageTriageRow.message_id.in_(message_ids),
                GmailMessageTriageRow.triage_version == TRIAGE_VERSION,
                GmailMessageTriageRow.sender_email.is_not(None),
            )))

    def record_many(self, results: list[dict]) -> None:
        if not results:
            return
        with self._sessions.begin() as session:
            for result in results:
                row = session.get(GmailMessageTriageRow, result["message_id"])
                if row is None:
                    row = GmailMessageTriageRow(message_id=result["message_id"], is_important=False,
                                                processed_at=datetime.now(timezone.utc))
                    session.add(row)
                preserve_saved = row.is_pinned and not result["is_important"]
                row.is_important = result["is_important"]
                row.sender_email = result.get("sender_email")
                if not preserve_saved:
                    row.title = result.get("title")
                    row.summary = result.get("summary")
                    row.category = result.get("category")
                    row.due_at = result.get("due_at")
                    row.event_start_at = result.get("event_start_at")
                    row.event_end_at = result.get("event_end_at")
                row.received_at = result.get("received_at")
                row.processed_at = datetime.now(timezone.utc)
                if result.get("auto_saved"):
                    row.review_state = "accepted"
                elif row.review_state not in {"accepted", "dismissed"}:
                    row.review_state = "pending" if row.is_important else "ignored"
                row.triage_version = TRIAGE_VERSION

    def save_scan_state(self, *, page_token: str | None, initial_complete: bool,
                        scan_kind: str, messages_seen: int, messages_classified: int,
                        important_found: int, last_error: str | None = None) -> None:
        with self._sessions.begin() as session:
            row = session.get(GmailScanStateRow, 1)
            if row is None:
                row = GmailScanStateRow(id=1)
                session.add(row)
            row.page_token = page_token
            row.initial_complete = initial_complete
            row.scan_kind = scan_kind
            row.messages_seen = messages_seen
            row.messages_classified = messages_classified
            row.important_found = important_found
            row.triage_version = TRIAGE_VERSION
            row.last_scan_at = datetime.now(timezone.utc)
            row.last_error = last_error[:500] if last_error else None

    def scan_state(self) -> dict:
        with self._sessions() as session:
            row = session.get(GmailScanStateRow, 1)
            if row is None:
                return {"initial_complete": False, "scan_kind": "initial", "page_token": None,
                        "messages_seen": 0, "messages_classified": 0, "important_found": 0,
                       "triage_version": 1, "scan_interval_days": 3,
                       "last_scan_at": None, "last_error": None}
            return {"initial_complete": row.initial_complete, "scan_kind": row.scan_kind,
                    "page_token": row.page_token, "messages_seen": row.messages_seen,
                    "messages_classified": row.messages_classified, "important_found": row.important_found,
                    "triage_version": row.triage_version,
                    "scan_interval_days": row.scan_interval_days,
                    "last_scan_at": row.last_scan_at.isoformat() if row.last_scan_at else None,
                    "last_error": row.last_error}

    def set_scan_interval_days(self, days: int) -> None:
        with self._sessions.begin() as session:
            row = session.get(GmailScanStateRow, 1)
            if row is None:
                row = GmailScanStateRow(id=1)
                session.add(row)
            row.scan_interval_days = days

    def important_senders(self) -> list[str]:
        with self._sessions() as session:
            return list(session.scalars(select(ImportantSenderRow.sender_email).order_by(
                ImportantSenderRow.sender_email.asc())))

    def is_sender_always_included(self, sender_email: str) -> bool:
        if not sender_email:
            return False
        with self._sessions() as session:
            return session.get(ImportantSenderRow, sender_email.casefold()) is not None

    def add_important_sender(self, sender_email: str) -> None:
        normalized = sender_email.strip().casefold()
        with self._sessions.begin() as session:
            if session.get(ImportantSenderRow, normalized) is None:
                session.add(ImportantSenderRow(sender_email=normalized, added_at=datetime.now(timezone.utc)))
            # Existing locally summarized messages from this sender become visible
            # immediately without needing another Gmail fetch.
            rows = session.scalars(select(GmailMessageTriageRow).where(
                GmailMessageTriageRow.sender_email == normalized
            ))
            for row in rows:
                row.is_important = True
                row.category = "important_sender"
                if row.summary:
                    row.review_state = "accepted"
                else:
                    row.triage_version = TRIAGE_VERSION - 1
                    row.review_state = "pending"

    def remove_important_sender(self, sender_email: str) -> None:
        normalized = sender_email.strip().casefold()
        with self._sessions.begin() as session:
            row = session.get(ImportantSenderRow, normalized)
            if row:
                session.delete(row)
            messages = session.scalars(select(GmailMessageTriageRow).where(
                GmailMessageTriageRow.sender_email == normalized,
                GmailMessageTriageRow.category == "important_sender",
                GmailMessageTriageRow.is_pinned.is_(False),
            ))
            for message in messages:
                message.is_important = False
                message.category = "none"
                message.review_state = "ignored"

    def pending_candidates(self) -> list[Candidate]:
        with self._sessions() as session:
            rows = session.scalars(select(GmailMessageTriageRow).where(
                GmailMessageTriageRow.is_important.is_(True),
                GmailMessageTriageRow.review_state == "pending",
                GmailMessageTriageRow.triage_version == TRIAGE_VERSION,
            ).order_by(GmailMessageTriageRow.received_at.desc().nullslast()))
            return [Candidate(title=row.title or "Important email", body=row.summary or "",
                              kind=ItemKind.EVENT if row.category == "meeting" else ItemKind.TASK,
                              category=row.category or "deadline", due_at=row.due_at or row.event_start_at,
                              source_ref=f"gmail:{row.message_id}", event_start_at=row.event_start_at,
                              event_end_at=row.event_end_at, is_pinned=row.is_pinned,
                              sender_email=row.sender_email) for row in rows]

    def set_review_state(self, message_id: str, state: str) -> None:
        if state not in {"accepted", "dismissed"}:
            raise ValueError("invalid Gmail review state")
        with self._sessions.begin() as session:
            row = session.get(GmailMessageTriageRow, message_id)
            if row is not None and row.is_important:
                row.review_state = state

    def set_pinned(self, message_id: str, pinned: bool) -> None:
        with self._sessions.begin() as session:
            row = session.get(GmailMessageTriageRow, message_id)
            if row is None or (not row.is_important and not row.is_pinned):
                raise ValueError("Important email was not found")
            row.is_pinned = pinned

    def saved_emails(self) -> list[dict]:
        with self._sessions() as session:
            rows = session.scalars(select(GmailMessageTriageRow).where(
                (GmailMessageTriageRow.is_pinned.is_(True)) |
                (GmailMessageTriageRow.category == "important_sender")
            ).order_by(GmailMessageTriageRow.received_at.desc().nullslast()))
            return [{"message_id": row.message_id, "title": row.title or "Important email",
                     "summary": row.summary or "", "category": row.category or "security",
                     "sender_email": row.sender_email,
                     "received_at": row.received_at.isoformat() if row.received_at else None}
                    for row in rows]

    def is_pinned(self, message_id: str) -> bool:
        row = self.get_message(message_id)
        return bool(row and row.is_pinned)

    def should_review(self, message_id: str) -> bool:
        row = self.get_message(message_id)
        return row is None or row.review_state not in {"accepted", "dismissed"}

    def get_message(self, message_id: str) -> GmailMessageTriageRow | None:
        with self._sessions() as session:
            return session.get(GmailMessageTriageRow, message_id)

    def set_calendar_event_id(self, message_id: str, event_id: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(GmailMessageTriageRow, message_id)
            if row is None:
                raise ValueError("Important email was not found")
            row.calendar_event_id = event_id
            row.review_state = "accepted"
