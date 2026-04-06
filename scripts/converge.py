"""
ConvergenceOrchestrator — drives the clone-vs-target parity loop.

Design:
- Runs compare → gap_analyze → create_issues in a loop until a stop condition is met.
- Stop conditions: parity_achieved, max_iterations, budget_exhausted, no_improvement.
- Scope is frozen before the first iteration; mutations raise ValueError.
- Iteration history is logged to history_dir via GapAnalyzer.save_history.
- Cost is tracked per iteration; the orchestrator rejects running if a budget has
  already been exceeded when checked at the start of each loop.

Invariants:
    INV-CNV-001: scope is frozen before run() begins. Mutations after freeze raise ValueError.
    INV-CNV-002: ConvergenceReport.total_cost == sum(r.cost for r in report.iterations).
    INV-CNV-003: stop_reason is one of: "parity_achieved" | "max_iterations" |
                 "budget_exhausted" | "no_improvement" | "all_circuit_breaker".
    INV-CNV-004: iterations list is ordered by iteration number (1-indexed, ascending).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.gap_analyzer import Gap, GapAnalyzer, GapReport
from scripts.scope import Scope, freeze_scope

if TYPE_CHECKING:
    from scripts.compare import BehavioralComparator, ComparisonResult


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ConvergenceConfig:
    """Configuration for a single convergence run."""

    target_url: str
    clone_url: str
    scope: Scope
    max_iterations: int = 10
    target_parity: float = 95.0  # stop when parity >= this
    cost_budget: float | None = None  # USD limit; None = unlimited
    weights: dict[str, float] | None = None
    repo: str = ""  # "owner/repo" for issue creation; "" = skip
    max_issues_per_iteration: int = 10
    history_dir: Path = field(default_factory=lambda: Path("convergence_history"))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class IterationResult:
    """Summary of a single convergence iteration."""

    iteration: int
    parity_score: float
    gaps_found: int
    gaps_fixed: int  # gaps resolved compared to previous iteration
    new_issues_created: int
    cost: float
    duration_seconds: float


@dataclass
class ConvergenceReport:
    """Full report produced at the end of a convergence run."""

    iterations: list[IterationResult]
    final_parity: float
    stop_reason: str  # INV-CNV-003
    total_cost: float
    duration_seconds: float
    gaps_remaining: list[Gap]
    circuit_breaker_gaps: list[Gap]

    def format_summary(self) -> str:
        """Render a human-readable convergence summary."""
        lines: list[str] = []
        lines.append("=" * 70)
        lines.append("CONVERGENCE SUMMARY")
        lines.append("=" * 70)
        lines.append(f"Stop reason:    {self.stop_reason}")
        lines.append(f"Final parity:   {self.final_parity:.1f}%")
        lines.append(f"Iterations:     {len(self.iterations)}")
        lines.append(f"Total cost:     ${self.total_cost:.4f}")
        lines.append(f"Duration:       {self.duration_seconds:.1f}s")
        lines.append("")
        lines.append("Iteration log:")
        for it in self.iterations:
            lines.append(
                f"  [{it.iteration:2d}] parity={it.parity_score:5.1f}%  "
                f"gaps={it.gaps_found}  fixed={it.gaps_fixed}  "
                f"issues_created={it.new_issues_created}  "
                f"cost=${it.cost:.4f}  duration={it.duration_seconds:.1f}s"
            )
        lines.append("")
        if self.gaps_remaining:
            lines.append(f"Gaps remaining: {len(self.gaps_remaining)}")
            for gap in self.gaps_remaining[:10]:
                lines.append(f"  [{gap.severity}] {gap.id}")
        if self.circuit_breaker_gaps:
            lines.append(f"Circuit-breaker gaps: {len(self.circuit_breaker_gaps)}")
            for gap in self.circuit_breaker_gaps[:10]:
                lines.append(f"  [{gap.severity}] {gap.id} (iter_count={gap.iteration_count})")
        lines.append("=" * 70)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ConvergenceOrchestrator:
    """
    Drives the clone-vs-target parity loop.

    Usage:
        orchestrator = ConvergenceOrchestrator(spec_store, comparator, gap_analyzer, test_generator)
        report = await orchestrator.run(config)
        print(report.format_summary())
    """

    def __init__(
        self,
        spec_store: object,  # SpecStore — typed as object to avoid circular import
        comparator: BehavioralComparator,
        gap_analyzer: GapAnalyzer,
        test_generator: object | None = None,  # TestGenerator (optional)
    ) -> None:
        self.spec_store = spec_store
        self.comparator = comparator
        self.gap_analyzer = gap_analyzer
        self.test_generator = test_generator

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, config: ConvergenceConfig) -> ConvergenceReport:
        """
        Execute the convergence loop until a stop condition is met.

        Loop body (per iteration):
        1. Run comparator (compare clone vs target)
        2. Run gap analyzer (identify what's wrong)
        3. Check stop conditions — if any met, break
        4. Create issues for gaps (for CW to implement)
        5. [In production: invoke CW's /implement-wave here]
        6. Save iteration history, increment counter, loop

        Stop conditions (checked in order):
        - parity_score >= target_parity   → "parity_achieved"
        - iteration >= max_iterations     → "max_iterations"
        - cost >= cost_budget             → "budget_exhausted"
        - no improvement for 2 consecutive iterations → "no_improvement"
        - all remaining gaps are circuit-breaker → "all_circuit_breaker"

        REQUIRES: config.scope is unfrozen or already frozen — this method
                  will freeze it if it isn't already (INV-CNV-001).
        ENSURES:  report.total_cost == sum(r.cost for r in report.iterations) (INV-CNV-002).
        ENSURES:  report.stop_reason is one of the five valid values (INV-CNV-003).
        ENSURES:  report.iterations is ordered ascending by iteration number (INV-CNV-004).
        """
        # Freeze scope (INV-CNV-001)
        if not config.scope.frozen:
            freeze_scope(config.scope)

        run_start = time.monotonic()
        iterations: list[IterationResult] = []
        total_cost = 0.0
        previous_gap_report: GapReport | None = None
        previous_parity: float | None = None
        no_improvement_streak = 0

        stop_reason = "max_iterations"  # default

        for iteration_num in range(1, config.max_iterations + 1):
            iter_start = time.monotonic()

            # --- Step 1: Compare ---
            comparison = await self.comparator.compare(
                config.target_url,
                config.clone_url,
                scope=config.scope,
                weights=config.weights,
            )

            # --- Step 2: Gap analysis ---
            gap_report = self.gap_analyzer.analyze(
                comparison,
                config.scope,
                previous_report=previous_gap_report,
            )

            parity = comparison.parity_score
            gaps_found = len(gap_report.gaps)
            gaps_fixed = len(gap_report.resolved_gaps)

            # --- Step 3a: Check parity stop condition ---
            if parity >= config.target_parity:
                stop_reason = "parity_achieved"
                # Record this iteration before breaking
                iter_cost = _estimate_iteration_cost(comparison, gap_report)
                total_cost += iter_cost
                iterations.append(
                    IterationResult(
                        iteration=iteration_num,
                        parity_score=parity,
                        gaps_found=gaps_found,
                        gaps_fixed=gaps_fixed,
                        new_issues_created=0,
                        cost=iter_cost,
                        duration_seconds=time.monotonic() - iter_start,
                    )
                )
                self.gap_analyzer.save_history(gap_report, iteration_num)
                break

            # --- Step 3b: Check no-improvement stop condition ---
            if previous_parity is not None:
                if parity <= previous_parity:
                    no_improvement_streak += 1
                else:
                    no_improvement_streak = 0

                if no_improvement_streak >= 2:
                    stop_reason = "no_improvement"
                    iter_cost = _estimate_iteration_cost(comparison, gap_report)
                    total_cost += iter_cost
                    iterations.append(
                        IterationResult(
                            iteration=iteration_num,
                            parity_score=parity,
                            gaps_found=gaps_found,
                            gaps_fixed=gaps_fixed,
                            new_issues_created=0,
                            cost=iter_cost,
                            duration_seconds=time.monotonic() - iter_start,
                        )
                    )
                    self.gap_analyzer.save_history(gap_report, iteration_num)
                    break

            # --- Step 3c: Check circuit-breaker-only stop condition ---
            if gaps_found > 0 and len(gap_report.circuit_breaker_triggered) == gaps_found:
                stop_reason = "all_circuit_breaker"
                iter_cost = _estimate_iteration_cost(comparison, gap_report)
                total_cost += iter_cost
                iterations.append(
                    IterationResult(
                        iteration=iteration_num,
                        parity_score=parity,
                        gaps_found=gaps_found,
                        gaps_fixed=gaps_fixed,
                        new_issues_created=0,
                        cost=iter_cost,
                        duration_seconds=time.monotonic() - iter_start,
                    )
                )
                self.gap_analyzer.save_history(gap_report, iteration_num)
                break

            # --- Step 4: Create issues ---
            new_issues: list[str] = []
            if config.repo:
                new_issues = self.gap_analyzer.create_issues(
                    gap_report,
                    config.repo,
                    max_issues=config.max_issues_per_iteration,
                )

            iter_cost = _estimate_iteration_cost(comparison, gap_report)
            total_cost += iter_cost

            # --- Step 3d: Check budget stop condition ---
            budget_exhausted = (
                config.cost_budget is not None
                and total_cost >= config.cost_budget
            )

            # Record iteration
            iterations.append(
                IterationResult(
                    iteration=iteration_num,
                    parity_score=parity,
                    gaps_found=gaps_found,
                    gaps_fixed=gaps_fixed,
                    new_issues_created=len(new_issues),
                    cost=iter_cost,
                    duration_seconds=time.monotonic() - iter_start,
                )
            )

            # Save gap history
            self.gap_analyzer.save_history(gap_report, iteration_num)

            if budget_exhausted:
                stop_reason = "budget_exhausted"
                break

            # Update state for next iteration
            previous_gap_report = gap_report
            previous_parity = parity

            # --- Step 5: [Production hook] Invoke /implement-wave here ---
            # In production this would trigger CW's implement-wave to fix gaps.
            # In the current implementation we log the intent and loop.

        else:
            # Loop completed without break → max_iterations reached
            stop_reason = "max_iterations"

        # Build final report
        final_parity = iterations[-1].parity_score if iterations else 0.0
        final_gap_report = self.gap_analyzer.load_history(len(iterations))
        gaps_remaining: list[Gap] = []
        circuit_breaker_gaps: list[Gap] = []
        if final_gap_report is not None:
            gaps_remaining = final_gap_report.gaps
            circuit_breaker_gaps = final_gap_report.circuit_breaker_triggered

        # Verify INV-CNV-002: total_cost == sum of iteration costs
        assert abs(total_cost - sum(r.cost for r in iterations)) < 1e-9, (
            "INV-CNV-002 violated: total_cost does not match sum of iteration costs"
        )

        return ConvergenceReport(
            iterations=iterations,
            final_parity=final_parity,
            stop_reason=stop_reason,
            total_cost=total_cost,
            duration_seconds=time.monotonic() - run_start,
            gaps_remaining=gaps_remaining,
            circuit_breaker_gaps=circuit_breaker_gaps,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _estimate_iteration_cost(
    comparison: ComparisonResult,
    gap_report: GapReport,
) -> float:
    """
    Estimate the USD cost of one iteration.

    In production this would integrate with LLM pricing APIs.
    For now we use a simple proxy: $0.001 per test run + $0.0001 per gap analyzed.
    This gives a realistic-enough cost curve for budget tracking.
    """
    test_count = len(comparison.passed) + len(comparison.failed) + len(comparison.errors)
    return test_count * 0.001 + len(gap_report.gaps) * 0.0001
