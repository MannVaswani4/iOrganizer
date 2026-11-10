from __future__ import annotations

import os
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .domain import ItemKind, ItemStatus, OrganizerItem
from .repositories import ItemRepository


class Base(DeclarativeBase):
    pass


class ItemRow(Base):
    __tablename__ = "items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    source_ref: Mapped[str | None] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(80), nullable=False, default="uncategorized")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAlchemyItemRepository(ItemRepository):
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @staticmethod
    def _domain(row: ItemRow) -> OrganizerItem:
        return OrganizerItem(id=UUID(row.id), title=row.title, kind=ItemKind(row.kind), body=row.body,
                             due_at=row.due_at, source=row.source, source_ref=row.source_ref,
                             category=row.category,
                             status=ItemStatus(row.status), created_at=row.created_at, updated_at=row.updated_at)

    def save(self, item: OrganizerItem) -> OrganizerItem:
        with self._sessions.begin() as session:
            row = session.get(ItemRow, str(item.id))
            values = dict(id=str(item.id), title=item.title, kind=item.kind.value, body=item.body,
                          due_at=item.due_at, source=item.source, source_ref=item.source_ref,
                          category=item.category,
                          status=item.status.value, created_at=item.created_at, updated_at=item.updated_at)
            if row is None:
                session.add(ItemRow(**values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
        return item

    def get(self, item_id: UUID) -> OrganizerItem | None:
        with self._sessions() as session:
            row = session.get(ItemRow, str(item_id))
            return self._domain(row) if row else None

    def list(self, query: str | None = None) -> list[OrganizerItem]:
        with self._sessions() as session:
            statement = select(ItemRow).order_by(ItemRow.created_at.desc())
            if query:
                term = f"%{query}%"
                statement = statement.where(ItemRow.title.ilike(term) | ItemRow.body.ilike(term))
            return [self._domain(row) for row in session.scalars(statement)]

    def delete(self, item_id: UUID) -> bool:
        with self._sessions.begin() as session:
            row = session.get(ItemRow, str(item_id))
            if row is None:
                return False
            session.delete(row)
            return True


def make_database(database_url: str | None = None) -> tuple[sessionmaker[Session], object]:
    url = database_url or os.getenv("IORGANIZER_DATABASE_URL", "sqlite:///./data/iorganizer.db")
    if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
        os.makedirs(os.path.dirname(url.removeprefix("sqlite:///")) or ".", exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False), engine
