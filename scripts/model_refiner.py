"""
ModelRefiner — update DomainModel based on experiment results.

After ExperimentRunner returns results, ModelRefiner:
  - Marks validated hypotheses as validated=True
  - Updates or flags failed hypotheses for refinement
  - Increments the model iteration counter
"""

from __future__ import annotations

import logging

from scripts.domain_model import DomainModel, Experiment

logger = logging.getLogger(__name__)


class ModelRefiner:
    """Update a DomainModel based on experiment results."""

    def refine(self, model: DomainModel, experiments: list[Experiment]) -> DomainModel:
        """Update model based on experiment results. Returns the updated model."""
        for exp in experiments:
            if exp.passed:
                self._mark_validated(model, exp)
            else:
                self._update_hypothesis(model, exp)
        model.iteration += 1
        return model

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _mark_validated(self, model: DomainModel, exp: Experiment) -> None:
        """Mark the operation and any related hypotheses as validated."""
        entity = model.entities.get(exp.entity)
        if entity is None:
            logger.warning("Experiment %s references unknown entity %s", exp.id, exp.entity)
            return

        matched = False
        for op in entity.operations:
            if op.name == exp.operation:
                op.validated = True
                if exp.actual:
                    op.evidence.append(f"experiment {exp.id}: {exp.actual}")
                matched = True

        if matched:
            # Recompute entity-level confidence
            entity.confidence = entity.validation_score()
        else:
            logger.debug(
                "No operation %s found on entity %s for experiment %s",
                exp.operation,
                exp.entity,
                exp.id,
            )

    def _update_hypothesis(self, model: DomainModel, exp: Experiment) -> None:
        """Record failure evidence on the hypothesis; do not remove it."""
        entity = model.entities.get(exp.entity)
        if entity is None:
            return

        for op in entity.operations:
            if op.name == exp.operation:
                failure_note = f"experiment {exp.id} FAILED: {exp.actual}"
                if exp.error:
                    failure_note += f" (error: {exp.error})"
                if failure_note not in op.evidence:
                    op.evidence.append(failure_note)
                # If specific status code info is available, update response_status hypothesis
                evidence_dict = exp.evidence
                if isinstance(evidence_dict, dict) and "status_code" in evidence_dict:
                    actual_status = evidence_dict["status_code"]
                    if isinstance(actual_status, int):
                        op.response_status = actual_status
