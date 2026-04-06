"""
Recon module base classes and shared dataclasses.

All recon modules implement ReconModule (ABC) and return ReconResult.
The orchestrator calls run() and receives a standardised result — it never
needs to know module internals.

INV-020: run() MUST NOT raise — all errors are captured in ReconResult.errors.
INV-028: Secrets never appear in facts, logs, or progress messages.
INV-029: Modules never access the keychain directly. Credentials are injected
         by the orchestrator via ReconServices.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from scripts.models import Authority, Fact, Scope, SourceType

# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------


class ReconModuleStatus(StrEnum):
    SUCCESS = "success"  # All sources processed, facts produced
    PARTIAL = "partial"  # Some sources failed, usable facts extracted
    FAILED = "failed"  # Module could not produce any facts
    SKIPPED = "skipped"  # Module not applicable for this scope/target


# ---------------------------------------------------------------------------
# Request and services
# ---------------------------------------------------------------------------


@dataclass
class ReconRequest:
    """Everything a module needs to execute a recon pass."""

    run_id: str  # UUID for this recon run
    target: str  # Target URL or domain (e.g. "trello.com")
    base_url: str = ""  # Full base URL if different from target
    scope: Scope = field(default_factory=Scope)
    credential_refs: list[str] = field(default_factory=list)  # Keychain key names
    prior_snapshot_id: str | None = None  # Previous snapshot for delta recon
    checkpoint: dict[str, Any] | None = None  # Resume state from previous partial run
    budgets: dict[str, int] = field(default_factory=dict)  # time_seconds, max_pages, etc.
    module_config: dict[str, Any] = field(default_factory=dict)  # Module-specific settings


@dataclass
class ReconServices:
    """Shared services injected by the orchestrator."""

    spec_store: Any  # SpecStore instance
    credentials: dict[str, str]  # Pre-fetched credentials (keys from module.requires_credentials)
    artifact_store: Any  # For saving screenshots, HARs, transcripts
    http_client: Any  # Pre-configured HTTP client (respects rate limits)
    browser: Any | None  # Playwright browser instance (if needed)
    clock: Any = None  # For testable time (defaults to real UTC)

    # NOTE: No keychain accessor. Modules NEVER access the keychain directly.
    # The orchestrator pre-fetches credentials and injects them here (INV-029).


# ---------------------------------------------------------------------------
# Progress and error reporting
# ---------------------------------------------------------------------------


@dataclass
class ReconProgress:
    """Emitted by modules during execution for orchestrator UI."""

    run_id: str
    module: str
    phase: str  # "init" | "auth" | "discover" | "extract" | "persist" | "complete"
    message: str
    completed: int | None = None
    total: int | None = None
    feature: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class ReconError:
    """A non-fatal error encountered during recon."""

    source_url: str | None
    error_type: str  # "timeout" | "auth_required" | "rate_limited" | "parse_error"
    message: str
    recoverable: bool  # True if a retry might succeed


@dataclass
class CoverageEntry:
    """Per-feature coverage report from a module."""

    feature: str
    status: str = "not_found"  # "observed" | "inferred" | "blocked" | "not_found"
    fact_count: int = 0


@dataclass
class ReconResult:
    """Standard output from every recon module."""

    module: str
    status: ReconModuleStatus
    facts: list[Fact] = field(default_factory=list)
    errors: list[ReconError] = field(default_factory=list)
    coverage: list[CoverageEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None  # For resumable runs
    duration_seconds: float = 0.0
    urls_visited: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class ReconModule(ABC):
    """
    Abstract base class for all recon modules.

    Contract:
    - run() MUST return a ReconResult, even on total failure (status=FAILED, facts=[]).
    - run() MUST NOT raise exceptions — all errors are captured in ReconResult.errors.
      Only contract violations or unrecoverable bootstrap failures may raise.
    - run() MUST call progress_callback at meaningful intervals if provided.
    - Each Fact produced MUST have module_name set to self.name.
    - Each Fact produced MUST have authority matching this module's authority tier.
    - Progress events MUST have monotonic phase sequence so the orchestrator
      can stream and resume cleanly.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique module identifier, e.g. 'browser_explore', 'api_docs'."""
        ...

    @property
    @abstractmethod
    def authority(self) -> Authority:
        """The authority tier of facts produced by this module."""
        ...

    @property
    @abstractmethod
    def source_type(self) -> SourceType:
        """The source type for facts produced by this module."""
        ...

    @property
    @abstractmethod
    def requires_credentials(self) -> list[str]:
        """
        Short credential key names this module needs. Empty list if none required.
        E.g. ["target.trello-com.username", "target.trello-com.password"]

        The orchestrator prepends the service namespace (e.g. "duplicat-rex")
        when fetching from the keychain:
            get_secret("target.trello-com.username", service="duplicat-rex")

        Modules declare short names; the orchestrator owns the namespace.
        """
        ...

    @abstractmethod
    async def run(
        self,
        request: ReconRequest,
        services: ReconServices,
        progress: Callable[[ReconProgress], None] | None = None,
    ) -> ReconResult:
        """
        Execute recon against the target for the given scope.

        REQUIRES: request.run_id is set, request.scope is frozen.
        ENSURES: ReconResult.module == self.name.
        ENSURES: All facts have authority == self.authority.
        ENSURES: All facts have module_name == self.name.
        ENSURES: If status == SUCCESS, at least one fact is returned.
        ENSURES: If status == FAILED, facts is empty.
        ERROR CASES:
          - Auth failure  -> status=FAILED, errors=[ReconError(error_type="auth_required")]
          - Partial scrape -> status=PARTIAL, facts=[...partial...], errors=[...what failed...]
          - Rate limited  -> status=PARTIAL or FAILED,
                             errors=[ReconError(error_type="rate_limited")]
          - Network timeout -> status=FAILED, errors=[ReconError(error_type="timeout")]
        """
        ...

    async def validate_prerequisites(self) -> list[str]:
        """
        Check that module-specific dependencies are available.
        Returns list of missing prerequisites (empty = ready).
        """
        return []
