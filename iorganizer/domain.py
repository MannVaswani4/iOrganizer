from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID, uuid4


class ItemKind(StrEnum):
    NOTE = "note"
    TASK = "task"
    EVENT = "event"
    LINK = "link"


class ItemStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    ARCHIVED = "archived"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class OrganizerItem:
    title: str
    kind: ItemKind
    body: str = ""
    due_at: datetime | None = None
    source: str = "manual"
    source_ref: str | None = None
    category: str = "uncategorized"
    status: ItemStatus = ItemStatus.OPEN
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)

    def update(self, *, title: str | None = None, body: str | None = None,
               category: str | None = None, due_at: datetime | None = None,
               status: ItemStatus | None = None) -> None:
        if title is not None:
            self.title = title.strip()
        if body is not None:
            self.body = body
        if category is not None:
            self.category = category.strip() or "uncategorized"
        if due_at is not None:
            self.due_at = due_at
        if status is not None:
            self.status = status
        self.updated_at = now_utc()


@dataclass(frozen=True)
class Candidate:
    title: str
    body: str
    kind: ItemKind
    category: str
    due_at: datetime | None = None
    source_ref: str | None = None


@dataclass
class ImportBatch:
    source_type: str
    filename: str
    status: str = "processing"
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=now_utc)
    error_message: str | None = None


@dataclass
class Suggestion:
    batch_id: UUID
    candidate: Candidate
    id: UUID = field(default_factory=uuid4)
    accepted_item_id: UUID | None = None
    reviewed_at: datetime | None = None
