# Architectural Decision Record: Foundation & Recon Engine

## ADR-001: File-Backed JSON Store for Spec Storage

### Decision
Use a file-backed JSON store (one JSON file per entity, with a queryable index file) for the spec store in the PoC phase.

### Alternatives Considered
1. **SQLite** — Single-file relational database. Better query performance, ACID transactions, and concurrent access. Higher implementation complexity for the PoC. Harder to inspect and diff in git.
2. **PostgreSQL** — Full relational database. Best query performance, concurrent access, and data integrity. Overkill for the PoC. Introduces infrastructure dependency (Docker or managed service).
3. **Pydantic + JSON** — Pydantic models serialized to JSON. Stronger validation but heavier dependency.

### Trade-offs
- **Chosen (file-backed JSON)**:
  - Pro: Git-friendly (entities are diffable, committable, inspectable)
  - Pro: Zero infrastructure dependency (no database to run)
  - Pro: Simple implementation with `json` stdlib module
  - Pro: Natural fit for immutable snapshots (just copy the files)
  - Con: No ACID transactions (file locking via `filelock` is a workaround)
  - Con: Query performance degrades with thousands of facts (mitigated by index.json)
  - Con: No concurrent write safety beyond file locking
- **SQLite upgrade path**: When fact count exceeds ~5,000 or concurrent module execution causes lock contention, migrate to SQLite. The `SpecStore` class API stays the same -- only the storage backend changes.
- **PostgreSQL upgrade path**: When the system needs multi-user access, remote storage, or production deployment, migrate to PostgreSQL. Same API surface.

### AI Consensus
All three consultations agreed on file-backed JSON for the PoC. Codex and Opus both noted the SQLite upgrade path. Gemini flagged "Fact Explosion" risk (thousands of Reddit posts) which reinforces the need for the upgrade path, but agreed file-based is right for PoC.

---

## ADR-002: Frozen Dataclasses vs Pydantic

### Decision
Use Python `dataclasses` (with `frozen=True` for Facts) for all data model definitions. No Pydantic dependency.

### Alternatives Considered
1. **Pydantic v2** — Automatic validation, serialization, JSON Schema generation. Richer field constraints. Heavier dependency (~2MB). Runtime validation overhead.
2. **attrs** — Similar to dataclasses but with more features (validators, converters). Smaller than Pydantic but still an extra dependency.
3. **TypedDict** — No runtime overhead, pure type hints. No validation, no methods, harder to enforce contracts.

### Trade-offs
- **Chosen (dataclasses)**:
  - Pro: Zero dependencies (stdlib)
  - Pro: `frozen=True` enforces immutability at the Python level
  - Pro: `to_dict()`/`from_dict()` methods give full control over serialization
  - Pro: AI models generate clean dataclass code with high reliability
  - Con: No automatic validation (must write `__post_init__` checks manually)
  - Con: No automatic JSON Schema generation (must maintain `spec-schema.json` separately)
- **Pydantic upgrade path**: If validation complexity grows (many inter-field constraints, nested validation), adopt Pydantic v2. The `to_dict()`/`from_dict()` pattern maps directly to Pydantic's `model_dump()`/`model_validate()`.

### AI Consensus
Opus proposed frozen dataclasses with explicit serialization. Codex leaned toward Pydantic for validation but acknowledged the dependency cost. Gemini preferred simplicity. Decision: start with dataclasses, upgrade if validation becomes painful. The Pydantic upgrade path is straightforward because the API shape is compatible.

---

## ADR-003: Async Recon Module Interface

### Decision
The `ReconModule.run()` method is `async`. All recon modules are async-first.

### Alternatives Considered
1. **Synchronous interface** — Simpler to implement and debug. Blocks during I/O (HTTP requests, browser automation, file writes).
2. **Thread-based parallelism** — Sync interface with `ThreadPoolExecutor` in the orchestrator. Avoids async complexity but introduces thread-safety concerns.
3. **Process-based parallelism** — Subprocess per module. Maximum isolation but high overhead and complex IPC.

### Trade-offs
- **Chosen (async)**:
  - Pro: Concurrent module execution without threads (orchestrator runs multiple modules in an event loop)
  - Pro: Natural fit for I/O-bound work (HTTP requests, browser automation, API calls)
  - Pro: Playwright and browser-use are already async
  - Pro: Progress callbacks work naturally with async (no blocking)
  - Con: Async adds complexity (event loop management, async file I/O considerations)
  - Con: CPU-bound work (video transcription) still blocks the event loop (mitigate with `asyncio.to_thread()`)
- **Hybrid approach**: CPU-bound operations within modules (e.g., whisper transcription) use `asyncio.to_thread()` to avoid blocking the event loop. The interface stays async; the implementation can mix sync and async internally.

### AI Consensus
All three consultations agreed on async. Codex explicitly specified the `ReconRequest`/`ReconServices` pattern for dependency injection, which pairs naturally with async. Opus designed the progress callback as a sync callable (called from async context), which keeps progress reporting simple.

---

## ADR-004: Authority Fixed Per Module, Not Per Fact

### Decision
Each recon module declares a fixed `authority` property. All facts produced by that module inherit this authority. Authority is never set per-fact by the module implementation.

### Alternatives Considered
1. **Per-fact authority** — Each module decides the authority of each fact individually based on the source URL, page type, or content analysis. More granular but error-prone.
2. **Authority inference** — A centralized classifier inspects fact content and source URL to determine authority. More accurate for edge cases but adds complexity and a single point of failure.

### Trade-offs
- **Chosen (per-module)**:
  - Pro: Simple, impossible to get wrong in module code (the module just produces facts, authority is automatic)
  - Pro: Enforced by INV-013 (module-authority consistency) -- the orchestrator rejects violations
  - Pro: Authority classification rules are centralized in the architecture, not scattered across 7 module implementations
  - Con: Cannot handle intra-module authority variance (e.g., a help center module that encounters both vendor pages and community wiki pages)
  - Con: Edge cases (official vs third-party videos) must be handled by module design, not per-fact tagging
- **Mitigation for edge cases**: Modules that encounter mixed-authority content should either (a) only process content matching their declared authority, or (b) be split into separate modules (e.g., `video_official` vs `video_community`). For the PoC, this is not expected to be a problem because each module targets a specific source class.

### AI Consensus
Opus and Codex both chose per-module authority. Codex highlighted the risk of authority drift if modules make per-fact decisions. Gemini's simpler model implicitly assumed per-module authority. The per-module approach was unanimous.

---

## ADR-005: Terminal Contradiction Status vs In-Place Edit

### Decision
When a fact is contradicted, its status becomes `CONTRADICTED` (terminal). The fact is never edited or deleted. If new evidence resolves the contradiction, a new Fact is created with `supersedes` pointing to the contradicted one.

### Alternatives Considered
1. **In-place edit** — Update the fact's content when contradicted, keeping the same ID. Simpler conceptually but destroys history.
2. **Soft delete + replace** — Mark the old fact as deleted and create a new one. Preserves some history but breaks the provenance chain (no explicit link between old and new).
3. **Versioned facts** — Each fact has a version number that increments on edit. Content changes create new versions under the same ID. More complex but preserves full history.

### Trade-offs
- **Chosen (terminal + supersede chain)**:
  - Pro: Full provenance is preserved. You can always ask "what did we believe before, and why did it change?"
  - Pro: Immutable facts are simpler to reason about (no concurrent mutation concerns)
  - Pro: Snapshot integrity is guaranteed (facts in a snapshot never change after the snapshot is taken)
  - Pro: Audit trail is built-in (follow the supersedes chain to see the evolution of understanding)
  - Con: More facts in the store over time (but storage is cheap and pruning is possible)
  - Con: Querying "current belief" requires filtering out contradicted and superseded facts (mitigated by `get_facts_for_feature()` which does this by default)
- **The provenance chain is the product**: For a system that reverse-engineers SaaS applications, knowing *why* a belief changed is as valuable as knowing the current belief. The supersede chain is not overhead -- it is the intelligence layer's memory.

### AI Consensus
Opus designed the full supersede chain with `contradicted_by` and `supersedes` fields. Codex explicitly called out "replacement means new fact revision or new fact linked by `contradicts`." Gemini noted that both contradicting facts should transition to `contradicted` for adjudication, which is a stricter variant. Decision follows Opus/Codex: the contradicting (higher-authority) fact stays in its current status; only the contradicted fact transitions. This avoids unnecessary status churn on the winning fact.
