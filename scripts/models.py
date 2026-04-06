"""
Canonical data model for duplicat-rex.

All enums and dataclasses defined here are the authoritative schema.
Every other module imports from this file — never re-define elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Authority(StrEnum):
    """
    Source trustworthiness tier — see Authority Classification Rules.

    Rank ordering (for "equal or higher" guards):
      AUTHORITATIVE (3) > OBSERVATIONAL (2) > ANECDOTAL (1)
    Use Authority.rank() for ordinal comparisons — do NOT compare string values.
    """

    AUTHORITATIVE = "authoritative"  # Live app observation, official API docs
    OBSERVATIONAL = "observational"  # Help center, training videos
    ANECDOTAL = "anecdotal"  # Marketing, Reddit, changelog

    def rank(self) -> int:
        return {"authoritative": 3, "observational": 2, "anecdotal": 1}[self.value]

    def __ge__(self, other: Authority) -> bool:  # type: ignore[override]
        return self.rank() >= other.rank()

    def __gt__(self, other: Authority) -> bool:  # type: ignore[override]
        return self.rank() > other.rank()


class Confidence(StrEnum):
    """
    Confidence level for a fact or spec item.

    Rank ordering (for "equal or higher" guards):
      HIGH (3) > MEDIUM (2) > LOW (1)
    Use Confidence.rank() for ordinal comparisons — do NOT compare string values.
    """

    HIGH = "high"  # Multiple authoritative sources agree, or directly observed
    MEDIUM = "medium"  # Single authoritative source, or multiple observational agree
    LOW = "low"  # Single observational/anecdotal, or sources conflict

    def rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1}[self.value]

    def __ge__(self, other: Confidence) -> bool:  # type: ignore[override]
        return self.rank() >= other.rank()

    def __gt__(self, other: Confidence) -> bool:  # type: ignore[override]
        return self.rank() > other.rank()


class FactStatus(StrEnum):
    UNVERIFIED = "unverified"  # Extracted but not cross-referenced
    VERIFIED = "verified"  # Confirmed by >=1 independent source at same or higher authority
    CONTRADICTED = "contradicted"  # Another fact at equal/higher authority disagrees


class BundleStatus(StrEnum):
    DRAFT = "draft"  # Accumulating facts, not yet synthesised
    VALIDATED = "validated"  # All validation checks pass, ready for snapshot
    SNAPSHOT = "snapshot"  # Immutable, hashed, committed to output repo


class FactCategory(StrEnum):
    """What aspect of the target this fact describes."""

    UI_COMPONENT = "ui_component"  # Visual element (button, modal, sidebar)
    USER_FLOW = "user_flow"  # Multi-step interaction sequence
    API_ENDPOINT = "api_endpoint"  # REST/GraphQL endpoint
    WS_EVENT = "ws_event"  # WebSocket event contract
    DATA_MODEL = "data_model"  # Entity, field, relationship
    AUTH = "auth"  # Authentication/authorization behavior
    BUSINESS_RULE = "business_rule"  # Validation, permission, constraint
    PERFORMANCE = "performance"  # Timing, rate limit, pagination
    INTEGRATION = "integration"  # Third-party service interaction
    CONFIGURATION = "configuration"  # Settings, preferences, defaults


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

    CLEAN = "clean"  # No sensitive material present
    REDACTED = "redacted"  # Sensitive material was removed
    RESTRICTED = "restricted"  # Entire fact is access-controlled


# ---------------------------------------------------------------------------
# Evidence Reference
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRef:
    """
    Pointer to a specific piece of source material.
    Must be sufficient to re-locate and re-verify the claim.
    """

    source_url: str  # URL, file path, or deep-link
    locator: str | None = None  # CSS selector, JSON path, video timestamp, etc.
    source_title: str | None = None  # Human-readable page/doc title
    artifact_uri: str | None = None  # Path to captured artifact
    artifact_sha256: str | None = None  # Hash of the captured artifact for integrity
    captured_at: str = ""  # ISO 8601 UTC when captured
    published_at: str | None = None  # When the source was published (if known)
    raw_excerpt: str | None = None  # Verbatim source text (max 2000 chars)

    def __post_init__(self) -> None:
        if not self.captured_at:
            object.__setattr__(self, "captured_at", datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "locator": self.locator,
            "source_title": self.source_title,
            "artifact_uri": self.artifact_uri,
            "artifact_sha256": self.artifact_sha256,
            "captured_at": self.captured_at,
            "published_at": self.published_at,
            "raw_excerpt": self.raw_excerpt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceRef:
        return cls(
            source_url=data["source_url"],
            locator=data.get("locator"),
            source_title=data.get("source_title"),
            artifact_uri=data.get("artifact_uri"),
            artifact_sha256=data.get("artifact_sha256"),
            captured_at=data.get("captured_at", ""),
            published_at=data.get("published_at"),
            raw_excerpt=data.get("raw_excerpt"),
        )


# ---------------------------------------------------------------------------
# Fact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    """
    The core data structure produced by every recon module.

    Immutable once created. If a fact needs correction, create a new Fact
    with `supersedes` pointing to the original's id.
    """

    # Content — immutable after creation (required fields first)
    feature: str  # Which scoped feature this relates to (e.g. "boards", "drag-drop")
    category: FactCategory  # What kind of fact (UI, API, data model, etc.)
    claim: str  # Human-readable atomic assertion (one sentence, one testable claim)
    evidence: list[EvidenceRef]  # At least one required — verifiable source pointers

    # Provenance — required, no default
    source_type: SourceType  # Which recon source class produced this

    # Auto-generated ID (after required fields to satisfy dataclass ordering)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # Structured evidence — schema depends on category
    structured_data: dict[str, Any] = field(default_factory=dict)

    # Provenance
    module_name: str = ""  # Concrete module name (e.g. "browser_explore")
    authority: Authority = Authority.ANECDOTAL
    confidence: Confidence = Confidence.LOW
    run_id: str = ""  # UUID of the recon run that produced this fact

    # Lifecycle — these are the only mutable fields (via update_fact_status)
    status: FactStatus = FactStatus.UNVERIFIED
    verified_by: list[str] = field(default_factory=list)  # IDs of corroborating Facts
    contradicted_by: list[str] = field(default_factory=list)  # IDs of contradicting Facts
    supersedes: str | None = None  # ID of the Fact this replaces (supersede chain)
    corroborates: list[str] = field(default_factory=list)  # IDs of facts this supports
    contradicts: list[str] = field(default_factory=list)  # IDs of facts this opposes
    revision: int = 1  # Incremented when superseded (new fact gets old.revision + 1)

    # Soft deletion (INV-027)
    deleted_at: str | None = None  # ISO 8601 UTC when soft-deleted, None if active

    # Freshness
    observed_at: str | None = None  # When the behavior was observed in the target
    freshness_ttl_days: int | None = None  # How long before this fact should be re-verified

    # Metadata
    tags: list[str] = field(default_factory=list)
    redaction_status: RedactionStatus = RedactionStatus.CLEAN
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def content_hash(self) -> str:
        """
        Deterministic hash of the fact's semantic content (excludes lifecycle fields).
        Used for deduplication and snapshot integrity.
        Includes source identifiers to distinguish same-claim from different sources.
        """
        canonical = json.dumps(
            {
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
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "feature": self.feature,
            "category": str(self.category),
            "claim": self.claim,
            "evidence": [e.to_dict() for e in self.evidence],
            "source_type": str(self.source_type),
            "structured_data": self.structured_data,
            "module_name": self.module_name,
            "authority": str(self.authority),
            "confidence": str(self.confidence),
            "run_id": self.run_id,
            "status": str(self.status),
            "verified_by": list(self.verified_by),
            "contradicted_by": list(self.contradicted_by),
            "supersedes": self.supersedes,
            "corroborates": list(self.corroborates),
            "contradicts": list(self.contradicts),
            "revision": self.revision,
            "deleted_at": self.deleted_at,
            "observed_at": self.observed_at,
            "freshness_ttl_days": self.freshness_ttl_days,
            "tags": list(self.tags),
            "redaction_status": str(self.redaction_status),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fact:
        return cls(
            id=data["id"],
            feature=data["feature"],
            category=FactCategory(data["category"]),
            claim=data["claim"],
            evidence=[EvidenceRef.from_dict(e) for e in data["evidence"]],
            source_type=SourceType(data["source_type"]),
            structured_data=data.get("structured_data", {}),
            module_name=data.get("module_name", ""),
            authority=Authority(data.get("authority", Authority.ANECDOTAL)),
            confidence=Confidence(data.get("confidence", Confidence.LOW)),
            run_id=data.get("run_id", ""),
            status=FactStatus(data.get("status", FactStatus.UNVERIFIED)),
            verified_by=data.get("verified_by", []),
            contradicted_by=data.get("contradicted_by", []),
            supersedes=data.get("supersedes"),
            corroborates=data.get("corroborates", []),
            contradicts=data.get("contradicts", []),
            revision=data.get("revision", 1),
            deleted_at=data.get("deleted_at"),
            observed_at=data.get("observed_at"),
            freshness_ttl_days=data.get("freshness_ttl_days"),
            tags=data.get("tags", []),
            redaction_status=RedactionStatus(data.get("redaction_status", RedactionStatus.CLEAN)),
            created_at=data.get("created_at", datetime.now(UTC).isoformat()),
        )


# ---------------------------------------------------------------------------
# Spec Bundle
# ---------------------------------------------------------------------------


@dataclass
class SpecItem:
    """A single synthesised specification within a bundle."""

    feature: str  # Feature key
    spec_type: str  # e.g. "api_contract", "ui_behavior", "data_model"
    content: dict[str, Any]  # The synthesised specification
    supporting_fact_ids: list[str] = field(default_factory=list)
    confidence: Confidence = Confidence.LOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "spec_type": self.spec_type,
            "content": self.content,
            "supporting_fact_ids": self.supporting_fact_ids,
            "confidence": str(self.confidence),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SpecItem:
        return cls(
            feature=data["feature"],
            spec_type=data["spec_type"],
            content=data["content"],
            supporting_fact_ids=data.get("supporting_fact_ids", []),
            confidence=Confidence(data.get("confidence", Confidence.LOW)),
        )


@dataclass
class SpecBundle:
    """
    A versioned collection of synthesised specs for a set of features.
    Transitions: draft -> validated -> snapshot (immutable).
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: str = "1.0"  # Schema format version
    target: str = ""  # e.g. "trello.com"
    scope_id: str = ""  # UUID of the Scope that defines this bundle's coverage
    scope_hash: str = ""  # Deterministic hash of the scope
    scope: list[str] = field(default_factory=list)  # Feature keys covered
    version: int = 1  # Monotonically increasing per (target, scope_hash)
    status: BundleStatus = BundleStatus.DRAFT
    spec_items: list[SpecItem] = field(default_factory=list)
    fact_ids: list[str] = field(default_factory=list)  # All supporting Facts
    parent_id: str | None = None  # Previous bundle version (for diff)

    # Computed at snapshot time
    content_hash: str | None = None  # SHA-256 of canonical manifest
    snapshot_at: str | None = None  # ISO 8601 UTC when frozen

    # Metadata
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    validated_at: str | None = None
    notes: str = ""

    def compute_hash(self, facts: list[Fact]) -> str:
        """
        Compute deterministic hash from the canonical manifest.
        Manifest includes schema_version, scope_hash, sorted spec_items,
        and sorted supporting fact (fact_id, content_hash) tuples.
        """
        manifest: dict[str, Any] = {
            "schema_version": self.schema_version,
            "scope_hash": self.scope_hash,
            "spec_items": sorted(
                [
                    {
                        "feature": s.feature,
                        "spec_type": s.spec_type,
                        "content": s.content,
                    }
                    for s in self.spec_items
                ],
                key=lambda x: (x["feature"], x["spec_type"]),
            ),
            "supporting_facts": sorted(
                [(f.id, f.content_hash()) for f in facts],
                key=lambda x: x[0],
            ),
        }
        canonical = json.dumps(manifest, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "target": self.target,
            "scope_id": self.scope_id,
            "scope_hash": self.scope_hash,
            "scope": self.scope,
            "version": self.version,
            "status": str(self.status),
            "spec_items": [s.to_dict() for s in self.spec_items],
            "fact_ids": self.fact_ids,
            "parent_id": self.parent_id,
            "content_hash": self.content_hash,
            "snapshot_at": self.snapshot_at,
            "created_at": self.created_at,
            "validated_at": self.validated_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SpecBundle:
        return cls(
            id=data["id"],
            schema_version=data.get("schema_version", "1.0"),
            target=data.get("target", ""),
            scope_id=data.get("scope_id", ""),
            scope_hash=data.get("scope_hash", ""),
            scope=data.get("scope", []),
            version=data.get("version", 1),
            status=BundleStatus(data.get("status", BundleStatus.DRAFT)),
            spec_items=[SpecItem.from_dict(s) for s in data.get("spec_items", [])],
            fact_ids=data.get("fact_ids", []),
            parent_id=data.get("parent_id"),
            content_hash=data.get("content_hash"),
            snapshot_at=data.get("snapshot_at"),
            created_at=data.get("created_at", datetime.now(UTC).isoformat()),
            validated_at=data.get("validated_at"),
            notes=data.get("notes", ""),
        )


# ---------------------------------------------------------------------------
# Scope (for completeness — used by ReconModule interface)
# ---------------------------------------------------------------------------


@dataclass
class ScopeNode:
    """A single feature in the user-specified scope."""

    feature: str  # Canonical name, lowercase slug (e.g. "boards", "drag-drop")
    label: str = ""  # Human-readable name
    description: str = ""  # Optional clarification
    inclusion_reason: str = "requested"  # "requested" | "dependency" | "implied"
    status: str = "in_scope"  # "in_scope" | "excluded" | "unknown"
    depends_on: list[str] = field(default_factory=list)
    priority: int = 1  # 1=core, 2=important, 3=nice-to-have


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
    target: str = ""  # e.g. "trello.com"
    raw_input: str = ""  # Original user string
    requested_features: list[ScopeNode] = field(default_factory=list)
    resolved_features: list[ScopeNode] = field(default_factory=list)
    dependency_edges: list[DependencyEdge] = field(default_factory=list)
    known_exclusions: list[str] = field(default_factory=list)
    unknown_features: list[str] = field(default_factory=list)
    frozen: bool = False
    scope_hash: str = ""  # Deterministic from requested + resolved
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

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
            wave = [n for n, deps in remaining.items() if not deps - {n for w in waves for n in w}]
            if not wave:
                raise ValueError(f"Circular dependency detected among: {list(remaining)}")
            waves.append(sorted(wave))
            for n in wave:
                del remaining[n]
        return waves

    def compute_scope_hash(self) -> str:
        """Deterministic hash from requested features + resolved dependency set."""
        canonical = json.dumps(
            {
                "requested": sorted(f.feature for f in self.requested_features),
                "resolved": sorted(f.feature for f in self.resolved_features),
                "edges": sorted(
                    [(e.from_feature, e.to_feature, e.kind) for e in self.dependency_edges]
                ),
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]
