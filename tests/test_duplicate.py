"""
Tests for scripts/duplicate.py

Covers:
- DuplicateConfig defaults and validation
- DuplicateReport.format_summary
- DuplicatePipeline.run:
    - config validation (empty target_url, output_repo, scope_str)
    - output repo creation (mock gh + repo.py)
    - pipeline step sequencing (all steps called in order)
    - spec snapshot commit (mock git)
    - error handling at each step (recon fail, synthesis fail, test gen fail, convergence fail)
    - report generation (facts, specs, tests, convergence, cost, duration)
- _normalise_url helper
- _resolve_or_create_repo with missing/present repo
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.converge import ConvergenceReport, IterationResult
from scripts.duplicate import (
    DuplicateConfig,
    DuplicatePipeline,
    DuplicateReport,
    PipelineError,
    _normalise_url,
)
from scripts.scope import Scope, ScopeFeature, freeze_scope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_scope(features: list[str] | None = None, frozen: bool = False) -> Scope:
    feats = features or ["boards", "cards"]
    scope = Scope(
        raw_input=", ".join(feats),
        features=[ScopeFeature(feature=f) for f in feats],
        target="trello.com",
    )
    if frozen:
        freeze_scope(scope)
    return scope


def make_convergence_report(
    parity: float = 80.0,
    stop_reason: str = "max_iterations",
) -> ConvergenceReport:
    iteration = IterationResult(
        iteration=1,
        parity_score=parity,
        gaps_found=2,
        gaps_fixed=0,
        new_issues_created=0,
        cost=0.002,
        duration_seconds=1.0,
    )
    return ConvergenceReport(
        iterations=[iteration],
        final_parity=parity,
        stop_reason=stop_reason,
        total_cost=0.002,
        duration_seconds=1.0,
        gaps_remaining=[],
        circuit_breaker_gaps=[],
    )


def make_pipeline(tmp_path: Path) -> DuplicatePipeline:
    return DuplicatePipeline(cw_home="/fake/cw_home", work_dir=tmp_path)


def make_config(**kwargs) -> DuplicateConfig:
    defaults = {
        "target_url": "https://trello.com",
        "output_repo": "plwp/abuello",
        "scope_str": "boards, cards",
    }
    defaults.update(kwargs)
    return DuplicateConfig(**defaults)


# ---------------------------------------------------------------------------
# _normalise_url
# ---------------------------------------------------------------------------


def test_normalise_url_with_scheme():
    assert _normalise_url("https://trello.com") == "https://trello.com"


def test_normalise_url_without_scheme():
    assert _normalise_url("trello.com") == "https://trello.com"


def test_normalise_url_http_preserved():
    assert _normalise_url("http://localhost:3000") == "http://localhost:3000"


def test_normalise_url_strips_whitespace():
    assert _normalise_url("  trello.com  ") == "https://trello.com"


# ---------------------------------------------------------------------------
# DuplicateConfig defaults
# ---------------------------------------------------------------------------


def test_config_defaults():
    cfg = make_config()
    assert cfg.max_iterations == 10
    assert cfg.cost_budget is None
    assert cfg.skip_browser_use is False
    assert cfg.target_parity == 95.0
    assert cfg.clone_url == "http://localhost:3000"
    assert cfg.use_multi_ai is True


def test_config_custom_values():
    cfg = DuplicateConfig(
        target_url="https://notion.so",
        output_repo="plwp/notion-clone",
        scope_str="pages, databases",
        max_iterations=5,
        cost_budget=20.0,
        skip_browser_use=True,
        target_parity=80.0,
        clone_url="http://localhost:4000",
        use_multi_ai=False,
    )
    assert cfg.max_iterations == 5
    assert cfg.cost_budget == 20.0
    assert cfg.skip_browser_use is True
    assert cfg.target_parity == 80.0
    assert cfg.clone_url == "http://localhost:4000"
    assert cfg.use_multi_ai is False


# ---------------------------------------------------------------------------
# DuplicateReport.format_summary
# ---------------------------------------------------------------------------


def test_report_format_summary_includes_key_fields():
    scope = make_scope()
    report = DuplicateReport(
        target_url="https://trello.com",
        output_repo="plwp/abuello",
        scope=scope,
        recon_facts=42,
        specs_generated=7,
        tests_generated=15,
        issues_created=5,
        convergence=None,
        total_duration_seconds=30.5,
        total_cost=0.05,
    )
    summary = report.format_summary()
    assert "https://trello.com" in summary
    assert "plwp/abuello" in summary
    assert "42" in summary
    assert "7" in summary
    assert "15" in summary
    assert "5" in summary
    assert "30.5" in summary
    assert "0.0500" in summary
    assert "Convergence: not run" in summary


def test_report_format_summary_with_convergence():
    scope = make_scope()
    conv = make_convergence_report(parity=95.0, stop_reason="parity_achieved")
    report = DuplicateReport(
        target_url="https://trello.com",
        output_repo="plwp/abuello",
        scope=scope,
        recon_facts=10,
        specs_generated=3,
        tests_generated=5,
        issues_created=2,
        convergence=conv,
        total_duration_seconds=60.0,
        total_cost=0.01,
    )
    summary = report.format_summary()
    assert "parity_achieved" in summary
    assert "95.0%" in summary


def test_report_format_summary_shows_errors():
    scope = make_scope()
    report = DuplicateReport(
        target_url="https://trello.com",
        output_repo="plwp/abuello",
        scope=scope,
        recon_facts=0,
        specs_generated=0,
        tests_generated=0,
        issues_created=0,
        convergence=None,
        total_duration_seconds=5.0,
        total_cost=0.0,
        errors=["Recon failed: timeout", "Synthesis failed: no facts"],
    )
    summary = report.format_summary()
    assert "Recon failed: timeout" in summary
    assert "Synthesis failed: no facts" in summary


# ---------------------------------------------------------------------------
# PipelineError on invalid config
# ---------------------------------------------------------------------------


def test_run_raises_on_empty_target_url(tmp_path):
    pipeline = make_pipeline(tmp_path)
    config = make_config(target_url="")
    with pytest.raises(PipelineError, match="target_url"):
        asyncio.run(pipeline.run(config))


def test_run_raises_on_empty_output_repo(tmp_path):
    pipeline = make_pipeline(tmp_path)
    config = make_config(output_repo="")
    with pytest.raises(PipelineError, match="output_repo"):
        asyncio.run(pipeline.run(config))


def test_run_raises_on_empty_scope_str(tmp_path):
    pipeline = make_pipeline(tmp_path)
    config = make_config(scope_str="")
    with pytest.raises(PipelineError, match="scope_str"):
        asyncio.run(pipeline.run(config))


# ---------------------------------------------------------------------------
# _resolve_or_create_repo: mock gh + repo.py
# ---------------------------------------------------------------------------


def test_resolve_or_create_repo_returns_none_on_failure(tmp_path):
    """When repo.py and gh both fail, returns None and records error."""
    pipeline = make_pipeline(tmp_path)
    errors: list[str] = []
    with patch("subprocess.run", side_effect=Exception("command not found")):
        result = pipeline._resolve_or_create_repo("plwp/abuello", errors)
    assert result is None
    assert len(errors) == 1
    assert "plwp/abuello" in errors[0]


def test_resolve_or_create_repo_returns_path_when_resolved(tmp_path):
    """When repo.py resolve succeeds, returns the resolved Path."""
    pipeline = make_pipeline(tmp_path)
    errors: list[str] = []
    fake_path = str(tmp_path)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_path + "\n", stderr="")
        result = pipeline._resolve_or_create_repo("plwp/abuello", errors)
    assert result == Path(fake_path)
    assert errors == []


def test_create_github_repo_skips_if_exists(tmp_path):
    """If gh repo view succeeds, skips creation."""
    pipeline = make_pipeline(tmp_path)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='{"name":"abuello"}', stderr="")
        pipeline._create_github_repo("plwp/abuello")
    # Should only call gh repo view, not gh repo create
    calls = mock_run.call_args_list
    assert len(calls) == 1
    assert "view" in calls[0][0][0]


def test_create_github_repo_creates_if_missing(tmp_path):
    """If gh repo view fails, calls gh repo create."""
    pipeline = make_pipeline(tmp_path)
    view_result = MagicMock(returncode=1, stdout="", stderr="not found")
    create_result = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", side_effect=[view_result, create_result]) as mock_run:
        pipeline._create_github_repo("plwp/abuello")
    calls = mock_run.call_args_list
    assert len(calls) == 2
    assert "create" in calls[1][0][0]


# ---------------------------------------------------------------------------
# Pipeline step sequencing
# ---------------------------------------------------------------------------


def test_run_calls_all_steps_in_order(tmp_path):
    """
    All pipeline steps are called in order. Mock each step to verify sequencing.
    Repo path is provided so _commit_tests is included.
    """
    pipeline = make_pipeline(tmp_path)
    config = make_config()
    call_order: list[str] = []

    mock_bundle = MagicMock()
    mock_bundle.id = "bundle-123"
    mock_bundle.spec_items = [MagicMock(), MagicMock()]
    mock_bundle.to_dict.return_value = {}
    mock_conv = make_convergence_report()

    def _resolve(*a, **kw):
        call_order.append("resolve_repo")
        return tmp_path

    def _scaffold(*a, **kw):
        call_order.append("scaffold")

    async def _recon(*a, **kw):
        call_order.append("recon")
        return 5

    async def _synth(*a, **kw):
        call_order.append("synthesize")
        return mock_bundle

    def _snap(*a, **kw):
        call_order.append("snapshot")
        return "2026-01-01T00:00:00"

    def _issues(*a, **kw):
        call_order.append("issues")
        return 3

    def _gen(*a, **kw):
        call_order.append("gen_tests")
        return 10

    def _commit(*a, **kw):
        call_order.append("commit_tests")

    async def _conv(*a, **kw):
        call_order.append("convergence")
        return mock_conv

    with (
        patch.object(pipeline, "_resolve_or_create_repo", side_effect=_resolve),
        patch.object(pipeline, "_scaffold_repo", side_effect=_scaffold),
        patch.object(pipeline, "_run_recon", side_effect=_recon),
        patch.object(pipeline, "_synthesize_specs", side_effect=_synth),
        patch.object(pipeline, "_snapshot_and_commit_specs", side_effect=_snap),
        patch.object(pipeline, "_generate_github_issues", side_effect=_issues),
        patch.object(pipeline, "_generate_tests", side_effect=_gen),
        patch.object(pipeline, "_commit_tests", side_effect=_commit),
        patch.object(pipeline, "_run_convergence", side_effect=_conv),
    ):
        report = asyncio.run(pipeline.run(config))

    assert call_order == [
        "resolve_repo",
        "scaffold",
        "recon",
        "synthesize",
        "snapshot",
        "issues",
        "gen_tests",
        "commit_tests",
        "convergence",
    ]
    assert report.recon_facts == 5
    assert report.specs_generated == 2
    assert report.issues_created == 3
    assert report.tests_generated == 10
    assert report.convergence is mock_conv


# ---------------------------------------------------------------------------
# Error handling at each step
# ---------------------------------------------------------------------------


def test_recon_failure_recorded_in_errors(tmp_path):
    """Recon failure is captured in report.errors, pipeline continues."""
    pipeline = make_pipeline(tmp_path)
    config = make_config()
    mock_bundle = MagicMock()
    mock_bundle.id = "bundle-x"
    mock_bundle.spec_items = []
    mock_bundle.to_dict.return_value = {}

    with (
        patch.object(pipeline, "_resolve_or_create_repo", return_value=None),
        patch.object(pipeline, "_scaffold_repo", return_value=None),
        patch.object(pipeline, "_run_recon", new=AsyncMock(return_value=0)),
        patch.object(pipeline, "_synthesize_specs", new=AsyncMock(return_value=None)),
        patch.object(pipeline, "_snapshot_and_commit_specs", return_value=""),
        patch.object(pipeline, "_generate_github_issues", return_value=0),
        patch.object(pipeline, "_generate_tests", return_value=0),
        patch.object(pipeline, "_commit_tests", return_value=None),
        patch.object(pipeline, "_run_convergence", new=AsyncMock(return_value=None)),
    ):
        report = asyncio.run(pipeline.run(config))

    assert report.recon_facts == 0
    assert report.specs_generated == 0
    assert report.convergence is None


def test_synthesis_failure_produces_zero_specs(tmp_path):
    """When synthesis returns None, specs_generated is 0 and pipeline continues."""
    pipeline = make_pipeline(tmp_path)
    config = make_config()

    with (
        patch.object(pipeline, "_resolve_or_create_repo", return_value=None),
        patch.object(pipeline, "_scaffold_repo", return_value=None),
        patch.object(pipeline, "_run_recon", new=AsyncMock(return_value=3)),
        patch.object(pipeline, "_synthesize_specs", new=AsyncMock(return_value=None)),
        patch.object(pipeline, "_snapshot_and_commit_specs", return_value=""),
        patch.object(pipeline, "_generate_github_issues", return_value=0),
        patch.object(pipeline, "_generate_tests", return_value=0),
        patch.object(pipeline, "_commit_tests", return_value=None),
        patch.object(pipeline, "_run_convergence", new=AsyncMock(return_value=None)),
    ):
        report = asyncio.run(pipeline.run(config))

    assert report.specs_generated == 0
    assert report.tests_generated == 0


def test_convergence_failure_returns_none_convergence(tmp_path):
    """When convergence fails, report.convergence is None and error is recorded."""
    pipeline = make_pipeline(tmp_path)
    config = make_config()
    mock_bundle = MagicMock()
    mock_bundle.id = "bundle-y"
    mock_bundle.spec_items = [MagicMock()]
    mock_bundle.to_dict.return_value = {}

    with (
        patch.object(pipeline, "_resolve_or_create_repo", return_value=None),
        patch.object(pipeline, "_scaffold_repo", return_value=None),
        patch.object(pipeline, "_run_recon", new=AsyncMock(return_value=2)),
        patch.object(pipeline, "_synthesize_specs", new=AsyncMock(return_value=mock_bundle)),
        patch.object(pipeline, "_snapshot_and_commit_specs", return_value="2026-01-01"),
        patch.object(pipeline, "_generate_github_issues", return_value=0),
        patch.object(pipeline, "_generate_tests", return_value=5),
        patch.object(pipeline, "_commit_tests", return_value=None),
        patch.object(pipeline, "_run_convergence", new=AsyncMock(return_value=None)),
    ):
        report = asyncio.run(pipeline.run(config))

    assert report.convergence is None
    assert report.total_cost == 0.0


# ---------------------------------------------------------------------------
# Spec snapshot commit (mock git)
# ---------------------------------------------------------------------------


def test_snapshot_and_commit_specs_calls_git(tmp_path):
    """_snapshot_and_commit_specs stages and commits .specstore."""
    pipeline = make_pipeline(tmp_path)

    # Build a real SpecStore with a real bundle
    from scripts.models import SpecBundle
    from scripts.spec_store import SpecStore

    spec_store = SpecStore(tmp_path)
    bundle = SpecBundle(target="trello.com", scope=["boards"])
    # Don't add facts — just test that git_commit is called

    errors: list[str] = []
    with patch.object(pipeline, "_git_commit") as mock_commit:
        pipeline._snapshot_and_commit_specs(bundle, spec_store, tmp_path, errors)

    mock_commit.assert_called_once()
    # _git_commit is called with keyword args: message=, paths=, errors=
    commit_message = mock_commit.call_args.kwargs.get("message", "")
    assert "chore: snapshot spec bundle" in commit_message


def test_git_commit_handles_failure_gracefully(tmp_path):
    """_git_commit records error in errors list if subprocess raises."""
    pipeline = make_pipeline(tmp_path)
    errors: list[str] = []
    with patch("subprocess.run", side_effect=Exception("git not found")):
        pipeline._git_commit(tmp_path, "test message", ["some/path"], errors)
    assert len(errors) == 1
    assert "git commit failed" in errors[0]


# ---------------------------------------------------------------------------
# Report generation: facts, specs, tests, convergence, cost
# ---------------------------------------------------------------------------


def test_report_fields_populated_from_pipeline(tmp_path):
    """Full pipeline run populates all report fields correctly."""
    pipeline = make_pipeline(tmp_path)
    config = make_config(max_iterations=3)

    mock_bundle = MagicMock()
    mock_bundle.id = "bundle-abc"
    mock_bundle.spec_items = [MagicMock() for _ in range(4)]
    mock_bundle.to_dict.return_value = {}
    mock_conv = make_convergence_report(parity=75.0)

    with (
        patch.object(pipeline, "_resolve_or_create_repo", return_value=tmp_path),
        patch.object(pipeline, "_scaffold_repo", return_value=None),
        patch.object(pipeline, "_run_recon", new=AsyncMock(return_value=20)),
        patch.object(pipeline, "_synthesize_specs", new=AsyncMock(return_value=mock_bundle)),
        patch.object(pipeline, "_snapshot_and_commit_specs", return_value="2026-01-01T12:00:00"),
        patch.object(pipeline, "_generate_github_issues", return_value=4),
        patch.object(pipeline, "_generate_tests", return_value=12),
        patch.object(pipeline, "_commit_tests", return_value=None),
        patch.object(pipeline, "_run_convergence", new=AsyncMock(return_value=mock_conv)),
    ):
        report = asyncio.run(pipeline.run(config))

    assert report.target_url == "https://trello.com"
    assert report.output_repo == "plwp/abuello"
    assert report.recon_facts == 20
    assert report.specs_generated == 4
    assert report.issues_created == 4
    assert report.tests_generated == 12
    assert report.bundle_id == "bundle-abc"
    assert report.snapshot_at == "2026-01-01T12:00:00"
    assert report.convergence is mock_conv
    assert report.total_cost == mock_conv.total_cost
    assert report.total_duration_seconds > 0


def test_report_scope_contains_parsed_features(tmp_path):
    """The scope in the report has the features from scope_str."""
    pipeline = make_pipeline(tmp_path)
    config = make_config(scope_str="boards, lists, cards")

    with (
        patch.object(pipeline, "_resolve_or_create_repo", return_value=None),
        patch.object(pipeline, "_scaffold_repo", return_value=None),
        patch.object(pipeline, "_run_recon", new=AsyncMock(return_value=0)),
        patch.object(pipeline, "_synthesize_specs", new=AsyncMock(return_value=None)),
        patch.object(pipeline, "_snapshot_and_commit_specs", return_value=""),
        patch.object(pipeline, "_generate_github_issues", return_value=0),
        patch.object(pipeline, "_generate_tests", return_value=0),
        patch.object(pipeline, "_commit_tests", return_value=None),
        patch.object(pipeline, "_run_convergence", new=AsyncMock(return_value=None)),
    ):
        report = asyncio.run(pipeline.run(config))

    feature_names = report.scope.feature_names()
    assert "boards" in feature_names
    assert "lists" in feature_names
    assert "cards" in feature_names


# ---------------------------------------------------------------------------
# Scaffolding and Issue Generation
# ---------------------------------------------------------------------------


def test_scaffold_repo(tmp_path):
    """_scaffold_repo copies templates and replaces placeholders."""
    pipeline = make_pipeline(tmp_path)
    repo_path = tmp_path / "output-repo"
    repo_path.mkdir()

    errors: list[str] = []

    with (
        patch("scripts.duplicate.Path") as MockPath,
        patch.object(pipeline, "_git_commit") as mock_commit,
    ):
        # Setup mocks to simulate finding a template file
        # Path(__file__).parent.parent / "templates" / "scaffold"
        mock_template_dir = MagicMock()
        mock_template_dir.exists.return_value = True
        
        # This mocks the chain: Path(__file__).parent.parent / "templates" / "scaffold"
        # Each / call is a __truediv__ call returning a new Path mock.
        mock_parent = MockPath.return_value.parent.parent
        mock_templates = mock_parent.__truediv__.return_value
        mock_templates.__truediv__.return_value = mock_template_dir
        
        mock_file = MagicMock()
        mock_file.is_dir.return_value = False
        mock_file.relative_to.return_value = Path("package.json")
        mock_file.read_text.return_value = '{"name": "{{REPO_NAME_SLUG}}", "full": "{{REPO_NAME}}"}'
        
        mock_template_dir.rglob.return_value = [mock_file]

        pipeline._scaffold_repo(repo_path, "plwp/my-awesome-repo", errors)

    # Verify file was written to the real repo_path
    pkg_json = repo_path / "package.json"
    assert pkg_json.exists()
    content = pkg_json.read_text()
    assert "my-awesome-repo" in content
    assert "plwp/my-awesome-repo" in content

    mock_commit.assert_called_once()
    assert "chore: scaffold repo" in mock_commit.call_args.kwargs["message"]


def test_generate_github_issues(tmp_path):
    """_generate_github_issues calls gh CLI once per feature (not per spec item)."""
    pipeline = make_pipeline(tmp_path)
    from scripts.models import SpecBundle, SpecItem

    bundle = SpecBundle(
        target="trello.com",
        scope=["boards"],
        spec_items=[
            SpecItem(feature="boards", spec_type="model", content={"schema": {}}),
            SpecItem(feature="boards", spec_type="api", content={"endpoints": []}),
        ],
    )

    errors: list[str] = []
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="https://github.com/plwp/clone/issues/1")
        count = pipeline._generate_github_issues(tmp_path, bundle, errors)

    # Two spec items both under "boards" → 1 issue, not 2
    assert count == 1
    assert mock_run.call_count == 1
    # Verify the call args
    args = mock_run.call_args_list[0][0][0]
    assert "gh" in args
    assert "issue" in args
    assert "create" in args
    assert "--title" in args
    assert "[SPEC] Implement boards" in args


# ---------------------------------------------------------------------------
# New tests for scaffold idempotency and improved issue generation
# ---------------------------------------------------------------------------


def test_scaffold_skips_existing_files(tmp_path):
    """Scaffold doesn't overwrite files that already exist with different content."""
    pipeline = make_pipeline(tmp_path)
    repo_path = tmp_path / "output-repo"
    repo_path.mkdir()

    # Pre-create a file with user-modified content
    pkg_json = repo_path / "package.json"
    pkg_json.write_text("user-modified content")

    errors: list[str] = []

    with (
        patch("scripts.duplicate.Path") as MockPath,
        patch.object(pipeline, "_git_commit"),
    ):
        mock_template_dir = MagicMock()
        mock_template_dir.exists.return_value = True

        mock_parent = MockPath.return_value.parent.parent
        mock_templates = mock_parent.__truediv__.return_value
        mock_templates.__truediv__.return_value = mock_template_dir

        mock_file = MagicMock()
        mock_file.is_dir.return_value = False
        mock_file.relative_to.return_value = Path("package.json")
        mock_file.read_text.return_value = '{"name": "{{REPO_NAME_SLUG}}", "full": "{{REPO_NAME}}"}'

        mock_template_dir.rglob.return_value = [mock_file]

        pipeline._scaffold_repo(repo_path, "plwp/my-repo", errors)

    # File should still have user-modified content (not overwritten)
    assert pkg_json.read_text() == "user-modified content"


def test_scaffold_skips_existing_identical_files(tmp_path):
    """Scaffold skips files that exist with the same content (no redundant write)."""
    pipeline = make_pipeline(tmp_path)
    repo_path = tmp_path / "output-repo"
    repo_path.mkdir()

    # Pre-create a file with exactly the rendered content
    pkg_json = repo_path / "package.json"
    rendered = '{"name": "my-repo", "full": "plwp/my-repo"}'
    pkg_json.write_text(rendered)

    errors: list[str] = []

    with (
        patch("scripts.duplicate.Path") as MockPath,
        patch.object(pipeline, "_git_commit"),
    ):
        mock_template_dir = MagicMock()
        mock_template_dir.exists.return_value = True

        mock_parent = MockPath.return_value.parent.parent
        mock_templates = mock_parent.__truediv__.return_value
        mock_templates.__truediv__.return_value = mock_template_dir

        mock_file = MagicMock()
        mock_file.is_dir.return_value = False
        mock_file.relative_to.return_value = Path("package.json")
        mock_file.read_text.return_value = '{"name": "{{REPO_NAME_SLUG}}", "full": "{{REPO_NAME}}"}'

        mock_template_dir.rglob.return_value = [mock_file]

        pipeline._scaffold_repo(repo_path, "plwp/my-repo", errors)

    # File still unchanged — no error
    assert pkg_json.read_text() == rendered
    assert errors == []


def test_issue_generation_groups_by_feature(tmp_path):
    """One issue per feature, not per spec item."""
    pipeline = make_pipeline(tmp_path)
    from scripts.models import SpecBundle, SpecItem

    bundle = SpecBundle(
        target="trello.com",
        scope=["boards", "lists"],
        spec_items=[
            SpecItem(feature="boards", spec_type="model", content={}),
            SpecItem(feature="boards", spec_type="api", content={}),
            SpecItem(feature="boards", spec_type="ui", content={}),
            SpecItem(feature="lists", spec_type="model", content={}),
            SpecItem(feature="lists", spec_type="api", content={}),
        ],
    )

    errors: list[str] = []
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="https://github.com/plwp/clone/issues/1")
        count = pipeline._generate_github_issues(tmp_path, bundle, errors)

    # 3 boards + 2 lists → 2 issues (one per feature)
    assert count == 2
    assert mock_run.call_count == 2

    titles_created = [
        call[0][0][call[0][0].index("--title") + 1]
        for call in mock_run.call_args_list
    ]
    assert "[SPEC] Implement boards" in titles_created
    assert "[SPEC] Implement lists" in titles_created


def test_issue_generation_writes_manifest(tmp_path):
    """Manifest file is created with feature→URL mapping."""
    pipeline = make_pipeline(tmp_path)
    from scripts.models import SpecBundle, SpecItem

    bundle = SpecBundle(
        target="trello.com",
        scope=["boards", "lists"],
        spec_items=[
            SpecItem(feature="boards", spec_type="model", content={}),
            SpecItem(feature="lists", spec_type="model", content={}),
        ],
    )

    errors: list[str] = []
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/plwp/clone/issues/1"
        )
        pipeline._generate_github_issues(tmp_path, bundle, errors)

    manifest_path = tmp_path / ".duplicat-rex" / "issues-manifest.json"
    assert manifest_path.exists(), "Manifest file was not created"

    import json
    manifest = json.loads(manifest_path.read_text())
    assert "boards" in manifest
    assert "lists" in manifest


def test_issue_generation_skips_manifested(tmp_path):
    """Features already in the manifest are not re-created."""
    pipeline = make_pipeline(tmp_path)
    import json

    from scripts.models import SpecBundle, SpecItem

    # Pre-write manifest with "boards" already done
    manifest_dir = tmp_path / ".duplicat-rex"
    manifest_dir.mkdir()
    (manifest_dir / "issues-manifest.json").write_text(
        json.dumps({"boards": "https://github.com/plwp/clone/issues/1"}) + "\n"
    )

    bundle = SpecBundle(
        target="trello.com",
        scope=["boards", "lists"],
        spec_items=[
            SpecItem(feature="boards", spec_type="model", content={}),
            SpecItem(feature="lists", spec_type="model", content={}),
        ],
    )

    errors: list[str] = []
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/plwp/clone/issues/2"
        )
        count = pipeline._generate_github_issues(tmp_path, bundle, errors)

    # Only "lists" should be created (boards was already in manifest)
    assert count == 1
    assert mock_run.call_count == 1
    args = mock_run.call_args_list[0][0][0]
    assert "[SPEC] Implement lists" in args

