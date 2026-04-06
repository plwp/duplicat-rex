"""
Tests for scripts/recon/orchestrator.py — ReconOrchestrator.

Tests cover:
- Module discovery
- Credential scoping (INV-029)
- Concurrent execution with mock modules
- Partial failure handling
- Coverage gap detection
- Targeted module filter
- Targeted feature re-run
- Statistics aggregation
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

from scripts.models import (
    Authority,
    Confidence,
    EvidenceRef,
    Fact,
    FactCategory,
    Scope,
    ScopeNode,
    SourceType,
)
from scripts.recon.base import (
    ReconError,
    ReconModule,
    ReconModuleStatus,
    ReconProgress,
    ReconRequest,
    ReconResult,
    ReconServices,
)
from scripts.recon.orchestrator import ReconOrchestrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fact(
    feature: str = "boards",
    authority: Authority = Authority.AUTHORITATIVE,
    module_name: str = "mock_module",
    run_id: str = "test-run-id",
) -> Fact:
    return Fact(
        feature=feature,
        category=FactCategory.UI_COMPONENT,
        claim=f"The {feature} feature exists.",
        evidence=[EvidenceRef(source_url="https://example.com", locator="body")],
        source_type=SourceType.LIVE_APP,
        module_name=module_name,
        authority=authority,
        confidence=Confidence.HIGH,
        run_id=run_id,
    )


def _make_scope(features: list[str] = None) -> Scope:
    if features is None:
        features = ["boards", "cards"]
    nodes = [ScopeNode(feature=f) for f in features]
    return Scope(
        target="trello.com",
        raw_input=", ".join(features),
        resolved_features=nodes,
        requested_features=nodes,
    )


class MockModule(ReconModule):
    """A configurable mock ReconModule for testing."""

    def __init__(
        self,
        name: str = "mock_module",
        authority: Authority = Authority.AUTHORITATIVE,
        requires_creds: list[str] | None = None,
        facts: list[Fact] | None = None,
        errors: list[ReconError] | None = None,
        status: ReconModuleStatus = ReconModuleStatus.SUCCESS,
        missing_prereqs: list[str] | None = None,
        raise_on_run: bool = False,
    ) -> None:
        self._name = name
        self._authority = authority
        self._requires_creds = requires_creds or []
        self._facts = facts or []
        self._errors = errors or []
        self._status = status
        self._missing_prereqs = missing_prereqs or []
        self._raise_on_run = raise_on_run
        self.run_calls: list[tuple[ReconRequest, ReconServices]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def authority(self) -> Authority:
        return self._authority

    @property
    def source_type(self) -> SourceType:
        return SourceType.LIVE_APP

    @property
    def requires_credentials(self) -> list[str]:
        return self._requires_creds

    async def validate_prerequisites(self) -> list[str]:
        return self._missing_prereqs

    async def run(
        self,
        request: ReconRequest,
        services: ReconServices,
        progress: Callable[[ReconProgress], None] | None = None,
    ) -> ReconResult:
        self.run_calls.append((request, services))
        if self._raise_on_run:
            raise RuntimeError("Unexpected module failure")
        return ReconResult(
            module=self._name,
            status=self._status,
            facts=self._facts,
            errors=self._errors,
        )


def _make_keychain(creds: dict[str, str] | None = None) -> MagicMock:
    """Return a mock keychain that returns secrets from the supplied dict."""
    kc = MagicMock()
    creds = creds or {}
    kc.get_secret.side_effect = lambda key, **_kwargs: creds.get(key)
    return kc


def _make_orchestrator(
    modules: list[ReconModule] | None = None,
    creds: dict[str, str] | None = None,
) -> tuple[ReconOrchestrator, MagicMock]:
    """Build an orchestrator with a mock spec store and patched discover_modules."""
    store = MagicMock()
    store.add_facts.return_value = []
    kc = _make_keychain(creds)
    orch = ReconOrchestrator(spec_store=store, keychain=kc)
    if modules is not None:
        orch.discover_modules = MagicMock(return_value=modules)
    return orch, store


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Module discovery
# ---------------------------------------------------------------------------


class TestDiscoverModules:
    def test_discover_returns_concrete_modules(self) -> None:
        """discover_modules() must return at least one concrete ReconModule."""
        orch = ReconOrchestrator(spec_store=None, keychain=_make_keychain())
        modules = orch.discover_modules()
        assert len(modules) > 0
        for mod in modules:
            assert isinstance(mod, ReconModule)

    def test_discover_skips_base_and_orchestrator(self) -> None:
        """base.py and orchestrator.py must not appear in discovered modules."""
        orch = ReconOrchestrator(spec_store=None, keychain=_make_keychain())
        modules = orch.discover_modules()
        names = [m.name for m in modules]
        # The base module name would be from ReconModule ABC — can't be instantiated
        # Orchestrator has no ReconModule subclass
        assert "base" not in names
        assert "orchestrator" not in names

    def test_discover_no_duplicates(self) -> None:
        """Each module class must appear at most once."""
        orch = ReconOrchestrator(spec_store=None, keychain=_make_keychain())
        modules = orch.discover_modules()
        names = [m.name for m in modules]
        assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# Credential scoping (INV-029)
# ---------------------------------------------------------------------------


class TestCredentialScoping:
    def test_only_declared_credentials_passed(self) -> None:
        """Only credentials declared in requires_credentials reach the module."""
        mod = MockModule(
            name="cred_mod",
            requires_creds=["target.trello-com.api-key"],
            facts=[_make_fact(module_name="cred_mod")],
        )
        kc_creds = {
            "target.trello-com.api-key": "secret-key",
            "target.trello-com.password": "secret-pass",
            "ANTHROPIC_API_KEY": "ai-key",
        }
        orch, _store = _make_orchestrator(modules=[mod], creds=kc_creds)

        _run(orch.run("trello.com", _make_scope()))

        assert len(mod.run_calls) == 1
        _req, services = mod.run_calls[0]
        # Only the declared key must be present
        assert services.credentials == {"target.trello-com.api-key": "secret-key"}
        assert "target.trello-com.password" not in services.credentials
        assert "ANTHROPIC_API_KEY" not in services.credentials

    def test_missing_credential_not_in_dict(self) -> None:
        """If a declared credential is not in the keychain, it is absent from dict."""
        mod = MockModule(
            name="cred_mod2",
            requires_creds=["target.trello-com.api-key", "target.trello-com.password"],
            facts=[_make_fact(module_name="cred_mod2")],
        )
        # Only api-key is stored, password is missing
        kc_creds = {"target.trello-com.api-key": "secret-key"}
        orch, _store = _make_orchestrator(modules=[mod], creds=kc_creds)

        _run(orch.run("trello.com", _make_scope()))

        _req, services = mod.run_calls[0]
        assert "target.trello-com.api-key" in services.credentials
        assert "target.trello-com.password" not in services.credentials

    def test_module_with_no_credentials(self) -> None:
        """Modules with empty requires_credentials get an empty credentials dict."""
        mod = MockModule(
            name="no_cred_mod",
            requires_creds=[],
            facts=[_make_fact(module_name="no_cred_mod")],
        )
        orch, _store = _make_orchestrator(modules=[mod])
        _run(orch.run("trello.com", _make_scope()))

        _req, services = mod.run_calls[0]
        assert services.credentials == {}


# ---------------------------------------------------------------------------
# Concurrent execution
# ---------------------------------------------------------------------------


class TestConcurrentExecution:
    def test_all_modules_run(self) -> None:
        """All discovered modules are called when no filter is applied."""
        mods = [
            MockModule(name="m1", facts=[_make_fact(module_name="m1")]),
            MockModule(name="m2", facts=[_make_fact(module_name="m2")]),
            MockModule(name="m3", facts=[_make_fact(module_name="m3")]),
        ]
        orch, _store = _make_orchestrator(modules=mods)
        report = _run(orch.run("trello.com", _make_scope()))

        assert len(report.results) == 3
        ran_names = {r.module for r in report.results}
        assert ran_names == {"m1", "m2", "m3"}

    def test_max_concurrent_respected(self) -> None:
        """max_concurrent=1 must still produce results for all modules."""
        mods = [
            MockModule(name=f"mod{i}", facts=[_make_fact(module_name=f"mod{i}")])
            for i in range(4)
        ]
        orch, _store = _make_orchestrator(modules=mods)
        report = _run(orch.run("trello.com", _make_scope(), max_concurrent=1))

        assert len(report.results) == 4

    def test_run_id_consistent_across_modules(self) -> None:
        """All ReconRequests must share the same run_id (INV-038)."""
        mods = [
            MockModule(name="r1", facts=[_make_fact(module_name="r1")]),
            MockModule(name="r2", facts=[_make_fact(module_name="r2")]),
        ]
        orch, _store = _make_orchestrator(modules=mods)
        fixed_run_id = str(uuid.uuid4())
        _run(orch.run("trello.com", _make_scope(), run_id=fixed_run_id))

        for mod in mods:
            for req, _ in mod.run_calls:
                assert req.run_id == fixed_run_id


# ---------------------------------------------------------------------------
# Partial failure handling
# ---------------------------------------------------------------------------


class TestPartialFailure:
    def test_one_failed_module_does_not_block_others(self) -> None:
        """If one module fails, other modules still run and store their facts."""
        good = MockModule(
            name="good_mod",
            facts=[_make_fact(module_name="good_mod")],
        )
        bad = MockModule(
            name="bad_mod",
            status=ReconModuleStatus.FAILED,
            errors=[
                ReconError(
                    source_url=None, error_type="timeout", message="Timed out", recoverable=True
                )
            ],
        )
        orch, store = _make_orchestrator(modules=[good, bad])
        report = _run(orch.run("trello.com", _make_scope()))

        assert report.total_facts == 1
        assert len(report.results) == 2
        assert report.modules_failed == ["bad_mod"]
        # Good module's facts were stored
        store.add_facts.assert_called_once()

    def test_module_that_raises_is_captured(self) -> None:
        """A module that raises unexpectedly produces FAILED result — no re-raise."""
        exploding = MockModule(name="exploding_mod", raise_on_run=True)
        good = MockModule(
            name="good_mod",
            facts=[_make_fact(module_name="good_mod")],
        )
        orch, _store = _make_orchestrator(modules=[exploding, good])
        report = _run(orch.run("trello.com", _make_scope()))

        failed_names = [r.module for r in report.results if r.status == ReconModuleStatus.FAILED]
        assert "exploding_mod" in failed_names
        assert report.total_facts == 1

    def test_module_errors_aggregated(self) -> None:
        """Errors from all modules are collected in ReconReport.errors."""
        e1 = ReconError(
            source_url="https://a.com", error_type="timeout", message="A", recoverable=True
        )
        e2 = ReconError(
            source_url="https://b.com", error_type="parse_error", message="B", recoverable=False
        )
        m1 = MockModule(
            name="m1", errors=[e1], status=ReconModuleStatus.PARTIAL, facts=[_make_fact()]
        )
        m2 = MockModule(
            name="m2", errors=[e2], status=ReconModuleStatus.PARTIAL, facts=[_make_fact()]
        )
        orch, _store = _make_orchestrator(modules=[m1, m2])
        report = _run(orch.run("trello.com", _make_scope()))

        assert len(report.errors) == 2


# ---------------------------------------------------------------------------
# Prerequisite validation
# ---------------------------------------------------------------------------


class TestPrerequisiteValidation:
    def test_module_with_missing_prereqs_is_skipped(self) -> None:
        """Modules that fail validate_prerequisites() are skipped."""
        unavailable = MockModule(
            name="needs_playwright",
            missing_prereqs=["playwright not installed"],
        )
        good = MockModule(
            name="good_mod",
            facts=[_make_fact(module_name="good_mod")],
        )
        orch, _store = _make_orchestrator(modules=[unavailable, good])
        report = _run(orch.run("trello.com", _make_scope()))

        assert "needs_playwright" in report.modules_skipped
        assert len(unavailable.run_calls) == 0
        assert any(r.module == "good_mod" for r in report.results)


# ---------------------------------------------------------------------------
# Coverage gap detection
# ---------------------------------------------------------------------------


class TestCoverageGaps:
    def test_no_gaps_when_all_features_covered(self) -> None:
        """No coverage gaps when every scope feature has an authoritative fact."""
        scope = _make_scope(["boards", "cards"])
        facts = [
            _make_fact(feature="boards", authority=Authority.AUTHORITATIVE, module_name="m"),
            _make_fact(feature="cards", authority=Authority.AUTHORITATIVE, module_name="m"),
        ]
        mod = MockModule(name="m", facts=facts)
        orch, _store = _make_orchestrator(modules=[mod])
        report = _run(orch.run("trello.com", scope))

        assert report.coverage_gaps == []

    def test_gap_when_feature_has_only_anecdotal_facts(self) -> None:
        """A feature with only ANECDOTAL facts is a coverage gap."""
        scope = _make_scope(["boards", "cards"])
        facts = [
            _make_fact(feature="boards", authority=Authority.AUTHORITATIVE, module_name="m"),
            _make_fact(feature="cards", authority=Authority.ANECDOTAL, module_name="m"),
        ]
        mod = MockModule(name="m", facts=facts)
        orch, _store = _make_orchestrator(modules=[mod])
        report = _run(orch.run("trello.com", scope))

        assert "cards" in report.coverage_gaps
        assert "boards" not in report.coverage_gaps

    def test_gap_when_feature_has_no_facts(self) -> None:
        """A feature with zero facts is a coverage gap."""
        scope = _make_scope(["boards", "labels"])
        facts = [_make_fact(feature="boards", authority=Authority.AUTHORITATIVE, module_name="m")]
        mod = MockModule(name="m", facts=facts)
        orch, _store = _make_orchestrator(modules=[mod])
        report = _run(orch.run("trello.com", scope))

        assert "labels" in report.coverage_gaps
        assert "boards" not in report.coverage_gaps

    def test_observational_fact_is_still_a_gap(self) -> None:
        """Only AUTHORITATIVE facts close coverage gaps."""
        scope = _make_scope(["drag-drop"])
        facts = [
            _make_fact(feature="drag-drop", authority=Authority.OBSERVATIONAL, module_name="m"),
        ]
        mod = MockModule(name="m", facts=facts)
        orch, _store = _make_orchestrator(modules=[mod])
        report = _run(orch.run("trello.com", scope))

        assert "drag-drop" in report.coverage_gaps


# ---------------------------------------------------------------------------
# Targeted module filter
# ---------------------------------------------------------------------------


class TestTargetedModuleFilter:
    def test_only_specified_modules_run(self) -> None:
        """With modules= filter, only the named modules execute."""
        m1 = MockModule(name="api_docs", facts=[_make_fact(module_name="api_docs")])
        m2 = MockModule(name="browser_explore", facts=[_make_fact(module_name="browser_explore")])
        m3 = MockModule(name="marketing", facts=[_make_fact(module_name="marketing")])
        orch, _store = _make_orchestrator(modules=[m1, m2, m3])

        report = _run(orch.run("trello.com", _make_scope(), modules=["api_docs", "marketing"]))

        ran_names = {r.module for r in report.results}
        assert "api_docs" in ran_names
        assert "marketing" in ran_names
        assert "browser_explore" not in ran_names
        assert len(m2.run_calls) == 0

    def test_unknown_module_filter_runs_nothing(self) -> None:
        """Filtering to a module name that doesn't exist produces zero results."""
        mod = MockModule(name="api_docs", facts=[_make_fact()])
        orch, _store = _make_orchestrator(modules=[mod])

        report = _run(orch.run("trello.com", _make_scope(), modules=["nonexistent"]))

        assert len(report.results) == 0
        assert report.total_facts == 0


# ---------------------------------------------------------------------------
# Targeted feature re-run
# ---------------------------------------------------------------------------


class TestTargetedFeatureRerun:
    def test_feature_filter_narrows_scope(self) -> None:
        """With features= filter, the scope passed to modules is narrowed."""
        scope = _make_scope(["boards", "cards", "labels"])
        mod = MockModule(
            name="m",
            facts=[
                _make_fact(feature="labels", module_name="m"),
            ],
        )
        orch, _store = _make_orchestrator(modules=[mod])

        _run(orch.run("trello.com", scope, features=["labels"]))

        # Module ran
        assert len(mod.run_calls) == 1
        req, _ = mod.run_calls[0]
        # Scope passed to module should only contain "labels"
        feature_keys = req.scope.feature_keys()
        assert feature_keys == ["labels"]

    def test_feature_filter_falls_back_when_no_match(self) -> None:
        """If none of the scope features match, the full scope is used (no empty scope)."""
        scope = _make_scope(["boards", "cards"])
        mod = MockModule(name="m", facts=[_make_fact()])
        orch, _store = _make_orchestrator(modules=[mod])

        _run(orch.run("trello.com", scope, features=["nonexistent-feature"]))

        req, _ = mod.run_calls[0]
        # Falls back to full scope
        assert len(req.scope.resolved_features) == 2


# ---------------------------------------------------------------------------
# Statistics aggregation
# ---------------------------------------------------------------------------


class TestStatisticsAggregation:
    def test_total_facts_sums_all_modules(self) -> None:
        m1 = MockModule(name="m1", facts=[_make_fact(module_name="m1")] * 3)
        m2 = MockModule(name="m2", facts=[_make_fact(module_name="m2")] * 5)
        orch, _store = _make_orchestrator(modules=[m1, m2])
        report = _run(orch.run("trello.com", _make_scope()))

        assert report.total_facts == 8

    def test_facts_by_module(self) -> None:
        m1 = MockModule(name="m1", facts=[_make_fact(module_name="m1")] * 2)
        m2 = MockModule(name="m2", facts=[_make_fact(module_name="m2")] * 4)
        orch, _store = _make_orchestrator(modules=[m1, m2])
        report = _run(orch.run("trello.com", _make_scope()))

        assert report.facts_by_module["m1"] == 2
        assert report.facts_by_module["m2"] == 4

    def test_facts_by_authority(self) -> None:
        auth_fact = _make_fact(authority=Authority.AUTHORITATIVE)
        obs_fact = _make_fact(authority=Authority.OBSERVATIONAL)
        anec_fact = _make_fact(authority=Authority.ANECDOTAL)
        mod = MockModule(name="m", facts=[auth_fact, auth_fact, obs_fact, anec_fact])
        orch, _store = _make_orchestrator(modules=[mod])
        report = _run(orch.run("trello.com", _make_scope()))

        assert report.facts_by_authority.get("authoritative", 0) == 2
        assert report.facts_by_authority.get("observational", 0) == 1
        assert report.facts_by_authority.get("anecdotal", 0) == 1

    def test_facts_by_feature(self) -> None:
        scope = _make_scope(["boards", "cards"])
        boards_facts = [_make_fact(feature="boards")] * 3
        cards_facts = [_make_fact(feature="cards")] * 2
        mod = MockModule(name="m", facts=boards_facts + cards_facts)
        orch, _store = _make_orchestrator(modules=[mod])
        report = _run(orch.run("trello.com", scope))

        assert report.facts_by_feature["boards"] == 3
        assert report.facts_by_feature["cards"] == 2

    def test_duration_is_positive(self) -> None:
        mod = MockModule(name="m", facts=[_make_fact()])
        orch, _store = _make_orchestrator(modules=[mod])
        report = _run(orch.run("trello.com", _make_scope()))
        assert report.duration_seconds > 0

    def test_report_has_run_id(self) -> None:
        mod = MockModule(name="m", facts=[_make_fact()])
        orch, _store = _make_orchestrator(modules=[mod])
        fixed_id = "my-run-id"
        report = _run(orch.run("trello.com", _make_scope(), run_id=fixed_id))
        assert report.run_id == fixed_id

    def test_facts_stored_in_spec_store(self) -> None:
        """All facts from successful modules are persisted in the spec store."""
        facts = [_make_fact(module_name="m")]
        mod = MockModule(name="m", facts=facts)
        orch, store = _make_orchestrator(modules=[mod])
        _run(orch.run("trello.com", _make_scope()))

        store.add_facts.assert_called_once_with(facts)
