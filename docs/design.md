# Low-level design

## Layering

`FastAPI router → OrganizerService → ItemRepository interface → SQLAlchemy adapter → SQLite`

File imports use `router → AgentPipeline → ContentExtractor strategy → Candidate`; candidates become persistent items only after explicit acceptance. Calendar reads use the `GoogleSource` adapter. Gmail backfill uses `GmailMessagesConnector → GmailMailboxScanner → LocalEmailTriageAgent → GmailTriageStore`; its Ollama model is constrained to loopback and selects only security, meeting/evaluation, deadline, or payment actions. It extracts summaries and dates. Users may persistently pin summaries or create a Google Calendar event from a meeting review. Google Photos Picker is a separate user-selection adapter; selected image bytes pass to local OCR. OAuth credentials remain in Keychain. Domain objects do not depend on FastAPI or SQLAlchemy.

## Main classes and responsibilities

| Type | Responsibility |
|---|---|
| `OrganizerItem` | Domain entity; owns item state and update behavior |
| `ItemKind`, `ItemStatus` | Closed vocabularies for item type and lifecycle |
| `Candidate` | Immutable agent proposal with provenance, category, deadline/meeting times, and pin state; not yet a saved item |
| `OrganizerService` | Coordinates validation and domain/repository operations |
| `ItemRepository` | Persistence port: save/get/list/delete |
| `SqlAlchemyItemRepository` | SQLite persistence adapter and row/domain mapping |
| `ContentExtractor` | Abstraction for extraction strategies |
| `RuleBasedExtractor` | Offline, explainable baseline extraction agent |
| `photo_ocr.extract_text` | Normalizes an explicitly uploaded image and invokes local Tesseract OCR; discards temporary image files |
| `AgentPipeline` | Orchestrates the selected extractor |
| `GoogleSource` | Read-only Google connector interface returning `Candidate` proposals |
| `CalendarConnector` | Fetch and map Calendar events into candidates |
| `GmailMessagesConnector` | List and fetch Gmail messages through the read-only API |
| `GmailMailboxScanner` | Paginate the mailbox, resume from SQLite checkpoints, and forward important candidates |
| `LocalEmailTriageAgent` | Use a local Ollama model to select four actionable categories and extract concise summaries and dates |
| `GmailTriageStore` | Persist processed IDs, scan progress, action fields, pin state, and Calendar event ID; never stores raw email bodies |
| `google_integrations` OAuth functions | Authorize, refresh and store credentials in Keychain; read Calendar and create events only after a review action |
| Google Photos Picker functions | Create selection sessions, retrieve selected image bytes and invoke local OCR |
| `ItemCreate`, `ItemResponse` | API input/output contracts |

## Object-oriented principles

- **Abstraction:** `ItemRepository`, `ContentExtractor`, and `GoogleSource` hide infrastructure and algorithm details.
- **Encapsulation:** `OrganizerItem.update()` owns state transitions and refreshes `updated_at`; service keeps collaborators private.
- **Inheritance:** concrete repository and extractor adapters implement abstract base classes.
- **Polymorphism:** service accepts any `ItemRepository`; pipeline accepts any `ContentExtractor`; Google sync accepts any `GoogleSource` connector.

## SOLID mapping

- **SRP:** API validates/translates HTTP; service coordinates; repository persists; extractor proposes candidates; domain owns item behavior.
- **OCP:** add an extractor, persistence adapter, or source connector by implementing its port, without rewriting the service or sync coordinator.
- **LSP:** repository implementations preserve save/get/list/delete semantics; extractors and Google source connectors return valid `Candidate` values.
- **ISP:** focused ports (`ItemRepository`, `ContentExtractor`, `GoogleSource`) avoid a broad all-purpose integration interface.
- **DIP:** high-level `OrganizerService` depends on `ItemRepository`; `AgentPipeline` depends on `ContentExtractor`; Google sync depends on the `GoogleSource` abstraction.

## Patterns and rationale

- **Repository (structural):** isolates domain/service from persistence.
- **Strategy (behavioral):** extraction algorithm can switch from rules to local model while preserving pipeline contract. OCR is a local adapter invoked only for user-selected images.
- **Adapter (structural):** Calendar and Gmail connectors map third-party REST responses to internal candidates; the Photos Picker adapts explicit selections to the same candidate review flow.
- **Factory / composition root (creational):** module-level setup constructs engine, repository, extractor, pipeline, and service in one place; production can move this to app lifespan/DI.
- **DTO / Data Mapper (structural):** Pydantic request/response types and SQLAlchemy mapping keep transport/storage types out of the domain.
- **Observer:** intentionally deferred; current refresh is a simple app-lifespan timer, not a notification bus. Add Observer if item changes need multiple independent subscribers.
- **Singleton:** intentionally not required. Database engine is one application-scoped object by composition, while test instances should remain isolated.

## API method boundaries

`OrganizerService.create_item(title, kind, body, source, category, due_at?, source_ref?) → OrganizerItem`

`OrganizerService.list_items(query?) → Iterable[OrganizerItem]`

`ItemRepository.save(item) → OrganizerItem`; `get(UUID) → OrganizerItem | None`; `list(query?) → Iterable[OrganizerItem]`; `delete(UUID) → bool`

`LocalEmailTriageAgent.classify(subject, sender, received_at, body) → EmailTriageResult(important, category, summary, due_at?, event_start_at?, event_end_at?)`

`GmailTriageStore.set_pinned(message_id, pinned) → None`; `saved_emails() → list[SavedEmail]`

`google.create_calendar_event(event_id, title, description, start_at, end_at?) → Google event`; a deterministic event ID makes retries idempotent.

`ContentExtractor.extract(text, source) → list[Candidate]`

## Error and concurrency policy

HTTP validation failures return 4xx responses. Missing resources return 404. Import size is bounded before full read (1 MB for text/calendar; 10 MB for photos). Photo OCR runs in a worker thread, limits decoded image dimensions, and removes temporary files after processing. Database operations use short SQLAlchemy transactions. The first MVP is single-user; if multiple editors are introduced, add a version column and compare-and-swap update to prevent lost updates. Suggestions are currently in process memory and are cleared on restart; persist import batches/suggestions before a multi-session deployment.
