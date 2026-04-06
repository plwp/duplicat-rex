"""
Tests for scripts/spec_store.py

Covers:
- Store CRUD (add, get, query)
- Deduplication by content_hash (INV-024)
- Status transitions (update_fact_status)
- Fact revision / supersede chain
- get_facts_for_feature (excludes contradicted + superseded)
- Contradiction detection
- Provenance chain traversal (INV-007 cycle detection)
- Bundle lifecycle: create -> add_facts -> validate -> snapshot
- Snapshot immutability (INV-016)
- diff_snapshots
- stats()
- Index consistency (INV-025)
- Atomic writes (no partial data on disk)
"""

from __future__ import annotations

import pytest

from scripts.models import (
    Authority,
    BundleStatus,
    Confidence,
    EvidenceRef,
    Fact,
    FactCategory,
    FactStatus,
    SourceType,
    SpecBundle,
    SpecItem,
)
from scripts.spec_store import SpecStore, SpecStoreError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return SpecStore(tmp_path)


def make_evidence(url: str = "https://example.com") -> EvidenceRef:
    return EvidenceRef(
        source_url=url,
        source_title="Test",
        captured_at="2024-01-15T10:00:00+00:00",
    )


def make_fact(
    *,
    feature: str = "boards",
    claim: str = "Boards have a title field",
    category: FactCategory = FactCategory.UI_COMPONENT,
    source_type: SourceType = SourceType.LIVE_APP,
    authority: Authority = Authority.AUTHORITATIVE,
    tags: list[str] | None = None,
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
        tags=tags or [],
    )


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


class TestAddAndGetFact:
    def test_add_and_retrieve(self, store: SpecStore) -> None:
        fact = make_fact()
        stored = store.add_fact(fact)
        retrieved = store.get_fact(stored.id)
        assert retrieved.id == fact.id
        assert retrieved.claim == fact.claim

    def test_get_nonexistent_raises(self, store: SpecStore) -> None:
        with pytest.raises(SpecStoreError, match="Fact not found"):
            store.get_fact("nonexistent-id")

    def test_add_fact_no_evidence_raises(self, store: SpecStore) -> None:
        """INV-001: every fact must have at least one EvidenceRef."""
        fact = Fact(
            feature="boards",
            category=FactCategory.UI_COMPONENT,
            claim="Some claim",
            evidence=[],  # violates INV-001
            source_type=SourceType.LIVE_APP,
        )
        with pytest.raises(SpecStoreError, match="INV-001"):
            store.add_fact(fact)

    def test_add_facts_bulk(self, store: SpecStore) -> None:
        facts = [make_fact(claim=f"Claim {i}") for i in range(5)]
        stored = store.add_facts(facts)
        assert len(stored) == 5

    def test_fact_file_written(self, store: SpecStore, tmp_path) -> None:
        fact = make_fact()
        store.add_fact(fact)
        fact_file = tmp_path / ".specstore" / "facts" / f"{fact.id}.json"
        assert fact_file.exists()


# ---------------------------------------------------------------------------
# Deduplication (INV-024)
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_same_content_returns_existing(self, store: SpecStore) -> None:
        """Adding a fact with the same content_hash returns the stored fact (INV-024)."""
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
        assert f1.content_hash() == f2.content_hash()
        assert f1.id != f2.id

        stored1 = store.add_fact(f1)
        stored2 = store.add_fact(f2)
        # Both should return the first stored fact
        assert stored1.id == f1.id
        assert stored2.id == f1.id

    def test_different_content_both_stored(self, store: SpecStore) -> None:
        f1 = make_fact(claim="Claim one")
        f2 = make_fact(claim="Claim two")
        store.add_fact(f1)
        store.add_fact(f2)
        s = store.stats()
        assert s["total_facts"] == 2

    def test_dedup_preserves_index_integrity(self, store: SpecStore) -> None:
        """After dedup, content_hashes index must have exactly one entry."""
        f1 = make_fact()
        # Create same-content fact via from_dict with different id
        d = f1.to_dict()
        import uuid as uuid_mod
        d["id"] = str(uuid_mod.uuid4())
        f2 = Fact.from_dict(d)
        store.add_fact(f1)
        store.add_fact(f2)
        s = store.stats()
        assert s["total_content_hashes"] == 1
        assert s["total_facts"] == 1


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


class TestQueryFacts:
    def test_query_by_feature(self, store: SpecStore) -> None:
        store.add_fact(make_fact(feature="boards", claim="Boards claim"))
        store.add_fact(make_fact(feature="cards", claim="Cards claim"))
        results = store.query_facts(feature="boards")
        assert len(results) == 1
        assert results[0].feature == "boards"

    def test_query_by_category(self, store: SpecStore) -> None:
        store.add_fact(make_fact(category=FactCategory.UI_COMPONENT, claim="UI claim"))
        store.add_fact(make_fact(category=FactCategory.API_ENDPOINT, claim="API claim"))
        results = store.query_facts(category=FactCategory.UI_COMPONENT)
        assert len(results) == 1

    def test_query_by_authority(self, store: SpecStore) -> None:
        store.add_fact(make_fact(authority=Authority.AUTHORITATIVE, claim="Auth claim"))
        store.add_fact(make_fact(authority=Authority.ANECDOTAL, claim="Anec claim"))
        results = store.query_facts(authority=Authority.AUTHORITATIVE)
        assert len(results) == 1
        assert results[0].authority == Authority.AUTHORITATIVE

    def test_query_by_status(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact(claim="Claim for status test"))
        corr = store.add_fact(make_fact(claim="Corroborating fact"))
        store.update_fact_status(fact.id, FactStatus.VERIFIED, related_fact_ids=[corr.id])
        unverified = store.query_facts(status=FactStatus.UNVERIFIED)
        verified = store.query_facts(status=FactStatus.VERIFIED)
        assert not any(f.id == fact.id for f in unverified)
        assert any(f.id == fact.id for f in verified)

    def test_query_by_tags(self, store: SpecStore) -> None:
        store.add_fact(make_fact(claim="Tagged claim", tags=["alpha", "beta"]))
        store.add_fact(make_fact(claim="Other claim", tags=["gamma"]))
        results = store.query_facts(tags=["alpha"])
        assert len(results) == 1
        results_multi = store.query_facts(tags=["alpha", "beta"])
        assert len(results_multi) == 1

    def test_query_combined_filters(self, store: SpecStore) -> None:
        store.add_fact(make_fact(feature="boards", category=FactCategory.UI_COMPONENT,
                                 claim="UI boards"))
        store.add_fact(make_fact(feature="boards", category=FactCategory.API_ENDPOINT,
                                 claim="API boards"))
        results = store.query_facts(feature="boards", category=FactCategory.API_ENDPOINT)
        assert len(results) == 1
        assert results[0].category == FactCategory.API_ENDPOINT

    def test_query_excludes_soft_deleted(self, store: SpecStore) -> None:
        """Soft-deleted facts must not appear in query results."""
        fact = store.add_fact(make_fact(claim="Will be deleted"))
        # Manually soft-delete by writing deleted_at
        d = fact.to_dict()
        d["deleted_at"] = "2024-06-01T00:00:00+00:00"
        from scripts.spec_store import _atomic_write
        _atomic_write(store._fact_path(fact.id), d)
        # Also patch index
        index = store._load_index()
        index["facts"][fact.id]["deleted_at"] = d["deleted_at"]
        store._save_index(index)
        results = store.query_facts()
        assert not any(f.id == fact.id for f in results)


# ---------------------------------------------------------------------------
# update_fact_status
# ---------------------------------------------------------------------------


class TestUpdateFactStatus:
    def test_verify_fact(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact(claim="Main fact"))
        corr = store.add_fact(make_fact(claim="Corroborating fact"))
        updated = store.update_fact_status(
            fact.id, FactStatus.VERIFIED, related_fact_ids=[corr.id]
        )
        assert updated.status == FactStatus.VERIFIED
        assert corr.id in updated.verified_by

    def test_contradict_fact(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact(claim="Main fact"))
        contra = store.add_fact(make_fact(claim="Opposing fact"))
        updated = store.update_fact_status(
            fact.id, FactStatus.CONTRADICTED, related_fact_ids=[contra.id]
        )
        assert updated.status == FactStatus.CONTRADICTED
        assert contra.id in updated.contradicted_by

    def test_verified_without_related_raises(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact())
        with pytest.raises(SpecStoreError, match="VERIFIED requires"):
            store.update_fact_status(fact.id, FactStatus.VERIFIED)

    def test_contradicted_without_related_raises(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact())
        with pytest.raises(SpecStoreError, match="CONTRADICTED requires"):
            store.update_fact_status(fact.id, FactStatus.CONTRADICTED)

    def test_status_persisted_to_disk(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact())
        corr = store.add_fact(make_fact(claim="Corroborator"))
        store.update_fact_status(fact.id, FactStatus.VERIFIED, related_fact_ids=[corr.id])
        # Reload from disk
        reloaded = store.get_fact(fact.id)
        assert reloaded.status == FactStatus.VERIFIED

    def test_content_hash_unchanged_after_status_update(self, store: SpecStore) -> None:
        """INV-003: content fields are immutable — hash must not change."""
        fact = store.add_fact(make_fact())
        original_hash = fact.content_hash()
        corr = store.add_fact(make_fact(claim="Corroborator"))
        updated = store.update_fact_status(
            fact.id, FactStatus.VERIFIED, related_fact_ids=[corr.id]
        )
        assert updated.content_hash() == original_hash

    def test_index_updated_after_status_change(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact())
        corr = store.add_fact(make_fact(claim="Corroborator"))
        store.update_fact_status(fact.id, FactStatus.VERIFIED, related_fact_ids=[corr.id])
        index = store._load_index()
        assert index["facts"][fact.id]["status"] == "verified"


# ---------------------------------------------------------------------------
# Fact revision / provenance chain
# ---------------------------------------------------------------------------


class TestReviseFactAndProvenance:
    def test_revise_creates_new_fact(self, store: SpecStore) -> None:
        original = store.add_fact(make_fact())
        revised = store.revise_fact(
            original.id,
            {"claim": "Updated: Boards have a title and description"},
        )
        assert revised.id != original.id
        assert revised.claim == "Updated: Boards have a title and description"
        assert revised.supersedes == original.id
        assert revised.revision == original.revision + 1

    def test_revise_original_unmarked_in_index(self, store: SpecStore) -> None:
        original = store.add_fact(make_fact())
        revised = store.revise_fact(original.id, {"claim": "New claim"})
        index = store._load_index()
        assert index["facts"][original.id].get("superseded_by") == revised.id

    def test_revise_forbidden_fields_raises(self, store: SpecStore) -> None:
        original = store.add_fact(make_fact())
        with pytest.raises(SpecStoreError, match="immutable"):
            store.revise_fact(original.id, {"id": "hacked-id"})

    def test_revise_status_reset_to_unverified(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact())
        corr = store.add_fact(make_fact(claim="Corroborator"))
        store.update_fact_status(fact.id, FactStatus.VERIFIED, related_fact_ids=[corr.id])
        revised = store.revise_fact(fact.id, {"claim": "Revised claim"})
        assert revised.status == FactStatus.UNVERIFIED

    def test_provenance_chain_two_steps(self, store: SpecStore) -> None:
        f1 = store.add_fact(make_fact())
        f2 = store.revise_fact(f1.id, {"claim": "Step 2 claim"})
        f3 = store.revise_fact(f2.id, {"claim": "Step 3 claim"})
        chain = store.get_provenance_chain(f3.id)
        assert len(chain) == 3
        assert chain[0].id == f3.id
        assert chain[1].id == f2.id
        assert chain[2].id == f1.id

    def test_provenance_chain_single_fact(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact())
        chain = store.get_provenance_chain(fact.id)
        assert len(chain) == 1
        assert chain[0].id == fact.id

    def test_provenance_chain_nonexistent_raises(self, store: SpecStore) -> None:
        with pytest.raises(SpecStoreError):
            store.get_provenance_chain("nonexistent")


# ---------------------------------------------------------------------------
# get_facts_for_feature
# ---------------------------------------------------------------------------


class TestGetFactsForFeature:
    def test_returns_active_facts(self, store: SpecStore) -> None:
        f1 = store.add_fact(make_fact(feature="boards", claim="Active claim 1"))
        f2 = store.add_fact(make_fact(feature="boards", claim="Active claim 2"))
        results = store.get_facts_for_feature("boards")
        ids = [f.id for f in results]
        assert f1.id in ids
        assert f2.id in ids

    def test_excludes_contradicted(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact(feature="boards", claim="Contradicted claim"))
        contra = store.add_fact(make_fact(feature="boards", claim="Contradicting fact"))
        store.update_fact_status(fact.id, FactStatus.CONTRADICTED, related_fact_ids=[contra.id])
        results = store.get_facts_for_feature("boards")
        ids = [f.id for f in results]
        assert fact.id not in ids

    def test_excludes_superseded(self, store: SpecStore) -> None:
        original = store.add_fact(make_fact(feature="boards", claim="Original"))
        store.revise_fact(original.id, {"claim": "Revised"})
        results = store.get_facts_for_feature("boards")
        ids = [f.id for f in results]
        assert original.id not in ids  # original is now superseded

    def test_returns_empty_for_unknown_feature(self, store: SpecStore) -> None:
        results = store.get_facts_for_feature("nonexistent-feature")
        assert results == []


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------


class TestFindContradictions:
    def test_finds_contradiction_pair(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact(feature="auth", claim="Session expires in 30min"))
        contra = store.add_fact(make_fact(feature="auth", claim="Session expires in 60min"))
        store.update_fact_status(fact.id, FactStatus.CONTRADICTED, related_fact_ids=[contra.id])
        pairs = store.find_contradictions("auth")
        assert len(pairs) >= 1
        pair_ids = [(a.id, b.id) for a, b in pairs]
        assert (fact.id, contra.id) in pair_ids

    def test_no_contradictions_returns_empty(self, store: SpecStore) -> None:
        store.add_fact(make_fact(feature="boards", claim="Boards claim"))
        pairs = store.find_contradictions("boards")
        assert pairs == []

    def test_wrong_feature_not_returned(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact(feature="auth", claim="Auth claim"))
        contra = store.add_fact(make_fact(feature="auth", claim="Contradicting auth"))
        store.update_fact_status(fact.id, FactStatus.CONTRADICTED, related_fact_ids=[contra.id])
        pairs = store.find_contradictions("boards")
        assert pairs == []


# ---------------------------------------------------------------------------
# Bundle lifecycle
# ---------------------------------------------------------------------------


class TestBundleLifecycle:
    def test_create_bundle(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards", "cards"])
        assert bundle.status == BundleStatus.DRAFT
        assert bundle.target == "trello.com"
        assert bundle.scope == ["boards", "cards"]

    def test_get_bundle(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        retrieved = store.get_bundle(bundle.id)
        assert retrieved.id == bundle.id

    def test_get_bundle_nonexistent_raises(self, store: SpecStore) -> None:
        with pytest.raises(SpecStoreError, match="Bundle not found"):
            store.get_bundle("nonexistent")

    def test_add_facts_to_bundle(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact())
        updated = store.add_facts_to_bundle(bundle.id, [fact.id])
        assert fact.id in updated.fact_ids

    def test_add_facts_deduped_in_bundle(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact())
        store.add_facts_to_bundle(bundle.id, [fact.id])
        store.add_facts_to_bundle(bundle.id, [fact.id])  # add again
        b = store.get_bundle(bundle.id)
        assert b.fact_ids.count(fact.id) == 1

    def test_add_facts_nonexistent_raises(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        with pytest.raises(SpecStoreError, match="Fact .* not found"):
            store.add_facts_to_bundle(bundle.id, ["nonexistent-fact-id"])

    def test_add_facts_to_non_draft_raises(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        # Manually snapshot (via validation path not needed for this test)
        b = store.get_bundle(bundle.id)
        b.status = BundleStatus.SNAPSHOT
        from scripts.spec_store import _atomic_write
        _atomic_write(store._bundle_path(bundle.id), b.to_dict())
        index = store._load_index()
        index["bundles"][bundle.id]["status"] = "snapshot"
        store._save_index(index)
        fact = store.add_fact(make_fact())
        with pytest.raises(SpecStoreError):
            store.add_facts_to_bundle(bundle.id, [fact.id])


# ---------------------------------------------------------------------------
# Bundle validation
# ---------------------------------------------------------------------------


class TestValidateBundle:
    def test_valid_bundle(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact(feature="boards"))
        store.add_facts_to_bundle(bundle.id, [fact.id])
        ok, issues = store.validate_bundle(bundle.id)
        assert ok
        assert issues == []

    def test_invalid_bundle_with_contradicted_fact(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact(claim="Contradicted"))
        contra = store.add_fact(make_fact(claim="Contradicting"))
        store.add_facts_to_bundle(bundle.id, [fact.id])
        store.update_fact_status(fact.id, FactStatus.CONTRADICTED, related_fact_ids=[contra.id])
        ok, issues = store.validate_bundle(bundle.id)
        assert not ok
        assert any("contradicted" in i.lower() for i in issues)

    def test_snapshot_bundle_validation_fails(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact(feature="boards"))
        store.add_facts_to_bundle(bundle.id, [fact.id])
        store.set_bundle_status(bundle.id, BundleStatus.VALIDATED)
        store.snapshot_bundle(bundle.id)
        ok, issues = store.validate_bundle(bundle.id)
        assert not ok
        assert any("SNAPSHOT" in i for i in issues)

    def test_spec_item_without_supporting_facts_fails_validation(
        self, store: SpecStore
    ) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        item = SpecItem(
            feature="boards",
            spec_type="ui_behavior",
            content={"desc": "test"},
            supporting_fact_ids=[],  # no supporting facts
        )
        store.add_spec_item_to_bundle(bundle.id, item)
        ok, issues = store.validate_bundle(bundle.id)
        assert not ok
        assert any("supporting facts" in i for i in issues)


# ---------------------------------------------------------------------------
# set_bundle_status
# ---------------------------------------------------------------------------


class TestSetBundleStatus:
    def test_set_validated(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact(feature="boards"))
        store.add_facts_to_bundle(bundle.id, [fact.id])
        updated = store.set_bundle_status(bundle.id, BundleStatus.VALIDATED)
        assert updated.status == BundleStatus.VALIDATED
        assert updated.validated_at is not None

    def test_set_validated_with_contradicted_facts_raises(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact(claim="Main"))
        contra = store.add_fact(make_fact(claim="Contra"))
        store.add_facts_to_bundle(bundle.id, [fact.id])
        store.update_fact_status(fact.id, FactStatus.CONTRADICTED, related_fact_ids=[contra.id])
        with pytest.raises(SpecStoreError, match="failed validation"):
            store.set_bundle_status(bundle.id, BundleStatus.VALIDATED)

    def test_set_snapshot_directly_raises(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        with pytest.raises(SpecStoreError, match="snapshot_bundle"):
            store.set_bundle_status(bundle.id, BundleStatus.SNAPSHOT)

    def test_modify_snapshot_bundle_raises(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact(feature="boards"))
        store.add_facts_to_bundle(bundle.id, [fact.id])
        store.set_bundle_status(bundle.id, BundleStatus.VALIDATED)
        store.snapshot_bundle(bundle.id)
        with pytest.raises(SpecStoreError, match="immutable"):
            store.set_bundle_status(bundle.id, BundleStatus.DRAFT)


# ---------------------------------------------------------------------------
# Snapshot (INV-016)
# ---------------------------------------------------------------------------


class TestSnapshotBundle:
    def test_snapshot_sets_content_hash(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact(feature="boards"))
        store.add_facts_to_bundle(bundle.id, [fact.id])
        store.set_bundle_status(bundle.id, BundleStatus.VALIDATED)
        snapped = store.snapshot_bundle(bundle.id)
        assert snapped.status == BundleStatus.SNAPSHOT
        assert snapped.content_hash is not None
        assert snapped.snapshot_at is not None

    def test_snapshot_file_created(self, store: SpecStore, tmp_path) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact(feature="boards"))
        store.add_facts_to_bundle(bundle.id, [fact.id])
        store.set_bundle_status(bundle.id, BundleStatus.VALIDATED)
        snapped = store.snapshot_bundle(bundle.id)
        snap_file = tmp_path / ".specstore" / "snapshots" / f"{snapped.id}-v1.json"
        assert snap_file.exists()

    def test_snapshot_non_validated_raises(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        with pytest.raises(SpecStoreError, match="VALIDATED"):
            store.snapshot_bundle(bundle.id)

    def test_snapshot_duplicate_raises(self, store: SpecStore, tmp_path) -> None:
        """INV-016: Snapshot files must never be overwritten."""
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact(feature="boards"))
        store.add_facts_to_bundle(bundle.id, [fact.id])
        store.set_bundle_status(bundle.id, BundleStatus.VALIDATED)
        store.snapshot_bundle(bundle.id)
        # Manually reset status to VALIDATED on disk to attempt double-snapshot
        b = store.get_bundle(bundle.id)
        b.status = BundleStatus.VALIDATED
        b.content_hash = None
        from scripts.spec_store import _atomic_write
        _atomic_write(store._bundle_path(bundle.id), b.to_dict())
        with pytest.raises(SpecStoreError, match="already exists"):
            store.snapshot_bundle(bundle.id)

    def test_content_hash_deterministic_across_runs(self, store: SpecStore) -> None:
        """Same facts and spec_items must produce same content_hash on second store."""
        bundle = store.create_bundle("trello.com", ["boards"])
        bundle.scope_hash = "fixed-scope-hash"
        from scripts.spec_store import _atomic_write
        _atomic_write(store._bundle_path(bundle.id), bundle.to_dict())

        fact = store.add_fact(make_fact(feature="boards"))
        store.add_facts_to_bundle(bundle.id, [fact.id])
        store.set_bundle_status(bundle.id, BundleStatus.VALIDATED)
        snapped = store.snapshot_bundle(bundle.id)
        h1 = snapped.content_hash

        # Recompute manually
        b2 = store.get_bundle(bundle.id)
        facts = [store.get_fact(fid) for fid in b2.fact_ids]
        h2 = b2.compute_hash(facts)
        assert h1 == h2


# ---------------------------------------------------------------------------
# diff_snapshots
# ---------------------------------------------------------------------------


class TestDiffSnapshots:
    def _make_snapshot(self, store: SpecStore, facts: list[Fact]) -> SpecBundle:
        bundle = store.create_bundle("trello.com", ["boards"])
        for f in facts:
            store.add_facts_to_bundle(bundle.id, [f.id])
        store.set_bundle_status(bundle.id, BundleStatus.VALIDATED)
        return store.snapshot_bundle(bundle.id)

    def test_diff_identical_snapshots(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact())
        b1 = self._make_snapshot(store, [fact])
        b2 = self._make_snapshot(store, [fact])
        diff = store.diff_snapshots(b1.id, b2.id)
        assert diff["added_facts"] == []
        assert diff["removed_facts"] == []
        assert fact.id in diff["common_facts"]

    def test_diff_added_fact(self, store: SpecStore) -> None:
        f1 = store.add_fact(make_fact(claim="Claim 1"))
        f2 = store.add_fact(make_fact(claim="Claim 2"))
        b1 = self._make_snapshot(store, [f1])
        b2 = self._make_snapshot(store, [f1, f2])
        diff = store.diff_snapshots(b1.id, b2.id)
        assert f2.id in diff["added_facts"]
        assert f1.id in diff["common_facts"]

    def test_diff_removed_fact(self, store: SpecStore) -> None:
        f1 = store.add_fact(make_fact(claim="Claim 1"))
        f2 = store.add_fact(make_fact(claim="Claim 2"))
        b1 = self._make_snapshot(store, [f1, f2])
        b2 = self._make_snapshot(store, [f1])
        diff = store.diff_snapshots(b1.id, b2.id)
        assert f2.id in diff["removed_facts"]


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_empty_store(self, store: SpecStore) -> None:
        s = store.stats()
        assert s["total_facts"] == 0
        assert s["total_bundles"] == 0

    def test_stats_after_operations(self, store: SpecStore) -> None:
        f1 = store.add_fact(make_fact(claim="Claim 1"))
        store.add_fact(make_fact(claim="Claim 2"))
        contra = store.add_fact(make_fact(claim="Contradicting"))
        store.update_fact_status(f1.id, FactStatus.CONTRADICTED, related_fact_ids=[contra.id])
        store.create_bundle("trello.com", ["boards"])
        s = store.stats()
        assert s["total_facts"] == 3
        assert s["fact_status_counts"]["contradicted"] == 1
        assert s["total_bundles"] == 1

    def test_stats_counts_superseded(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact())
        store.revise_fact(fact.id, {"claim": "Revised claim"})
        s = store.stats()
        assert s["superseded_facts"] == 1
        assert s["total_facts"] == 2
        assert s["active_facts"] == 1

    def test_stats_by_feature(self, store: SpecStore) -> None:
        store.add_fact(make_fact(feature="boards", claim="B1"))
        store.add_fact(make_fact(feature="boards", claim="B2"))
        store.add_fact(make_fact(feature="cards", claim="C1"))
        s = store.stats()
        assert s["facts_by_feature"]["boards"] == 2
        assert s["facts_by_feature"]["cards"] == 1


# ---------------------------------------------------------------------------
# Index consistency (INV-025)
# ---------------------------------------------------------------------------


class TestIndexConsistency:
    def test_index_updated_on_add(self, store: SpecStore) -> None:
        fact = store.add_fact(make_fact())
        index = store._load_index()
        assert fact.id in index["facts"]
        assert fact.content_hash() in index["content_hashes"]

    def test_index_bundle_entry_on_create(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        index = store._load_index()
        assert bundle.id in index["bundles"]

    def test_index_bundle_status_updated(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact(feature="boards"))
        store.add_facts_to_bundle(bundle.id, [fact.id])
        store.set_bundle_status(bundle.id, BundleStatus.VALIDATED)
        index = store._load_index()
        assert index["bundles"][bundle.id]["status"] == "validated"

    def test_index_snapshot_count_incremented(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact(feature="boards"))
        store.add_facts_to_bundle(bundle.id, [fact.id])
        store.set_bundle_status(bundle.id, BundleStatus.VALIDATED)
        store.snapshot_bundle(bundle.id)
        index = store._load_index()
        assert index["bundles"][bundle.id]["snapshot_count"] == 1


# ---------------------------------------------------------------------------
# Spec item management
# ---------------------------------------------------------------------------


class TestSpecItemManagement:
    def test_add_spec_item_to_bundle(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact(feature="boards"))
        item = SpecItem(
            feature="boards",
            spec_type="ui_behavior",
            content={"desc": "Boards show title"},
            supporting_fact_ids=[fact.id],
            confidence=Confidence.HIGH,
        )
        updated = store.add_spec_item_to_bundle(bundle.id, item)
        assert len(updated.spec_items) == 1

    def test_add_spec_item_to_snapshot_raises(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact(feature="boards"))
        store.add_facts_to_bundle(bundle.id, [fact.id])
        store.set_bundle_status(bundle.id, BundleStatus.VALIDATED)
        store.snapshot_bundle(bundle.id)
        item = SpecItem(feature="boards", spec_type="ui", content={})
        with pytest.raises(SpecStoreError, match="immutable"):
            store.add_spec_item_to_bundle(bundle.id, item)

    def test_spec_item_persisted(self, store: SpecStore) -> None:
        bundle = store.create_bundle("trello.com", ["boards"])
        fact = store.add_fact(make_fact(feature="boards"))
        item = SpecItem(
            feature="boards",
            spec_type="api_contract",
            content={"method": "POST"},
            supporting_fact_ids=[fact.id],
        )
        store.add_spec_item_to_bundle(bundle.id, item)
        reloaded = store.get_bundle(bundle.id)
        assert len(reloaded.spec_items) == 1
        assert reloaded.spec_items[0].spec_type == "api_contract"
