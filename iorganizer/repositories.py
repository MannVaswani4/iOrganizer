from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from uuid import UUID

from .domain import OrganizerItem


class ItemRepository(ABC):
    @abstractmethod
    def save(self, item: OrganizerItem) -> OrganizerItem: ...

    @abstractmethod
    def get(self, item_id: UUID) -> OrganizerItem | None: ...

    @abstractmethod
    def list(self, query: str | None = None) -> Iterable[OrganizerItem]: ...

    @abstractmethod
    def delete(self, item_id: UUID) -> bool: ...


class InMemoryItemRepository(ItemRepository):
    """Small adapter for development and isolated service use."""
    def __init__(self) -> None:
        self._items: dict[UUID, OrganizerItem] = {}

    def save(self, item: OrganizerItem) -> OrganizerItem:
        self._items[item.id] = item
        return item

    def get(self, item_id: UUID) -> OrganizerItem | None:
        return self._items.get(item_id)

    def list(self, query: str | None = None) -> list[OrganizerItem]:
        items = list(self._items.values())
        if query:
            term = query.casefold()
            items = [i for i in items if term in i.title.casefold() or term in i.body.casefold()]
        return sorted(items, key=lambda i: i.created_at, reverse=True)

    def delete(self, item_id: UUID) -> bool:
        return self._items.pop(item_id, None) is not None
