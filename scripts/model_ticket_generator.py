"""
ModelTicketGenerator — generate implementation tickets from a validated DomainModel.

Each operation in the domain model becomes a ticket with:
  - Entity name and operation
  - API contract (method, path, request/response shape)
  - UI trigger and location
  - State machine constraints
  - Validation rules
  - Concrete acceptance criteria

Also generates one entity model ticket (e.g. Prisma schema) per entity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scripts.domain_model import DomainModel, EntityHypothesis, OperationHypothesis


@dataclass
class TicketSpec:
    """A single implementation ticket derived from a domain model operation."""

    title: str
    entity: str
    operation: str  # "create", "read", "update", "delete", or entity model ticket: "model"
    api_method: str
    api_endpoint: str
    required_fields: list[str]
    optional_fields: list[str]
    preconditions: list[str]
    postconditions: list[str]
    ui_trigger: str
    ui_location: str
    state_constraints: list[str]  # relevant state transitions
    validation_rules: list[str]  # field-level rules
    acceptance_criteria: list[str]
    evidence: list[str] = field(default_factory=list)
    validated: bool = False


class ModelTicketGenerator:
    """Generate implementation tickets from a DomainModel."""

    def generate_tickets(self, model: DomainModel) -> list[TicketSpec]:
        """Generate implementation tickets from the domain model."""
        tickets: list[TicketSpec] = []
        for entity in model.entities.values():
            # Entity model ticket first (Prisma schema / data layer)
            tickets.insert(0, self._entity_model_ticket(entity))
            # One ticket per operation
            for op in entity.operations:
                ticket = self._operation_to_ticket(entity, op)
                tickets.append(ticket)
        return tickets

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _entity_model_ticket(self, entity: EntityHypothesis) -> TicketSpec:
        """Generate a schema/model ticket for an entity (e.g. Prisma model)."""
        fields_summary = [
            f"{fname} ({fh.field_type}, {'required' if fh.required else 'optional'})"
            for fname, fh in entity.fields.items()
        ]
        ac = [
            f"{entity.name} model is defined with all discovered fields",
            "Field types match observed API responses",
        ]
        if entity.relationships:
            rel_desc = ", ".join(
                f"{r.relation_type} {r.to_entity}" for r in entity.relationships
            )
            ac.append(f"Relationships are implemented: {rel_desc}")
        if entity.states:
            ac.append(f"State column supports values: {', '.join(entity.states)}")

        return TicketSpec(
            title=f"Implement {entity.name} data model",
            entity=entity.name,
            operation="model",
            api_method="",
            api_endpoint=entity.api_prefix,
            required_fields=[
                fname for fname, fh in entity.fields.items() if fh.required
            ],
            optional_fields=[
                fname for fname, fh in entity.fields.items() if not fh.required
            ],
            preconditions=[],
            postconditions=[],
            ui_trigger="",
            ui_location="",
            state_constraints=[],
            validation_rules=fields_summary,
            acceptance_criteria=ac,
            evidence=entity.evidence,
            validated=entity.confidence >= 0.5,
        )

    def _operation_to_ticket(
        self, entity: EntityHypothesis, op: OperationHypothesis
    ) -> TicketSpec:
        """Convert an OperationHypothesis to a TicketSpec."""
        # Build state constraints from transitions triggered by this operation
        state_constraints = [
            f"{t.from_state} → {t.to_state} (reversible: {t.reversible})"
            for t in entity.transitions
            if t.operation == op.name
        ]

        # Build validation rules from required fields
        validation_rules = [
            f"{fname} is required"
            for fname in op.required_fields
        ]
        for fname in op.required_fields:
            fh = entity.fields.get(fname)
            if fh:
                if fh.min_length is not None:
                    validation_rules.append(
                        f"{fname} min length: {fh.min_length}"
                    )
                if fh.max_length is not None:
                    validation_rules.append(
                        f"{fname} max length: {fh.max_length}"
                    )
                if fh.enum_values:
                    validation_rules.append(
                        f"{fname} must be one of: {', '.join(fh.enum_values)}"
                    )

        # Acceptance criteria
        ac = [
            f"{op.method} {op.endpoint_pattern} returns {op.response_status}",
        ]
        if op.required_fields:
            ac.append(
                f"Request includes required fields: {', '.join(op.required_fields)}"
            )
        if op.postconditions:
            ac.extend(op.postconditions)
        if op.ui_trigger:
            ac.append(f"UI: {op.ui_trigger}")
        if not ac:
            ac.append(f"{entity.name} {op.name} operation completes successfully")

        return TicketSpec(
            title=f"Implement {entity.name} {op.name}",
            entity=entity.name,
            operation=op.name,
            api_method=op.method,
            api_endpoint=op.endpoint_pattern,
            required_fields=list(op.required_fields),
            optional_fields=list(op.optional_fields),
            preconditions=list(op.preconditions),
            postconditions=list(op.postconditions),
            ui_trigger=op.ui_trigger,
            ui_location=op.ui_location,
            state_constraints=state_constraints,
            validation_rules=validation_rules,
            acceptance_criteria=ac,
            evidence=list(op.evidence),
            validated=op.validated,
        )
