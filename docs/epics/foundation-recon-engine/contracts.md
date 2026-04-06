# Contracts: Foundation & Recon Engine

## 1. Canonical Data Model

### 1.1 Enums (shared across all entities)

```python
from enum import StrEnum


class Authority(StrEnum):
    """
    Source trustworthiness tier — see Authority Classification Rules.

    Rank ordering (for "equal or higher" guards):
      AUTHORITATIVE (3) > OBSERVATIONAL (2) > ANECDOTAL (1)
    Use Authority.rank() for ordinal comparisons — do NOT compare string values.
    """
    AUTHORITATIVE = "authoritative"   # Live app observation, official API docs
    OBSERVATIONAL = "observational"   # Help center, training videos
    ANECDOTAL = "anecdotal"           # Marketing, Reddit, changelog

    def rank(self) -> int:
        return {"authoritative": 3, "observational": 2, "anecdotal": 1}[self.value]

    def __ge__(self, other: "Authority") -> bool:
        return self.rank() >= other.rank()

    def __gt__(self, other: "Authority") -> bool:
        return self.rank() > other.rank()


class Confidence(StrEnum):
    """
    Confidence level for a fact or spec item.

    Rank ordering (for "equal or higher" guards):
      HIGH (3) > MEDIUM (2) > LOW (1)
    Use Confidence.rank() for ordinal comparisons — do NOT compare string values.
    """
    HIGH = "high"       # Multiple authoritative sources agree, or directly observed
    MEDIUM = "medium"   # Single authoritative source, or multiple observational agree
    LOW = "low"         # Single observational/anecdotal, or sources conflict

    def rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1}[self.value]

    def __ge__(self, other: "Confidence") -> bool:
        return self.rank() >= other.rank()

    def __gt__(self, other: "Confidence") -> bool:
        return self.rank() > other.rank()


class FactStatus(StrEnum):
    UNVERIFIED = "unverified"     # Extracted but not cross-referenced
    VERIFIED = "verified"         # Confirmed by >=1 independent source at same or higher authority
    CONTRADICTED = "contradicted" # Another fact at equal/higher authority disagrees


class BundleStatus(StrEnum):
    DRAFT = "draft"           # Accumulating facts, not yet synthesised
    VALIDATED = "validated"   # All validation checks pass (no contradicted facts, coverage complete), ready for snapshot
    SNAPSHOT = "snapshot"     # Immutable, hashed, committed to output repo


class FactCategory(StrEnum):
    """What aspect of the target this fact describes."""
    UI_COMPONENT = "ui_component"       # Visual element (button, modal, sidebar)
    USER_FLOW = "user_flow"             # Multi-step interaction sequence
    API_ENDPOINT = "api_endpoint"       # REST/GraphQL endpoint
    WS_EVENT = "ws_event"               # WebSocket event contract
    DATA_MODEL = "data_model"           # Entity, field, relationship
    AUTH = "auth"                        # Authentication/authorization behavior
    BUSINESS_RULE = "business_rule"     # Validation, permission, constraint
    PERFORMANCE = "performance"         # Timing, rate limit, pagination
    INTEGRATION = "integration"         # Third-party service interaction
    CONFIGURATION = "configuration"     # Settings, preferences, defaults


class SourceType(StrEnum):
    """Which recon source produced the fact."""
    LIVE_APP = "live_app"
    API_DOCS = "api_docs"
    HELP_CENTER = "help_center"
    VIDEO = "video"
    MARKETING = "marketing"
    COMMUNITY = "community"
    CHANGELOG = "changelog"


class RedactionStatus(StrEnum):
    """Whether secret material has been scrubbed from fact content."""
    CLEAN = "clean"           # No sensitive material present
    REDACTED = "redacted"     # Sensitive material was removed
    RESTRICTED = "restricted" # Entire fact is access-controlled
```

### 1.2 Evidence Reference

Every Fact must carry at least one EvidenceRef — the verifiable pointer back to the source material.

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import hashlib
import json
import uuid


@dataclass(frozen=True)
class EvidenceRef:
    """
    Pointer to a specific piece of source material.
    Must be sufficient to re-locate and re-verify the claim.
    """
    source_url: str                        # URL, file path, or deep-link
    locator: str | None = None             # CSS selector, JSON path, video timestamp,
                                           # WS event name, DOM path, endpoint
    source_title: str | None = None        # Human-readable page/doc title
    artifact_uri: str | None = None        # Path to captured artifact (screenshot, HAR, transcript)
    artifact_sha256: str | None = None     # Hash of the captured artifact for integrity
    captured_at: str = ""                  # ISO 8601 UTC when captured
    published_at: str | None = None        # When the source was published (if known)
    raw_excerpt: str | None = None         # Verbatim source text (max 2000 chars)

    def __post_init__(self):
        if not self.captured_at:
            object.__setattr__(self, "captured_at",
                               datetime.now(timezone.utc).isoformat())
```

### 1.3 Fact

The atomic unit of intelligence. Every recon module produces Facts. Facts are frozen (immutable content) once created — updates create new Facts that reference the original via `supersedes`.

```python
@dataclass(frozen=True)
class Fact:
    """
    The core data structure produced by every recon module.

    Immutable once created. If a fact needs correction, create a new Fact
    with `supersedes` pointing to the original's id.
    """
    # Content — immutable after creation (required fields first)
    feature: str               # Which scoped feature this relates to (e.g. "boards", "drag-drop")
    category: FactCategory     # What kind of fact (UI, API, data model, etc.)
    claim: str                 # Human-readable atomic assertion (one sentence, one testable claim)
    evidence: list[EvidenceRef]  # At least one required — verifiable source pointers

    # Provenance — required, no default
    source_type: SourceType    # Which recon source class produced this

    # Auto-generated ID (after required fields to satisfy dataclass ordering)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # Structured evidence — schema depends on category
    # e.g. {"method": "POST", "path": "/1/boards", "params": [...]}
    # e.g. {"component": "CardModal", "fields": ["title", "desc", "labels"]}
    structured_data: dict[str, Any] = field(default_factory=dict)

    # Provenance
    module_name: str = ""      # Concrete module name (e.g. "browser_explore")
    authority: Authority = Authority.ANECDOTAL
    confidence: Confidence = Confidence.LOW
    run_id: str = ""           # UUID of the recon run that produced this fact

    # Lifecycle — these are the only mutable fields (via update_fact_status)
    status: FactStatus = FactStatus.UNVERIFIED
    verified_by: list[str] = field(default_factory=list)      # IDs of corroborating Facts
    contradicted_by: list[str] = field(default_factory=list)   # IDs of contradicting Facts
    supersedes: str | None = None  # ID of the Fact this replaces (supersede chain)
    corroborates: list[str] = field(default_factory=list)      # IDs of facts this supports
    contradicts: list[str] = field(default_factory=list)       # IDs of facts this opposes
    revision: int = 1              # Incremented when superseded (new fact gets old.revision + 1)

    # Soft deletion (INV-027)
    deleted_at: str | None = None          # ISO 8601 UTC when soft-deleted, None if active

    # Freshness
    observed_at: str | None = None         # When the behavior was observed in the target
    freshness_ttl_days: int | None = None  # How long before this fact should be re-verified

    # Metadata
    tags: list[str] = field(default_factory=list)
    redaction_status: RedactionStatus = RedactionStatus.CLEAN
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def content_hash(self) -> str:
        """
        Deterministic hash of the fact's semantic content (excludes lifecycle fields).
        Used for deduplication and snapshot integrity.
        Includes source identifiers to distinguish same-claim from different sources.
        """
        canonical = json.dumps({
            "feature": self.feature,
            "category": self.category,
            "claim": self.claim,
            "structured_data": self.structured_data,
            "evidence": [
                {
                    "source_url": e.source_url,
                    "locator": e.locator,
                    "artifact_sha256": e.artifact_sha256,
                }
                for e in self.evidence
            ],
            "module_name": self.module_name,
            "source_type": self.source_type,
        }, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]
```

#### Fact Validation Rules

1. **Atomic claim**: Each Fact contains exactly one testable assertion. Multi-part claims must be split into separate Facts.
2. **Evidence required**: At least one `EvidenceRef` with a non-empty `source_url`.
3. **Authority-source consistency**: `authority` must match the `source_type` according to the Module-to-Authority mapping table. A `community` fact is always ANECDOTAL.
4. **Verification guard**: A fact can only reach `verified` status if at least one corroborating fact exists at the required authority level. For ANECDOTAL facts, only AUTHORITATIVE corroboration verifies (OBSERVATIONAL raises confidence but does not flip status). For OBSERVATIONAL/AUTHORITATIVE facts, corroboration at equal or higher authority verifies. See Cross-Reference Rules 1 and 4.
5. **Contradiction guard**: `contradicted` status requires at least one opposing fact ID in `contradicted_by`.
6. **Revision creates new fact**: Changing `claim`, `structured_data`, or `evidence` creates a new Fact with `supersedes` pointing to the original. The original is never mutated.
7. **No secrets in content**: Secret values (API keys, passwords, session tokens) and raw PII never persist in `claim`, `structured_data`, or `evidence`. Use redaction before storage.

### 1.4 SpecBundle

A versioned collection of synthesised specs, representing a snapshot of intelligence about the target.

```python
@dataclass
class SpecItem:
    """A single synthesised specification within a bundle."""
    feature: str               # Feature key
    spec_type: str             # e.g. "api_contract", "ui_behavior", "data_model"
    content: dict[str, Any]    # The synthesised specification
    supporting_fact_ids: list[str] = field(default_factory=list)
    confidence: Confidence = Confidence.LOW


@dataclass
class SpecBundle:
    """
    A versioned collection of synthesised specs for a set of features.
    Transitions: draft -> validated -> snapshot (immutable).
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: str = "1.0"            # Schema format version
    target: str = ""                       # e.g. "trello.com"
    scope_id: str = ""                     # UUID of the Scope that defines this bundle's coverage
    scope_hash: str = ""                   # Deterministic hash of the scope
    scope: list[str] = field(default_factory=list)  # Feature keys covered
    version: int = 1                       # Monotonically increasing per (target, scope_hash)
    status: BundleStatus = BundleStatus.DRAFT
    spec_items: list[SpecItem] = field(default_factory=list)
    fact_ids: list[str] = field(default_factory=list)  # All supporting Facts
    parent_id: str | None = None           # Previous bundle version (for diff)

    # Computed at snapshot time
    content_hash: str | None = None        # SHA-256 of canonical manifest
    snapshot_at: str | None = None         # ISO 8601 UTC when frozen

    # Metadata
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    validated_at: str | None = None
    notes: str = ""

    def compute_hash(self, facts: list[Fact]) -> str:
        """
        Compute deterministic hash from the canonical manifest.
        Manifest includes schema_version, scope_hash, sorted spec_items,
        and sorted supporting fact (fact_id, content_hash) tuples.
        """
        manifest = {
            "schema_version": self.schema_version,
            "scope_hash": self.scope_hash,
            "spec_items": sorted(
                [{"feature": s.feature, "spec_type": s.spec_type,
                  "content": s.content} for s in self.spec_items],
                key=lambda x: (x["feature"], x["spec_type"])
            ),
            "supporting_facts": sorted(
                [(f.id, f.content_hash()) for f in facts],
                key=lambda x: x[0]
            ),
        }
        canonical = json.dumps(manifest, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]
```

#### Bundle Validation Rules

1. **Version monotonicity**: `version` is strictly monotonically increasing per `(target, scope_hash)`.
2. **Validation prerequisite**: `validated` status requires schema pass plus no contradicted supporting facts.
3. **Snapshot immutability**: `snapshot` status is terminal and immutable. No modifications, no deletions.
4. **Hash determinism**: `content_hash` is `sha256(canonical_json(manifest))` and must be reproducible across machines given the same inputs.
5. **Spec-to-fact traceability**: Every `SpecItem` in a bundle must trace back to at least one non-contradicted fact via `supporting_fact_ids`.

### 1.5 Scope

```python
@dataclass
class ScopeNode:
    """A single feature in the user-specified scope."""
    feature: str                         # Canonical name, lowercase slug (e.g. "boards", "drag-drop")
    label: str = ""                      # Human-readable name
    description: str = ""                # Optional clarification
    inclusion_reason: str = "requested"  # "requested" | "dependency" | "implied"
    status: str = "in_scope"             # "in_scope" | "excluded" | "unknown"
    depends_on: list[str] = field(default_factory=list)
    priority: int = 1                    # 1=core, 2=important, 3=nice-to-have


@dataclass
class DependencyEdge:
    """A directed edge in the feature dependency graph."""
    from_feature: str
    to_feature: str
    kind: str = "requires"  # "requires" | "enhances" | "conflicts_with"


@dataclass
class Scope:
    """
    Parsed and enriched scope from user input.
    Lifecycle: parsed -> resolved -> frozen.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    target: str = ""                     # e.g. "trello.com"
    raw_input: str = ""                  # Original user string
    requested_features: list[ScopeNode] = field(default_factory=list)
    resolved_features: list[ScopeNode] = field(default_factory=list)
    dependency_edges: list[DependencyEdge] = field(default_factory=list)
    known_exclusions: list[str] = field(default_factory=list)
    unknown_features: list[str] = field(default_factory=list)
    frozen: bool = False
    scope_hash: str = ""                 # Deterministic from requested + resolved
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def feature_keys(self) -> list[str]:
        return [f.feature for f in self.resolved_features]

    def dependency_order(self) -> list[list[str]]:
        """
        Return features in topological waves (features with no unresolved
        deps first). Each inner list is a parallelizable wave.
        """
        remaining = {f.feature: set(f.depends_on) for f in self.resolved_features}
        waves: list[list[str]] = []
        while remaining:
            wave = [n for n, deps in remaining.items()
                    if not deps - {n for w in waves for n in w}]
            if not wave:
                raise ValueError(
                    f"Circular dependency detected among: {list(remaining)}"
                )
            waves.append(sorted(wave))
            for n in wave:
                del remaining[n]
        return waves

    def compute_scope_hash(self) -> str:
        """Deterministic hash from requested features + resolved dependency set."""
        canonical = json.dumps({
            "requested": sorted(f.feature for f in self.requested_features),
            "resolved": sorted(f.feature for f in self.resolved_features),
            "edges": sorted(
                [(e.from_feature, e.to_feature, e.kind) for e in self.dependency_edges]
            ),
        }, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]
```

#### Scope Validation Rules

1. **Normalized keys**: Feature keys are lowercase slugs (letters, digits, hyphens only).
2. **Transitive inclusion**: Every transitively required feature carries `inclusion_reason="dependency"`.
3. **Acyclic graph**: The `requires` dependency graph must be acyclic after cycle compaction.
4. **Deterministic hash**: `scope_hash` is deterministic from requested features plus resolved dependency set.
5. **Frozen per run**: Once frozen, scope cannot be expanded. New discoveries outside scope become flagged in `unknown_features` or queued findings, not silent scope expansion.

---

## 2. Recon Module Interface Contract

Every recon module (#8-#14) implements a single abstract base class. The orchestrator (#15) calls `run()` and receives a `ReconResult` -- it never needs to know module internals.

### 2.1 Request and Services

```python
@dataclass
class ReconRequest:
    """Everything a module needs to execute a recon pass."""
    run_id: str                            # UUID for this recon run
    target: str                            # Target URL or domain (e.g. "trello.com")
    base_url: str = ""                     # Full base URL if different from target
    scope: Scope = field(default_factory=Scope)
    credential_refs: list[str] = field(default_factory=list)  # Keychain key names
    prior_snapshot_id: str | None = None   # Previous snapshot for delta recon
    checkpoint: dict[str, Any] | None = None  # Resume state from previous partial run
    budgets: dict[str, int] = field(default_factory=dict)  # time_seconds, max_pages, max_requests
    module_config: dict[str, Any] = field(default_factory=dict)  # Module-specific settings


@dataclass
class ReconServices:
    """Shared services injected by the orchestrator."""
    spec_store: Any        # SpecStore instance
    credentials: dict[str, str]  # Pre-fetched credentials (keys from module.requires_credentials)
    artifact_store: Any    # For saving screenshots, HARs, transcripts
    http_client: Any       # Pre-configured HTTP client (respects rate limits)
    browser: Any | None    # Playwright browser instance (if needed)
    clock: Any             # For testable time
    # NOTE: No keychain accessor. Modules NEVER access the keychain directly.
    # The orchestrator pre-fetches credentials and injects them here (INV-029).
```

### 2.2 Result Types

```python
class ReconModuleStatus(StrEnum):
    SUCCESS = "success"           # All sources processed
    PARTIAL = "partial"           # Some sources failed, but usable facts were extracted
    FAILED = "failed"             # Module could not produce any facts
    SKIPPED = "skipped"           # Module not applicable for this scope/target


@dataclass
class CoverageEntry:
    """Per-feature coverage report from a module."""
    feature: str
    status: str = "not_found"  # "observed" | "inferred" | "blocked" | "not_found"
    fact_count: int = 0


@dataclass
class ReconProgress:
    """Emitted by modules during execution for orchestrator UI."""
    run_id: str
    module: str
    phase: str             # "init" | "auth" | "discover" | "extract" | "persist" | "complete"
    message: str
    completed: int | None = None
    total: int | None = None
    feature: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class ReconError:
    """A non-fatal error encountered during recon."""
    source_url: str | None
    error_type: str         # "timeout" | "auth_required" | "rate_limited" | "parse_error"
    message: str
    recoverable: bool       # True if a retry might succeed


@dataclass
class ReconResult:
    """Standard output from every recon module."""
    module: str
    status: ReconModuleStatus
    facts: list[Fact] = field(default_factory=list)
    errors: list[ReconError] = field(default_factory=list)
    coverage: list[CoverageEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None  # For resumable runs
    duration_seconds: float = 0.0
    urls_visited: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
```

### 2.3 Module Abstract Base Class

```python
from abc import ABC, abstractmethod
from typing import Callable


class ReconModule(ABC):
    """
    Abstract base class for all recon modules.

    Contract:
    - run() MUST return a ReconResult, even on total failure (status=FAILED, facts=[]).
    - run() MUST NOT raise exceptions -- all errors are captured in ReconResult.errors.
      Only contract violations or unrecoverable bootstrap failures may raise.
    - run() MUST call progress_callback at meaningful intervals if provided.
    - Each Fact produced MUST have module_name set to self.name.
    - Each Fact produced MUST have authority matching this module's authority tier.
    - Progress events MUST have monotonic phase sequence so the orchestrator
      can stream and resume cleanly.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique module identifier, e.g. 'browser_explore', 'api_docs'."""
        ...

    @property
    @abstractmethod
    def authority(self) -> Authority:
        """The authority tier of facts produced by this module."""
        ...

    @property
    @abstractmethod
    def source_type(self) -> SourceType:
        """The source type for facts produced by this module."""
        ...

    @property
    @abstractmethod
    def requires_credentials(self) -> list[str]:
        """
        Short credential key names this module needs. Empty list if none required.
        E.g. ["target.trello-com.username", "target.trello-com.password"]

        The orchestrator prepends the service namespace (e.g. "duplicat-rex")
        when fetching from the keychain:
            get_secret("target.trello-com.username", service="duplicat-rex")

        Modules declare short names; the orchestrator owns the namespace.
        """
        ...

    @abstractmethod
    async def run(
        self,
        request: ReconRequest,
        services: ReconServices,
        progress: Callable[[ReconProgress], None] | None = None,
    ) -> ReconResult:
        """
        Execute recon against the target for the given scope.

        REQUIRES: request.run_id is set, request.scope is frozen.
        ENSURES: ReconResult.module == self.name.
        ENSURES: All facts have authority == self.authority.
        ENSURES: All facts have module_name == self.name.
        ENSURES: If status == SUCCESS, at least one fact is returned.
        ENSURES: If status == FAILED, facts is empty.
        ERROR CASES:
          - Auth failure -> status=FAILED, errors=[ReconError(error_type="auth_required")]
          - Partial scrape -> status=PARTIAL, facts=[...partial...], errors=[...what failed...]
          - Rate limited -> status=PARTIAL or FAILED, errors=[ReconError(error_type="rate_limited")]
          - Network timeout -> status=FAILED, errors=[ReconError(error_type="timeout")]
        """
        ...

    async def validate_prerequisites(self) -> list[str]:
        """
        Check that module-specific dependencies are available.
        Returns list of missing prerequisites (empty = ready).
        """
        return []
```

### 2.4 Module-to-Authority Mapping

| Module | Class | Authority | SourceType | Credentials Needed |
|--------|-------|-----------|------------|--------------------|
| `browser_explore` | `BrowserExploreModule` | AUTHORITATIVE | LIVE_APP | target login (optional) |
| `api_docs` | `ApiDocsModule` | AUTHORITATIVE | API_DOCS | none (public docs) |
| `help_center` | `HelpCenterModule` | OBSERVATIONAL | HELP_CENTER | none |
| `video_transcribe` | `VideoTranscribeModule` | OBSERVATIONAL | VIDEO | none |
| `marketing` | `MarketingModule` | ANECDOTAL | MARKETING | none |
| `community` | `CommunityModule` | ANECDOTAL | COMMUNITY | none |
| `changelog` | `ChangelogModule` | ANECDOTAL | CHANGELOG | none |

**Authority is fixed per module, not per fact.** This prevents bugs where a module accidentally claims higher authority. The module's `authority` property is the single source of truth, and INV-013 enforces it.

---

## 3. Spec Store API Contract (`scripts/spec_store.py`)

File-backed store using JSON. Manages Facts and SpecBundles with querying, versioning, and snapshotting.

### 3.1 Storage Layout

```
{repo_root}/.specstore/
├── facts/
│   ├── {fact-id}.json           # One file per fact
│   └── ...
├── bundles/
│   ├── {bundle-id}.json         # One file per bundle
│   └── ...
├── snapshots/
│   ├── {bundle-id}-v{N}.json    # Immutable snapshot (bundle + all its facts)
│   └── ...
└── index.json                   # Queryable index (feature->fact_ids, module->fact_ids, etc.)
```

### 3.2 Public API

```python
class SpecStore:
    """
    File-backed store for Facts and SpecBundles.
    Thread-safe via file locking. JSON serialization.
    """

    def __init__(self, store_path: Path): ...

    # -- Fact CRUD --

    def add_fact(self, fact: Fact) -> Fact:
        """
        Store a new fact. Deduplicates by content_hash.

        REQUIRES: fact.id is set, fact.module_name is non-empty,
                  fact.run_id is non-empty (INV-006),
                  fact.evidence has at least one EvidenceRef.
        ENSURES: Fact is persisted and indexed. Returns the stored fact.
                 If a fact with the same content_hash exists, returns
                 the existing one (no duplicate created).
        ERROR CASES:
          - Empty module_name -> ValueError
          - Empty run_id -> ValueError
          - No evidence -> ValueError
          - Store I/O failure -> IOError
        """
        ...

    def add_facts(self, facts: list[Fact]) -> list[Fact]:
        """
        Batch add. Acquires lock once for all facts.

        REQUIRES: All facts pass individual validation.
        ENSURES: All facts stored atomically (all or none on I/O error).
        """
        ...

    def get_fact(self, fact_id: str, revision: int | None = None) -> Fact | None:
        """
        Retrieve a fact by ID. If revision is specified, retrieves
        that specific version from the supersedes chain.

        REQUIRES: fact_id is a valid UUID string.
        ENSURES: Returns the Fact or None if not found.
        """
        ...

    def revise_fact(self, fact_id: str, patch: dict[str, Any], reason: str) -> Fact:
        """
        Create a new revision of a fact with updated content fields.
        The original fact is not mutated — a new Fact is created with
        supersedes=fact_id.

        REQUIRES: Fact exists.
        ENSURES: New fact has supersedes=fact_id, new UUID, incremented revision.
                 The new fact starts as UNVERIFIED regardless of the original's status.
                 If the original was CONTRADICTED, this effectively "revives" the
                 claim with updated evidence — the contradiction chain is preserved.
        ERROR CASES:
          - Fact not found -> KeyError
        """
        ...

    def update_fact_status(
        self,
        fact_id: str,
        status: FactStatus,
        *,
        related_fact_ids: list[str] | None = None,
        note: str = "",
    ) -> Fact:
        """
        Update a fact's lifecycle status.

        REQUIRES: Fact exists. Transition is valid per state machine.
        ENSURES: Status is updated. Related IDs appended to verified_by
                 or contradicted_by as appropriate. Bidirectional relations
                 are also updated — if A verifies B, then B.corroborates
                 includes A and A.verified_by includes B.
        ERROR CASES:
          - Invalid transition (e.g. CONTRADICTED -> VERIFIED) -> ValueError
          - Related fact doesn't exist -> KeyError
        """
        ...

    def delete_fact(self, fact_id: str, soft: bool = True) -> bool:
        """
        Soft-delete only for unreferenced facts.

        REQUIRES: Fact exists.
        ENSURES: Fact is marked as deleted (soft) but provenance chains
                 remain intact. Hard deletion is NOT supported.
        ERROR CASES:
          - Fact referenced by a SNAPSHOT bundle -> ValueError (cannot delete)
        """
        ...

    # -- Fact Queries --

    def query_facts(
        self,
        *,
        feature: str | None = None,
        category: FactCategory | None = None,
        module: str | None = None,
        source_type: SourceType | None = None,
        authority: Authority | None = None,
        min_authority: Authority | None = None,
        confidence: Confidence | None = None,
        status: FactStatus | None = None,
        run_id: str | None = None,
        tags: list[str] | None = None,
    ) -> list[Fact]:
        """
        Query facts with optional filters. All filters are AND-combined.

        REQUIRES: At least one filter or no filter (returns all).
        ENSURES: Results sorted by (authority DESC, confidence DESC, created_at DESC).
        """
        ...

    def get_facts_for_feature(self, feature: str) -> list[Fact]:
        """
        Shortcut: all non-contradicted, non-superseded facts for a feature.

        A fact is superseded if another fact in the store has
        supersedes=this.id. Superseded facts are excluded from default
        queries but remain accessible via get_fact() and get_fact_lineage().
        """
        ...

    def get_fact_lineage(self, fact_id: str, depth: int = 5) -> list[Fact]:
        """
        Follow the supersedes chain backwards from a fact to its origin.

        REQUIRES: fact_id exists.
        ENSURES: Returns [oldest_ancestor, ..., fact_id] in chronological order.
                 Stops at `depth` levels.
        """
        ...

    def find_contradictions(self, feature: str | None = None) -> list[tuple[Fact, Fact]]:
        """
        Find pairs of facts that contradict each other.

        ENSURES: Each pair contains (higher_authority_fact, lower_authority_fact).
                 Optionally scoped to a feature.
        """
        ...

    # -- Bundle CRUD --

    def create_bundle(
        self,
        target: str,
        scope: list[str],
        *,
        scope_id: str = "",
        scope_hash: str = "",
        notes: str = "",
    ) -> SpecBundle:
        """
        Create a new draft bundle.

        REQUIRES: target is non-empty.
        ENSURES: status=DRAFT, version=max(existing for same target+scope_hash)+1.
        """
        ...

    def get_bundle(self, bundle_id: str) -> SpecBundle | None:
        """Retrieve a bundle by ID."""
        ...

    def revise_bundle(self, bundle_id: str, patch: dict[str, Any]) -> SpecBundle:
        """
        Update a draft bundle's metadata or fact list.

        REQUIRES: Bundle exists and status=DRAFT.
        ENSURES: Bundle is updated in place.
        ERROR CASES:
          - Bundle is VALIDATED or SNAPSHOT -> ValueError
        """
        ...

    def query_bundles(
        self,
        *,
        target: str | None = None,
        scope_hash: str | None = None,
        status: BundleStatus | None = None,
    ) -> list[SpecBundle]:
        """Query bundles with optional filters."""
        ...

    def validate_bundle(self, bundle_id: str) -> tuple[bool, list[str]]:
        """
        Check if a bundle can transition to VALIDATED.

        Checks:
        - All fact_ids reference existing facts
        - No contradicted supporting facts
        - At least one fact per scoped feature
        - Schema validation passes

        REQUIRES: Bundle exists and status=DRAFT.
        ENSURES: Returns (is_valid, list_of_issues).
        """
        ...

    def set_bundle_status(self, bundle_id: str, status: BundleStatus) -> SpecBundle:
        """
        Transition bundle status.

        REQUIRES: Valid transition per state machine.
        ENSURES: If transitioning to SNAPSHOT, content_hash and snapshot_at are set.
        ERROR CASES:
          - Invalid transition -> ValueError
          - Validation fails (for DRAFT->VALIDATED) -> ValueError with issues
        """
        ...

    def snapshot_bundle(self, bundle_id: str) -> Path:
        """
        Create an immutable snapshot of a VALIDATED bundle.

        1. Validate the bundle (all checks from validate_bundle)
        2. Compute content_hash from canonical manifest
        3. Set status=SNAPSHOT, snapshot_at=now
        4. Write snapshot file: bundle metadata + all facts inlined
        5. Return path to snapshot file

        REQUIRES: Bundle status is VALIDATED.
        ENSURES: Snapshot file is self-contained and immutable.
        ERROR CASES:
          - Bundle not VALIDATED -> ValueError
          - Hash computation fails -> RuntimeError
        """
        ...

    def get_latest_snapshot(self, target: str, scope_hash: str) -> SpecBundle | None:
        """Get the most recent snapshot for a target + scope combination."""
        ...

    def compute_bundle_hash(self, bundle_id: str) -> str:
        """
        Compute the content hash for a bundle without changing its status.

        REQUIRES: Bundle exists with at least one fact.
        ENSURES: Returns deterministic SHA-256 hash string.
        """
        ...

    def get_bundle_lineage(self, bundle_id: str, depth: int = 5) -> list[SpecBundle]:
        """
        Follow the parent_id chain backwards.

        ENSURES: Returns [oldest_ancestor, ..., bundle_id] in version order.
        """
        ...

    # -- Snapshot Operations --

    def diff_snapshots(
        self, bundle_id_a: str, bundle_id_b: str
    ) -> dict[str, Any]:
        """
        Compute diff between two snapshots.

        REQUIRES: Both bundles exist and are SNAPSHOT status.
        ENSURES: Returns {added: [...], removed: [...], changed: [...]} fact summaries.
        """
        ...

    # -- Statistics --

    def stats(self) -> dict[str, Any]:
        """
        Returns aggregate statistics about the store.

        ENSURES: Returns dict with keys: total_facts, by_status, by_authority,
                 by_feature, by_module, bundles, snapshots.
        """
        ...
```

---

## 4. Keychain Credential Naming Convention

All keys use a hierarchical dot-separated namespace stored under the `duplicat-rex` service in the system keychain.

### Convention

```
duplicat-rex.{category}.{target-or-service}.{key-name}
```

### Concrete Keys

```
# AI API keys (shared with chief-wiggum -- use CW's keychain service for these)
chief-wiggum.ANTHROPIC_API_KEY
chief-wiggum.OPENAI_API_KEY
chief-wiggum.GEMINI_API_KEY

# Target SaaS credentials (per target domain, normalized)
duplicat-rex.target.trello-com.username
duplicat-rex.target.trello-com.password
duplicat-rex.target.trello-com.api-key
duplicat-rex.target.trello-com.api-token
duplicat-rex.target.trello-com.oauth-token

# Service credentials (tools used by recon)
duplicat-rex.service.youtube.api-key
```

### Normalization Rules

- Target domains: lowercase, dots replaced with hyphens (`trello.com` -> `trello-com`)
- No special characters beyond hyphens and dots in the key path
- Key names are lowercase kebab-case
- Profile segment (e.g. `default`, `admin`, `readonly`) may be added between target and key-name when multi-profile support is needed: `duplicat-rex.target.trello-com.admin.api-key`

### Lookup in Code

```python
from keychain import get_secret

# For AI keys, delegate to chief-wiggum's keychain
api_key = get_secret("ANTHROPIC_API_KEY", service="chief-wiggum")

# For target credentials
username = get_secret("target.trello-com.username", service="duplicat-rex")
password = get_secret("target.trello-com.password", service="duplicat-rex")
```

The keychain integration (#5) must support both service namespaces -- its own and chief-wiggum's (for shared AI keys).

---

## 5. Authority Classification Rules

### Tier Definitions

**AUTHORITATIVE** -- Direct observation of the system's actual behavior or official specification.

| Source | Why Authoritative | Edge Cases |
|--------|-------------------|------------|
| Live app (browser explore) | You saw the system do it. Directly observed UI state, HTTP responses, WS events. | Authenticated vs unauthenticated: both are authoritative, but for different permission contexts. Stale browser cache can produce false observations. |
| Official API docs | The vendor explicitly documents this as the contract. | Stale docs: if API docs contradict live observation, live observation wins. API docs from third-party mirrors (not vendor domain) are OBSERVATIONAL. |

**OBSERVATIONAL** -- Reliable descriptions of behavior, but not direct observation.

| Source | Why Observational | Edge Cases |
|--------|-------------------|------------|
| Help center / knowledge base | Vendor-written, intended to be accurate, but may lag behind actual behavior. | Only vendor-hosted help centers qualify. Community wikis are ANECDOTAL. |
| Training videos (official) | Vendor-produced walkthroughs show real usage, but may show beta or deprecated UI. | Third-party tutorial videos are ANECDOTAL, not OBSERVATIONAL. |

**ANECDOTAL** -- Hypothesis generators. Useful for coverage but must be validated.

| Source | Why Anecdotal | When Confidence Can Rise |
|--------|---------------|--------------------------|
| Marketing / pricing pages | Aspirational. May include unreleased, sunset, or add-on features. | Never upgrades authority. Must always be validated against authoritative sources. |
| Reddit / community forums | User perceptions. May be outdated or misunderstood. | 3+ independent users describing the same behavior raises confidence to MEDIUM, but authority stays ANECDOTAL. |
| Changelog / release notes | Describes what changed, not the current state. | Recent entries (< 30 days) for additive changes can be treated as OBSERVATIONAL confidence-wise, but authority stays ANECDOTAL until verified. |

### Cross-Reference Rules

1. **Anecdotal facts start as UNVERIFIED.** They become VERIFIED only when an AUTHORITATIVE source produces a corroborating fact. OBSERVATIONAL corroboration raises confidence but does not flip status to VERIFIED (see Rule 4).
2. **Contradictions resolve by authority tier**: AUTHORITATIVE beats OBSERVATIONAL beats ANECDOTAL. Within the same tier, the more recent observation wins.
3. **Confidence is independent of authority**: A single Reddit post is ANECDOTAL/LOW. Five independent Reddit posts saying the same thing are ANECDOTAL/MEDIUM. But even ANECDOTAL/HIGH does not override AUTHORITATIVE/LOW.
4. **Only authoritative evidence can verify an anecdotal claim.** Observational evidence can raise confidence but cannot flip status from UNVERIFIED to VERIFIED for an anecdotal fact.
