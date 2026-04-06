"""
Tests for scripts/converge.py

Covers:
- ConvergenceConfig and dataclass defaults
- IterationResult and ConvergenceReport dataclasses
- ConvergenceOrchestrator.run:
    - stop condition: parity_achieved (parity >= target_parity)
    - stop condition: max_iterations (loop exhausted)
    - stop condition: budget_exhausted (cost >= cost_budget)
    - stop condition: no_improvement (parity stalls for 2 consecutive iterations)
    - stop condition: all_circuit_breaker (all remaining gaps are circuit-breaker)
    - iteration tracking (IterationResult list, 1-indexed, ordered)
    - scope freeze enforcement (unfrozen scope is frozen on entry)
    - scope freeze already frozen (no double-freeze error)
    - cost tracking (total_cost == sum of iteration costs, INV-CNV-002)
    - circuit breaker integration (circuit_breaker_gaps in report)
    - iteration history saved via GapAnalyzer.save_history
    - issue creation via GapAnalyzer.create_issues
- ConvergenceReport.format_summary
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from scripts.compare import ComparisonResult
from scripts.converge import (
    ConvergenceConfig,
    ConvergenceOrchestrator,
    ConvergenceReport,
    IterationResult,
    _estimate_iteration_cost,
)
from scripts.gap_analyzer import Gap, GapReport
from scripts.scope import Scope, ScopeFeature, freeze_scope

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def make_scope(features: list[str] = None, frozen: bool = False) -> Scope:
    """Build a simple Scope for testing."""
    scope = Scope(
        raw_input=", ".join(features or ["boards", "cards"]),
        features=[ScopeFeature(feature=f) for f in (features or ["boards", "cards"])],
        target="trello.com",
    )
    if frozen:
        freeze_scope(scope)
    return scope


def make_comparison(parity: float = 80.0, gaps: int = 2) -> ComparisonResult:
    """Build a ComparisonResult with the given parity and gap count."""
    from scripts.compare import TestDiff
    details = [
        TestDiff(
            test_name=f"test_boards_{i}",
            feature="boards",
            target_result="pass",
            clone_result="fail",
            diff=f"diff {i}",
        )
        for i in range(gaps)
    ]
    return ComparisonResult(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        parity_score=parity,
        feature_scores={"boards": parity},
        passed=["test_pass_0"],
        failed=[f"test_boards_{i}" for i in range(gaps)],
        errors=[],
        details=details,
    )


def make_gap(gap_id: str = "boards::test_boards_0", iteration_count: int = 1) -> Gap:
    return Gap(
        id=gap_id,
        feature="boards",
        severity="P2",
        category="broken",
        description="gap",
        test_name="test_boards_0",
        diff=None,
        related_spec_ids=[],
        related_fact_ids=[],
        iteration_count=iteration_count,
    )


def make_gap_report(gaps: list[Gap] | None = None, resolved: list[str] | None = None) -> GapReport:
    gaps = gaps or [make_gap()]
    circuit_breaker = [g for g in gaps if g.iteration_count >= 3]
    return GapReport(
        gaps=gaps,
        by_severity={"P1": 0, "P2": len(gaps), "P3": 0},
        by_feature={"boards": gaps},
        circuit_breaker_triggered=circuit_breaker,
        new_gaps=gaps,
        recurring_gaps=[],
        resolved_gaps=resolved or [],
    )


def make_orchestrator(
    parity_values: list[float] | None = None,
    gaps_per_iter: list[int] | None = None,
) -> tuple[ConvergenceOrchestrator, MagicMock, MagicMock]:
    """
    Build an orchestrator with mocked comparator and gap_analyzer.

    parity_values: list of parity scores to return on successive compare() calls
    gaps_per_iter: list of gap counts to return on successive analyze() calls
    """
    parity_values = parity_values or [80.0]
    gaps_per_iter = gaps_per_iter or [2] * len(parity_values)

    comparator = MagicMock()
    gap_analyzer = MagicMock()
    spec_store = MagicMock()

    compare_returns = [make_comparison(p, g) for p, g in zip(parity_values, gaps_per_iter)]
    gap_report_returns = [make_gap_report([make_gap() for _ in range(g)]) for g in gaps_per_iter]

    # AsyncMock for compare
    async def _compare(*args, **kwargs):
        return compare_returns.pop(0) if compare_returns else make_comparison(99.0, 0)

    comparator.compare = _compare

    # Sync mock for analyze
    analyze_call_count = [0]
    def _analyze(*args, **kwargs):
        idx = analyze_call_count[0]
        analyze_call_count[0] += 1
        return gap_report_returns[idx] if idx < len(gap_report_returns) else make_gap_report([])

    gap_analyzer.analyze.side_effect = _analyze
    gap_analyzer.create_issues.return_value = []
    gap_analyzer.save_history.return_value = Path("history/gap_report_iter_0001.json")
    gap_analyzer.load_history.return_value = None

    return ConvergenceOrchestrator(spec_store, comparator, gap_analyzer), comparator, gap_analyzer


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


def test_config_defaults():
    scope = make_scope()
    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
    )
    assert config.max_iterations == 10
    assert config.target_parity == 95.0
    assert config.cost_budget is None
    assert config.weights is None
    assert config.repo == ""
    assert config.max_issues_per_iteration == 10


# ---------------------------------------------------------------------------
# Scope freeze enforcement (INV-CNV-001)
# ---------------------------------------------------------------------------


def test_scope_is_frozen_on_entry_if_not_already():
    """Unfrozen scope must be frozen when run() begins."""
    scope = make_scope(frozen=False)
    assert not scope.frozen

    orchestrator, _, gap_analyzer = make_orchestrator(parity_values=[99.0], gaps_per_iter=[0])
    gap_analyzer.load_history.return_value = make_gap_report([])

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=1,
        target_parity=95.0,
    )
    asyncio.run(orchestrator.run(config))
    assert scope.frozen


def test_already_frozen_scope_is_not_double_frozen():
    """If scope is already frozen, run() must not raise."""
    scope = make_scope(frozen=True)
    assert scope.frozen

    orchestrator, _, gap_analyzer = make_orchestrator(parity_values=[99.0], gaps_per_iter=[0])
    gap_analyzer.load_history.return_value = make_gap_report([])

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=1,
        target_parity=95.0,
    )
    # Should not raise
    asyncio.run(orchestrator.run(config))
    assert scope.frozen


# ---------------------------------------------------------------------------
# Stop condition: parity_achieved
# ---------------------------------------------------------------------------


def test_stop_parity_achieved():
    """Stops on first iteration when parity >= target_parity."""
    scope = make_scope()
    orchestrator, _, gap_analyzer = make_orchestrator(parity_values=[97.0], gaps_per_iter=[0])
    gap_analyzer.load_history.return_value = make_gap_report([])

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=10,
        target_parity=95.0,
    )
    report = asyncio.run(orchestrator.run(config))

    assert report.stop_reason == "parity_achieved"
    assert len(report.iterations) == 1
    assert report.final_parity == 97.0


def test_stop_parity_achieved_exact():
    """Stops when parity exactly equals target_parity."""
    scope = make_scope()
    orchestrator, _, gap_analyzer = make_orchestrator(parity_values=[95.0], gaps_per_iter=[0])
    gap_analyzer.load_history.return_value = make_gap_report([])

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=10,
        target_parity=95.0,
    )
    report = asyncio.run(orchestrator.run(config))
    assert report.stop_reason == "parity_achieved"


# ---------------------------------------------------------------------------
# Stop condition: max_iterations
# ---------------------------------------------------------------------------


def test_stop_max_iterations():
    """Stops after max_iterations when parity is never achieved."""
    scope = make_scope()
    parity_values = [70.0, 75.0, 80.0]
    orchestrator, _, gap_analyzer = make_orchestrator(
        parity_values=parity_values,
        gaps_per_iter=[2, 2, 2],
    )
    gap_analyzer.load_history.return_value = make_gap_report()

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=3,
        target_parity=95.0,
    )
    report = asyncio.run(orchestrator.run(config))

    assert report.stop_reason == "max_iterations"
    assert len(report.iterations) == 3


def test_iterations_are_1_indexed_and_ordered():
    """Iteration list must be 1-indexed and in ascending order."""
    scope = make_scope()
    orchestrator, _, gap_analyzer = make_orchestrator(
        parity_values=[70.0, 75.0, 80.0],
        gaps_per_iter=[2, 2, 2],
    )
    gap_analyzer.load_history.return_value = make_gap_report()

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=3,
        target_parity=95.0,
    )
    report = asyncio.run(orchestrator.run(config))

    assert [r.iteration for r in report.iterations] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Stop condition: budget_exhausted
# ---------------------------------------------------------------------------


def test_stop_budget_exhausted():
    """Stops when accumulated cost >= cost_budget."""
    scope = make_scope()
    # Each iteration: 1 passed + 2 gaps = 1*0.001 + 2*0.0001 = 0.0012
    # Budget of 0.002 should exhaust after 2 iterations
    orchestrator, _, gap_analyzer = make_orchestrator(
        parity_values=[70.0, 75.0, 80.0, 85.0, 90.0],
        gaps_per_iter=[2, 2, 2, 2, 2],
    )
    gap_analyzer.load_history.return_value = make_gap_report()

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=10,
        target_parity=95.0,
        cost_budget=0.002,  # Very small budget
    )
    report = asyncio.run(orchestrator.run(config))

    assert report.stop_reason == "budget_exhausted"
    assert report.total_cost >= config.cost_budget


def test_budget_none_does_not_stop():
    """With no budget, loop runs until max_iterations."""
    scope = make_scope()
    orchestrator, _, gap_analyzer = make_orchestrator(
        parity_values=[70.0, 75.0],
        gaps_per_iter=[2, 2],
    )
    gap_analyzer.load_history.return_value = make_gap_report()

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=2,
        target_parity=95.0,
        cost_budget=None,
    )
    report = asyncio.run(orchestrator.run(config))

    assert report.stop_reason == "max_iterations"


# ---------------------------------------------------------------------------
# Stop condition: no_improvement
# ---------------------------------------------------------------------------


def test_stop_no_improvement():
    """Stops when parity stalls for 2 consecutive iterations."""
    scope = make_scope()
    # Parity goes up then stalls twice
    orchestrator, _, gap_analyzer = make_orchestrator(
        parity_values=[70.0, 75.0, 75.0, 75.0],
        gaps_per_iter=[2, 2, 2, 2],
    )
    gap_analyzer.load_history.return_value = make_gap_report()

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=10,
        target_parity=95.0,
    )
    report = asyncio.run(orchestrator.run(config))

    assert report.stop_reason == "no_improvement"
    # Should have stopped at iteration 4 (streak hit 2 at iter 3, stop at iter 4)
    assert len(report.iterations) <= 5


def test_no_improvement_resets_on_progress():
    """no_improvement streak resets when parity improves."""
    scope = make_scope()
    # Goes up, stalls once (streak=1), improves (streak reset), stalls twice -> stop
    orchestrator, _, gap_analyzer = make_orchestrator(
        parity_values=[70.0, 75.0, 75.0, 80.0, 80.0, 80.0],
        gaps_per_iter=[2, 2, 2, 2, 2, 2],
    )
    gap_analyzer.load_history.return_value = make_gap_report()

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=10,
        target_parity=95.0,
    )
    report = asyncio.run(orchestrator.run(config))

    assert report.stop_reason == "no_improvement"
    # Must have run more than 3 iterations (streak reset mid-run)
    assert len(report.iterations) >= 4


# ---------------------------------------------------------------------------
# Stop condition: all_circuit_breaker
# ---------------------------------------------------------------------------


def test_stop_all_circuit_breaker():
    """Stops when all remaining gaps have iteration_count >= 3."""
    scope = make_scope()

    # Build orchestrator manually with a gap_analyzer that returns
    # circuit-breaker-only gaps from iteration 1
    cb_gap = make_gap(iteration_count=3)
    cb_report = GapReport(
        gaps=[cb_gap],
        by_severity={"P1": 0, "P2": 1, "P3": 0},
        by_feature={"boards": [cb_gap]},
        circuit_breaker_triggered=[cb_gap],  # All gaps are CB
        new_gaps=[],
        recurring_gaps=[cb_gap],
        resolved_gaps=[],
    )

    comparator = MagicMock()
    async def _compare(*args, **kwargs):
        return make_comparison(80.0, 1)
    comparator.compare = _compare

    gap_analyzer = MagicMock()
    gap_analyzer.analyze.return_value = cb_report
    gap_analyzer.create_issues.return_value = []
    gap_analyzer.save_history.return_value = Path("history/x.json")
    gap_analyzer.load_history.return_value = cb_report

    spec_store = MagicMock()
    orchestrator = ConvergenceOrchestrator(spec_store, comparator, gap_analyzer)

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=10,
        target_parity=95.0,
    )
    report = asyncio.run(orchestrator.run(config))

    assert report.stop_reason == "all_circuit_breaker"
    assert len(report.circuit_breaker_gaps) == 1


# ---------------------------------------------------------------------------
# Cost tracking (INV-CNV-002)
# ---------------------------------------------------------------------------


def test_total_cost_equals_sum_of_iteration_costs():
    """total_cost must equal sum(r.cost for r in iterations) (INV-CNV-002)."""
    scope = make_scope()
    orchestrator, _, gap_analyzer = make_orchestrator(
        parity_values=[70.0, 75.0, 80.0],
        gaps_per_iter=[2, 2, 2],
    )
    gap_analyzer.load_history.return_value = make_gap_report()

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=3,
        target_parity=95.0,
    )
    report = asyncio.run(orchestrator.run(config))

    assert abs(report.total_cost - sum(r.cost for r in report.iterations)) < 1e-9


def test_cost_estimate_helper():
    """_estimate_iteration_cost is non-negative and scales with test/gap count."""
    comparison = make_comparison(parity=80.0, gaps=2)
    gap_report = make_gap_report([make_gap(), make_gap("boards::test_boards_1")])

    cost = _estimate_iteration_cost(comparison, gap_report)
    assert cost > 0.0

    # More tests = higher cost
    big_comparison = make_comparison(parity=80.0, gaps=10)
    big_report = make_gap_report([make_gap(f"boards::test_{i}") for i in range(10)])
    big_cost = _estimate_iteration_cost(big_comparison, big_report)
    assert big_cost > cost


# ---------------------------------------------------------------------------
# Iteration history logging
# ---------------------------------------------------------------------------


def test_save_history_called_each_iteration():
    """gap_analyzer.save_history must be called once per iteration."""
    scope = make_scope()
    orchestrator, _, gap_analyzer = make_orchestrator(
        parity_values=[70.0, 75.0, 80.0],
        gaps_per_iter=[2, 2, 2],
    )
    gap_analyzer.load_history.return_value = make_gap_report()

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=3,
        target_parity=95.0,
    )
    asyncio.run(orchestrator.run(config))

    assert gap_analyzer.save_history.call_count == 3


def test_save_history_iteration_numbers():
    """save_history is called with sequential 1-indexed iteration numbers."""
    scope = make_scope()
    orchestrator, _, gap_analyzer = make_orchestrator(
        parity_values=[70.0, 75.0],
        gaps_per_iter=[2, 2],
    )
    gap_analyzer.load_history.return_value = make_gap_report()

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=2,
        target_parity=95.0,
    )
    asyncio.run(orchestrator.run(config))

    saved_iter_numbers = [c.args[1] for c in gap_analyzer.save_history.call_args_list]
    assert saved_iter_numbers == [1, 2]


# ---------------------------------------------------------------------------
# Issue creation
# ---------------------------------------------------------------------------


def test_issues_created_when_repo_set():
    """create_issues is called with the configured repo."""
    scope = make_scope()
    orchestrator, _, gap_analyzer = make_orchestrator(
        parity_values=[70.0, 75.0],
        gaps_per_iter=[2, 2],
    )
    gap_analyzer.create_issues.return_value = ["https://github.com/plwp/clone/issues/1"]
    gap_analyzer.load_history.return_value = make_gap_report()

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=2,
        target_parity=95.0,
        repo="plwp/trello-clone",
    )
    report = asyncio.run(orchestrator.run(config))

    assert gap_analyzer.create_issues.called
    call_args = gap_analyzer.create_issues.call_args_list[0]
    assert call_args.args[1] == "plwp/trello-clone"
    # issues_created should be reflected in iterations
    assert report.iterations[0].new_issues_created == 1


def test_no_issues_when_repo_empty():
    """create_issues is not called when repo is empty string."""
    scope = make_scope()
    orchestrator, _, gap_analyzer = make_orchestrator(
        parity_values=[70.0],
        gaps_per_iter=[2],
    )
    gap_analyzer.load_history.return_value = make_gap_report()

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=1,
        target_parity=95.0,
        repo="",
    )
    asyncio.run(orchestrator.run(config))

    gap_analyzer.create_issues.assert_not_called()


# ---------------------------------------------------------------------------
# Circuit breaker integration
# ---------------------------------------------------------------------------


def test_circuit_breaker_gaps_in_report():
    """circuit_breaker_gaps in the report comes from the final gap report."""
    scope = make_scope()
    cb_gap = make_gap(iteration_count=3)
    final_report = GapReport(
        gaps=[cb_gap],
        by_severity={"P1": 0, "P2": 1, "P3": 0},
        by_feature={"boards": [cb_gap]},
        circuit_breaker_triggered=[cb_gap],
        new_gaps=[],
        recurring_gaps=[cb_gap],
        resolved_gaps=[],
    )

    orchestrator, _, gap_analyzer = make_orchestrator(
        parity_values=[70.0],
        gaps_per_iter=[1],
    )
    gap_analyzer.load_history.return_value = final_report

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=1,
        target_parity=95.0,
    )
    report = asyncio.run(orchestrator.run(config))

    assert len(report.circuit_breaker_gaps) == 1
    assert report.circuit_breaker_gaps[0].iteration_count == 3


# ---------------------------------------------------------------------------
# ConvergenceReport.format_summary
# ---------------------------------------------------------------------------


def test_format_summary_contains_key_fields():
    """format_summary output must contain stop_reason, final_parity, total_cost."""
    report = ConvergenceReport(
        iterations=[
            IterationResult(
                iteration=1,
                parity_score=97.5,
                gaps_found=0,
                gaps_fixed=2,
                new_issues_created=0,
                cost=0.003,
                duration_seconds=1.2,
            )
        ],
        final_parity=97.5,
        stop_reason="parity_achieved",
        total_cost=0.003,
        duration_seconds=1.2,
        gaps_remaining=[],
        circuit_breaker_gaps=[],
    )
    summary = report.format_summary()

    assert "parity_achieved" in summary
    assert "97.5" in summary
    assert "0.003" in summary or "$0.003" in summary
    assert "CONVERGENCE SUMMARY" in summary


def test_format_summary_lists_gaps_remaining():
    """format_summary includes gaps remaining when present."""
    gap = make_gap()
    report = ConvergenceReport(
        iterations=[
            IterationResult(
                iteration=1,
                parity_score=70.0,
                gaps_found=1,
                gaps_fixed=0,
                new_issues_created=0,
                cost=0.001,
                duration_seconds=0.5,
            )
        ],
        final_parity=70.0,
        stop_reason="max_iterations",
        total_cost=0.001,
        duration_seconds=0.5,
        gaps_remaining=[gap],
        circuit_breaker_gaps=[],
    )
    summary = report.format_summary()
    assert "boards::test_boards_0" in summary


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_single_iteration_with_zero_gaps():
    """Zero gaps on first iteration with parity below target still runs."""
    scope = make_scope()
    orchestrator, _, gap_analyzer = make_orchestrator(
        parity_values=[80.0],
        gaps_per_iter=[0],
    )
    gap_analyzer.load_history.return_value = make_gap_report([])

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=1,
        target_parity=95.0,
    )
    report = asyncio.run(orchestrator.run(config))
    # With max_iterations=1 and parity < target, should hit max_iterations
    assert report.stop_reason == "max_iterations"
    assert len(report.iterations) == 1


def test_decreasing_parity_triggers_no_improvement():
    """Decreasing parity counts as no improvement."""
    scope = make_scope()
    orchestrator, _, gap_analyzer = make_orchestrator(
        parity_values=[80.0, 75.0, 70.0],
        gaps_per_iter=[2, 2, 2],
    )
    gap_analyzer.load_history.return_value = make_gap_report()

    config = ConvergenceConfig(
        target_url="https://trello.com",
        clone_url="http://localhost:3000",
        scope=scope,
        max_iterations=10,
        target_parity=95.0,
    )
    report = asyncio.run(orchestrator.run(config))
    assert report.stop_reason == "no_improvement"
