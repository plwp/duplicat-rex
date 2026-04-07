"""
BehavioralComparator — dual-execution conformance comparator.

Design:
- Discovers generated test files in a test_suite_dir
- Runs each test against target (BASE_URL=target_url) via subprocess
- Runs each test against clone (BASE_URL=clone_url) via subprocess
- Tests that pass on target but fail on clone = conformance gaps
- Computes weighted parity score per feature and overall
- Returns structured ComparisonResult with actionable diffs

Invariants:
    INV-CMP-001: parity_score is always in [0, 100].
    INV-CMP-002: A test that errors on both target AND clone is not counted
                 as a gap (cannot measure conformance without a baseline).
    INV-CMP-003: feature_scores keys are exactly the set of features found
                 in discovered test files.
    INV-CMP-004: Weights are normalised before scoring; missing features
                 default to weight 1.0.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from scripts.models import Scope

# ---------------------------------------------------------------------------
# Non-deterministic fields — stripped before diff comparison
# ---------------------------------------------------------------------------

_VOLATILE_FIELDS = frozenset(
    [
        "id",
        "uuid",
        "created_at",
        "updated_at",
        "modified_at",
        "timestamp",
        "token",
        "session_id",
        "request_id",
        "trace_id",
        "etag",
        "last_modified",
    ]
)

# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass
class TestDiff:
    """Detailed diff for a single test that diverged between target and clone."""

    test_name: str
    feature: str
    target_result: str  # "pass" | "fail" | "error"
    clone_result: str  # "pass" | "fail" | "error"
    diff: str  # Human-readable explanation of the divergence


@dataclass
class ComparisonResult:
    """Full conformance comparison result."""

    target_url: str
    clone_url: str
    parity_score: float  # 0-100 (INV-CMP-001)
    feature_scores: dict[str, float]  # per-feature 0-100 (INV-CMP-003)
    passed: list[str]  # test names that passed on both target and clone
    failed: list[str]  # test names that passed on target but failed on clone (gaps)
    errors: list[str]  # test names that errored on target (baseline unavailable)
    details: list[TestDiff]  # detailed diffs for failures


# ---------------------------------------------------------------------------
# Internal result types
# ---------------------------------------------------------------------------


@dataclass
class _RunResult:
    """Result of running a single pytest file against one URL."""

    test_file: Path
    feature: str
    url: str
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    output: str = ""
    returncode: int = 0


# ---------------------------------------------------------------------------
# BehavioralComparator
# ---------------------------------------------------------------------------


class BehavioralComparator:
    """
    Runs conformance test suites against target and clone, then compares results.

    Usage:
        comparator = BehavioralComparator(test_suite_dir)
        result = await comparator.compare(target_url, clone_url)
        print(f"Parity score: {result.parity_score:.1f}%")
    """

    def __init__(self, test_suite_dir: Path) -> None:
        self.test_suite_dir = test_suite_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compare(
        self,
        target_url: str,
        clone_url: str,
        *,
        scope: Scope | None = None,
        weights: dict[str, float] | None = None,
    ) -> ComparisonResult:
        """
        Run conformance tests against target and clone, return parity report.

        Steps:
        1. Discover generated test files in test_suite_dir (tests/conformance/)
        2. Run each test against target (BASE_URL=target_url)
        3. Run each test against clone (BASE_URL=clone_url)
        4. Compare: tests passing on target but failing on clone = gaps
        5. Compute weighted parity score
        6. Generate detailed diffs for failures

        REQUIRES: test_suite_dir exists and contains *.py test files.
        ENSURES:  result.parity_score is in [0, 100] (INV-CMP-001).
        ENSURES:  result.feature_scores.keys() == features found in test files (INV-CMP-003).
        """
        test_files = self._discover_tests(scope)

        if not test_files:
            # No tests — perfect parity by default (nothing to fail)
            return ComparisonResult(
                target_url=target_url,
                clone_url=clone_url,
                parity_score=100.0,
                feature_scores={},
                passed=[],
                failed=[],
                errors=[],
                details=[],
            )

        # Run against target and clone concurrently
        target_results, clone_results = await asyncio.gather(
            self._run_suite(test_files, target_url),
            self._run_suite(test_files, clone_url),
        )

        # Build lookup: test_name -> result
        target_by_test = _index_by_test(target_results)
        clone_by_test = _index_by_test(clone_results)

        # Compare results
        passed: list[str] = []
        failed: list[str] = []
        errors: list[str] = []
        details: list[TestDiff] = []
        feature_test_counts: dict[str, int] = {}
        feature_pass_counts: dict[str, int] = {}

        all_tests = set(target_by_test) | set(clone_by_test)

        for test_name in sorted(all_tests):
            t_status, t_feature = target_by_test.get(test_name, ("error", "unknown"))
            c_status, _ = clone_by_test.get(test_name, ("error", "unknown"))
            feature = t_feature

            feature_test_counts[feature] = feature_test_counts.get(feature, 0) + 1

            if t_status == "error":
                # Target errored — baseline unavailable (INV-CMP-002)
                errors.append(test_name)
                # Don't penalise clone for missing baseline
                feature_pass_counts[feature] = feature_pass_counts.get(feature, 0) + 1
            elif t_status == "pass" and c_status == "pass":
                passed.append(test_name)
                feature_pass_counts[feature] = feature_pass_counts.get(feature, 0) + 1
            elif t_status == "pass" and c_status in ("fail", "error"):
                # Gap: target passes, clone does not
                failed.append(test_name)
                diff_text = _build_diff(test_name, t_status, c_status)
                details.append(
                    TestDiff(
                        test_name=test_name,
                        feature=feature,
                        target_result=t_status,
                        clone_result=c_status,
                        diff=diff_text,
                    )
                )
            elif t_status == "fail" and c_status == "fail":
                # Both fail — not a conformance gap (spec itself may be wrong)
                passed.append(test_name)
                feature_pass_counts[feature] = feature_pass_counts.get(feature, 0) + 1
            else:
                # t_status == "fail", c_status in ("pass", "error")
                # Clone does better or errors — treat as pass (no gap)
                passed.append(test_name)
                feature_pass_counts[feature] = feature_pass_counts.get(feature, 0) + 1

        # Compute per-feature scores
        feature_scores: dict[str, float] = {}
        for feature, total in feature_test_counts.items():
            pass_count = feature_pass_counts.get(feature, 0)
            feature_scores[feature] = (pass_count / total * 100.0) if total > 0 else 100.0

        # Compute weighted overall score (INV-CMP-004)
        parity_score = _weighted_score(feature_scores, weights)

        # Clamp to [0, 100] (INV-CMP-001)
        parity_score = max(0.0, min(100.0, parity_score))

        return ComparisonResult(
            target_url=target_url,
            clone_url=clone_url,
            parity_score=parity_score,
            feature_scores=feature_scores,
            passed=passed,
            failed=failed,
            errors=errors,
            details=details,
        )

    # ------------------------------------------------------------------
    # Test discovery
    # ------------------------------------------------------------------

    def _discover_tests(self, scope: Scope | None) -> list[Path]:
        """
        Find test files in test_suite_dir (tests/conformance/ subdirectory).

        If scope is provided, filter to only features in scope.
        """
        conformance_dir = self.test_suite_dir / "tests" / "conformance"
        if not conformance_dir.exists():
            # Fall back to test_suite_dir itself
            conformance_dir = self.test_suite_dir

        test_files = sorted(conformance_dir.glob("test_*.py"))

        if scope is not None:
            # Support both models.Scope (feature_keys) and scope.Scope (feature_names)
            if hasattr(scope, "feature_keys"):
                allowed = set(scope.feature_keys())
            elif hasattr(scope, "feature_names"):
                allowed = set(scope.feature_names())
            else:
                allowed = set()
            if allowed:
                test_files = [f for f in test_files if _feature_from_path(f) in allowed]

        return test_files

    # ------------------------------------------------------------------
    # Test execution
    # ------------------------------------------------------------------

    async def _run_suite(self, test_files: list[Path], base_url: str) -> list[_RunResult]:
        """Run all test files against a URL concurrently."""
        tasks = [self._run_file(tf, base_url) for tf in test_files]
        return list(await asyncio.gather(*tasks))

    async def _run_file(self, test_file: Path, base_url: str) -> _RunResult:
        """Run a single test file via pytest subprocess, parse results."""
        feature = _feature_from_path(test_file)
        env = {"BASE_URL": base_url}

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, _run_pytest, test_file, env
        )

        passed, failed, errors = _parse_pytest_output(result.stdout + result.stderr)

        return _RunResult(
            test_file=test_file,
            feature=feature,
            url=base_url,
            passed=passed,
            failed=failed,
            errors=errors,
            output=result.stdout + result.stderr,
            returncode=result.returncode,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_pytest(test_file: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run pytest on a single file, returning CompletedProcess."""
    import os
    import sys

    python = sys.executable or "python3"
    env = {**os.environ, **extra_env}
    return subprocess.run(
        [python, "-m", "pytest", str(test_file), "-v", "--tb=short", "--no-header"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _parse_pytest_output(output: str) -> tuple[list[str], list[str], list[str]]:
    """
    Parse pytest -v output into passed, failed, and error test names.

    Matches lines like:
        tests/conformance/test_api_boards.py::test_api_boards_0 PASSED
        tests/conformance/test_api_boards.py::test_api_boards_1 FAILED
        tests/conformance/test_api_boards.py::test_api_boards_2 ERROR
    """
    passed: list[str] = []
    failed: list[str] = []
    errors: list[str] = []

    # Match pytest -v output: "path::test_name STATUS"
    pattern = re.compile(r"^(\S+::test_\w+)\s+(PASSED|FAILED|ERROR)\s*$", re.MULTILINE)
    for match in pattern.finditer(output):
        test_id = match.group(1)
        # Use just the test function name (after ::)
        test_name = test_id.split("::")[-1]
        status = match.group(2)
        if status == "PASSED":
            passed.append(test_name)
        elif status == "FAILED":
            failed.append(test_name)
        elif status == "ERROR":
            errors.append(test_name)

    return passed, failed, errors


def _index_by_test(
    results: list[_RunResult],
) -> dict[str, tuple[str, str]]:
    """
    Build a mapping from test_name -> (status, feature).

    Status is "pass", "fail", or "error".
    """
    index: dict[str, tuple[str, str]] = {}
    for run in results:
        for t in run.passed:
            index[t] = ("pass", run.feature)
        for t in run.failed:
            index[t] = ("fail", run.feature)
        for t in run.errors:
            index[t] = ("error", run.feature)
    return index


def _feature_from_path(path: Path) -> str:
    """
    Extract feature slug from a conformance test file name.

    test_api_boards.py      -> "boards"
    test_e2e_drag_drop.py   -> "drag-drop"
    test_auth_members.py    -> "members"
    test_schema_cards.py    -> "cards"
    """
    stem = path.stem  # e.g. "test_api_boards"
    # Strip category prefix (test_api_, test_e2e_, test_auth_, test_schema_)
    stem = re.sub(r"^test_(api|e2e|auth|schema)_", "", stem)
    # Convert underscores back to hyphens (matching _feature_slug convention)
    return stem.replace("_", "-")


def _build_diff(test_name: str, target_result: str, clone_result: str) -> str:
    """Generate a human-readable diff description for a failing test."""
    return textwrap.dedent(f"""\
        Test:   {test_name}
        Target: {target_result.upper()}
        Clone:  {clone_result.upper()}

        The clone did not reproduce the target's behavior for this test.
        Investigate the endpoint or flow exercised by `{test_name}` —
        ensure the clone handles the same inputs and returns equivalent outputs,
        excluding non-deterministic fields (id, uuid, timestamps, tokens).
    """).strip()


def _weighted_score(
    feature_scores: dict[str, float],
    weights: dict[str, float] | None,
) -> float:
    """
    Compute a weighted average of feature scores.

    INV-CMP-004: weights are normalised; missing features default to 1.0.
    Returns 100.0 if feature_scores is empty.
    """
    if not feature_scores:
        return 100.0

    effective_weights: dict[str, float] = {}
    for feature in feature_scores:
        w = (weights or {}).get(feature, 1.0)
        effective_weights[feature] = max(0.0, w)  # no negative weights

    total_weight = sum(effective_weights.values())
    if total_weight == 0.0:
        # All weights zero — fall back to uniform
        return sum(feature_scores.values()) / len(feature_scores)

    weighted_sum = sum(
        feature_scores[f] * effective_weights[f] for f in feature_scores
    )
    return weighted_sum / total_weight


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_report(result: ComparisonResult) -> str:
    """
    Render a human-readable conformance report from a ComparisonResult.

    Returns a multi-line string suitable for printing to the terminal.
    """
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("BEHAVIORAL CONFORMANCE REPORT")
    lines.append("=" * 70)
    lines.append(f"Target: {result.target_url}")
    lines.append(f"Clone:  {result.clone_url}")
    lines.append("")
    lines.append(f"Overall Parity Score: {result.parity_score:.1f}%")
    lines.append("")

    # Feature breakdown
    if result.feature_scores:
        lines.append("Score by Feature:")
        for feature, score in sorted(result.feature_scores.items()):
            bar = _progress_bar(score)
            lines.append(f"  {feature:<30} {bar}  {score:5.1f}%")
        lines.append("")

    # Summary counts
    total = len(result.passed) + len(result.failed) + len(result.errors)
    lines.append(f"Tests run: {total}")
    lines.append(f"  Passed (both):  {len(result.passed)}")
    lines.append(f"  Failed (clone): {len(result.failed)}  ← conformance gaps")
    lines.append(f"  Errors (target baseline unavailable): {len(result.errors)}")
    lines.append("")

    # Failure details
    if result.details:
        lines.append("─" * 70)
        lines.append("CONFORMANCE GAPS — Action Required:")
        lines.append("─" * 70)
        for diff in result.details:
            lines.append("")
            lines.append(f"[{diff.feature}] {diff.test_name}")
            for line in diff.diff.splitlines():
                lines.append(f"  {line}")

    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


def _progress_bar(score: float, width: int = 20) -> str:
    """Render a compact ASCII progress bar for a 0-100 score."""
    filled = round(score / 100 * width)
    return "[" + "#" * filled + "." * (width - filled) + "]"
