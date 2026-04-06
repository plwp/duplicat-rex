"""
SpecStore — file-backed store for Facts and SpecBundles.

Storage layout:
    .specstore/
    ├── facts/{fact-id}.json
    ├── bundles/{bundle-id}.json
    ├── snapshots/{bundle-id}-v{N}.json
    └── index.json

Design decisions:
- All writes are atomic (write-to-temp then os.replace) to prevent partial writes.
- The index is always updated after successful fact/bundle mutations.
- Snapshot files are named {bundle-id}-v{N}.json and are never overwritten.
- Provenance chains are traversed by following supersedes links.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.models import (
    Authority,
    BundleStatus,
    Fact,
    FactCategory,
    FactStatus,
    SpecBundle,
    SpecItem,
)

# ---------------------------------------------------------------------------
# Index structure
# ---------------------------------------------------------------------------

_DEFAULT_INDEX: dict[str, Any] = {
    "version": 1,
    "facts": {},  # fact_id -> {content_hash, feature, category, status, superseded_by}
    "bundles": {},  # bundle_id -> {status, version, target, scope_hash, snapshot_count}
    "content_hashes": {},  # content_hash -> fact_id  (INV-024 dedup)
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp_path = f.name
    os.replace(tmp_path, path)


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# SpecStore
# ---------------------------------------------------------------------------


class SpecStoreError(Exception):
    """Raised for all store-level contract violations."""


class SpecStore:
    """
    File-backed store for Facts and SpecBundles.

    All public methods raise SpecStoreError on contract violations.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / ".specstore"
        self._facts_dir = self.root / "facts"
        self._bundles_dir = self.root / "bundles"
        self._snapshots_dir = self.root / "snapshots"
        self._index_path = self.root / "index.json"
        self._ensure_dirs()

    # ------------------------------------------------------------------
    # Internal: directory / index management
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        for d in (self._facts_dir, self._bundles_dir, self._snapshots_dir):
            d.mkdir(parents=True, exist_ok=True)
        if not self._index_path.exists():
            _atomic_write(self._index_path, _DEFAULT_INDEX)

    def _load_index(self) -> dict[str, Any]:
        return _read_json(self._index_path)

    def _save_index(self, index: dict[str, Any]) -> None:
        _atomic_write(self._index_path, index)

    def _fact_path(self, fact_id: str) -> Path:
        return self._facts_dir / f"{fact_id}.json"

    def _bundle_path(self, bundle_id: str) -> Path:
        return self._bundles_dir / f"{bundle_id}.json"

    def _snapshot_path(self, bundle_id: str, version: int) -> Path:
        return self._snapshots_dir / f"{bundle_id}-v{version}.json"

    # ------------------------------------------------------------------
    # Fact CRUD
    # ------------------------------------------------------------------

    def add_fact(self, fact: Fact) -> Fact:
        """
        Persist a Fact.

        - Enforces INV-001: at least one EvidenceRef.
        - Enforces INV-024: dedup by content_hash (returns existing if duplicate).
        - Updates index (INV-025).
        """
        if not fact.evidence:
            raise SpecStoreError(f"Fact {fact.id!r} violates INV-001: no EvidenceRef present")

        h = fact.content_hash()
        index = self._load_index()

        # INV-024: dedup
        if h in index["content_hashes"]:
            existing_id = index["content_hashes"][h]
            return self.get_fact(existing_id)

        _atomic_write(self._fact_path(fact.id), fact.to_dict())

        index["facts"][fact.id] = {
            "content_hash": h,
            "feature": fact.feature,
            "category": str(fact.category),
            "status": str(fact.status),
            "superseded_by": None,
            "deleted_at": fact.deleted_at,
            "tags": list(fact.tags),
            "authority": str(fact.authority),
            "module_name": fact.module_name,
        }
        index["content_hashes"][h] = fact.id
        self._save_index(index)
        return fact

    def add_facts(self, facts: list[Fact]) -> list[Fact]:
        """Bulk add with per-fact dedup. Returns the stored fact for each input."""
        return [self.add_fact(f) for f in facts]

    def get_fact(self, fact_id: str) -> Fact:
        path = self._fact_path(fact_id)
        if not path.exists():
            raise SpecStoreError(f"Fact not found: {fact_id!r}")
        return Fact.from_dict(_read_json(path))

    def query_facts(
        self,
        *,
        feature: str | None = None,
        category: FactCategory | None = None,
        module: str | None = None,
        authority: Authority | None = None,
        status: FactStatus | None = None,
        tags: list[str] | None = None,
    ) -> list[Fact]:
        """Return facts matching all supplied filters (AND semantics). Excludes soft-deleted."""
        index = self._load_index()
        results: list[Fact] = []
        for fact_id, meta in index["facts"].items():
            if meta.get("deleted_at"):
                continue
            if feature is not None and meta["feature"] != feature:
                continue
            if category is not None and meta["category"] != str(category):
                continue
            if module is not None and meta.get("module_name") != module:
                continue
            if authority is not None and meta.get("authority") != str(authority):
                continue
            if status is not None and meta["status"] != str(status):
                continue
            if tags:
                fact_tags = set(meta.get("tags", []))
                if not set(tags).issubset(fact_tags):
                    continue
            results.append(self.get_fact(fact_id))
        return results

    def update_fact_status(
        self,
        fact_id: str,
        status: FactStatus,
        related_fact_ids: list[str] | None = None,
        note: str = "",  # noqa: ARG002 — reserved for future audit log
    ) -> Fact:
        """
        Update lifecycle status of a fact.

        - VERIFIED requires at least one entry in related_fact_ids (corroborating facts).
        - CONTRADICTED requires at least one entry in related_fact_ids (opposing facts).
        - Immutable content fields are never touched (INV-003).
        """
        fact = self.get_fact(fact_id)
        related = related_fact_ids or []

        if status == FactStatus.VERIFIED and not related:
            raise SpecStoreError(
                f"Fact {fact_id!r}: VERIFIED requires at least one corroborating fact ID"
            )
        if status == FactStatus.CONTRADICTED and not related:
            raise SpecStoreError(
                f"Fact {fact_id!r}: CONTRADICTED requires at least one opposing fact ID"
            )

        # Build a new Fact with updated lifecycle fields (frozen dataclass, so reconstruct)
        new_verified_by = list(fact.verified_by)
        new_contradicted_by = list(fact.contradicted_by)
        if status == FactStatus.VERIFIED:
            new_verified_by = sorted(set(new_verified_by + related))
        elif status == FactStatus.CONTRADICTED:
            new_contradicted_by = sorted(set(new_contradicted_by + related))

        updated = Fact(
            id=fact.id,
            feature=fact.feature,
            category=fact.category,
            claim=fact.claim,
            evidence=fact.evidence,
            source_type=fact.source_type,
            structured_data=fact.structured_data,
            module_name=fact.module_name,
            authority=fact.authority,
            confidence=fact.confidence,
            run_id=fact.run_id,
            status=status,
            verified_by=new_verified_by,
            contradicted_by=new_contradicted_by,
            supersedes=fact.supersedes,
            corroborates=fact.corroborates,
            contradicts=fact.contradicts,
            revision=fact.revision,
            deleted_at=fact.deleted_at,
            observed_at=fact.observed_at,
            freshness_ttl_days=fact.freshness_ttl_days,
            tags=fact.tags,
            redaction_status=fact.redaction_status,
            created_at=fact.created_at,
        )
        _atomic_write(self._fact_path(fact_id), updated.to_dict())

        index = self._load_index()
        if fact_id in index["facts"]:
            index["facts"][fact_id]["status"] = str(status)
            self._save_index(index)
        return updated

    def revise_fact(self, fact_id: str, patch: dict[str, Any], reason: str = "") -> Fact:  # noqa: ARG002
        """
        Create a revised Fact that supersedes the original.

        Only content fields (claim, structured_data, evidence, tags, confidence,
        authority, freshness_ttl_days, observed_at) may be patched.
        Lifecycle fields are inherited.

        Returns the new Fact.
        """
        original = self.get_fact(fact_id)

        # Disallow patching identity or lifecycle fields
        forbidden = {"id", "feature", "category", "source_type", "supersedes",
                     "status", "verified_by", "contradicted_by", "revision",
                     "created_at", "deleted_at", "corroborates", "contradicts"}
        bad = set(patch.keys()) & forbidden
        if bad:
            raise SpecStoreError(f"Cannot patch immutable/identity fields: {bad}")

        d = original.to_dict()
        d.update(patch)
        # Reset identity / provenance for the new fact
        d["id"] = str(uuid.uuid4())
        d["supersedes"] = original.id
        d["revision"] = original.revision + 1
        d["status"] = str(FactStatus.UNVERIFIED)
        d["verified_by"] = []
        d["contradicted_by"] = []
        d["created_at"] = _now_iso()

        new_fact = Fact.from_dict(d)

        # Validate
        if not new_fact.evidence:
            raise SpecStoreError("Revised fact must have at least one EvidenceRef (INV-001)")

        # Persist new fact (will go through add_fact dedup)
        self._persist_revision(new_fact, original.id)
        return new_fact

    def _persist_revision(self, new_fact: Fact, superseded_id: str) -> None:
        """Persist a revised fact and mark the original as superseded in the index."""
        h = new_fact.content_hash()
        index = self._load_index()

        _atomic_write(self._fact_path(new_fact.id), new_fact.to_dict())
        index["facts"][new_fact.id] = {
            "content_hash": h,
            "feature": new_fact.feature,
            "category": str(new_fact.category),
            "status": str(new_fact.status),
            "superseded_by": None,
            "deleted_at": new_fact.deleted_at,
            "tags": list(new_fact.tags),
            "authority": str(new_fact.authority),
            "module_name": new_fact.module_name,
        }
        index["content_hashes"][h] = new_fact.id

        # Mark original as superseded
        if superseded_id in index["facts"]:
            index["facts"][superseded_id]["superseded_by"] = new_fact.id

        self._save_index(index)

    def get_facts_for_feature(self, feature: str) -> list[Fact]:
        """
        Return all active (non-contradicted, non-superseded, non-deleted) facts for a feature.
        """
        index = self._load_index()
        results: list[Fact] = []
        for fact_id, meta in index["facts"].items():
            if meta["feature"] != feature:
                continue
            if meta.get("deleted_at"):
                continue
            if meta.get("superseded_by"):
                continue
            if meta["status"] == str(FactStatus.CONTRADICTED):
                continue
            results.append(self.get_fact(fact_id))
        return results

    def find_contradictions(self, feature: str) -> list[tuple[Fact, Fact]]:
        """
        Return pairs of (fact, contradicting_fact) for all contradicted facts in a feature.
        """
        index = self._load_index()
        pairs: list[tuple[Fact, Fact]] = []
        for fact_id, meta in index["facts"].items():
            if meta["feature"] != feature:
                continue
            if meta.get("deleted_at"):
                continue
            if meta["status"] != str(FactStatus.CONTRADICTED):
                continue
            fact = self.get_fact(fact_id)
            for contra_id in fact.contradicted_by:
                if self._fact_path(contra_id).exists():
                    pairs.append((fact, self.get_fact(contra_id)))
        return pairs

    def get_provenance_chain(self, fact_id: str) -> list[Fact]:
        """
        Walk the supersedes chain from the given fact back to the original.

        Returns [given_fact, ..., original_fact] — oldest-last order.
        Detects cycles (INV-007).
        """
        chain: list[Fact] = []
        seen: set[str] = set()
        current_id: str | None = fact_id
        while current_id is not None:
            if current_id in seen:
                raise SpecStoreError(
                    f"Provenance chain cycle detected at fact {current_id!r} (INV-007)"
                )
            seen.add(current_id)
            fact = self.get_fact(current_id)
            chain.append(fact)
            current_id = fact.supersedes
        return chain

    # ------------------------------------------------------------------
    # Bundle lifecycle
    # ------------------------------------------------------------------

    def create_bundle(self, target: str, scope: list[str]) -> SpecBundle:
        """Create a new DRAFT bundle for the given target and feature scope."""
        bundle = SpecBundle(target=target, scope=list(scope))
        _atomic_write(self._bundle_path(bundle.id), bundle.to_dict())

        index = self._load_index()
        index["bundles"][bundle.id] = {
            "status": str(BundleStatus.DRAFT),
            "version": bundle.version,
            "target": bundle.target,
            "scope_hash": bundle.scope_hash,
            "snapshot_count": 0,
        }
        self._save_index(index)
        return bundle

    def get_bundle(self, bundle_id: str) -> SpecBundle:
        path = self._bundle_path(bundle_id)
        if not path.exists():
            raise SpecStoreError(f"Bundle not found: {bundle_id!r}")
        return SpecBundle.from_dict(_read_json(path))

    def add_facts_to_bundle(self, bundle_id: str, fact_ids: list[str]) -> SpecBundle:
        """Add fact IDs to a DRAFT bundle. Raises if bundle is not DRAFT."""
        bundle = self.get_bundle(bundle_id)
        if bundle.status != BundleStatus.DRAFT:
            raise SpecStoreError(
                f"Bundle {bundle_id!r} is {bundle.status!r} — only DRAFT bundles accept new facts"
            )
        # Validate fact IDs exist
        for fid in fact_ids:
            if not self._fact_path(fid).exists():
                raise SpecStoreError(f"Fact {fid!r} not found — cannot add to bundle")
        # Dedup
        existing = set(bundle.fact_ids)
        new_ids = [fid for fid in fact_ids if fid not in existing]
        bundle.fact_ids.extend(new_ids)
        _atomic_write(self._bundle_path(bundle_id), bundle.to_dict())
        return bundle

    def validate_bundle(self, bundle_id: str) -> tuple[bool, list[str]]:
        """
        Validate a bundle and return (ok, issues).

        Checks:
        - Bundle must be DRAFT or VALIDATED (not SNAPSHOT).
        - No contradicted supporting facts.
        - Every SpecItem has at least one non-contradicted supporting fact.
        """
        bundle = self.get_bundle(bundle_id)
        issues: list[str] = []

        if bundle.status == BundleStatus.SNAPSHOT:
            return False, ["Bundle is already a SNAPSHOT — immutable (INV-016)"]

        # Check supporting facts
        for fid in bundle.fact_ids:
            try:
                fact = self.get_fact(fid)
            except SpecStoreError:
                issues.append(f"Fact {fid!r} referenced by bundle does not exist")
                continue
            if fact.status == FactStatus.CONTRADICTED:
                issues.append(
                    f"Bundle contains contradicted fact {fid!r} — resolve before validating"
                )

        # Check SpecItem traceability
        for item in bundle.spec_items:
            if not item.supporting_fact_ids:
                issues.append(
                    f"SpecItem feature={item.feature!r} spec_type={item.spec_type!r} "
                    f"has no supporting facts"
                )
                continue
            valid_support = False
            for sfid in item.supporting_fact_ids:
                try:
                    sf = self.get_fact(sfid)
                    if sf.status != FactStatus.CONTRADICTED:
                        valid_support = True
                        break
                except SpecStoreError:
                    pass
            if not valid_support:
                issues.append(
                    f"SpecItem feature={item.feature!r} spec_type={item.spec_type!r} "
                    f"has no non-contradicted supporting facts"
                )

        return len(issues) == 0, issues

    def set_bundle_status(self, bundle_id: str, status: BundleStatus) -> SpecBundle:
        """
        Transition a bundle's status.

        Allowed transitions: DRAFT -> VALIDATED, VALIDATED -> DRAFT,
        VALIDATED -> SNAPSHOT (use snapshot_bundle for the latter).
        SNAPSHOT is terminal — no further transitions.
        """
        bundle = self.get_bundle(bundle_id)

        if bundle.status == BundleStatus.SNAPSHOT:
            raise SpecStoreError(
                f"Bundle {bundle_id!r} is SNAPSHOT — immutable, status cannot change (INV-016)"
            )

        if status == BundleStatus.SNAPSHOT:
            raise SpecStoreError(
                "Use snapshot_bundle() to transition to SNAPSHOT status"
            )

        if status == BundleStatus.VALIDATED:
            ok, issues = self.validate_bundle(bundle_id)
            if not ok:
                raise SpecStoreError(
                    f"Bundle {bundle_id!r} failed validation: " + "; ".join(issues)
                )
            bundle.validated_at = _now_iso()

        bundle.status = status
        _atomic_write(self._bundle_path(bundle_id), bundle.to_dict())

        index = self._load_index()
        if bundle_id in index["bundles"]:
            index["bundles"][bundle_id]["status"] = str(status)
        self._save_index(index)
        return bundle

    def snapshot_bundle(self, bundle_id: str) -> SpecBundle:
        """
        Freeze a VALIDATED bundle as an immutable snapshot (INV-016).

        - Computes and stores content_hash.
        - Writes an immutable snapshot file to snapshots/{bundle_id}-v{N}.json.
        - Snapshot files are never overwritten.
        """
        bundle = self.get_bundle(bundle_id)

        if bundle.status != BundleStatus.VALIDATED:
            raise SpecStoreError(
                f"Bundle {bundle_id!r} must be VALIDATED before snapshotting "
                f"(current: {bundle.status!r})"
            )

        # Resolve supporting facts for hash computation
        facts = [self.get_fact(fid) for fid in bundle.fact_ids]
        bundle.content_hash = bundle.compute_hash(facts)
        bundle.snapshot_at = _now_iso()
        bundle.status = BundleStatus.SNAPSHOT

        # Write immutable snapshot file (never overwrite)
        snap_path = self._snapshot_path(bundle_id, bundle.version)
        if snap_path.exists():
            raise SpecStoreError(
                f"Snapshot file {snap_path} already exists — refusing to overwrite (INV-016)"
            )
        _atomic_write(snap_path, bundle.to_dict())

        # Update mutable bundle record too
        _atomic_write(self._bundle_path(bundle_id), bundle.to_dict())

        index = self._load_index()
        if bundle_id in index["bundles"]:
            index["bundles"][bundle_id]["status"] = str(BundleStatus.SNAPSHOT)
            index["bundles"][bundle_id]["snapshot_count"] = (
                index["bundles"][bundle_id].get("snapshot_count", 0) + 1
            )
        self._save_index(index)
        return bundle

    def diff_snapshots(
        self, bundle_id_a: str, bundle_id_b: str
    ) -> dict[str, Any]:
        """
        Compare two snapshots (or bundles) and return a structured diff.

        Returns a dict with keys: added_facts, removed_facts, common_facts,
        added_spec_items, removed_spec_items.
        """
        a = self.get_bundle(bundle_id_a)
        b = self.get_bundle(bundle_id_b)

        a_facts = set(a.fact_ids)
        b_facts = set(b.fact_ids)

        a_items = {(s.feature, s.spec_type) for s in a.spec_items}
        b_items = {(s.feature, s.spec_type) for s in b.spec_items}

        return {
            "bundle_a": bundle_id_a,
            "bundle_b": bundle_id_b,
            "added_facts": sorted(b_facts - a_facts),
            "removed_facts": sorted(a_facts - b_facts),
            "common_facts": sorted(a_facts & b_facts),
            "added_spec_items": sorted(b_items - a_items),
            "removed_spec_items": sorted(a_items - b_items),
            "content_hash_a": a.content_hash,
            "content_hash_b": b.content_hash,
        }

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return aggregate statistics about the store."""
        index = self._load_index()
        facts = index["facts"]
        bundles = index["bundles"]

        status_counts: dict[str, int] = {}
        feature_counts: dict[str, int] = {}
        superseded_count = 0
        deleted_count = 0

        for meta in facts.values():
            s = meta["status"]
            status_counts[s] = status_counts.get(s, 0) + 1
            feat = meta["feature"]
            feature_counts[feat] = feature_counts.get(feat, 0) + 1
            if meta.get("superseded_by"):
                superseded_count += 1
            if meta.get("deleted_at"):
                deleted_count += 1

        bundle_status_counts: dict[str, int] = {}
        for bmeta in bundles.values():
            s = bmeta["status"]
            bundle_status_counts[s] = bundle_status_counts.get(s, 0) + 1

        return {
            "total_facts": len(facts),
            "active_facts": len(facts) - superseded_count - deleted_count,
            "superseded_facts": superseded_count,
            "deleted_facts": deleted_count,
            "fact_status_counts": status_counts,
            "facts_by_feature": feature_counts,
            "total_bundles": len(bundles),
            "bundle_status_counts": bundle_status_counts,
            "total_content_hashes": len(index["content_hashes"]),
        }

    # ------------------------------------------------------------------
    # Spec Items (convenience mutators on bundles)
    # ------------------------------------------------------------------

    def add_spec_item_to_bundle(self, bundle_id: str, item: SpecItem) -> SpecBundle:
        """Add a SpecItem to a DRAFT or VALIDATED bundle."""
        bundle = self.get_bundle(bundle_id)
        if bundle.status == BundleStatus.SNAPSHOT:
            raise SpecStoreError(
                f"Bundle {bundle_id!r} is SNAPSHOT — immutable (INV-016)"
            )
        bundle.spec_items.append(item)
        _atomic_write(self._bundle_path(bundle_id), bundle.to_dict())
        return bundle
