# Low-level design

## Layering

`FastAPI router → OrganizerService → ItemRepository interface → SQLAlchemy adapter → SQLite`

Import processing uses `router → AgentPipeline → ContentExtractor strategy → Candidate`; candidates become persistent items only after explicit acceptance. Domain objects do not depend on FastAPI or SQLAlchemy.

## Main classes and responsibilities

| Type | Responsibility |
|---|---|
| `OrganizerItem` | Domain entity; owns item state and update behavior |
| `ItemKind`, `ItemStatus` | Closed vocabularies for item type and lifecycle |
| `Candidate` | Immutable agent proposal with provenance; not yet a saved item |
| `OrganizerService` | Coordinates validation and domain/repository operations |
| `ItemRepository` | Persistence port: save/get/list/delete |
| `SqlAlchemyItemRepository` | SQLite persistence adapter and row/domain mapping |
| `ContentExtractor` | Abstraction for extraction strategies |
| `RuleBasedExtractor` | Offline, explainable baseline extraction agent |
| `AgentPipeline` | Orchestrates the selected extractor |
| `ItemCreate`, `ItemResponse` | API input/output contracts |

## Object-oriented principles

- **Abstraction:** `ItemRepository` and `ContentExtractor` hide infrastructure and algorithm details.
- **Encapsulation:** `OrganizerItem.update()` owns state transitions and refreshes `updated_at`; service keeps collaborators private.
- **Inheritance:** concrete repository and extractor adapters implement abstract base classes.
- **Polymorphism:** service accepts any `ItemRepository`; pipeline accepts any `ContentExtractor` (rule-based now, local-model strategy later).

## SOLID mapping

- **SRP:** API validates/translates HTTP; service coordinates; repository persists; extractor proposes candidates; domain owns item behavior.
- **OCP:** add an extractor or persistence adapter by implementing its port, without rewriting service logic.
- **LSP:** any repository implementation must honor the same save/get/list/delete semantics; any extractor returns valid `Candidate` values.
- **ISP:** small focused ports (`ItemRepository`, `ContentExtractor`) avoid a broad all-purpose integration interface.
- **DIP:** high-level `OrganizerService` depends on `ItemRepository`; `AgentPipeline` depends on `ContentExtractor`, not SQLite or a model vendor.

## Patterns and rationale

- **Repository (structural):** isolates domain/service from persistence.
- **Strategy (behavioral):** extraction algorithm can switch from rules to local model while preserving pipeline contract.
- **Factory / composition root (creational):** module-level setup constructs engine, repository, extractor, pipeline, and service in one place; production can move this to app lifespan/DI.
- **DTO / Data Mapper (structural):** Pydantic request/response types and SQLAlchemy mapping keep transport/storage types out of the domain.
- **Observer:** intentionally deferred; no asynchronous notification need exists in the MVP. Add only when reminders/background jobs are introduced.
- **Singleton:** intentionally not required. Database engine is one application-scoped object by composition, while test instances should remain isolated.

## API method boundaries

`OrganizerService.create_item(title, kind, body, source, category, due_at?, source_ref?) → OrganizerItem`

`OrganizerService.list_items(query?) → Iterable[OrganizerItem]`

`ItemRepository.save(item) → OrganizerItem`; `get(UUID) → OrganizerItem | None`; `list(query?) → Iterable[OrganizerItem]`; `delete(UUID) → bool`

`ContentExtractor.extract(text, source) → list[Candidate]`

## Error and concurrency policy

HTTP validation failures return 4xx responses. Missing resources return 404. Import size is bounded before full read. Database operations use short SQLAlchemy transactions. The first MVP is single-user; if multiple editors are introduced, add a version column and compare-and-swap update to prevent lost updates. Suggestions are currently in process memory and are cleared on restart; persist import batches/suggestions before a multi-session deployment.
