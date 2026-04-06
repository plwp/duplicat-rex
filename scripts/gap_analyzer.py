"""
GapAnalyzer — converts comparison results into a prioritised, tracked gap list.

Design:
- Converts ComparisonResult failures to Gap objects with severity, category, and provenance.
- Maps each gap to source spec items and facts via SpecStore.
- Groups related gaps by feature.
- Compares with a previous GapReport to classify gaps as new, recurring, or resolved.
- Circuit breaker: flags gaps that have appeared in 3+ consecutive iterations.
- Creates GitHub issues via `gh` CLI for actionable gaps.
- Persists gap history as JSON in history_dir for cross-iteration tracking.

Invariants:
    INV-GAP-001: Every Gap.id is a deterministic slug derived from feature+test_name.
    INV-GAP-002: circuit_breaker_triggered contains only gaps with iteration_count >= 3.
    INV-GAP-003: resolved_gaps lists IDs from the previous report no longer present.
    INV-GAP-004: GapReport.by_severity counts match len(gaps) total.
    INV-GAP-005: Issues are never created for circuit-breaker gaps (already filed).
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

from scripts.compare import ComparisonResult
from scripts.spec_store import SpecStore

# ---------------------------------------------------------------------------
# Severity thresholds
# ---------------------------------------------------------------------------

# Feature score -> severity mapping
# P1: feature score < 50% (core flow broken)
# P2: feature score 50-80% (feature divergent)
# P3: feature score > 80% (edge case)
_P1_THRESHOLD = 50.0
_P2_THRESHOLD = 80.0

# Circuit breaker fires at this many consecutive appearances
_CIRCUIT_BREAKER_LIMIT = 3


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Gap:
    """A single conformance gap derived from a comparison failure."""

    id: str  # Deterministic slug: "{feature}::{test_name}"
    feature: str
    severity: str  # "P1", "P2", "P3"
    category: str  # "missing", "divergent", "broken"
    description: str
    test_name: str | None
    diff: str | None
    related_spec_ids: list[str]  # SpecItem (feature, spec_type) combos as "feature::spec_type"
    related_fact_ids: list[str]  # Fact IDs from SpecStore
    iteration_count: int  # How many times this gap has appeared

    def to_dict(self) -> dict:  # type: ignore[type-arg]
        return {
            "id": self.id,
            "feature": self.feature,
            "severity": self.severity,
            "category": self.category,
            "description": self.description,
            "test_name": self.test_name,
            "diff": self.diff,
            "related_spec_ids": self.related_spec_ids,
            "related_fact_ids": self.related_fact_ids,
            "iteration_count": self.iteration_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Gap:  # type: ignore[type-arg]
        return cls(
            id=data["id"],
            feature=data["feature"],
            severity=data["severity"],
            category=data["category"],
            description=data["description"],
            test_name=data.get("test_name"),
            diff=data.get("diff"),
            related_spec_ids=data.get("related_spec_ids", []),
            related_fact_ids=data.get("related_fact_ids", []),
            iteration_count=data.get("iteration_count", 1),
        )


@dataclass
class GapReport:
    """Full gap analysis report for one iteration."""

    gaps: list[Gap]
    by_severity: dict[str, int]  # {"P1": N, "P2": N, "P3": N}
    by_feature: dict[str, list[Gap]]  # feature -> list of gaps
    circuit_breaker_triggered: list[Gap]  # gaps with iteration_count >= 3 (INV-GAP-002)
    new_gaps: list[Gap]
    recurring_gaps: list[Gap]
    resolved_gaps: list[str]  # gap IDs from previous iteration no longer present (INV-GAP-003)

    def to_dict(self) -> dict:  # type: ignore[type-arg]
        return {
            "gaps": [g.to_dict() for g in self.gaps],
            "by_severity": self.by_severity,
            # by_feature: store as list per key (Gap objects)
            "by_feature": {k: [g.to_dict() for g in v] for k, v in self.by_feature.items()},
            "circuit_breaker_triggered": [g.to_dict() for g in self.circuit_breaker_triggered],
            "new_gaps": [g.to_dict() for g in self.new_gaps],
            "recurring_gaps": [g.to_dict() for g in self.recurring_gaps],
            "resolved_gaps": self.resolved_gaps,
        }

    @classmethod
    def from_dict(cls, data: dict) -> GapReport:  # type: ignore[type-arg]
        gaps = [Gap.from_dict(g) for g in data.get("gaps", [])]
        by_feature = {
            k: [Gap.from_dict(g) for g in v]
            for k, v in data.get("by_feature", {}).items()
        }
        return cls(
            gaps=gaps,
            by_severity=data.get("by_severity", {}),
            by_feature=by_feature,
            circuit_breaker_triggered=[
                Gap.from_dict(g) for g in data.get("circuit_breaker_triggered", [])
            ],
            new_gaps=[Gap.from_dict(g) for g in data.get("new_gaps", [])],
            recurring_gaps=[Gap.from_dict(g) for g in data.get("recurring_gaps", [])],
            resolved_gaps=data.get("resolved_gaps", []),
        )


# ---------------------------------------------------------------------------
# GapAnalyzer
# ---------------------------------------------------------------------------


class GapAnalyzer:
    """
    Converts ComparisonResult failures into prioritised, tracked Gap objects.

    Usage:
        analyzer = GapAnalyzer(spec_store, history_dir)
        report = analyzer.analyze(comparison_result, scope)
        issue_urls = analyzer.create_issues(report, "owner/repo")
        analyzer.save_history(report, iteration=1)
    """

    def __init__(self, spec_store: SpecStore, history_dir: Path) -> None:
        self.spec_store = spec_store
        self.history_dir = Path(history_dir)
        self.history_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(
        self,
        comparison: ComparisonResult,
        scope: object,
        *,
        previous_report: GapReport | None = None,
    ) -> GapReport:
        """
        Convert comparison failures to a prioritised GapReport.

        Steps:
        1. Convert each failed test to a Gap with severity + category.
        2. Map each gap to source spec items and facts in the SpecStore.
        3. Prioritise by severity (P1=core flow broken, P2=feature divergent, P3=edge case).
        4. Group related gaps by feature.
        5. Compare with previous_report to detect recurring vs new vs resolved.
        6. Circuit breaker: flag gaps that have appeared 3+ times (INV-GAP-002).

        REQUIRES: comparison is a valid ComparisonResult.
        ENSURES: report.by_severity counts sum to len(report.gaps) (INV-GAP-004).
        ENSURES: circuit_breaker_triggered subset of gaps with iteration_count >= 3 (INV-GAP-002).
        """
        # Build lookup for feature scores (for severity assignment)
        feature_scores = comparison.feature_scores

        # Build iteration count map from previous report
        prev_iteration_counts: dict[str, int] = {}
        prev_gap_ids: set[str] = set()
        if previous_report is not None:
            for g in previous_report.gaps:
                prev_iteration_counts[g.id] = g.iteration_count
                prev_gap_ids.add(g.id)

        # Convert failures to Gaps
        gaps: list[Gap] = []
        for detail in comparison.details:
            gap_id = _make_gap_id(detail.feature, detail.test_name)
            severity = _assign_severity(detail.feature, feature_scores)
            category = _assign_category(detail.target_result, detail.clone_result)

            # Lookup provenance from spec store
            spec_ids, fact_ids = self._lookup_provenance(detail.feature)

            # Determine iteration count
            prev_count = prev_iteration_counts.get(gap_id, 0)
            iteration_count = prev_count + 1

            gap = Gap(
                id=gap_id,
                feature=detail.feature,
                severity=severity,
                category=category,
                description=_build_description(detail),
                test_name=detail.test_name,
                diff=detail.diff,
                related_spec_ids=spec_ids,
                related_fact_ids=fact_ids,
                iteration_count=iteration_count,
            )
            gaps.append(gap)

        # Sort by priority: P1 first, then P2, then P3; within same severity by feature name
        gaps.sort(key=lambda g: (_severity_rank(g.severity), g.feature, g.test_name or ""))

        # Group by feature
        by_feature: dict[str, list[Gap]] = {}
        for gap in gaps:
            by_feature.setdefault(gap.feature, []).append(gap)

        # Count by severity (INV-GAP-004)
        by_severity: dict[str, int] = {"P1": 0, "P2": 0, "P3": 0}
        for gap in gaps:
            by_severity[gap.severity] = by_severity.get(gap.severity, 0) + 1

        # Classify new vs recurring
        current_gap_ids = {g.id for g in gaps}
        new_gaps = [g for g in gaps if g.id not in prev_gap_ids]
        recurring_gaps = [g for g in gaps if g.id in prev_gap_ids]

        # Resolved gaps: in previous report but not in current (INV-GAP-003)
        resolved_gaps = sorted(prev_gap_ids - current_gap_ids)

        # Circuit breaker: gaps at or beyond limit (INV-GAP-002)
        circuit_breaker_triggered = [
            g for g in gaps if g.iteration_count >= _CIRCUIT_BREAKER_LIMIT
        ]

        return GapReport(
            gaps=gaps,
            by_severity=by_severity,
            by_feature=by_feature,
            circuit_breaker_triggered=circuit_breaker_triggered,
            new_gaps=new_gaps,
            recurring_gaps=recurring_gaps,
            resolved_gaps=resolved_gaps,
        )

    def create_issues(
        self,
        report: GapReport,
        repo: str,
        *,
        max_issues: int = 10,
    ) -> list[str]:
        """
        Create GitHub issues for top gaps (circuit-breaker gaps are skipped).

        Issues are created for the highest-priority gaps only (P1 first, then P2, P3).
        Circuit-breaker gaps are excluded — they already have issues from prior iterations.

        REQUIRES: repo is in "owner/repo" format.
        ENSURES: returns list of issue URLs (one per created issue).
        ENSURES: no issues created for gaps in circuit_breaker_triggered (INV-GAP-005).
        """
        # Exclude circuit-breaker gaps (INV-GAP-005)
        cb_ids = {g.id for g in report.circuit_breaker_triggered}
        eligible = [g for g in report.gaps if g.id not in cb_ids]

        # Take top max_issues by priority (already sorted P1 -> P3)
        to_file = eligible[:max_issues]

        issue_urls: list[str] = []
        for gap in to_file:
            url = self._create_github_issue(gap, repo)
            if url:
                issue_urls.append(url)

        return issue_urls

    def save_history(self, report: GapReport, iteration: int) -> Path:
        """
        Persist a GapReport to history_dir/gap_report_iter_{N:04d}.json.

        Returns the path of the written file.
        """
        path = self.history_dir / f"gap_report_iter_{iteration:04d}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
        return path

    def load_history(self, iteration: int) -> GapReport | None:
        """
        Load a GapReport from history_dir/gap_report_iter_{N:04d}.json.

        Returns None if the file does not exist.
        """
        path = self.history_dir / f"gap_report_iter_{iteration:04d}.json"
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return GapReport.from_dict(data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _lookup_provenance(self, feature: str) -> tuple[list[str], list[str]]:
        """
        Look up spec items and facts for a given feature from the SpecStore.

        Returns (spec_ids, fact_ids). Both are best-effort — empty lists if not found.
        """
        spec_ids: list[str] = []
        fact_ids: list[str] = []

        try:
            # Query facts for this feature
            facts = self.spec_store.query_facts(feature=feature)
            fact_ids = [f.id for f in facts]

            # Look up any bundles for spec item provenance
            # We'll search the index for bundle spec_items matching this feature
            index = self.spec_store._load_index()
            for bundle_id in index.get("bundles", {}):
                try:
                    bundle = self.spec_store.get_bundle(bundle_id)
                    for item in bundle.spec_items:
                        if item.feature == feature:
                            spec_id = f"{item.feature}::{item.spec_type}"
                            if spec_id not in spec_ids:
                                spec_ids.append(spec_id)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass

        return spec_ids, fact_ids

    def _create_github_issue(self, gap: Gap, repo: str) -> str | None:
        """
        Create a GitHub issue for a gap via `gh issue create`.

        Returns the issue URL on success, None on failure.
        """
        title = f"[{gap.severity}] Conformance gap: {gap.feature} — {gap.test_name or gap.id}"
        body = _build_issue_body(gap)
        labels = _gap_labels(gap)

        cmd = [
            "gh", "issue", "create",
            "--repo", repo,
            "--title", title,
            "--body", body,
        ]
        if labels:
            cmd += ["--label", ",".join(labels)]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                # gh prints the issue URL to stdout
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        return None


# ---------------------------------------------------------------------------
# Internal pure helpers
# ---------------------------------------------------------------------------


def _make_gap_id(feature: str, test_name: str) -> str:
    """
    Build a deterministic gap ID from feature and test name (INV-GAP-001).

    e.g. "boards::test_boards_create"
    """
    safe_feature = re.sub(r"[^a-z0-9-]", "-", feature.lower()).strip("-")
    safe_test = re.sub(r"[^a-z0-9_]", "_", test_name.lower()).strip("_")
    return f"{safe_feature}::{safe_test}"


def _assign_severity(feature: str, feature_scores: dict[str, float]) -> str:
    """
    Assign P1/P2/P3 severity based on the feature's parity score.

    P1: score < 50% (core flow broken)
    P2: score 50-80% (feature divergent)
    P3: score > 80% (edge case)
    """
    score = feature_scores.get(feature, 0.0)
    if score < _P1_THRESHOLD:
        return "P1"
    elif score < _P2_THRESHOLD:
        return "P2"
    else:
        return "P3"


def _assign_category(target_result: str, clone_result: str) -> str:
    """
    Classify gap category from target vs clone results.

    - "missing": clone errored (feature not implemented)
    - "broken": clone failed (feature exists but produces wrong result)
    - "divergent": any other divergence
    """
    if clone_result == "error":
        return "missing"
    elif clone_result == "fail":
        return "broken"
    return "divergent"


def _severity_rank(severity: str) -> int:
    """Return sort key for severity (lower = higher priority)."""
    return {"P1": 1, "P2": 2, "P3": 3}.get(severity, 9)


def _build_description(detail: object) -> str:
    """Build a concise human-readable description for a gap."""
    return (
        f"Conformance gap in feature '{detail.feature}': "  # type: ignore[attr-defined]
        f"test '{detail.test_name}' passes on target ({detail.target_result}) "  # type: ignore[attr-defined]
        f"but fails on clone ({detail.clone_result})."  # type: ignore[attr-defined]
    )


def _build_issue_body(gap: Gap) -> str:
    """Render a GitHub issue body with sufficient context for /implement."""
    sections: list[str] = []

    sections.append(textwrap.dedent(f"""\
        ## Conformance Gap: `{gap.feature}` — `{gap.test_name or gap.id}`

        **Severity:** {gap.severity}
        **Category:** {gap.category}
        **Iteration count:** {gap.iteration_count}

        ### Description

        {gap.description}
    """))

    if gap.diff:
        sections.append(textwrap.dedent(f"""\
            ### Diff

            ```
            {gap.diff}
            ```
        """))

    if gap.related_spec_ids:
        spec_list = "\n".join(f"- `{s}`" for s in gap.related_spec_ids)
        sections.append(f"### Related Specs\n\n{spec_list}\n")

    if gap.related_fact_ids:
        fact_list = "\n".join(f"- `{f}`" for f in gap.related_fact_ids[:10])
        sections.append(f"### Related Facts\n\n{fact_list}\n")

    sections.append(textwrap.dedent("""\
        ### Implementation Notes

        Use `/implement` to fix this gap. Ensure the clone reproduces the target's
        behavior for this test, excluding non-deterministic fields (id, uuid, timestamps, tokens).
    """))

    return "\n".join(sections)


def _gap_labels(gap: Gap) -> list[str]:
    """Return GitHub label slugs for a gap."""
    labels = ["conformance-gap", gap.severity.lower()]
    if gap.category:
        labels.append(gap.category)
    return labels
