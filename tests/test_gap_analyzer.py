"""
Tests for scripts/gap_analyzer.py

Covers:
- Gap and GapReport dataclasses: to_dict / from_dict round-trips
- _make_gap_id: deterministic slug from feature + test_name
- _assign_severity: P1/P2/P3 based on feature parity score
- _assign_category: missing/broken/divergent from result pair
- GapAnalyzer.analyze:
    - converts ComparisonResult failures to Gaps
    - prioritises P1 before P2 before P3
    - groups by feature
    - maps provenance from SpecStore (with empty store)
    - detects new vs recurring gaps
    - resolves gaps absent from current iteration
    - circuit breaker triggers at 3+ iterations (INV-GAP-002)
    - by_severity counts match total gaps (INV-GAP-004)
- GapAnalyzer.create_issues:
    - calls `gh issue create` for top gaps
    - skips circuit-breaker gaps (INV-GAP-005)
    - respects max_issues limit
    - returns empty list on gh failure
- GapAnalyzer.save_history / load_history:
    - round-trip through JSON
    - load_history returns None for missing iteration
- _build_issue_body: renders expected sections
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.compare import ComparisonResult, TestDiff
from scripts.gap_analyzer import (
    Gap,
    GapAnalyzer,
    GapReport,
    _assign_category,
    _assign_severity,
    _build_issue_body,
    _make_gap_id,
)
from scripts.spec_store import SpecStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_store(tmp_path: Path) -> SpecStore:
    """An empty SpecStore backed by a temp directory."""
    return SpecStore(tmp_path / "store")


@pytest.fixture()
def history_dir(tmp_path: Path) -> Path:
    """Temporary history directory."""
    d = tmp_path / "gap_history"
    d.mkdir()
    return d


@pytest.fixture()
def analyzer(tmp_store: SpecStore, history_dir: Path) -> GapAnalyzer:
    return GapAnalyzer(tmp_store, history_dir)


def _make_comparison(
    *,
    failed: list[str] | None = None,
    details: list[TestDiff] | None = None,
    feature_scores: dict[str, float] | None = None,
    parity_score: float = 100.0,
) -> ComparisonResult:
    """Build a minimal ComparisonResult for testing."""
    return ComparisonResult(
        target_url="https://target.example.com",
        clone_url="https://clone.example.com",
        parity_score=parity_score,
        feature_scores=feature_scores or {},
        passed=[],
        failed=failed or [],
        errors=[],
        details=details or [],
    )


def _make_detail(
    test_name: str = "test_boards_create",
    feature: str = "boards",
    target_result: str = "pass",
    clone_result: str = "fail",
    diff: str = "target passed, clone failed",
) -> TestDiff:
    return TestDiff(
        test_name=test_name,
        feature=feature,
        target_result=target_result,
        clone_result=clone_result,
        diff=diff,
    )


def _make_gap(
    *,
    gap_id: str = "boards::test_boards_create",
    feature: str = "boards",
    severity: str = "P1",
    category: str = "broken",
    iteration_count: int = 1,
) -> Gap:
    return Gap(
        id=gap_id,
        feature=feature,
        severity=severity,
        category=category,
        description="A test description",
        test_name="test_boards_create",
        diff="target PASS, clone FAIL",
        related_spec_ids=[],
        related_fact_ids=[],
        iteration_count=iteration_count,
    )


# ---------------------------------------------------------------------------
# Unit tests: pure helpers
# ---------------------------------------------------------------------------


class TestMakeGapId:
    def test_basic(self) -> None:
        gid = _make_gap_id("boards", "test_boards_create")
        assert gid == "boards::test_boards_create"

    def test_hyphens_preserved_in_feature(self) -> None:
        gid = _make_gap_id("drag-drop", "test_drag_drop_0")
        assert gid == "drag-drop::test_drag_drop_0"

    def test_uppercase_normalised(self) -> None:
        gid = _make_gap_id("Boards", "Test_Boards_Create")
        assert "::" in gid
        assert gid == gid.lower()  # should be lowercased

    def test_deterministic(self) -> None:
        a = _make_gap_id("lists", "test_lists_order")
        b = _make_gap_id("lists", "test_lists_order")
        assert a == b


class TestAssignSeverity:
    def test_p1_low_score(self) -> None:
        assert _assign_severity("boards", {"boards": 10.0}) == "P1"

    def test_p1_at_threshold(self) -> None:
        # score < 50 → P1
        assert _assign_severity("boards", {"boards": 49.9}) == "P1"

    def test_p2_mid_score(self) -> None:
        assert _assign_severity("boards", {"boards": 65.0}) == "P2"

    def test_p2_at_boundary(self) -> None:
        # score == 50.0 → P2 (not P1)
        assert _assign_severity("boards", {"boards": 50.0}) == "P2"

    def test_p3_high_score(self) -> None:
        assert _assign_severity("boards", {"boards": 90.0}) == "P3"

    def test_missing_feature_defaults_zero(self) -> None:
        # Missing feature → score defaults to 0.0 → P1
        assert _assign_severity("unknown", {}) == "P1"


class TestAssignCategory:
    def test_missing_when_clone_errors(self) -> None:
        assert _assign_category("pass", "error") == "missing"

    def test_broken_when_clone_fails(self) -> None:
        assert _assign_category("pass", "fail") == "broken"

    def test_divergent_fallback(self) -> None:
        assert _assign_category("pass", "unknown") == "divergent"


# ---------------------------------------------------------------------------
# Data model: round-trips
# ---------------------------------------------------------------------------


class TestGapRoundTrip:
    def test_to_from_dict(self) -> None:
        gap = _make_gap(iteration_count=2)
        restored = Gap.from_dict(gap.to_dict())
        assert restored.id == gap.id
        assert restored.feature == gap.feature
        assert restored.severity == gap.severity
        assert restored.category == gap.category
        assert restored.iteration_count == gap.iteration_count

    def test_optional_fields_none(self) -> None:
        gap = Gap(
            id="x::y",
            feature="x",
            severity="P2",
            category="broken",
            description="desc",
            test_name=None,
            diff=None,
            related_spec_ids=[],
            related_fact_ids=[],
            iteration_count=1,
        )
        restored = Gap.from_dict(gap.to_dict())
        assert restored.test_name is None
        assert restored.diff is None


class TestGapReportRoundTrip:
    def test_to_from_dict(self) -> None:
        gap = _make_gap()
        report = GapReport(
            gaps=[gap],
            by_severity={"P1": 1, "P2": 0, "P3": 0},
            by_feature={"boards": [gap]},
            circuit_breaker_triggered=[],
            new_gaps=[gap],
            recurring_gaps=[],
            resolved_gaps=[],
        )
        restored = GapReport.from_dict(report.to_dict())
        assert len(restored.gaps) == 1
        assert restored.gaps[0].id == gap.id
        assert restored.by_severity["P1"] == 1
        assert "boards" in restored.by_feature

    def test_resolved_gaps_preserved(self) -> None:
        report = GapReport(
            gaps=[],
            by_severity={"P1": 0, "P2": 0, "P3": 0},
            by_feature={},
            circuit_breaker_triggered=[],
            new_gaps=[],
            recurring_gaps=[],
            resolved_gaps=["boards::test_old"],
        )
        restored = GapReport.from_dict(report.to_dict())
        assert restored.resolved_gaps == ["boards::test_old"]


# ---------------------------------------------------------------------------
# GapAnalyzer.analyze
# ---------------------------------------------------------------------------


class TestAnalyzeBasic:
    def test_empty_comparison_produces_empty_report(
        self, analyzer: GapAnalyzer
    ) -> None:
        comparison = _make_comparison()
        report = analyzer.analyze(comparison, scope=None)
        assert report.gaps == []
        assert report.by_severity == {"P1": 0, "P2": 0, "P3": 0}
        assert report.circuit_breaker_triggered == []
        assert report.new_gaps == []
        assert report.resolved_gaps == []

    def test_single_failure_becomes_gap(self, analyzer: GapAnalyzer) -> None:
        detail = _make_detail()
        comparison = _make_comparison(
            failed=["test_boards_create"],
            details=[detail],
            feature_scores={"boards": 30.0},
        )
        report = analyzer.analyze(comparison, scope=None)
        assert len(report.gaps) == 1
        gap = report.gaps[0]
        assert gap.feature == "boards"
        assert gap.severity == "P1"
        assert gap.category == "broken"
        assert gap.test_name == "test_boards_create"

    def test_gap_id_is_deterministic(self, analyzer: GapAnalyzer) -> None:
        detail = _make_detail(feature="lists", test_name="test_lists_order")
        comparison = _make_comparison(
            details=[detail],
            feature_scores={"lists": 60.0},
        )
        report = analyzer.analyze(comparison, scope=None)
        assert report.gaps[0].id == "lists::test_lists_order"

    def test_by_severity_counts_match_total(self, analyzer: GapAnalyzer) -> None:
        """INV-GAP-004: by_severity counts must sum to len(gaps)."""
        details = [
            _make_detail("test_a", "boards", diff="d"),
            _make_detail("test_b", "lists", diff="d"),
            _make_detail("test_c", "cards", diff="d"),
        ]
        comparison = _make_comparison(
            details=details,
            feature_scores={"boards": 10.0, "lists": 60.0, "cards": 85.0},
        )
        report = analyzer.analyze(comparison, scope=None)
        total_from_severity = sum(report.by_severity.values())
        assert total_from_severity == len(report.gaps)

    def test_gaps_sorted_p1_first(self, analyzer: GapAnalyzer) -> None:
        details = [
            _make_detail("test_p3", "feature-c", diff="d"),
            _make_detail("test_p1", "feature-a", diff="d"),
            _make_detail("test_p2", "feature-b", diff="d"),
        ]
        comparison = _make_comparison(
            details=details,
            feature_scores={
                "feature-a": 10.0,  # P1
                "feature-b": 65.0,  # P2
                "feature-c": 90.0,  # P3
            },
        )
        report = analyzer.analyze(comparison, scope=None)
        severities = [g.severity for g in report.gaps]
        assert severities == ["P1", "P2", "P3"]

    def test_by_feature_groups_gaps(self, analyzer: GapAnalyzer) -> None:
        details = [
            _make_detail("test_a", "boards", diff="d"),
            _make_detail("test_b", "boards", diff="d"),
            _make_detail("test_c", "lists", diff="d"),
        ]
        comparison = _make_comparison(
            details=details, feature_scores={"boards": 20.0, "lists": 20.0}
        )
        report = analyzer.analyze(comparison, scope=None)
        assert len(report.by_feature["boards"]) == 2
        assert len(report.by_feature["lists"]) == 1

    def test_missing_category_when_clone_errors(self, analyzer: GapAnalyzer) -> None:
        detail = _make_detail(clone_result="error")
        comparison = _make_comparison(details=[detail], feature_scores={"boards": 30.0})
        report = analyzer.analyze(comparison, scope=None)
        assert report.gaps[0].category == "missing"


class TestAnalyzeHistory:
    def test_first_iteration_all_new(self, analyzer: GapAnalyzer) -> None:
        detail = _make_detail()
        comparison = _make_comparison(details=[detail], feature_scores={"boards": 30.0})
        report = analyzer.analyze(comparison, scope=None)
        assert len(report.new_gaps) == 1
        assert report.recurring_gaps == []

    def test_second_iteration_gap_is_recurring(self, analyzer: GapAnalyzer) -> None:
        detail = _make_detail()
        comparison = _make_comparison(details=[detail], feature_scores={"boards": 30.0})
        # First iteration
        report1 = analyzer.analyze(comparison, scope=None)
        # Second iteration
        report2 = analyzer.analyze(comparison, scope=None, previous_report=report1)
        assert len(report2.recurring_gaps) == 1
        assert report2.new_gaps == []
        assert report2.recurring_gaps[0].iteration_count == 2

    def test_resolved_gap_detected(self, analyzer: GapAnalyzer) -> None:
        detail = _make_detail()
        comparison_with_gap = _make_comparison(details=[detail], feature_scores={"boards": 30.0})
        comparison_no_gap = _make_comparison()

        report1 = analyzer.analyze(comparison_with_gap, scope=None)
        report2 = analyzer.analyze(comparison_no_gap, scope=None, previous_report=report1)

        assert "boards::test_boards_create" in report2.resolved_gaps

    def test_iteration_count_increments(self, analyzer: GapAnalyzer) -> None:
        detail = _make_detail()
        comparison = _make_comparison(details=[detail], feature_scores={"boards": 30.0})
        r1 = analyzer.analyze(comparison, scope=None)
        r2 = analyzer.analyze(comparison, scope=None, previous_report=r1)
        r3 = analyzer.analyze(comparison, scope=None, previous_report=r2)
        assert r3.gaps[0].iteration_count == 3


class TestCircuitBreaker:
    def test_circuit_breaker_triggers_at_3(self, analyzer: GapAnalyzer) -> None:
        """INV-GAP-002: circuit_breaker_triggered contains gaps with iteration_count >= 3."""
        detail = _make_detail()
        comparison = _make_comparison(details=[detail], feature_scores={"boards": 30.0})
        r1 = analyzer.analyze(comparison, scope=None)
        r2 = analyzer.analyze(comparison, scope=None, previous_report=r1)
        r3 = analyzer.analyze(comparison, scope=None, previous_report=r2)

        # At iteration 3 (count==3), circuit breaker fires
        assert len(r3.circuit_breaker_triggered) == 1
        assert r3.circuit_breaker_triggered[0].iteration_count == 3

    def test_circuit_breaker_not_at_2(self, analyzer: GapAnalyzer) -> None:
        detail = _make_detail()
        comparison = _make_comparison(details=[detail], feature_scores={"boards": 30.0})
        r1 = analyzer.analyze(comparison, scope=None)
        r2 = analyzer.analyze(comparison, scope=None, previous_report=r1)
        assert r2.circuit_breaker_triggered == []

    def test_circuit_breaker_at_4(self, analyzer: GapAnalyzer) -> None:
        """Gaps with iteration_count > 3 also trigger the circuit breaker."""
        detail = _make_detail()
        comparison = _make_comparison(details=[detail], feature_scores={"boards": 30.0})
        r1 = analyzer.analyze(comparison, scope=None)
        r2 = analyzer.analyze(comparison, scope=None, previous_report=r1)
        r3 = analyzer.analyze(comparison, scope=None, previous_report=r2)
        r4 = analyzer.analyze(comparison, scope=None, previous_report=r3)
        assert len(r4.circuit_breaker_triggered) == 1
        assert r4.circuit_breaker_triggered[0].iteration_count == 4


# ---------------------------------------------------------------------------
# GapAnalyzer.create_issues
# ---------------------------------------------------------------------------


class TestCreateIssues:
    def _mock_gh_success(self, url: str = "https://github.com/owner/repo/issues/42"):
        """Return a mock CompletedProcess that looks like a successful gh call."""
        mock = MagicMock(spec=subprocess.CompletedProcess)
        mock.returncode = 0
        mock.stdout = url
        return mock

    def _mock_gh_failure(self):
        mock = MagicMock(spec=subprocess.CompletedProcess)
        mock.returncode = 1
        mock.stdout = ""
        return mock

    def test_creates_issue_for_top_gap(
        self, analyzer: GapAnalyzer
    ) -> None:
        gap = _make_gap()
        report = GapReport(
            gaps=[gap],
            by_severity={"P1": 1, "P2": 0, "P3": 0},
            by_feature={"boards": [gap]},
            circuit_breaker_triggered=[],
            new_gaps=[gap],
            recurring_gaps=[],
            resolved_gaps=[],
        )
        with patch("subprocess.run", return_value=self._mock_gh_success()) as mock_run:
            urls = analyzer.create_issues(report, "owner/repo")

        assert len(urls) == 1
        assert urls[0] == "https://github.com/owner/repo/issues/42"
        mock_run.assert_called_once()

        # Verify gh was invoked with correct repo
        call_args = mock_run.call_args[0][0]
        assert "gh" in call_args
        assert "owner/repo" in call_args

    def test_skips_circuit_breaker_gaps(self, analyzer: GapAnalyzer) -> None:
        """INV-GAP-005: no issues for circuit-breaker gaps."""
        gap = _make_gap(iteration_count=3)
        report = GapReport(
            gaps=[gap],
            by_severity={"P1": 1, "P2": 0, "P3": 0},
            by_feature={"boards": [gap]},
            circuit_breaker_triggered=[gap],  # circuit breaker fired
            new_gaps=[],
            recurring_gaps=[gap],
            resolved_gaps=[],
        )
        with patch("subprocess.run") as mock_run:
            urls = analyzer.create_issues(report, "owner/repo")

        assert urls == []
        mock_run.assert_not_called()

    def test_respects_max_issues(self, analyzer: GapAnalyzer) -> None:
        gaps = [_make_gap(gap_id=f"boards::test_{i}", feature="boards") for i in range(5)]
        report = GapReport(
            gaps=gaps,
            by_severity={"P1": 5, "P2": 0, "P3": 0},
            by_feature={"boards": gaps},
            circuit_breaker_triggered=[],
            new_gaps=gaps,
            recurring_gaps=[],
            resolved_gaps=[],
        )
        with patch("subprocess.run", return_value=self._mock_gh_success()) as mock_run:
            urls = analyzer.create_issues(report, "owner/repo", max_issues=2)

        assert len(urls) == 2
        assert mock_run.call_count == 2

    def test_returns_empty_list_on_gh_failure(self, analyzer: GapAnalyzer) -> None:
        gap = _make_gap()
        report = GapReport(
            gaps=[gap],
            by_severity={"P1": 1, "P2": 0, "P3": 0},
            by_feature={"boards": [gap]},
            circuit_breaker_triggered=[],
            new_gaps=[gap],
            recurring_gaps=[],
            resolved_gaps=[],
        )
        with patch("subprocess.run", return_value=self._mock_gh_failure()):
            urls = analyzer.create_issues(report, "owner/repo")
        assert urls == []

    def test_handles_subprocess_timeout(self, analyzer: GapAnalyzer) -> None:
        gap = _make_gap()
        report = GapReport(
            gaps=[gap],
            by_severity={"P1": 1, "P2": 0, "P3": 0},
            by_feature={"boards": [gap]},
            circuit_breaker_triggered=[],
            new_gaps=[gap],
            recurring_gaps=[],
            resolved_gaps=[],
        )
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 30)):
            urls = analyzer.create_issues(report, "owner/repo")
        assert urls == []

    def test_handles_gh_not_found(self, analyzer: GapAnalyzer) -> None:
        gap = _make_gap()
        report = GapReport(
            gaps=[gap],
            by_severity={"P1": 1, "P2": 0, "P3": 0},
            by_feature={"boards": [gap]},
            circuit_breaker_triggered=[],
            new_gaps=[gap],
            recurring_gaps=[],
            resolved_gaps=[],
        )
        with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
            urls = analyzer.create_issues(report, "owner/repo")
        assert urls == []

    def test_issue_title_contains_severity_and_feature(
        self, analyzer: GapAnalyzer
    ) -> None:
        gap = _make_gap(severity="P2")
        report = GapReport(
            gaps=[gap],
            by_severity={"P1": 0, "P2": 1, "P3": 0},
            by_feature={"boards": [gap]},
            circuit_breaker_triggered=[],
            new_gaps=[gap],
            recurring_gaps=[],
            resolved_gaps=[],
        )
        with patch("subprocess.run", return_value=self._mock_gh_success()) as mock_run:
            analyzer.create_issues(report, "owner/repo")

        call_args = mock_run.call_args[0][0]
        title_idx = call_args.index("--title") + 1
        title = call_args[title_idx]
        assert "[P2]" in title
        assert "boards" in title

    def test_empty_report_creates_no_issues(self, analyzer: GapAnalyzer) -> None:
        report = GapReport(
            gaps=[],
            by_severity={"P1": 0, "P2": 0, "P3": 0},
            by_feature={},
            circuit_breaker_triggered=[],
            new_gaps=[],
            recurring_gaps=[],
            resolved_gaps=[],
        )
        with patch("subprocess.run") as mock_run:
            urls = analyzer.create_issues(report, "owner/repo")
        assert urls == []
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# GapAnalyzer.save_history / load_history
# ---------------------------------------------------------------------------


class TestHistory:
    def test_save_creates_file(self, analyzer: GapAnalyzer, history_dir: Path) -> None:
        report = GapReport(
            gaps=[],
            by_severity={"P1": 0, "P2": 0, "P3": 0},
            by_feature={},
            circuit_breaker_triggered=[],
            new_gaps=[],
            recurring_gaps=[],
            resolved_gaps=[],
        )
        path = analyzer.save_history(report, iteration=1)
        assert path.exists()
        assert path.name == "gap_report_iter_0001.json"

    def test_load_returns_none_for_missing(self, analyzer: GapAnalyzer) -> None:
        result = analyzer.load_history(iteration=99)
        assert result is None

    def test_save_load_round_trip(self, analyzer: GapAnalyzer) -> None:
        gap = _make_gap(iteration_count=2)
        report = GapReport(
            gaps=[gap],
            by_severity={"P1": 1, "P2": 0, "P3": 0},
            by_feature={"boards": [gap]},
            circuit_breaker_triggered=[],
            new_gaps=[gap],
            recurring_gaps=[],
            resolved_gaps=["old::gap"],
        )
        analyzer.save_history(report, iteration=5)
        restored = analyzer.load_history(iteration=5)

        assert restored is not None
        assert len(restored.gaps) == 1
        assert restored.gaps[0].id == gap.id
        assert restored.gaps[0].iteration_count == 2
        assert restored.resolved_gaps == ["old::gap"]

    def test_iteration_zero_padded(self, analyzer: GapAnalyzer, history_dir: Path) -> None:
        report = GapReport(
            gaps=[],
            by_severity={"P1": 0, "P2": 0, "P3": 0},
            by_feature={},
            circuit_breaker_triggered=[],
            new_gaps=[],
            recurring_gaps=[],
            resolved_gaps=[],
        )
        path = analyzer.save_history(report, iteration=3)
        assert path.name == "gap_report_iter_0003.json"


# ---------------------------------------------------------------------------
# _build_issue_body
# ---------------------------------------------------------------------------


class TestBuildIssueBody:
    def test_contains_severity(self) -> None:
        gap = _make_gap(severity="P1")
        body = _build_issue_body(gap)
        assert "P1" in body

    def test_contains_feature(self) -> None:
        gap = _make_gap(feature="boards")
        body = _build_issue_body(gap)
        assert "boards" in body

    def test_contains_diff_when_present(self) -> None:
        gap = _make_gap()
        gap = Gap(
            id=gap.id,
            feature=gap.feature,
            severity=gap.severity,
            category=gap.category,
            description=gap.description,
            test_name=gap.test_name,
            diff="Clone returned 404",
            related_spec_ids=[],
            related_fact_ids=[],
            iteration_count=1,
        )
        body = _build_issue_body(gap)
        assert "Clone returned 404" in body

    def test_contains_implement_hint(self) -> None:
        gap = _make_gap()
        body = _build_issue_body(gap)
        assert "/implement" in body

    def test_spec_ids_listed(self) -> None:
        gap = Gap(
            id="boards::t",
            feature="boards",
            severity="P1",
            category="broken",
            description="desc",
            test_name="t",
            diff=None,
            related_spec_ids=["boards::api_contract"],
            related_fact_ids=[],
            iteration_count=1,
        )
        body = _build_issue_body(gap)
        assert "boards::api_contract" in body

    def test_no_diff_section_when_diff_none(self) -> None:
        gap = Gap(
            id="boards::t",
            feature="boards",
            severity="P1",
            category="broken",
            description="desc",
            test_name="t",
            diff=None,
            related_spec_ids=[],
            related_fact_ids=[],
            iteration_count=1,
        )
        body = _build_issue_body(gap)
        # Diff section header should not appear if diff is None
        assert "### Diff" not in body
