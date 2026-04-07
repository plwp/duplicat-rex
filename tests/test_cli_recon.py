"""
Tests for scripts/cli.py — recon CLI wiring and helper functions.

Tests cover:
- _bridge_scope() conversion from scripts.scope.Scope to scripts.models.Scope
- recon CLI command invokes ReconOrchestrator
- recon CLI module filter passes through correctly
- _print_report() formatting
- recon CLI exits with code 1 when total_facts == 0
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from scripts.models import Scope as ModelsScope
from scripts.recon.orchestrator import ReconReport
from scripts.scope import Scope as ParsedScope
from scripts.scope import ScopeFeature

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_parsed_scope() -> ParsedScope:
    """Build a scripts.scope.Scope with 3 features."""
    s = ParsedScope(raw_input="boards, lists")
    boards = ScopeFeature(feature="boards")
    lists_ = ScopeFeature(feature="lists", depends_on=["boards"])
    auth = ScopeFeature(feature="auth", is_dependency=True)
    s.features = [boards, lists_, auth]
    s.known_exclusions = ["webhooks"]
    s.frozen = True
    s.scope_hash = "abc123"
    return s


def make_recon_report(
    total_facts: int = 10,
    modules_skipped: list[str] | None = None,
    coverage_gaps: list[str] | None = None,
    facts_by_module: dict[str, int] | None = None,
    facts_by_feature: dict[str, int] | None = None,
    duration_seconds: float = 1.5,
) -> ReconReport:
    return ReconReport(
        target="trello.com",
        scope=ModelsScope(target="trello.com"),
        results=[],
        total_facts=total_facts,
        facts_by_module=facts_by_module or {"api_docs": total_facts},
        facts_by_authority={},
        facts_by_feature=facts_by_feature or {"boards": total_facts},
        coverage_gaps=coverage_gaps or [],
        errors=[],
        duration_seconds=duration_seconds,
        modules_skipped=modules_skipped or [],
    )


# ---------------------------------------------------------------------------
# Test 1: _bridge_scope maps features correctly
# ---------------------------------------------------------------------------


def test_bridge_scope_maps_features() -> None:
    """_bridge_scope converts scripts.scope.Scope to scripts.models.Scope."""
    from scripts.cli import _bridge_scope

    parsed = make_parsed_scope()
    result = _bridge_scope(parsed, "trello.com")

    assert result.target == "trello.com"

    # 2 requested features (boards + lists); auth is a dependency
    assert len(result.requested_features) == 2
    requested_names = {n.feature for n in result.requested_features}
    assert "boards" in requested_names
    assert "lists" in requested_names

    # 3 resolved features total
    assert len(result.resolved_features) == 3
    resolved_map = {n.feature: n for n in result.resolved_features}

    # auth has inclusion_reason == "dependency"
    assert resolved_map["auth"].inclusion_reason == "dependency"

    # boards has inclusion_reason == "requested"
    assert resolved_map["boards"].inclusion_reason == "requested"

    # lists.depends_on == ["boards"]
    assert resolved_map["lists"].depends_on == ["boards"]

    # known_exclusions preserved
    assert result.known_exclusions == ["webhooks"]


# ---------------------------------------------------------------------------
# Test 2: _bridge_scope with None returns an empty Scope
# ---------------------------------------------------------------------------


def test_bridge_scope_empty_scope() -> None:
    """_bridge_scope(None, ...) returns an empty Scope with raw_input='all'."""
    from scripts.cli import _bridge_scope

    result = _bridge_scope(None, "trello.com")

    assert result.raw_input == "all"
    assert result.resolved_features == []
    assert result.target == "trello.com"


# ---------------------------------------------------------------------------
# Test 3: recon CLI invokes ReconOrchestrator
# ---------------------------------------------------------------------------


def test_recon_cli_invokes_orchestrator(tmp_path: Path) -> None:
    """recon CLI wires up and calls ReconOrchestrator.run()."""
    from scripts.cli import app

    report = make_recon_report(total_facts=10)

    mock_orchestrator_instance = MagicMock()
    mock_orchestrator_instance.run = AsyncMock(return_value=report)

    runner = CliRunner()

    with (
        patch("scripts.cli.ReconOrchestrator", return_value=mock_orchestrator_instance),
        patch("scripts.cli.SpecStore"),
    ):
        result = runner.invoke(
            app,
            ["recon", "trello.com", "--scope", "boards,lists", "--store", str(tmp_path)],
        )

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert "10" in result.output


# ---------------------------------------------------------------------------
# Test 4: recon CLI module filter is forwarded to orchestrator
# ---------------------------------------------------------------------------


def test_recon_cli_module_filter(tmp_path: Path) -> None:
    """--modules flag is forwarded to orchestrator.run() as a list."""
    from scripts.cli import app

    report = make_recon_report()

    mock_orchestrator_instance = MagicMock()
    mock_orchestrator_instance.run = AsyncMock(return_value=report)

    runner = CliRunner()

    with (
        patch("scripts.cli.ReconOrchestrator", return_value=mock_orchestrator_instance),
        patch("scripts.cli.SpecStore"),
    ):
        result = runner.invoke(
            app,
            [
                "recon",
                "trello.com",
                "--scope",
                "boards",
                "--modules",
                "api_docs,marketing",
                "--store",
                str(tmp_path),
            ],
        )

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"

    call_kwargs = mock_orchestrator_instance.run.call_args
    assert call_kwargs is not None, "orchestrator.run() was not called"
    # modules may be positional or keyword
    modules_arg = (
        call_kwargs.kwargs.get("modules")
        or (call_kwargs.args[0] if call_kwargs.args else None)
    )
    assert modules_arg == ["api_docs", "marketing"]


# ---------------------------------------------------------------------------
# Test 5: _print_report formatting
# ---------------------------------------------------------------------------


def test_print_report_formatting(capsys: pytest.CaptureFixture[str]) -> None:
    """_print_report outputs key stats in a human-readable format."""
    from scripts.cli import _print_report

    report = make_recon_report(
        total_facts=42,
        facts_by_module={"api_docs": 20, "marketing": 22},
        coverage_gaps=["drag-drop"],
        modules_skipped=["browser_explore"],
        duration_seconds=5.3,
    )

    _print_report(report)

    captured = capsys.readouterr()
    output = captured.out

    assert "42" in output
    assert "api_docs" in output
    assert "20" in output
    assert "drag-drop" in output
    assert "browser_explore" in output
    assert "5.3" in output


# ---------------------------------------------------------------------------
# Test 6: recon CLI exits 1 when total_facts == 0
# ---------------------------------------------------------------------------


def test_recon_exits_1_on_zero_facts(tmp_path: Path) -> None:
    """recon CLI exits with code 1 when the report contains zero facts."""
    from scripts.cli import app

    report = make_recon_report(total_facts=0, facts_by_module={}, facts_by_feature={})

    mock_orchestrator_instance = MagicMock()
    mock_orchestrator_instance.run = AsyncMock(return_value=report)

    runner = CliRunner()

    with (
        patch("scripts.cli.ReconOrchestrator", return_value=mock_orchestrator_instance),
        patch("scripts.cli.SpecStore"),
    ):
        result = runner.invoke(
            app,
            ["recon", "trello.com", "--store", str(tmp_path)],
        )

    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}"


# ---------------------------------------------------------------------------
# Test 7: recon CLI rejects invalid max_concurrent
# ---------------------------------------------------------------------------


def test_recon_rejects_zero_max_concurrent(tmp_path: Path) -> None:
    """--max-concurrent 0 exits with code 2 (not deadlock)."""
    from scripts.cli import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["recon", "trello.com", "--max-concurrent", "0", "--store", str(tmp_path)],
    )
    assert result.exit_code == 2


def test_recon_rejects_negative_max_concurrent(tmp_path: Path) -> None:
    """--max-concurrent -1 exits with code 2."""
    from scripts.cli import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["recon", "trello.com", "--max-concurrent", "-1", "--store", str(tmp_path)],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Test 9: scope is frozen before passing to orchestrator
# ---------------------------------------------------------------------------


def test_recon_scope_is_frozen(tmp_path: Path) -> None:
    """Scope passed to orchestrator has frozen=True and a scope_hash."""
    from scripts.cli import app

    report = make_recon_report()
    mock_orchestrator_instance = MagicMock()
    mock_orchestrator_instance.run = AsyncMock(return_value=report)

    runner = CliRunner()

    with (
        patch("scripts.cli.ReconOrchestrator", return_value=mock_orchestrator_instance),
        patch("scripts.cli.SpecStore"),
    ):
        result = runner.invoke(
            app,
            ["recon", "trello.com", "--scope", "boards,lists", "--store", str(tmp_path)],
        )

    assert result.exit_code == 0
    call_kwargs = mock_orchestrator_instance.run.call_args
    scope_arg = call_kwargs.kwargs.get("scope") or call_kwargs.args[1]
    assert scope_arg.frozen is True
    assert scope_arg.scope_hash != ""
