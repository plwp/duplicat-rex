"""
Tests for scripts/cli.py — converge CLI command wiring.

Tests cover:
- converge CLI invokes ConvergenceOrchestrator.run()
- converge CLI prints format_summary() output
- converge CLI exits 1 when stop_reason != "parity_achieved"
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from scripts.converge import ConvergenceReport, IterationResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_convergence_report(stop_reason: str = "parity_achieved") -> ConvergenceReport:
    it1 = IterationResult(
        iteration=1,
        parity_score=95.0,
        gaps_found=0,
        gaps_fixed=0,
        new_issues_created=0,
        cost=0.001,
        duration_seconds=3.0,
    )
    return ConvergenceReport(
        iterations=[it1],
        final_parity=95.0,
        stop_reason=stop_reason,
        total_cost=0.001,
        duration_seconds=3.0,
        gaps_remaining=[],
        circuit_breaker_gaps=[],
    )


# ---------------------------------------------------------------------------
# Test 1: converge CLI invokes ConvergenceOrchestrator
# ---------------------------------------------------------------------------


def test_converge_invokes_orchestrator(tmp_path: Path) -> None:
    """converge CLI wires up and calls ConvergenceOrchestrator.run()."""
    from scripts.cli import app

    report = make_convergence_report(stop_reason="parity_achieved")
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
                "http://localhost:3000",
                "--suite-dir",
                str(tmp_path),
                "--max-iterations",
                "5",
            ],
        )

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    mock_orchestrator_instance.run.assert_called_once()
    call_args = mock_orchestrator_instance.run.call_args
    config = call_args.args[0]
    assert config.target_url == "https://trello.com"
    assert config.clone_url == "http://localhost:3000"
    assert config.max_iterations == 5
    assert config.repo == "plwp/clone"


# ---------------------------------------------------------------------------
# Test 2: converge CLI prints format_summary() output
# ---------------------------------------------------------------------------


def test_converge_prints_summary(tmp_path: Path) -> None:
    """converge CLI output contains the format_summary() content."""
    from scripts.cli import app

    report = make_convergence_report(stop_reason="parity_achieved")
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
                "http://localhost:3000",
                "--suite-dir",
                str(tmp_path),
            ],
        )

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert "CONVERGENCE SUMMARY" in result.output
    assert "95.0%" in result.output


# ---------------------------------------------------------------------------
# Test 3: converge CLI exits 1 when stop_reason != "parity_achieved"
# ---------------------------------------------------------------------------


def test_converge_exits_1_on_non_parity_stop(tmp_path: Path) -> None:
    """converge CLI exits with code 1 when stop_reason is not 'parity_achieved'."""
    from scripts.cli import app

    report = make_convergence_report(stop_reason="max_iterations")
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
                "http://localhost:3000",
                "--suite-dir",
                str(tmp_path),
            ],
        )

    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}\n{result.output}"
