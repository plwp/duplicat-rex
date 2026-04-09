"""
ScientificRecon — orchestrates the observe → hypothesize → experiment → refine loop.

The loop:
  1. Passive observation via existing recon modules (produces Facts)
  2. Build initial hypothesis model from facts
  3. Iteratively run experiments and refine until confidence >= threshold or max_iterations
  4. Save model snapshots at each iteration
"""

from __future__ import annotations

import logging
from pathlib import Path

from scripts.domain_model import DomainModel
from scripts.experiment_runner import ExperimentRunner
from scripts.hypothesis_builder import HypothesisBuilder
from scripts.model_refiner import ModelRefiner
from scripts.models import Fact

logger = logging.getLogger(__name__)

_DEFAULT_CONFIDENCE_THRESHOLD = 0.9
_DEFAULT_MAX_ITERATIONS = 5
_DEFAULT_MAX_EXPERIMENTS = 50


class ScientificRecon:
    """Orchestrates observe → hypothesize → experiment → refine loop."""

    def __init__(
        self,
        target_url: str,
        auth_state_path: str | None = None,
        output_dir: Path | None = None,
        confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.target_url = target_url.rstrip("/")
        self.auth_state_path = auth_state_path
        self.output_dir = output_dir or Path(".")
        self.confidence_threshold = confidence_threshold
        self.headers = headers or {}

    async def run(
        self,
        facts: list[Fact],
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
        max_experiments: int = _DEFAULT_MAX_EXPERIMENTS,
    ) -> DomainModel:
        """
        Run the full scientific recon loop.

        Args:
            facts: Pre-collected facts from passive observation (recon modules).
            max_iterations: Maximum number of experiment-refine cycles.
            max_experiments: Maximum experiments per iteration.

        Returns:
            The refined DomainModel.
        """
        logger.info("ScientificRecon: building initial hypothesis from %d facts", len(facts))

        # Phase 2: Build initial hypothesis model
        model = HypothesisBuilder().build(facts, self.target_url)
        logger.info(
            "Initial model: %d entities, %d total hypotheses",
            len(model.entities),
            model.total_hypotheses(),
        )

        runner = ExperimentRunner(
            base_url=self.target_url,
            auth_state_path=self.auth_state_path,
            headers=self.headers,
        )
        refiner = ModelRefiner()

        # Phase 3: Experiment loop
        for i in range(max_iterations):
            confidence = model.overall_confidence()
            logger.info(
                "Iteration %d/%d: confidence=%.2f (threshold=%.2f)",
                i + 1,
                max_iterations,
                confidence,
                self.confidence_threshold,
            )

            if confidence >= self.confidence_threshold:
                logger.info("Confidence threshold reached — stopping early")
                break

            unvalidated_count = sum(
                len(e.unvalidated_operations()) for e in model.entities.values()
            )
            if unvalidated_count == 0:
                logger.info("No unvalidated operations remain — stopping early")
                break

            # Run experiments
            experiments = await runner.run_experiments(
                model, max_experiments=max_experiments
            )
            passed = sum(1 for e in experiments if e.passed)
            logger.info(
                "Iteration %d: ran %d experiments, %d passed",
                i + 1,
                len(experiments),
                passed,
            )

            # Refine model
            model = refiner.refine(model, experiments)

            # Save snapshot
            snapshot_path = self.output_dir / f"model_v{model.iteration}.json"
            model.save(snapshot_path)
            logger.info("Saved model snapshot to %s", snapshot_path)

        logger.info(
            "ScientificRecon complete: confidence=%.2f, %d/%d hypotheses validated",
            model.overall_confidence(),
            model.validated_hypotheses(),
            model.total_hypotheses(),
        )
        return model

    async def run_with_passive_observation(
        self,
        scope: list[str],
        spec_store: object | None = None,
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
        max_experiments: int = _DEFAULT_MAX_EXPERIMENTS,
    ) -> DomainModel:
        """
        Full pipeline: passive observation → hypothesis → experiment loop.

        This variant drives the recon modules to collect facts first,
        then calls run() with the resulting facts.

        Args:
            scope: List of feature names to include in observation.
            spec_store: SpecStore instance for persisting facts.
            max_iterations: Maximum experiment-refine iterations.
            max_experiments: Maximum experiments per iteration.
        """
        facts = await self._observe(scope, spec_store)
        return await self.run(facts, max_iterations=max_iterations, max_experiments=max_experiments)

    async def _observe(
        self, scope: list[str], spec_store: object | None = None
    ) -> list[Fact]:
        """
        Phase 1: passive observation via existing recon modules.

        Loads previously collected facts from the spec store if available,
        otherwise returns an empty list (caller must supply facts).
        """
        if spec_store is None:
            logger.warning(
                "No spec_store provided — returning empty facts list. "
                "Supply pre-collected facts via run() instead."
            )
            return []

        # Load facts from spec store
        try:
            facts: list[Fact] = list(spec_store.all_facts())  # type: ignore[attr-defined]
            if scope:
                facts = [f for f in facts if f.feature in scope]
            logger.info(
                "Loaded %d facts from spec_store (scope: %s)",
                len(facts),
                scope or "all",
            )
            return facts
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load facts from spec_store: %s", exc)
            return []
