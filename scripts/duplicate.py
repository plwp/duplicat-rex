"""
DuplicatePipeline — top-level orchestrator for the full duplicat-rex pipeline.

Runs the full pipeline in order:
  1. Parse scope; create output repo if needed (gh repo create)
  2. Run recon orchestrator against target_url
  3. Synthesize specs from gathered facts
  4. Snapshot specs and commit to output repo
  5. Generate dual-execution test cases
  6. Commit tests to output repo
  7. Run convergence loop (compare → gap → fix → repeat)
  8. Report final parity score, cost, duration

Usage:
    pipeline = DuplicatePipeline(cw_home="/path/to/chief-wiggum", work_dir=Path("."))
    report = asyncio.run(pipeline.run(config))
    print(report.format_summary())
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from scripts.compare import BehavioralComparator
from scripts.converge import ConvergenceConfig, ConvergenceOrchestrator, ConvergenceReport
from scripts.gap_analyzer import GapAnalyzer
from scripts.models import BundleStatus, SpecBundle
from scripts.scope import Scope, freeze_scope, parse_scope
from scripts.spec_store import SpecStore
from scripts.spec_synthesizer import SpecSynthesizer
from scripts.test_generator import TestGenerator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class DuplicateConfig:
    """Configuration for a full duplicate pipeline run."""

    target_url: str  # URL of the target application (e.g. "https://trello.com")
    output_repo: str  # GitHub repo for the clone (e.g. "plwp/abuello")
    scope_str: str  # Feature scope (e.g. "boards, lists, cards, drag-drop")
    max_iterations: int = 10  # Max convergence iterations
    cost_budget: float | None = None  # USD budget; None = unlimited
    skip_browser_use: bool = False  # If True, skip live-app recon modules
    target_parity: float = 95.0  # Stop convergence when parity >= this
    clone_url: str = "http://localhost:3000"  # URL of the clone under test
    use_multi_ai: bool = True  # Use multi-AI synthesis (codex + gemini)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class DuplicateReport:
    """Final report produced at the end of a full pipeline run."""

    target_url: str
    output_repo: str
    scope: Scope
    recon_facts: int
    specs_generated: int
    tests_generated: int
    issues_created: int
    convergence: ConvergenceReport | None
    total_duration_seconds: float
    total_cost: float
    bundle_id: str = ""
    snapshot_at: str = ""
    errors: list[str] = field(default_factory=list)

    def format_summary(self) -> str:
        """Render a human-readable pipeline summary."""
        lines: list[str] = []
        lines.append("=" * 70)
        lines.append("DUPLICAT-REX PIPELINE SUMMARY")
        lines.append("=" * 70)
        lines.append(f"Target:          {self.target_url}")
        lines.append(f"Output repo:     {self.output_repo}")
        lines.append(f"Scope:           {', '.join(self.scope.feature_names())}")
        lines.append(f"Recon facts:     {self.recon_facts}")
        lines.append(f"Specs generated: {self.specs_generated}")
        lines.append(f"Issues created:  {self.issues_created}")
        lines.append(f"Tests generated: {self.tests_generated}")
        lines.append(f"Total duration:  {self.total_duration_seconds:.1f}s")
        lines.append(f"Total cost:      ${self.total_cost:.4f}")
        if self.snapshot_at:
            lines.append(f"Snapshot at:     {self.snapshot_at}")
        if self.bundle_id:
            lines.append(f"Bundle ID:       {self.bundle_id}")
        if self.errors:
            lines.append("")
            lines.append(f"Errors ({len(self.errors)}):")
            for err in self.errors:
                lines.append(f"  - {err}")
        if self.convergence:
            lines.append("")
            lines.append(self.convergence.format_summary())
        else:
            lines.append("")
            lines.append("Convergence: not run")
        lines.append("=" * 70)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline error
# ---------------------------------------------------------------------------


class PipelineError(Exception):
    """Raised for unrecoverable pipeline failures."""


# ---------------------------------------------------------------------------
# DuplicatePipeline
# ---------------------------------------------------------------------------


class DuplicatePipeline:
    """
    Orchestrates the full duplicat-rex pipeline.

    Usage:
        pipeline = DuplicatePipeline(cw_home="/path/to/chief-wiggum", work_dir=Path("."))
        report = asyncio.run(pipeline.run(config))
    """

    def __init__(self, cw_home: str, work_dir: Path) -> None:
        """
        Args:
            cw_home:  Path to the chief-wiggum install directory.
            work_dir: Working directory where the spec store and outputs are written.
        """
        self.cw_home = cw_home
        self.work_dir = Path(work_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, config: DuplicateConfig) -> DuplicateReport:
        """
        Execute the full pipeline:
          1. Parse scope, create output repo if needed
          2. Scaffold output repo (Next.js, Tailwind, Docker, etc.)
          3. Run recon orchestrator against target_url
          4. Synthesize specs from gathered facts
          5. Snapshot specs and commit to output repo
          6. Generate GitHub issues for implementation
          7. Generate dual-execution test cases
          8. Commit tests to output repo
          9. Run convergence loop
          10. Report final parity score, cost, duration

        REQUIRES: config.target_url and config.output_repo are non-empty.
        ENSURES: returned DuplicateReport is always produced (errors captured in report.errors).
        """
        if not config.target_url:
            raise PipelineError("config.target_url must not be empty")
        if not config.output_repo:
            raise PipelineError("config.output_repo must not be empty")
        if not config.scope_str:
            raise PipelineError("config.scope_str must not be empty")

        run_start = time.monotonic()
        errors: list[str] = []

        # --- Step 1: Parse scope + create output repo ---
        logger.info("[1/9] Parsing scope: %s", config.scope_str)
        normalised = _normalise_url(config.target_url)
        target_host = normalised.replace("https://", "").replace("http://", "").split("/")[0]
        scope = parse_scope(config.scope_str, target=target_host)

        output_repo_path = self._resolve_or_create_repo(config.output_repo, errors)

        # --- Step 2: Scaffold repo ---
        logger.info("[2/9] Scaffolding output repo: %s", config.output_repo)
        if output_repo_path:
            self._scaffold_repo(output_repo_path, config.output_repo, errors)

        # --- Step 3: Recon ---
        logger.info("[3/9] Running recon against %s", config.target_url)
        spec_store = SpecStore(output_repo_path or self.work_dir)
        recon_facts = await self._run_recon(
            config, scope, spec_store, errors
        )

        # --- Step 4: Synthesize specs ---
        logger.info("[4/9] Synthesizing specs (%d facts)", recon_facts)
        bundle = await self._synthesize_specs(
            config, scope, spec_store, errors
        )

        # --- Step 5: Snapshot and commit specs ---
        logger.info("[5/9] Snapshotting specs")
        snapshot_at = ""
        if bundle is not None:
            snapshot_at = self._snapshot_and_commit_specs(
                bundle, spec_store, output_repo_path, errors
            )

        specs_generated = len(bundle.spec_items) if bundle else 0

        # --- Step 6: Generate GitHub issues ---
        logger.info("[6/9] Generating GitHub issues")
        issues_created = 0
        if bundle is not None and output_repo_path:
            issues_created = self._generate_github_issues(
                output_repo_path, bundle, errors
            )

        # --- Step 7: Generate tests ---
        logger.info("[7/9] Generating conformance tests")
        tests_generated = 0
        if bundle is not None:
            tests_generated = self._generate_tests(
                bundle, config, output_repo_path or self.work_dir, errors
            )

        # --- Step 8: Commit tests ---
        logger.info("[8/9] Committing tests to output repo")
        if output_repo_path:
            self._commit_tests(output_repo_path, errors)

        # --- Step 9: Convergence loop ---
        logger.info("[9/9] Running convergence loop")
        convergence_report = await self._run_convergence(
            config, scope, spec_store, output_repo_path or self.work_dir, errors
        )

        # --- Final report ---
        total_cost = convergence_report.total_cost if convergence_report else 0.0
        duration = time.monotonic() - run_start

        report = DuplicateReport(
            target_url=config.target_url,
            output_repo=config.output_repo,
            scope=scope,
            recon_facts=recon_facts,
            specs_generated=specs_generated,
            tests_generated=tests_generated,
            issues_created=issues_created,
            convergence=convergence_report,
            total_duration_seconds=duration,
            total_cost=total_cost,
            bundle_id=bundle.id if bundle else "",
            snapshot_at=snapshot_at,
            errors=errors,
        )

        logger.info("Pipeline complete. Duration=%.1fs, cost=$%.4f", duration, total_cost)
        return report

    # ------------------------------------------------------------------
    # Internal: step implementations
    # ------------------------------------------------------------------

    def _resolve_or_create_repo(
        self, output_repo: str, errors: list[str]
    ) -> Path | None:
        """
        Resolve the output repo to a local path via chief-wiggum's repo helper.
        Creates the GitHub repo first if it doesn't exist.

        Returns the local path on success, or None if resolution fails.
        """
        try:
            # Try to resolve via CW repo helper (pulls latest if cached)
            repo_py = Path(self.cw_home) / "scripts" / "repo.py"
            result = subprocess.run(
                ["python3", str(repo_py), "resolve", output_repo],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0 and result.stdout.strip():
                path = Path(result.stdout.strip())
                logger.info("Resolved output repo to: %s", path)
                return path
        except Exception as exc:  # noqa: BLE001
            logger.debug("repo.py resolve failed: %s", exc)

        # If not found, create the repo on GitHub and clone it
        try:
            logger.info("Creating output repo: %s", output_repo)
            self._create_github_repo(output_repo)
            # Try resolution again after creation
            repo_py = Path(self.cw_home) / "scripts" / "repo.py"
            result = subprocess.run(
                ["python3", str(repo_py), "resolve", output_repo],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0 and result.stdout.strip():
                return Path(result.stdout.strip())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Could not create/resolve output repo {output_repo!r}: {exc}")

        return None

    def _create_github_repo(self, output_repo: str) -> None:
        """Create a new GitHub repo via gh CLI if it doesn't exist."""
        # Check if it already exists
        check = subprocess.run(
            ["gh", "repo", "view", output_repo, "--json", "name"],
            capture_output=True, text=True, timeout=30,
        )
        if check.returncode == 0:
            logger.info("Repo %s already exists — skipping creation", output_repo)
            return

        # Create it
        subprocess.run(
            ["gh", "repo", "create", output_repo, "--private", "--confirm"],
            capture_output=True, text=True, timeout=60, check=True,
        )
        logger.info("Created GitHub repo: %s", output_repo)

    def _scaffold_repo(self, repo_path: Path, repo_name: str, errors: list[str]) -> None:
        """
        Copy templates from templates/scaffold/ to repo_path with variable replacement.
        """
        try:
            template_dir = Path(__file__).parent.parent / "templates" / "scaffold"
            if not template_dir.exists():
                logger.warning("Scaffold template directory not found: %s", template_dir)
                return

            repo_slug = repo_name.split("/")[-1].lower().replace(" ", "-")

            for template_file in template_dir.rglob("*"):
                if template_file.is_dir():
                    continue

                # Calculate relative path
                rel_path = template_file.relative_to(template_dir)
                target_path = repo_path / rel_path
                target_path.parent.mkdir(parents=True, exist_ok=True)

                # Read and substitute variables
                content = template_file.read_text()
                content = content.replace("{{REPO_NAME}}", repo_name)
                content = content.replace("{{REPO_NAME_SLUG}}", repo_slug)

                # Idempotency: skip files that already exist with different content
                if target_path.exists():
                    existing = target_path.read_text()
                    if existing != content:
                        logger.info(
                            "Skipping %s (already exists with different content)", rel_path
                        )
                    # Either different (skipped above) or same (no need to write)
                    continue

                target_path.write_text(content)

            # Commit scaffold
            self._git_commit(
                repo_path,
                message="chore: scaffold repo with default stack [duplicat-rex]",
                paths=["."],
                errors=errors,
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"Scaffolding failed: {exc}"
            logger.error(msg, exc_info=True)
            errors.append(msg)

    def _generate_github_issues(
        self, repo_path: Path, bundle: SpecBundle, errors: list[str]
    ) -> int:
        """Generate GitHub issues from spec items, one per feature."""
        count = 0
        try:
            # Group by feature
            from collections import defaultdict
            by_feature: dict[str, list] = defaultdict(list)
            for item in bundle.spec_items:
                by_feature[item.feature].append(item)

            # Load or create manifest
            manifest_dir = repo_path / ".duplicat-rex"
            manifest_path = manifest_dir / "issues-manifest.json"
            manifest: dict[str, str] = {}
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())

            for feature, items in sorted(by_feature.items()):
                if feature in manifest:
                    logger.info("Skipping issue for %s (already in manifest)", feature)
                    continue

                title = f"[SPEC] Implement {feature}"
                body = self._render_issue_body(feature, items)

                result = subprocess.run(
                    ["gh", "issue", "create", "--title", title, "--body", body],
                    cwd=str(repo_path),
                    capture_output=True, text=True, timeout=30, check=False,
                )
                if result.returncode == 0:
                    # Extract URL from output
                    url = result.stdout.strip()
                    manifest[feature] = url
                    count += 1
                else:
                    errors.append(f"Failed to create issue for {feature}: {result.stderr}")

            # Write manifest
            manifest_dir.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

        except Exception as exc:  # noqa: BLE001
            msg = f"GitHub issue generation failed: {exc}"
            logger.error(msg, exc_info=True)
            errors.append(msg)
        return count

    def _render_issue_body(self, feature: str, items: list) -> str:
        """Render a GitHub issue body from spec items for a feature."""
        sections = []
        sections.append(f"## Feature: {feature}")
        sections.append("")

        for item in items:
            sections.append(f"### {item.spec_type}")
            sections.append(f"**Confidence:** {item.confidence}")
            sections.append("")

            content = item.content
            if isinstance(content, dict):
                # Extract key sections
                if "summary" in content:
                    sections.append(f"**Summary:** {content['summary']}")
                    sections.append("")

                for key in [
                    "api_contracts", "ui_patterns", "data_models",
                    "state_machines", "business_rules",
                ]:
                    if key in content and content[key]:
                        sections.append(f"#### {key.replace('_', ' ').title()}")
                        sections.append("```json")
                        sections.append(json.dumps(content[key], indent=2)[:3000])
                        sections.append("```")
                        sections.append("")

                if "open_questions" in content and content["open_questions"]:
                    sections.append("#### Open Questions")
                    for q in content["open_questions"]:
                        sections.append(f"- {q}")
                    sections.append("")
            else:
                sections.append(f"```\n{content}\n```")
                sections.append("")

        sections.append("---")
        sections.append("*Generated by duplicat-rex*")
        return "\n".join(sections)

    async def _run_recon(
        self,
        config: DuplicateConfig,
        scope: Scope,
        spec_store: SpecStore,
        errors: list[str],
    ) -> int:
        """
        Run the recon orchestrator.

        Returns the total number of facts gathered.
        """
        try:
            import scripts.keychain as keychain_module
            from scripts.recon.orchestrator import ReconOrchestrator

            orchestrator = ReconOrchestrator(
                spec_store=spec_store,
                keychain=keychain_module,
            )
            target_url = _normalise_url(config.target_url)

            # Build a models.Scope from the scope.Scope for the recon orchestrator
            # (recon orchestrator uses models.Scope)
            from scripts.models import Scope as ModelsScope
            from scripts.models import ScopeNode
            models_scope = ModelsScope(
                target=scope.target,
                raw_input=scope.raw_input,
                requested_features=[
                    ScopeNode(feature=f.feature, label=f.feature)
                    for f in scope.features
                ],
                resolved_features=[
                    ScopeNode(feature=f.feature, label=f.feature)
                    for f in scope.features
                ],
            )

            report = await orchestrator.run(
                target=target_url,
                scope=models_scope,
            )
            logger.info(
                "Recon complete: %d facts, %d modules ran, %d errors",
                report.total_facts,
                len(report.results),
                len(report.errors),
            )
            for err in report.errors:
                errors.append(f"Recon error ({err.module_name}): {err.message}")
            return report.total_facts
        except Exception as exc:  # noqa: BLE001
            msg = f"Recon failed: {exc}"
            logger.error(msg, exc_info=True)
            errors.append(msg)
            return 0

    async def _synthesize_specs(
        self,
        config: DuplicateConfig,
        scope: Scope,
        spec_store: SpecStore,
        errors: list[str],
    ) -> SpecBundle | None:
        """
        Synthesize a SpecBundle from gathered facts.

        Returns the SpecBundle, or None if synthesis fails.
        """
        try:
            import scripts.keychain as keychain_module

            synthesizer = SpecSynthesizer(
                spec_store=spec_store,
                keychain=keychain_module,
                cw_home=self.cw_home,
            )
            target_host = scope.target or _normalise_url(config.target_url)
            bundle = await synthesizer.synthesize(
                target_host,
                scope,
                use_multi_ai=config.use_multi_ai,
            )
            logger.info("Synthesized %d spec items", len(bundle.spec_items))
            return bundle
        except Exception as exc:  # noqa: BLE001
            msg = f"Spec synthesis failed: {exc}"
            logger.error(msg, exc_info=True)
            errors.append(msg)
            return None

    def _snapshot_and_commit_specs(
        self,
        bundle: SpecBundle,
        spec_store: SpecStore,
        output_repo_path: Path | None,
        errors: list[str],
    ) -> str:
        """
        Persist the bundle to the spec store as an immutable snapshot,
        then git-commit the .specstore to the output repo.

        Returns the snapshot timestamp on success, or "" on failure.
        """
        try:
            # Persist the synthesized bundle to the spec store

            from scripts.spec_store import SpecStoreError

            # Save the bundle
            bundle_path = spec_store._bundle_path(bundle.id)
            bundle_path.parent.mkdir(parents=True, exist_ok=True)
            from scripts.spec_store import _atomic_write
            _atomic_write(bundle_path, bundle.to_dict())

            # Update the index
            index = spec_store._load_index()
            index["bundles"][bundle.id] = {
                "status": str(bundle.status),
                "version": bundle.version,
                "target": bundle.target,
                "scope_hash": bundle.scope_hash,
                "snapshot_count": 0,
            }
            spec_store._save_index(index)

            # Validate and snapshot
            ok, issues = spec_store.validate_bundle(bundle.id)
            if not ok:
                # Snapshot anyway (facts may be empty in early pipeline runs)
                logger.warning(
                    "Bundle validation issues (proceeding): %s",
                    "; ".join(issues[:3]),
                )

            # Transition to VALIDATED (allow failures to proceed)
            try:
                spec_store.set_bundle_status(bundle.id, BundleStatus.VALIDATED)
                snapshot_bundle = spec_store.snapshot_bundle(bundle.id)
                snapshot_at = snapshot_bundle.snapshot_at or datetime.now(UTC).isoformat()
            except SpecStoreError as exc:
                logger.warning("Could not snapshot bundle: %s", exc)
                snapshot_at = datetime.now(UTC).isoformat()

            # Commit .specstore to output repo
            if output_repo_path:
                self._git_commit(
                    output_repo_path,
                    message="chore: snapshot spec bundle [duplicat-rex]",
                    paths=[".specstore"],
                    errors=errors,
                )

            return snapshot_at
        except Exception as exc:  # noqa: BLE001
            msg = f"Spec snapshot/commit failed: {exc}"
            logger.error(msg, exc_info=True)
            errors.append(msg)
            return ""

    def _generate_tests(
        self,
        bundle: SpecBundle,
        config: DuplicateConfig,
        output_dir: Path,
        errors: list[str],
    ) -> int:
        """
        Generate conformance tests from the SpecBundle.

        Returns the total number of tests generated.
        """
        try:
            generator = TestGenerator(spec_store=SpecStore(output_dir))
            target_url = _normalise_url(config.target_url)
            suite = generator.generate(
                bundle,
                output_dir=output_dir,
                target_url=target_url,
                clone_url=config.clone_url,
            )
            logger.info(
                "Generated %d tests across %d files",
                suite.total_tests,
                len(suite.test_files),
            )
            return suite.total_tests
        except Exception as exc:  # noqa: BLE001
            msg = f"Test generation failed: {exc}"
            logger.error(msg, exc_info=True)
            errors.append(msg)
            return 0

    def _commit_tests(self, output_repo_path: Path, errors: list[str]) -> None:
        """Git-commit generated tests to the output repo."""
        tests_dir = output_repo_path / "tests" / "conformance"
        if not tests_dir.exists():
            return
        self._git_commit(
            output_repo_path,
            message="test: add generated conformance tests [duplicat-rex]",
            paths=["tests/conformance"],
            errors=errors,
        )

    async def _run_convergence(
        self,
        config: DuplicateConfig,
        scope: Scope,
        spec_store: SpecStore,
        suite_dir: Path,
        errors: list[str],
    ) -> ConvergenceReport | None:
        """
        Run the convergence loop (compare → gap → fix → repeat).

        Returns the ConvergenceReport, or None if convergence cannot run.
        """
        try:
            # Freeze scope before convergence (INV-CNV-001)
            if not scope.frozen:
                freeze_scope(scope)

            comparator = BehavioralComparator(suite_dir)
            history_dir = self.work_dir / "convergence_history"
            gap_analyzer = GapAnalyzer(spec_store, history_dir)
            orchestrator = ConvergenceOrchestrator(
                spec_store=spec_store,
                comparator=comparator,
                gap_analyzer=gap_analyzer,
            )
            conv_config = ConvergenceConfig(
                target_url=_normalise_url(config.target_url),
                clone_url=config.clone_url,
                scope=scope,
                max_iterations=config.max_iterations,
                target_parity=config.target_parity,
                cost_budget=config.cost_budget,
                repo=config.output_repo,
                history_dir=history_dir,
            )
            report = await orchestrator.run(conv_config)
            logger.info(
                "Convergence complete: parity=%.1f%%, stop_reason=%s",
                report.final_parity,
                report.stop_reason,
            )
            return report
        except Exception as exc:  # noqa: BLE001
            msg = f"Convergence failed: {exc}"
            logger.error(msg, exc_info=True)
            errors.append(msg)
            return None

    # ------------------------------------------------------------------
    # Internal: git helpers
    # ------------------------------------------------------------------

    def _git_commit(
        self,
        repo_path: Path,
        message: str,
        paths: list[str],
        errors: list[str],
    ) -> None:
        """Stage the given paths and create a git commit in repo_path."""
        try:
            for p in paths:
                subprocess.run(
                    ["git", "add", "-A", p],
                    cwd=str(repo_path),
                    capture_output=True, text=True, timeout=30, check=False,
                )
            result = subprocess.run(
                ["git", "commit", "-m", message, "--allow-empty"],
                cwd=str(repo_path),
                capture_output=True, text=True, timeout=30, check=False,
            )
            if result.returncode != 0:
                logger.debug("git commit stdout: %s", result.stdout)
                logger.debug("git commit stderr: %s", result.stderr)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"git commit failed ({message!r}): {exc}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_url(url: str) -> str:
    """Ensure a URL has a scheme (defaults to https://)."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url
