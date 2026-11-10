from __future__ import annotations

from uuid import UUID

from .agents import AgentPipeline
from .domain import Candidate, ImportBatch, ItemKind, ItemStatus, OrganizerItem, Suggestion
from .repositories import ItemRepository


class OrganizerService:
    def __init__(self, items: ItemRepository, pipeline: AgentPipeline) -> None:
        self._items = items
        self._pipeline = pipeline

    def create_item(self, title: str, kind: ItemKind, body: str = "", source: str = "manual",
                    category: str = "uncategorized", due_at=None,
                    source_ref: str | None = None) -> OrganizerItem:
        if not title.strip():
            raise ValueError("title must not be empty")
        return self._items.save(OrganizerItem(title=title.strip(), kind=kind, body=body,
                                              source=source, category=category, due_at=due_at,
                                              source_ref=source_ref))

    def get_item(self, item_id: UUID) -> OrganizerItem | None:
        return self._items.get(item_id)

    def update_item(self, item_id: UUID, *, title: str | None = None, body: str | None = None,
                    category: str | None = None, due_at=None,
                    status: ItemStatus | None = None) -> OrganizerItem | None:
        item = self._items.get(item_id)
        if item is None:
            return None
        if title is not None and not title.strip():
            raise ValueError("title must not be empty")
        item.update(title=title, body=body, category=category, due_at=due_at, status=status)
        return self._items.save(item)

    def list_items(self, query: str | None = None):
        return self._items.list(query)

    def delete_item(self, item_id: UUID) -> bool:
        return self._items.delete(item_id)

    def suggest(self, text: str, source: str) -> list[Candidate]:
        return self._pipeline.process(text, source)

    def suggest_calendar(self, content: bytes, source: str) -> list[Candidate]:
        return self._pipeline.process_calendar(content, source)


def accept_candidate(service: OrganizerService, candidate: Candidate) -> OrganizerItem:
    return service.create_item(candidate.title, candidate.kind, candidate.body,
                                source="import", category=candidate.category,
                                due_at=candidate.due_at, source_ref=candidate.source_ref)
