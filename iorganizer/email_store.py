"""SQLite checkpoint and minimal-result storage for whole-mailbox triage."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .domain import Candidate, ItemKind
from .persistence import GmailMessageTriageRow, GmailScanStateRow


class GmailTriageStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def processed_ids(self, message_ids: list[str]) -> set[str]:
        if not message_ids:
            return set()
        with self._sessions() as session:
            return set(session.scalars(select(GmailMessageTriageRow.message_id).where(
                GmailMessageTriageRow.message_id.in_(message_ids)
            )))

    def record_many(self, results: list[dict]) -> None:
        if not results:
            return
        with self._sessions.begin() as session:
            for result in results:
                session.merge(GmailMessageTriageRow(
                    message_id=result["message_id"], is_important=result["is_important"],
                    title=result.get("title"), summary=result.get("summary"), category=result.get("category"),
                    received_at=result.get("received_at"), processed_at=datetime.now(timezone.utc),
                    review_state="pending" if result["is_important"] else "ignored",
                ))

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
            row.last_scan_at = datetime.now(timezone.utc)
            row.last_error = last_error[:500] if last_error else None

    def scan_state(self) -> dict:
        with self._sessions() as session:
            row = session.get(GmailScanStateRow, 1)
            if row is None:
                return {"initial_complete": False, "scan_kind": "initial", "page_token": None,
                        "messages_seen": 0, "messages_classified": 0, "important_found": 0,
                        "last_scan_at": None, "last_error": None}
            return {"initial_complete": row.initial_complete, "scan_kind": row.scan_kind,
                    "page_token": row.page_token, "messages_seen": row.messages_seen,
                    "messages_classified": row.messages_classified, "important_found": row.important_found,
                    "last_scan_at": row.last_scan_at.isoformat() if row.last_scan_at else None,
                    "last_error": row.last_error}

    def pending_candidates(self) -> list[Candidate]:
        with self._sessions() as session:
            rows = session.scalars(select(GmailMessageTriageRow).where(
                GmailMessageTriageRow.is_important.is_(True),
                GmailMessageTriageRow.review_state == "pending",
            ).order_by(GmailMessageTriageRow.received_at.desc().nullslast()))
            return [Candidate(title=row.title or "Important email", body=row.summary or "",
                              kind=ItemKind.NOTE, category=row.category or "other",
                              source_ref=f"gmail:{row.message_id}") for row in rows]

    def set_review_state(self, message_id: str, state: str) -> None:
        if state not in {"accepted", "dismissed"}:
            raise ValueError("invalid Gmail review state")
        with self._sessions.begin() as session:
            row = session.get(GmailMessageTriageRow, message_id)
            if row is not None and row.is_important:
                row.review_state = state
