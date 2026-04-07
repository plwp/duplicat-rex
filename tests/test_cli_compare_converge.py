"""
Tests for scripts/cli.py — compare and converge CLI command wiring.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from scripts.compare import ComparisonResult
from scripts.converge import ConvergenceReport, IterationResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_comparison_result() -> ComparisonResult:
    return ComparisonResult(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        parity_score=85.0,
        feature_scores={"boards": 80.0, "lists": 90.0},
        passed=["test_boards_1", "test_lists_1"],
        failed=["test_boards_2"],
        errors=[],
        details=[],
    )


def make_convergence_report() -> ConvergenceReport:
    it1 = IterationResult(
        iteration=1,
        parity_score=85.0,
        gaps_found=1,
        gaps_fixed=0,
        new_issues_created=1,
        cost=0.0011,
        duration_seconds=5.0,
    )
    return ConvergenceReport(
        iterations=[it1],
        final_parity=85.0,
        stop_reason="parity_achieved",
        total_cost=0.0011,
        duration_seconds=5.0,
        gaps_remaining=[],
        circuit_breaker_gaps=[],
    )


# ---------------------------------------------------------------------------
# Test 1: compare CLI invokes BehavioralComparator
# ---------------------------------------------------------------------------

def test_compare_cli_invokes_comparator(tmp_path: Path) -> None:
    """compare CLI wires up and calls BehavioralComparator.compare()."""
    from scripts.cli import app

    result_obj = make_comparison_result()
    mock_comparator_instance = MagicMock()
    mock_comparator_instance.compare = AsyncMock(return_value=result_obj)

    runner = CliRunner()

    with patch("scripts.cli.BehavioralComparator", return_value=mock_comparator_instance):
        result = runner.invoke(
            app,
            [
                "compare",
                "trello.com",
                "--clone-url",
                "http://localhost:4000",
                "--suite-dir",
                str(tmp_path),
                "--scope",
                "boards,lists",
            ],
        )

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert "BEHAVIORAL CONFORMANCE REPORT" in result.output
    assert "85.0%" in result.output
    
    mock_comparator_instance.compare.assert_called_once()
    call_args = mock_comparator_instance.compare.call_args
    assert call_args.args[0] == "https://trello.com"
    assert call_args.args[1] == "http://localhost:4000"


# ---------------------------------------------------------------------------
# Test 2: converge CLI invokes ConvergenceOrchestrator
# ---------------------------------------------------------------------------

def test_converge_cli_invokes_orchestrator(tmp_path: Path) -> None:
    """converge CLI wires up and calls ConvergenceOrchestrator.run()."""
    from scripts.cli import app

    report = make_convergence_report()
    mock_orchestrator_instance = MagicMock()
    mock_orchestrator_instance.run = AsyncMock(return_value=report)

    runner = CliRunner()

    with (
        patch("scripts.cli.ConvergenceOrchestrator", return_value=mock_orchestrator_instance),
        patch("scripts.cli.SpecStore"),
        patch("scripts.cli.GapAnalyzer"),
        patch("scripts.cli.BehavioralComparator"),
    ):
        result = runner.invoke(
            app,
            [
                "converge",
                "trello.com",
                "--output",
                "plwp/clone",
                "--clone-url",
                "http://localhost:4000",
                "--suite-dir",
                str(tmp_path),
                "--max-iterations",
                "3",
            ],
        )

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert "CONVERGENCE SUMMARY" in result.output
    assert "85.0%" in result.output
    
    mock_orchestrator_instance.run.assert_called_once()
    call_args = mock_orchestrator_instance.run.call_args
    config = call_args.args[0]
    assert config.target_url == "https://trello.com"
    assert config.clone_url == "http://localhost:4000"
    assert config.max_iterations == 3
    assert config.repo == "plwp/clone"


# ---------------------------------------------------------------------------
# Test 3: _normalise_url helper
# ---------------------------------------------------------------------------

def test_normalise_url() -> None:
    from scripts.cli import _normalise_url
    assert _normalise_url("trello.com") == "https://trello.com"
    assert _normalise_url("http://localhost:3000") == "http://localhost:3000"
    assert _normalise_url("  https://trello.com  ") == "https://trello.com"
