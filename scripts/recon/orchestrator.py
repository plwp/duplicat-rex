"""
Recon Orchestrator — coordinates all ReconModules for a single recon run.

Design:
- Discovers all ReconModule subclasses by importing the recon package.
- Validates prerequisites before running (skips modules that fail).
- Fetches credentials from the keychain per INV-029: only passes what each
  module declared in requires_credentials.
- Runs modules concurrently (asyncio.gather) respecting max_concurrent.
- Collects ReconResults and stores all facts in the SpecStore.
- Reports aggregate statistics and identifies coverage gaps.
- Supports targeted mode: filter by module names or feature slugs.

Invariants honoured:
  INV-020: run() never raises — module errors are captured in ReconReport.
  INV-028: Credentials never appear in facts, logs, or progress messages.
  INV-029: Only credentials declared by a module are passed to it.
  INV-038: All facts in a ReconResult share the same run_id.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import pkgutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from scripts.models import Authority, Scope  # noqa: F401 — Scope re-exported for callers
from scripts.recon.base import (
    ReconError,
    ReconModule,
    ReconModuleStatus,
    ReconProgress,
    ReconRequest,
    ReconResult,
    ReconServices,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


@dataclass
class ReconReport:
    """Aggregate result from a full recon run across all modules."""

    target: str
    scope: Scope
    results: list[ReconResult]  # one per module that ran
    total_facts: int
    facts_by_module: dict[str, int]
    facts_by_authority: dict[str, int]
    facts_by_feature: dict[str, int]
    coverage_gaps: list[str]  # feature slugs with no authoritative facts
    errors: list[ReconError]  # aggregated from all modules
    duration_seconds: float
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: str = ""
    finished_at: str = ""
    modules_skipped: list[str] = field(default_factory=list)
    modules_failed: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ReconOrchestrator:
    """
    Orchestrates all ReconModules for a single recon pass.

    Usage:
        store = SpecStore(repo_path)
        orchestrator = ReconOrchestrator(spec_store=store, keychain=keychain_module)
        report = asyncio.run(orchestrator.run("trello.com", scope))
    """

    def __init__(
        self,
        spec_store: Any,
        keychain: Any,
        artifact_dir: str | None = None,
        progress_callback: Callable[[ReconProgress], None] | None = None,
    ) -> None:
        """
        Args:
            spec_store:        SpecStore instance for persisting facts.
            keychain:          Module with get_secret(key, service) interface.
            artifact_dir:      Optional directory for saving module artifacts.
            progress_callback: Optional callback for streaming progress events.
        """
        self.spec_store = spec_store
        self.keychain = keychain
        self.artifact_dir = artifact_dir
        self.progress_callback = progress_callback

    # ------------------------------------------------------------------
    # Module discovery
    # ------------------------------------------------------------------

    def discover_modules(self) -> list[ReconModule]:
        """
        Import all modules under scripts.recon and return instances of every
        concrete ReconModule subclass found.

        Skips: base.py, orchestrator.py, __init__.py.
        """
        import scripts.recon as recon_pkg

        instances: list[ReconModule] = []
        seen_classes: set[type] = set()

        for _finder, modname, _ispkg in pkgutil.iter_modules(recon_pkg.__path__):
            if modname in ("base", "orchestrator"):
                continue
            full_name = f"scripts.recon.{modname}"
            try:
                mod = importlib.import_module(full_name)
            except Exception:
                logger.warning("Failed to import %s — skipping", full_name, exc_info=True)
                continue

            for _name, obj in inspect.getmembers(mod, inspect.isclass):
                if (
                    issubclass(obj, ReconModule)
                    and obj is not ReconModule
                    and obj not in seen_classes
                ):
                    seen_classes.add(obj)
                    try:
                        instances.append(obj())
                    except Exception:
                        logger.warning(
                            "Failed to instantiate %s — skipping", obj.__name__, exc_info=True
                        )

        return instances

    # ------------------------------------------------------------------
    # Credential scoping (INV-029)
    # ------------------------------------------------------------------

    def _fetch_credentials(self, module: ReconModule) -> dict[str, str]:
        """
        Fetch only the credentials declared by the module (INV-029).
        Missing credentials are silently omitted — the module must handle
        auth failures gracefully via ReconResult.errors.
        """
        creds: dict[str, str] = {}
        for key in module.requires_credentials:
            value = self.keychain.get_secret(key)
            if value is not None:
                creds[key] = value
        return creds

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(
        self,
        target: str,
        scope: Scope,
        *,
        modules: list[str] | None = None,  # filter to specific module names
        features: list[str] | None = None,  # targeted feature re-run
        max_concurrent: int = 3,
        budgets: dict[str, int] | None = None,
        run_id: str | None = None,
    ) -> ReconReport:
        """
        Run all (or filtered) recon modules against the target.

        ENSURES: Never raises — all errors appear in ReconReport.
        ENSURES: All facts share the same run_id (INV-038).

        Args:
            target:         Target URL or domain (e.g. "trello.com").
            scope:          Resolved Scope object defining features of interest.
            modules:        If set, only run modules with these names.
            features:       If set, pass as a targeted feature hint to modules.
            max_concurrent: Max number of modules to run in parallel.
            budgets:        Per-run budget overrides (time_seconds, max_pages…).
            run_id:         Use a specific run_id (default: new UUID).

        Returns:
            ReconReport with aggregated statistics, gaps, and errors.
        """
        effective_run_id = run_id or str(uuid.uuid4())
        started_at = datetime.now(UTC).isoformat()
        t0 = time.monotonic()

        # Apply targeted feature filter to scope if features specified
        effective_scope = scope
        if features:
            # Build a narrow scope for targeted re-runs
            from scripts.models import Scope as ModelScope

            filtered = [f for f in scope.resolved_features if f.feature in features]
            effective_scope = ModelScope(
                target=scope.target,
                raw_input=scope.raw_input,
                requested_features=scope.requested_features,
                resolved_features=filtered if filtered else scope.resolved_features,
            )

        # Discover and optionally filter modules
        all_modules = self.discover_modules()
        if modules:
            all_modules = [m for m in all_modules if m.name in modules]

        # Validate prerequisites
        runnable: list[ReconModule] = []
        skipped: list[str] = []
        for mod in all_modules:
            try:
                missing = await mod.validate_prerequisites()
            except Exception:
                logger.warning(
                    "validate_prerequisites() raised for %s — skipping", mod.name, exc_info=True
                )
                missing = ["validate_prerequisites() raised unexpectedly"]
            if missing:
                logger.info("Skipping %s — missing prerequisites: %s", mod.name, missing)
                skipped.append(mod.name)
            else:
                runnable.append(mod)

        # Run modules with bounded concurrency
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _run_one(mod: ReconModule) -> ReconResult:
            async with semaphore:
                creds = self._fetch_credentials(mod)
                services = ReconServices(
                    spec_store=self.spec_store,
                    credentials=creds,
                    artifact_store=self.artifact_dir,
                    http_client=None,
                    browser=None,
                )
                request = ReconRequest(
                    run_id=effective_run_id,
                    target=target,
                    base_url=f"https://{target}" if not target.startswith("http") else target,
                    scope=effective_scope,
                    budgets=budgets or {},
                )
                cb = self.progress_callback

                def _progress(p: ReconProgress) -> None:
                    if cb:
                        cb(p)

                try:
                    return await mod.run(request, services, _progress)
                except Exception:
                    # INV-020: run() should not raise, but be defensive
                    logger.exception("Module %s raised unexpectedly", mod.name)
                    return ReconResult(
                        module=mod.name,
                        status=ReconModuleStatus.FAILED,
                        errors=[
                            ReconError(
                                source_url=None,
                                error_type="parse_error",
                                message=f"Module {mod.name} raised an unhandled exception",
                                recoverable=False,
                            )
                        ],
                    )

        results: list[ReconResult] = list(
            await asyncio.gather(*[_run_one(m) for m in runnable])
        )

        # Store facts in spec store (partial failures are fine — store what we have)
        for result in results:
            if result.facts and self.spec_store is not None:
                try:
                    self.spec_store.add_facts(result.facts)
                except Exception:
                    logger.exception(
                        "Failed to store facts from module %s", result.module
                    )

        # Aggregate statistics
        total_facts = sum(len(r.facts) for r in results)

        facts_by_module: dict[str, int] = {}
        facts_by_authority: dict[str, int] = {}
        facts_by_feature: dict[str, int] = {}
        all_errors: list[ReconError] = []
        failed_modules: list[str] = []

        for result in results:
            facts_by_module[result.module] = len(result.facts)
            if result.status == ReconModuleStatus.FAILED:
                failed_modules.append(result.module)
            for fact in result.facts:
                auth_key = str(fact.authority)
                facts_by_authority[auth_key] = facts_by_authority.get(auth_key, 0) + 1
                feat_key = fact.feature
                facts_by_feature[feat_key] = facts_by_feature.get(feat_key, 0) + 1
            all_errors.extend(result.errors)

        # Identify coverage gaps: features in scope with no authoritative facts
        coverage_gaps: list[str] = []
        scope_features = effective_scope.feature_keys() if effective_scope.resolved_features else []
        authoritative_key = str(Authority.AUTHORITATIVE)
        for feature in scope_features:
            # Check if any authoritative fact covers this feature
            feature_has_authoritative = False
            for result in results:
                for fact in result.facts:
                    if (
                        fact.feature == feature
                        and str(fact.authority) == authoritative_key
                    ):
                        feature_has_authoritative = True
                        break
                if feature_has_authoritative:
                    break
            if not feature_has_authoritative:
                coverage_gaps.append(feature)

        finished_at = datetime.now(UTC).isoformat()
        duration = time.monotonic() - t0

        return ReconReport(
            target=target,
            scope=effective_scope,
            results=results,
            total_facts=total_facts,
            facts_by_module=facts_by_module,
            facts_by_authority=facts_by_authority,
            facts_by_feature=facts_by_feature,
            coverage_gaps=coverage_gaps,
            errors=all_errors,
            duration_seconds=duration,
            run_id=effective_run_id,
            started_at=started_at,
            finished_at=finished_at,
            modules_skipped=skipped,
            modules_failed=failed_modules,
        )
