"""
Tests for scripts/models.py

Covers:
- Enum rank ordering and comparisons
- EvidenceRef creation and serialization
- Fact creation, content_hash stability, to_dict/from_dict round-trip
- SpecItem and SpecBundle serialization and compute_hash
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from scripts.models import (
    Authority,
    BundleStatus,
    Confidence,
    DependencyEdge,
    EvidenceRef,
    Fact,
    FactCategory,
    FactStatus,
    RedactionStatus,
    Scope,
    ScopeNode,
    SourceType,
    SpecBundle,
    SpecItem,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_evidence(url: str = "https://example.com") -> EvidenceRef:
    return EvidenceRef(
        source_url=url,
        source_title="Test Page",
        locator="#main",
        captured_at="2024-01-15T10:00:00+00:00",
    )


def make_fact(
    *,
    feature: str = "boards",
    claim: str = "Boards have a title field",
    category: FactCategory = FactCategory.UI_COMPONENT,
    source_type: SourceType = SourceType.LIVE_APP,
    authority: Authority = Authority.AUTHORITATIVE,
) -> Fact:
    return Fact(
        feature=feature,
        category=category,
        claim=claim,
        evidence=[make_evidence()],
        source_type=source_type,
        authority=authority,
        confidence=Confidence.HIGH,
        module_name="browser_explore",
    )


# ---------------------------------------------------------------------------
# Authority enum
# ---------------------------------------------------------------------------


class TestAuthority:
    def test_rank_values(self) -> None:
        assert Authority.AUTHORITATIVE.rank() == 3
        assert Authority.OBSERVATIONAL.rank() == 2
        assert Authority.ANECDOTAL.rank() == 1

    def test_ge_operator(self) -> None:
        assert Authority.AUTHORITATIVE >= Authority.AUTHORITATIVE
        assert Authority.AUTHORITATIVE >= Authority.OBSERVATIONAL
        assert Authority.AUTHORITATIVE >= Authority.ANECDOTAL
        assert Authority.OBSERVATIONAL >= Authority.OBSERVATIONAL
        assert Authority.OBSERVATIONAL >= Authority.ANECDOTAL
        assert Authority.ANECDOTAL >= Authority.ANECDOTAL

    def test_gt_operator(self) -> None:
        assert Authority.AUTHORITATIVE > Authority.OBSERVATIONAL
        assert Authority.AUTHORITATIVE > Authority.ANECDOTAL
        assert Authority.OBSERVATIONAL > Authority.ANECDOTAL
        assert not Authority.ANECDOTAL > Authority.ANECDOTAL
        assert not Authority.OBSERVATIONAL > Authority.AUTHORITATIVE

    def test_str_values(self) -> None:
        assert str(Authority.AUTHORITATIVE) == "authoritative"
        assert str(Authority.OBSERVATIONAL) == "observational"
        assert str(Authority.ANECDOTAL) == "anecdotal"

    def test_from_value(self) -> None:
        assert Authority("authoritative") == Authority.AUTHORITATIVE


# ---------------------------------------------------------------------------
# Confidence enum
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_rank_values(self) -> None:
        assert Confidence.HIGH.rank() == 3
        assert Confidence.MEDIUM.rank() == 2
        assert Confidence.LOW.rank() == 1

    def test_ge_operator(self) -> None:
        assert Confidence.HIGH >= Confidence.HIGH
        assert Confidence.HIGH >= Confidence.LOW
        assert not Confidence.LOW >= Confidence.HIGH

    def test_gt_operator(self) -> None:
        assert Confidence.HIGH > Confidence.MEDIUM
        assert Confidence.MEDIUM > Confidence.LOW
        assert not Confidence.LOW > Confidence.LOW


# ---------------------------------------------------------------------------
# EvidenceRef
# ---------------------------------------------------------------------------


class TestEvidenceRef:
    def test_basic_creation(self) -> None:
        ref = make_evidence()
        assert ref.source_url == "https://example.com"
        assert ref.source_title == "Test Page"
        assert ref.locator == "#main"

    def test_auto_captured_at(self) -> None:
        """When captured_at is omitted, it is auto-populated."""
        ref = EvidenceRef(source_url="https://example.com")
        assert ref.captured_at
        # Should be parseable as ISO datetime
        datetime.fromisoformat(ref.captured_at)

    def test_explicit_captured_at_preserved(self) -> None:
        ts = "2024-03-01T12:00:00+00:00"
        ref = EvidenceRef(source_url="https://example.com", captured_at=ts)
        assert ref.captured_at == ts

    def test_frozen(self) -> None:
        ref = make_evidence()
        with pytest.raises((AttributeError, TypeError)):
            ref.source_url = "changed"  # type: ignore[misc]

    def test_to_dict_round_trip(self) -> None:
        ref = make_evidence()
        d = ref.to_dict()
        restored = EvidenceRef.from_dict(d)
        assert restored.source_url == ref.source_url
        assert restored.locator == ref.locator
        assert restored.source_title == ref.source_title
        assert restored.captured_at == ref.captured_at

    def test_optional_fields_none(self) -> None:
        ref = EvidenceRef(source_url="https://x.com", captured_at="2024-01-01T00:00:00+00:00")
        assert ref.locator is None
        assert ref.artifact_uri is None
        assert ref.artifact_sha256 is None
        assert ref.raw_excerpt is None


# ---------------------------------------------------------------------------
# Fact
# ---------------------------------------------------------------------------


class TestFact:
    def test_basic_creation(self) -> None:
        fact = make_fact()
        assert fact.feature == "boards"
        assert fact.category == FactCategory.UI_COMPONENT
        assert len(fact.evidence) == 1
        assert fact.status == FactStatus.UNVERIFIED

    def test_auto_id(self) -> None:
        f1 = make_fact()
        f2 = make_fact()
        assert f1.id != f2.id

    def test_frozen_immutable_fields(self) -> None:
        fact = make_fact()
        with pytest.raises((AttributeError, TypeError)):
            fact.claim = "changed"  # type: ignore[misc]

    def test_content_hash_is_deterministic(self) -> None:
        fact = make_fact()
        h1 = fact.content_hash()
        h2 = fact.content_hash()
        assert h1 == h2
        assert len(h1) == 16  # first 16 hex chars of sha256

    def test_content_hash_differs_on_different_claims(self) -> None:
        f1 = make_fact(claim="Boards have a title field")
        f2 = make_fact(claim="Boards have a description field")
        assert f1.content_hash() != f2.content_hash()

    def test_content_hash_stable_across_instances(self) -> None:
        """Two Fact instances with same content must produce same hash (INV-004)."""
        evidence = [EvidenceRef(
            source_url="https://example.com",
            locator="#main",
            captured_at="2024-01-15T10:00:00+00:00",
        )]
        f1 = Fact(
            feature="boards",
            category=FactCategory.UI_COMPONENT,
            claim="Boards have a title field",
            evidence=evidence,
            source_type=SourceType.LIVE_APP,
            module_name="browser_explore",
        )
        f2 = Fact(
            feature="boards",
            category=FactCategory.UI_COMPONENT,
            claim="Boards have a title field",
            evidence=evidence,
            source_type=SourceType.LIVE_APP,
            module_name="browser_explore",
        )
        # Even though IDs differ, content hashes must match
        assert f1.id != f2.id
        assert f1.content_hash() == f2.content_hash()

    def test_content_hash_excludes_lifecycle_fields(self) -> None:
        """Changing status should not affect content hash (INV-004)."""
        fact = make_fact()
        h1 = fact.content_hash()
        # Reconstruct with different status
        d = fact.to_dict()
        d["status"] = "verified"
        d["verified_by"] = ["some-id"]
        updated = Fact.from_dict(d)
        assert updated.content_hash() == h1

    def test_to_dict_round_trip(self) -> None:
        fact = make_fact()
        d = fact.to_dict()
        restored = Fact.from_dict(d)
        assert restored.id == fact.id
        assert restored.feature == fact.feature
        assert restored.category == fact.category
        assert restored.claim == fact.claim
        assert restored.authority == fact.authority
        assert restored.confidence == fact.confidence
        assert restored.status == fact.status
        assert len(restored.evidence) == 1
        assert restored.evidence[0].source_url == fact.evidence[0].source_url

    def test_to_dict_is_json_serializable(self) -> None:
        fact = make_fact()
        d = fact.to_dict()
        # Should not raise
        json.dumps(d)

    def test_default_lists_are_independent(self) -> None:
        """Default mutable fields must not be shared between instances."""
        f1 = make_fact()
        f2 = make_fact()
        # Access the list to trigger any shared-state bug
        assert f1.tags is not f2.tags
        assert f1.verified_by is not f2.verified_by

    def test_tags_preserved_in_round_trip(self) -> None:
        evidence = [make_evidence()]
        fact = Fact(
            feature="boards",
            category=FactCategory.UI_COMPONENT,
            claim="Boards have a title",
            evidence=evidence,
            source_type=SourceType.LIVE_APP,
            tags=["alpha", "beta"],
        )
        d = fact.to_dict()
        restored = Fact.from_dict(d)
        assert restored.tags == ["alpha", "beta"]

    def test_supersedes_preserved(self) -> None:
        original = make_fact()
        d = make_fact().to_dict()
        d["supersedes"] = original.id
        revised = Fact.from_dict(d)
        assert revised.supersedes == original.id

    def test_redaction_status_defaults_to_clean(self) -> None:
        fact = make_fact()
        assert fact.redaction_status == RedactionStatus.CLEAN

    def test_from_dict_with_all_optional_fields(self) -> None:
        fact = make_fact()
        d = fact.to_dict()
        d["observed_at"] = "2024-01-15T10:00:00+00:00"
        d["freshness_ttl_days"] = 30
        d["deleted_at"] = None
        d["run_id"] = "some-run-id"
        restored = Fact.from_dict(d)
        assert restored.observed_at == "2024-01-15T10:00:00+00:00"
        assert restored.freshness_ttl_days == 30
        assert restored.run_id == "some-run-id"


# ---------------------------------------------------------------------------
# SpecItem
# ---------------------------------------------------------------------------


class TestSpecItem:
    def test_basic_creation(self) -> None:
        item = SpecItem(
            feature="boards",
            spec_type="ui_behavior",
            content={"description": "Boards show a title"},
        )
        assert item.feature == "boards"
        assert item.spec_type == "ui_behavior"
        assert item.confidence == Confidence.LOW

    def test_to_dict_round_trip(self) -> None:
        item = SpecItem(
            feature="boards",
            spec_type="api_contract",
            content={"method": "POST", "path": "/boards"},
            supporting_fact_ids=["fact-id-1"],
            confidence=Confidence.HIGH,
        )
        d = item.to_dict()
        restored = SpecItem.from_dict(d)
        assert restored.feature == item.feature
        assert restored.spec_type == item.spec_type
        assert restored.content == item.content
        assert restored.supporting_fact_ids == item.supporting_fact_ids
        assert restored.confidence == item.confidence


# ---------------------------------------------------------------------------
# SpecBundle
# ---------------------------------------------------------------------------


class TestSpecBundle:
    def test_basic_creation(self) -> None:
        bundle = SpecBundle(target="trello.com", scope=["boards", "drag-drop"])
        assert bundle.status == BundleStatus.DRAFT
        assert bundle.version == 1
        assert bundle.target == "trello.com"
        assert bundle.content_hash is None

    def test_auto_id(self) -> None:
        b1 = SpecBundle()
        b2 = SpecBundle()
        assert b1.id != b2.id

    def test_to_dict_round_trip(self) -> None:
        bundle = SpecBundle(
            target="trello.com",
            scope=["boards"],
            notes="Initial draft",
        )
        d = bundle.to_dict()
        restored = SpecBundle.from_dict(d)
        assert restored.id == bundle.id
        assert restored.target == bundle.target
        assert restored.scope == bundle.scope
        assert restored.status == bundle.status
        assert restored.notes == bundle.notes

    def test_compute_hash_is_deterministic(self) -> None:
        bundle = SpecBundle(
            target="trello.com",
            scope_hash="abc123",
            spec_items=[
                SpecItem(
                    feature="boards",
                    spec_type="ui_behavior",
                    content={"desc": "test"},
                )
            ],
        )
        fact = make_fact()
        h1 = bundle.compute_hash([fact])
        h2 = bundle.compute_hash([fact])
        assert h1 == h2
        assert len(h1) == 32

    def test_compute_hash_differs_on_different_facts(self) -> None:
        bundle = SpecBundle(target="trello.com")
        f1 = make_fact(claim="Claim one")
        f2 = make_fact(claim="Claim two")
        h1 = bundle.compute_hash([f1])
        h2 = bundle.compute_hash([f2])
        assert h1 != h2

    def test_compute_hash_order_independent(self) -> None:
        """compute_hash must sort facts — order of input list should not matter."""
        bundle = SpecBundle(target="trello.com", scope_hash="fixed")
        f1 = make_fact(claim="Claim one")
        f2 = make_fact(claim="Claim two")
        h1 = bundle.compute_hash([f1, f2])
        h2 = bundle.compute_hash([f2, f1])
        assert h1 == h2

    def test_spec_items_round_trip(self) -> None:
        bundle = SpecBundle(
            target="trello.com",
            spec_items=[
                SpecItem(
                    feature="boards",
                    spec_type="api_contract",
                    content={"method": "GET"},
                    supporting_fact_ids=["fact-1"],
                )
            ],
        )
        d = bundle.to_dict()
        restored = SpecBundle.from_dict(d)
        assert len(restored.spec_items) == 1
        assert restored.spec_items[0].feature == "boards"


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class TestScope:
    def test_feature_keys(self) -> None:
        scope = Scope(
            resolved_features=[
                ScopeNode(feature="boards"),
                ScopeNode(feature="drag-drop"),
            ]
        )
        assert scope.feature_keys() == ["boards", "drag-drop"]

    def test_dependency_order_simple(self) -> None:
        scope = Scope(
            resolved_features=[
                ScopeNode(feature="drag-drop", depends_on=["boards"]),
                ScopeNode(feature="boards"),
            ]
        )
        order = scope.dependency_order()
        assert order[0] == ["boards"]
        assert order[1] == ["drag-drop"]

    def test_dependency_order_cycle_raises(self) -> None:
        scope = Scope(
            resolved_features=[
                ScopeNode(feature="a", depends_on=["b"]),
                ScopeNode(feature="b", depends_on=["a"]),
            ]
        )
        with pytest.raises(ValueError, match="Circular dependency"):
            scope.dependency_order()

    def test_compute_scope_hash_deterministic(self) -> None:
        scope = Scope(
            requested_features=[ScopeNode(feature="boards")],
            resolved_features=[ScopeNode(feature="boards"), ScopeNode(feature="drag-drop")],
            dependency_edges=[DependencyEdge(from_feature="drag-drop", to_feature="boards")],
        )
        h1 = scope.compute_scope_hash()
        h2 = scope.compute_scope_hash()
        assert h1 == h2
        assert len(h1) == 16

    def test_compute_scope_hash_differs_on_different_features(self) -> None:
        s1 = Scope(requested_features=[ScopeNode(feature="boards")])
        s2 = Scope(requested_features=[ScopeNode(feature="cards")])
        assert s1.compute_scope_hash() != s2.compute_scope_hash()
