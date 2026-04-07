"""
Tests for scripts/cli.py — duplicate CLI command wiring.

Tests cover:
- duplicate CLI constructs DuplicateConfig and calls pipeline.run()
- exit code 1 when report has errors
- format_summary() output appears in stdout
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from scripts.duplicate import DuplicateReport
from scripts.scope import Scope, ScopeFeature

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_scope(features: list[str] | None = None) -> Scope:
    feats = features or ["boards", "lists"]
    scope = Scope(
        raw_input=", ".join(feats),
        features=[ScopeFeature(feature=f) for f in feats],
        target="trello.com",
    )
    return scope


def make_report(errors: list[str] | None = None) -> DuplicateReport:
    scope = make_scope()
    return DuplicateReport(
        target_url="https://trello.com",
        output_repo="plwp/clone",
        scope=scope,
        recon_facts=10,
        specs_generated=3,
        tests_generated=5,
        issues_created=2,
        convergence=None,
        total_duration_seconds=15.0,
        total_cost=0.01,
        errors=errors or [],
    )


# ---------------------------------------------------------------------------
# Test 1: CLI constructs DuplicateConfig and calls pipeline.run()
# ---------------------------------------------------------------------------


def test_duplicate_calls_pipeline(tmp_path: Path) -> None:
    """CLI constructs DuplicateConfig and calls pipeline.run()."""
    from scripts.cli import app

    report = make_report()

    mock_pipeline_instance = MagicMock()
    mock_pipeline_instance.run = AsyncMock(return_value=report)

    runner = CliRunner()

    with patch("scripts.cli.DuplicatePipeline", return_value=mock_pipeline_instance):
        result = runner.invoke(
            app,
            [
                "duplicate",
                "trello.com",
                "--output",
                "plwp/clone",
                "--scope",
                "boards,lists",
                "--cw-home",
                str(tmp_path),
            ],
        )

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    mock_pipeline_instance.run.assert_called_once()

    # Verify the config passed to pipeline.run()
    call_args = mock_pipeline_instance.run.call_args
    config = call_args.args[0] if call_args.args else call_args.kwargs.get("config")
    assert config is not None
    assert "trello.com" in config.target_url
    assert config.output_repo == "plwp/clone"
    assert "boards" in config.scope_str or "lists" in config.scope_str


# ---------------------------------------------------------------------------
# Test 2: Exit code 1 when report has errors
# ---------------------------------------------------------------------------


def test_duplicate_exits_on_errors(tmp_path: Path) -> None:
    """Exit code 1 when report has errors."""
    from scripts.cli import app

    report = make_report(errors=["something broke"])

    mock_pipeline_instance = MagicMock()
    mock_pipeline_instance.run = AsyncMock(return_value=report)

    runner = CliRunner()

    with patch("scripts.cli.DuplicatePipeline", return_value=mock_pipeline_instance):
        result = runner.invoke(
            app,
            [
                "duplicate",
                "trello.com",
                "--output",
                "plwp/clone",
                "--cw-home",
                str(tmp_path),
            ],
        )

    assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}"


# ---------------------------------------------------------------------------
# Test 3: format_summary() output appears in stdout
# ---------------------------------------------------------------------------


def test_duplicate_shows_summary(tmp_path: Path) -> None:
    """format_summary() output appears in stdout."""
    from scripts.cli import app

    report = make_report()

    mock_pipeline_instance = MagicMock()
    mock_pipeline_instance.run = AsyncMock(return_value=report)

    runner = CliRunner()

    with patch("scripts.cli.DuplicatePipeline", return_value=mock_pipeline_instance):
        result = runner.invoke(
            app,
            [
                "duplicate",
                "trello.com",
                "--output",
                "plwp/clone",
                "--cw-home",
                str(tmp_path),
            ],
        )

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert "DUPLICAT-REX PIPELINE SUMMARY" in result.output


# ---------------------------------------------------------------------------
# Test 4: CLI passes correct config fields through to DuplicatePipeline
# ---------------------------------------------------------------------------


def test_duplicate_config_fields(tmp_path: Path) -> None:
    """CLI options are correctly mapped to DuplicateConfig fields."""
    from scripts.cli import app

    report = make_report()

    mock_pipeline_instance = MagicMock()
    mock_pipeline_instance.run = AsyncMock(return_value=report)

    runner = CliRunner()

    with patch("scripts.cli.DuplicatePipeline", return_value=mock_pipeline_instance):
        result = runner.invoke(
            app,
            [
                "duplicate",
                "trello.com",
                "--output",
                "plwp/clone",
                "--scope",
                "boards,lists",
                "--max-iterations",
                "5",
                "--target-parity",
                "80.0",
                "--clone-url",
                "http://localhost:4000",
                "--no-multi-ai",
                "--cw-home",
                str(tmp_path),
            ],
        )

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"

    call_args = mock_pipeline_instance.run.call_args
    config = call_args.args[0] if call_args.args else call_args.kwargs.get("config")
    assert config.max_iterations == 5
    assert config.target_parity == 80.0
    assert config.clone_url == "http://localhost:4000"
    assert config.use_multi_ai is False


# ---------------------------------------------------------------------------
# Test 5: DuplicatePipeline is constructed with correct cw_home
# ---------------------------------------------------------------------------


def test_duplicate_pipeline_receives_cw_home(tmp_path: Path) -> None:
    """DuplicatePipeline is constructed with the --cw-home value."""
    from scripts.cli import app

    report = make_report()

    mock_pipeline_instance = MagicMock()
    mock_pipeline_instance.run = AsyncMock(return_value=report)

    runner = CliRunner()

    with patch(
        "scripts.cli.DuplicatePipeline", return_value=mock_pipeline_instance
    ) as MockPipeline:
        result = runner.invoke(
            app,
            [
                "duplicate",
                "trello.com",
                "--output",
                "plwp/clone",
                "--cw-home",
                str(tmp_path),
            ],
        )

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    MockPipeline.assert_called_once()
    pipeline_kwargs = MockPipeline.call_args
    cw_home_used = (
        pipeline_kwargs.kwargs.get("cw_home")
        or (pipeline_kwargs.args[0] if pipeline_kwargs.args else None)
    )
    assert str(tmp_path) in str(cw_home_used)
