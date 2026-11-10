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
    }
    OrganizerService --> ItemRepository : depends on
    OrganizerService --> AgentPipeline : uses
    SqlAlchemyItemRepository ..|> ItemRepository : implements
    RuleBasedExtractor ..|> ContentExtractor : implements
    AgentPipeline --> ContentExtractor : strategy
    AgentPipeline ..> Candidate : creates
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
