from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel, Field

from .agents import AgentPipeline, RuleBasedExtractor
from .domain import Candidate, ItemKind, ItemStatus, OrganizerItem
from .persistence import SqlAlchemyItemRepository, make_database
from .services import OrganizerService, accept_candidate


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


sessions, _engine = make_database()
service = OrganizerService(SqlAlchemyItemRepository(sessions), AgentPipeline(RuleBasedExtractor()))
app = FastAPI(title="iOrganizer", version="0.1.0", description="Local-first personal information organizer")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


suggestions: dict[int, Candidate] = {}
next_suggestion_id = 1


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
async def import_text(file: UploadFile = File(...)) -> list[SuggestionResponse]:
    global next_suggestion_id
    if not file.filename or len(file.filename) > 255:
        raise HTTPException(400, "A valid filename is required")
    if not (file.filename.endswith(".txt") or file.filename.endswith(".ics")):
        raise HTTPException(415, "Only .txt and .ics imports are supported")
    raw = await file.read(1_000_001)
    if len(raw) > 1_000_000:
        raise HTTPException(413, "Import must be at most 1 MB")
    try:
        if file.filename.endswith(".txt"):
            candidates = service.suggest(raw.decode("utf-8-sig"), file.filename)
        else:
            candidates = service.suggest_calendar(raw, file.filename)
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "Import must be UTF-8 text") from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "Invalid iCalendar file") from exc
    output = []
    for candidate in candidates:
        suggestion_id = next_suggestion_id
        next_suggestion_id += 1
        suggestions[suggestion_id] = candidate
        output.append(SuggestionResponse(id=suggestion_id, title=candidate.title, body=candidate.body,
                                         kind=candidate.kind, category=candidate.category,
                                         source_ref=candidate.source_ref))
    return output


@app.post("/api/v1/suggestions/{suggestion_id}/accept", response_model=ItemResponse, status_code=201)
def accept_suggestion(suggestion_id: int) -> ItemResponse:
    candidate = suggestions.pop(suggestion_id, None)
    if candidate is None:
        raise HTTPException(404, "Suggestion not found or already reviewed")
    return ItemResponse.from_item(accept_candidate(service, candidate))
