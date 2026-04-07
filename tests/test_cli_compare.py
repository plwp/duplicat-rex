"""
Tests for scripts/cli.py — compare CLI command wiring.

Tests cover:
- compare CLI invokes BehavioralComparator.compare()
- compare CLI prints parity score in output
- compare CLI exits 1 when parity < --min-parity threshold
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from scripts.compare import ComparisonResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_comparison_result(parity: float = 85.3) -> ComparisonResult:
    return ComparisonResult(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        parity_score=parity,
        feature_scores={"boards": 92.0, "cards": 78.5},
        passed=["test_boards_1", "test_cards_1"],
        failed=["test_boards_2"],
        errors=[],
        details=[],
    )


# ---------------------------------------------------------------------------
# Test 1: compare CLI invokes BehavioralComparator
# ---------------------------------------------------------------------------


def test_compare_invokes_comparator(tmp_path: Path) -> None:
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
                "http://localhost:3000",
                "--suite-dir",
                str(tmp_path),
                "--scope",
                "boards,cards",
            ],
        )

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    mock_comparator_instance.compare.assert_called_once()
    call_args = mock_comparator_instance.compare.call_args
    assert call_args.args[0] == "https://trello.com"
    assert call_args.args[1] == "http://localhost:3000"


# ---------------------------------------------------------------------------
# Test 2: compare CLI prints parity score
# ---------------------------------------------------------------------------


def test_compare_prints_parity(tmp_path: Path) -> None:
    """compare CLI output contains the parity score."""
    from scripts.cli import app

    result_obj = make_comparison_result(parity=85.3)
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
                "http://localhost:3000",
                "--suite-dir",
                str(tmp_path),
            ],
        )

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert "85.3%" in result.output


# ---------------------------------------------------------------------------
# Test 3: compare CLI exits 1 when parity < --min-parity
# ---------------------------------------------------------------------------


def test_compare_exits_1_below_min_parity(tmp_path: Path) -> None:
    """compare CLI exits with code 1 when parity score < --min-parity."""
    from scripts.cli import app

    # parity=40 is below min-parity=80
    result_obj = make_comparison_result(parity=40.0)
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
                "http://localhost:3000",
                "--suite-dir",
                str(tmp_path),
                "--min-parity",
                "80",
            ],
        )

    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}\n{result.output}"
