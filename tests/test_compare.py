"""
Tests for scripts/compare.py

Covers:
- ComparisonResult and TestDiff dataclasses
- _feature_from_path: extract feature slug from test file name
- _parse_pytest_output: parse pytest -v stdout into passed/failed/error lists
- _weighted_score: weighted average with normalisation and edge cases
- _build_diff: human-readable diff string
- format_report: report rendering
- BehavioralComparator._discover_tests: file discovery with and without scope filter
- BehavioralComparator.compare: full comparison with mocked subprocess calls
  - both pass -> passed list, parity 100%
  - target passes, clone fails -> failed list, gap in details
  - target errors -> excluded from gap counting (INV-CMP-002)
  - no test files -> parity 100%, empty result
  - weighted scoring: higher-weight features dominate score
- INV-CMP-001: parity_score always in [0, 100]
- INV-CMP-003: feature_scores keys match discovered features
- INV-CMP-004: missing weight defaults to 1.0
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.compare import (
    BehavioralComparator,
    ComparisonResult,
    _build_diff,
    _feature_from_path,
    _parse_pytest_output,
    _progress_bar,
    _weighted_score,
    format_report,
)
from scripts.compare import (
    TestDiff as ConformanceDiff,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_result(**kwargs) -> ComparisonResult:
    defaults = dict(
        target_url="https://target.example.com",
        clone_url="https://clone.example.com",
        parity_score=100.0,
        feature_scores={},
        passed=[],
        failed=[],
        errors=[],
        details=[],
    )
    defaults.update(kwargs)
    return ComparisonResult(**defaults)


def _fake_pytest_output(passed: list[str], failed: list[str], errors: list[str]) -> str:
    """Build fake pytest -v output string for the given test outcomes."""
    lines = []
    for name in passed:
        lines.append(f"tests/conformance/test_api_boards.py::{name} PASSED")
    for name in failed:
        lines.append(f"tests/conformance/test_api_boards.py::{name} FAILED")
    for name in errors:
        lines.append(f"tests/conformance/test_api_boards.py::{name} ERROR")
    return "\n".join(lines)


def _make_completed_process(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.stdout = stdout
    cp.stderr = ""
    cp.returncode = returncode
    return cp


# ---------------------------------------------------------------------------
# _feature_from_path
# ---------------------------------------------------------------------------


class TestFeatureFromPath:
    def test_api_prefix(self):
        assert _feature_from_path(Path("tests/conformance/test_api_boards.py")) == "boards"

    def test_e2e_prefix(self):
        assert _feature_from_path(Path("test_e2e_drag_drop.py")) == "drag-drop"

    def test_auth_prefix(self):
        assert _feature_from_path(Path("test_auth_members.py")) == "members"

    def test_schema_prefix(self):
        assert _feature_from_path(Path("test_schema_cards.py")) == "cards"

    def test_multi_word_feature(self):
        assert _feature_from_path(Path("test_api_card_labels.py")) == "card-labels"

    def test_no_prefix(self):
        # If no category prefix, return as-is (underscores to hyphens)
        assert _feature_from_path(Path("test_boards.py")) == "test-boards"


# ---------------------------------------------------------------------------
# _parse_pytest_output
# ---------------------------------------------------------------------------


class TestParsePytestOutput:
    def test_empty_output(self):
        passed, failed, errors = _parse_pytest_output("")
        assert passed == [] and failed == [] and errors == []

    def test_all_passed(self):
        output = (
            "tests/conformance/test_api_boards.py::test_api_boards_0 PASSED\n"
            "tests/conformance/test_api_boards.py::test_api_boards_1 PASSED\n"
        )
        passed, failed, errors = _parse_pytest_output(output)
        assert passed == ["test_api_boards_0", "test_api_boards_1"]
        assert failed == [] and errors == []

    def test_mixed_results(self):
        output = (
            "tests/conformance/test_api_boards.py::test_api_boards_0 PASSED\n"
            "tests/conformance/test_api_boards.py::test_api_boards_1 FAILED\n"
            "tests/conformance/test_api_boards.py::test_api_boards_2 ERROR\n"
        )
        passed, failed, errors = _parse_pytest_output(output)
        assert passed == ["test_api_boards_0"]
        assert failed == ["test_api_boards_1"]
        assert errors == ["test_api_boards_2"]

    def test_ignores_non_test_lines(self):
        output = (
            "collected 3 items\n"
            "tests/conformance/test_api_boards.py::test_api_boards_0 PASSED\n"
            "===== 1 passed in 0.1s =====\n"
        )
        passed, failed, errors = _parse_pytest_output(output)
        assert passed == ["test_api_boards_0"]
        assert failed == [] and errors == []

    def test_trailing_whitespace_handled(self):
        output = "tests/conformance/test_api_boards.py::test_api_boards_0 PASSED  \n"
        passed, failed, errors = _parse_pytest_output(output)
        assert "test_api_boards_0" in passed


# ---------------------------------------------------------------------------
# _weighted_score
# ---------------------------------------------------------------------------


class TestWeightedScore:
    def test_empty_scores(self):
        assert _weighted_score({}, None) == 100.0

    def test_uniform_weights(self):
        scores = {"a": 100.0, "b": 50.0}
        # No weights = uniform = (100 + 50) / 2 = 75
        assert _weighted_score(scores, None) == pytest.approx(75.0)

    def test_custom_weights(self):
        scores = {"a": 100.0, "b": 0.0}
        weights = {"a": 1.0, "b": 3.0}
        # (100*1 + 0*3) / (1+3) = 25.0
        assert _weighted_score(scores, weights) == pytest.approx(25.0)

    def test_missing_feature_defaults_to_1(self):
        scores = {"a": 100.0, "b": 0.0}
        weights = {"a": 2.0}  # "b" missing, defaults to 1.0
        # (100*2 + 0*1) / (2+1) = 66.667
        assert _weighted_score(scores, weights) == pytest.approx(200.0 / 3.0)

    def test_all_zero_weights_falls_back_to_uniform(self):
        scores = {"a": 80.0, "b": 60.0}
        weights = {"a": 0.0, "b": 0.0}
        # Falls back to uniform: (80 + 60) / 2 = 70
        assert _weighted_score(scores, weights) == pytest.approx(70.0)

    def test_single_feature(self):
        assert _weighted_score({"boards": 87.5}, None) == pytest.approx(87.5)

    def test_inv_cmp_004_extra_weight_keys_ignored(self):
        # Weights for features not in scores are ignored
        scores = {"boards": 50.0}
        weights = {"boards": 2.0, "nonexistent": 100.0}
        # Only "boards" is in scores, weight=2.0 -> score=50.0
        assert _weighted_score(scores, weights) == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# _build_diff
# ---------------------------------------------------------------------------


class TestBuildDiff:
    def test_contains_test_name(self):
        diff = _build_diff("test_api_boards_0", "pass", "fail")
        assert "test_api_boards_0" in diff

    def test_contains_statuses(self):
        diff = _build_diff("test_api_boards_0", "pass", "error")
        assert "PASS" in diff
        assert "ERROR" in diff

    def test_actionable_hint(self):
        diff = _build_diff("test_api_auth_0", "pass", "fail")
        assert "clone" in diff.lower() or "investigate" in diff.lower()


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


class TestFormatReport:
    def test_contains_score(self):
        result = make_result(parity_score=87.5, feature_scores={"boards": 87.5})
        report = format_report(result)
        assert "87.5%" in report

    def test_contains_urls(self):
        result = make_result()
        report = format_report(result)
        assert "https://target.example.com" in report
        assert "https://clone.example.com" in report

    def test_contains_feature_breakdown(self):
        result = make_result(
            parity_score=80.0,
            feature_scores={"boards": 100.0, "auth": 60.0},
        )
        report = format_report(result)
        assert "boards" in report
        assert "auth" in report

    def test_gaps_section_present_when_failures_exist(self):
        diff = ConformanceDiff(
            test_name="test_api_boards_0",
            feature="boards",
            target_result="pass",
            clone_result="fail",
            diff="divergence detail",
        )
        result = make_result(
            parity_score=50.0,
            failed=["test_api_boards_0"],
            details=[diff],
        )
        report = format_report(result)
        assert "CONFORMANCE GAPS" in report
        assert "divergence detail" in report

    def test_no_gaps_section_when_perfect(self):
        result = make_result(parity_score=100.0, passed=["test_a", "test_b"])
        report = format_report(result)
        assert "CONFORMANCE GAPS" not in report

    def test_progress_bar_bounds(self):
        assert _progress_bar(0) == "[....................]"
        assert _progress_bar(100) == "[####################]"
        assert len(_progress_bar(50)) == 22  # "[" + 20 + "]"


# ---------------------------------------------------------------------------
# BehavioralComparator._discover_tests
# ---------------------------------------------------------------------------


class TestDiscoverTests:
    def test_finds_conformance_files(self, tmp_path: Path):
        conf_dir = tmp_path / "tests" / "conformance"
        conf_dir.mkdir(parents=True)
        (conf_dir / "test_api_boards.py").write_text("")
        (conf_dir / "test_e2e_cards.py").write_text("")
        (conf_dir / "not_a_test.py").write_text("")

        comparator = BehavioralComparator(tmp_path)
        files = comparator._discover_tests(scope=None)
        names = [f.name for f in files]
        assert "test_api_boards.py" in names
        assert "test_e2e_cards.py" in names
        assert "not_a_test.py" not in names

    def test_falls_back_to_suite_dir_when_no_conformance_subdir(self, tmp_path: Path):
        (tmp_path / "test_api_boards.py").write_text("")
        comparator = BehavioralComparator(tmp_path)
        files = comparator._discover_tests(scope=None)
        assert any(f.name == "test_api_boards.py" for f in files)

    def test_scope_filter(self, tmp_path: Path):
        conf_dir = tmp_path / "tests" / "conformance"
        conf_dir.mkdir(parents=True)
        (conf_dir / "test_api_boards.py").write_text("")
        (conf_dir / "test_api_cards.py").write_text("")

        from scripts.models import Scope, ScopeNode

        scope = Scope()
        scope.resolved_features = [ScopeNode(feature="boards", label="Boards")]
        comparator = BehavioralComparator(tmp_path)
        files = comparator._discover_tests(scope=scope)
        names = [f.name for f in files]
        assert "test_api_boards.py" in names
        assert "test_api_cards.py" not in names

    def test_empty_suite_dir_returns_empty(self, tmp_path: Path):
        comparator = BehavioralComparator(tmp_path)
        files = comparator._discover_tests(scope=None)
        assert files == []


# ---------------------------------------------------------------------------
# BehavioralComparator.compare — mocked subprocess
# ---------------------------------------------------------------------------


class TestComparatorCompare:
    """Tests for the full compare() flow with mocked _run_pytest calls."""

    def _make_file(self, tmp_path: Path, name: str) -> Path:
        conf_dir = tmp_path / "tests" / "conformance"
        conf_dir.mkdir(parents=True, exist_ok=True)
        p = conf_dir / name
        p.write_text("# stub")
        return p

    def _run(self, coro):
        return asyncio.run(coro)

    def test_no_test_files_returns_perfect_parity(self, tmp_path: Path):
        comparator = BehavioralComparator(tmp_path)
        result = self._run(comparator.compare("https://t.com", "https://c.com"))
        assert result.parity_score == 100.0
        assert result.passed == [] and result.failed == [] and result.errors == []

    def test_both_pass_parity_100(self, tmp_path: Path):
        self._make_file(tmp_path, "test_api_boards.py")
        target_out = _fake_pytest_output(["test_api_boards_0"], [], [])
        clone_out = _fake_pytest_output(["test_api_boards_0"], [], [])

        with patch("scripts.compare._run_pytest") as mock_run:
            mock_run.side_effect = [
                _make_completed_process(target_out),
                _make_completed_process(clone_out),
            ]
            result = self._run(BehavioralComparator(tmp_path).compare(
                "https://t.com", "https://c.com"
            ))

        assert result.parity_score == pytest.approx(100.0)
        assert "test_api_boards_0" in result.passed
        assert result.failed == []
        assert result.errors == []

    def test_clone_fails_creates_gap(self, tmp_path: Path):
        self._make_file(tmp_path, "test_api_boards.py")
        target_out = _fake_pytest_output(["test_api_boards_0"], [], [])
        clone_out = _fake_pytest_output([], ["test_api_boards_0"], [])

        with patch("scripts.compare._run_pytest") as mock_run:
            mock_run.side_effect = [
                _make_completed_process(target_out),
                _make_completed_process(clone_out),
            ]
            result = self._run(BehavioralComparator(tmp_path).compare(
                "https://t.com", "https://c.com"
            ))

        assert result.parity_score < 100.0
        assert "test_api_boards_0" in result.failed
        assert len(result.details) == 1
        assert result.details[0].target_result == "pass"
        assert result.details[0].clone_result == "fail"

    def test_target_error_excluded_from_gaps_inv_cmp_002(self, tmp_path: Path):
        self._make_file(tmp_path, "test_api_boards.py")
        target_out = _fake_pytest_output([], [], ["test_api_boards_0"])
        clone_out = _fake_pytest_output([], ["test_api_boards_0"], [])

        with patch("scripts.compare._run_pytest") as mock_run:
            mock_run.side_effect = [
                _make_completed_process(target_out),
                _make_completed_process(clone_out),
            ]
            result = self._run(BehavioralComparator(tmp_path).compare(
                "https://t.com", "https://c.com"
            ))

        # INV-CMP-002: target errored, so test goes to errors list, not failed
        assert "test_api_boards_0" in result.errors
        assert "test_api_boards_0" not in result.failed
        assert result.details == []
        # Score should not be penalised
        assert result.parity_score == pytest.approx(100.0)

    def test_both_fail_not_a_gap(self, tmp_path: Path):
        self._make_file(tmp_path, "test_api_boards.py")
        target_out = _fake_pytest_output([], ["test_api_boards_0"], [])
        clone_out = _fake_pytest_output([], ["test_api_boards_0"], [])

        with patch("scripts.compare._run_pytest") as mock_run:
            mock_run.side_effect = [
                _make_completed_process(target_out),
                _make_completed_process(clone_out),
            ]
            result = self._run(BehavioralComparator(tmp_path).compare(
                "https://t.com", "https://c.com"
            ))

        assert result.parity_score == pytest.approx(100.0)
        assert result.failed == []

    def test_parity_score_in_range_inv_cmp_001(self, tmp_path: Path):
        self._make_file(tmp_path, "test_api_boards.py")
        target_out = _fake_pytest_output(["test_api_boards_0", "test_api_boards_1"], [], [])
        clone_out = _fake_pytest_output([], ["test_api_boards_0", "test_api_boards_1"], [])

        with patch("scripts.compare._run_pytest") as mock_run:
            mock_run.side_effect = [
                _make_completed_process(target_out),
                _make_completed_process(clone_out),
            ]
            result = self._run(BehavioralComparator(tmp_path).compare(
                "https://t.com", "https://c.com"
            ))

        assert 0.0 <= result.parity_score <= 100.0

    def test_feature_scores_keys_match_features_inv_cmp_003(self, tmp_path: Path):
        self._make_file(tmp_path, "test_api_boards.py")
        self._make_file(tmp_path, "test_api_cards.py")
        target_out_boards = _fake_pytest_output(["test_api_boards_0"], [], [])
        target_out_cards = _fake_pytest_output(["test_api_cards_0"], [], [])
        clone_out_boards = _fake_pytest_output(["test_api_boards_0"], [], [])
        clone_out_cards = _fake_pytest_output([], ["test_api_cards_0"], [])

        call_count = 0

        def side_effect(test_file, env):
            nonlocal call_count
            call_count += 1
            url = env.get("BASE_URL", "")
            is_target = "t.com" in url
            if "boards" in str(test_file):
                return _make_completed_process(target_out_boards if is_target else clone_out_boards)
            else:
                return _make_completed_process(target_out_cards if is_target else clone_out_cards)

        with patch("scripts.compare._run_pytest", side_effect=side_effect):
            result = self._run(BehavioralComparator(tmp_path).compare(
                "https://t.com", "https://c.com"
            ))

        # INV-CMP-003: feature_scores keys == features found in test files
        assert set(result.feature_scores.keys()) == {"boards", "cards"}
        assert result.feature_scores["boards"] == pytest.approx(100.0)
        assert result.feature_scores["cards"] == pytest.approx(0.0)

    def test_weighted_scoring_applied(self, tmp_path: Path):
        self._make_file(tmp_path, "test_api_boards.py")
        self._make_file(tmp_path, "test_api_auth.py")
        # boards passes, auth fails
        target_out_boards = _fake_pytest_output(["test_api_boards_0"], [], [])
        target_out_auth = _fake_pytest_output(["test_api_auth_0"], [], [])
        clone_out_boards = _fake_pytest_output(["test_api_boards_0"], [], [])
        clone_out_auth = _fake_pytest_output([], ["test_api_auth_0"], [])

        def side_effect(test_file, env):
            url = env.get("BASE_URL", "")
            is_target = "t.com" in url
            if "boards" in str(test_file):
                return _make_completed_process(target_out_boards if is_target else clone_out_boards)
            else:
                return _make_completed_process(target_out_auth if is_target else clone_out_auth)

        # auth weight = 3, boards weight = 1
        # boards score = 100, auth score = 0
        # weighted = (100*1 + 0*3) / (1+3) = 25.0
        weights = {"auth": 3.0, "boards": 1.0}
        with patch("scripts.compare._run_pytest", side_effect=side_effect):
            result = self._run(BehavioralComparator(tmp_path).compare(
                "https://t.com", "https://c.com", weights=weights
            ))

        assert result.parity_score == pytest.approx(25.0)
