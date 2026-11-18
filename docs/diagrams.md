# Diagrams

## Class diagram

```mermaid
classDiagram
    class OrganizerItem {
      +UUID id
      +string title
      +ItemKind kind
      +string body
      +datetime due_at
      +string source
      +string category
      +ItemStatus status
      +update(...) void
    }
    class Candidate {
      +string title
      +string body
      +ItemKind kind
      +string category
      +string source_ref
    }
    class OrganizerService {
      +create_item(...) OrganizerItem
      +list_items(query) Iterable
      +get_item(id) OrganizerItem
      +delete_item(id) bool
      +suggest(text, source) Candidate[]
    }
    class ItemRepository {
      <<interface>>
      +save(item) OrganizerItem
      +get(id) OrganizerItem
      +list(query) Iterable
      +delete(id) bool
    }
    class SqlAlchemyItemRepository
    class ContentExtractor {
      <<interface>>
      +extract(text, source) Candidate[]
    }
    class RuleBasedExtractor
    class AgentPipeline {
      +process(text, source) Candidate[]
      +process_photo_text(text, source) Candidate[]
    }
    class PhotoOCR {
      +extract_text(image_bytes, filename) string
    }
    OrganizerService --> ItemRepository : depends on
    OrganizerService --> AgentPipeline : uses
    SqlAlchemyItemRepository ..|> ItemRepository : implements
    RuleBasedExtractor ..|> ContentExtractor : implements
    AgentPipeline --> ContentExtractor : strategy
    AgentPipeline ..> Candidate : creates
    PhotoOCR ..> AgentPipeline : supplies local OCR text
    ItemRepository o--> OrganizerItem : stores 0..*
    OrganizerService ..> OrganizerItem : creates
```

Composition is appropriate for service-to-repository/pipeline references; the repository aggregates independently-lived items. Each item has one repository identity; one service can use one repository and one pipeline. The arrow/cardinality notation is conceptual; Python object references are not ownership of item lifetimes.

## Sequence: import and accept

```mermaid
sequenceDiagram
    actor User
    participant API as Import API
    participant Pipeline as AgentPipeline
    participant Extractor as RuleBasedExtractor
    participant Service as OrganizerService
    participant Repo as ItemRepository
    User->>API: Upload authorized .txt/.ics file
    API->>API: Validate extension, size, UTF-8
    API->>Pipeline: process(text, filename)
    Pipeline->>Extractor: extract(text, source)
    Extractor-->>Pipeline: Candidate[]
    Pipeline-->>API: suggestions
    API-->>User: suggestions for review
    User->>API: Accept suggestion ID
    API->>Service: create item from Candidate
    Service->>Repo: save(OrganizerItem)
    Repo-->>Service: persisted item
    Service-->>API: OrganizerItem
    API-->>User: 201 item response
```

## Activity: capture/search

```mermaid
flowchart TD
    A[Start] --> B{Capture or search?}
    B -->|Manual capture| C[Validate fields]
    C -->|Invalid| D[Return validation error]
    C -->|Valid| E[Create domain item]
    E --> F[Persist through repository]
    B -->|Import| G[Validate user-selected file]
    G -->|Invalid| D
    G -->|Valid| H[Extract suggestions]
    H --> I{User accepts?}
    I -->|No| J[Discard or leave for review]
    I -->|Yes| E
    B -->|Search| K[Query repository]
    K --> L[Return matching items]
    F --> M[End]
    J --> M
    L --> M
```


## Sequence: selected iPhone photo to reviewable note

```mermaid
sequenceDiagram
    actor User
    participant Browser as iOrganizer UI
    participant API as Import API
    participant OCR as Local Tesseract OCR
    participant Pipeline as AgentPipeline
    participant UserReview as Suggestion review
    User->>Browser: Select photo on iPhone and AirDrop to Mac
    User->>Browser: Choose the photo file
    Browser->>API: POST /api/v1/imports (multipart image)
    API->>API: Check extension and 10 MB limit
    API->>OCR: Decode temporary image and recognize text
    OCR-->>API: OCR text; temporary image removed
    API->>Pipeline: process_photo_text(text, filename)
    Pipeline-->>API: Task candidates or one note candidate
    API-->>Browser: Suggestions with photo provenance
    User->>UserReview: Review and accept or dismiss
    UserReview->>API: POST /api/v1/suggestions/{id}/accept
    API-->>Browser: Saved item response
```

## Google Calendar and Gmail sync

```mermaid
sequenceDiagram
    actor User
    participant UI as Local UI
    participant API as FastAPI
    participant OAuth as Google OAuth
    participant Keychain as macOS Keychain
    participant Source as Calendar/Gmail adapters
    participant Review as Suggestion inbox
    User->>UI: Connect Google
    UI->>API: GET /google/authorize
    API-->>User: Redirect to consent with state + read-only scopes
    User->>OAuth: Approve access
    OAuth->>API: Redirect callback with code + state
    API->>API: Validate one-time state
    API->>OAuth: Exchange code for tokens
    API->>Keychain: Store credentials
    API->>Source: Fetch authorized records
    Source-->>API: Candidate proposals with source IDs
    API->>Review: Deduplicate and queue proposals
    API-->>UI: Redirect to organizer
    User->>UI: Review and accept selected proposal
    UI->>API: POST /suggestions/{id}/accept
    API-->>UI: Persisted item
```

## Google Photos selection

```mermaid
sequenceDiagram
    actor User
    participant UI as Local UI
    participant API as FastAPI
    participant Picker as Google Photos Picker
    participant OCR as Local Tesseract
    User->>UI: Choose Google Photos
    UI->>API: POST /google/photos/sessions
    API->>Picker: Create session capped at 10 items
    Picker-->>UI: Picker URI and recommended poll interval
    User->>Picker: Select photos
    UI->>API: Poll session status
    API->>Picker: GET session / selected media items
    Picker-->>API: Selected image bytes
    API->>OCR: OCR temporary bytes
    OCR-->>API: Extracted text
    API-->>UI: Reviewable candidates
    User->>UI: Accept chosen candidates
```

## Full Gmail triage

```mermaid
sequenceDiagram
    participant Scheduler as App scheduler
    participant Scanner as GmailMailboxScanner
    participant Gmail as Gmail API
    participant Agent as LocalEmailTriageAgent
    participant Ollama as Loopback Ollama
    participant Store as GmailTriageStore / SQLite
    participant Review as Suggestion inbox
    Scheduler->>Scanner: Start or resume scan
    loop Each Gmail page (up to 500 IDs)
        Scanner->>Gmail: messages.list(pageToken)
        Gmail-->>Scanner: Message IDs + nextPageToken
        Scanner->>Store: Skip IDs already classified
        loop Each new message
            Scanner->>Gmail: messages.get(format=full)
            Gmail-->>Scanner: Headers and text body
            Scanner->>Agent: Subject, sender, date, body
            Agent->>Ollama: Local JSON importance + summary request
            Ollama-->>Agent: important/category/summary
            alt Important
                Agent->>Store: Save summary and pending review state
                Agent->>Review: Add summarized Candidate
            else Not important
                Agent->>Store: Save processed ID/decision only
            end
        end
        Scanner->>Store: Save page checkpoint and progress
    end
    Review-->>Scanner: User accepts or dismisses
    Scanner->>Store: Persist review state
```
