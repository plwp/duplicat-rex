"""Domain model data structures for hypothesis-driven recon."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class FieldType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATETIME = "datetime"
    ENUM = "enum"
    RELATION = "relation"
    UNKNOWN = "unknown"


@dataclass
class FieldHypothesis:
    name: str
    field_type: FieldType = FieldType.UNKNOWN
    required: bool = False
    unique: bool = False
    default: Any = None
    enum_values: list[str] = field(default_factory=list)
    min_length: int | None = None
    max_length: int | None = None
    related_entity: str | None = None  # for RELATION type
    evidence: list[str] = field(default_factory=list)  # how we know this
    validated: bool = False


@dataclass
class StateTransition:
    from_state: str
    to_state: str
    operation: str  # which operation triggers this
    reversible: bool = False
    validated: bool = False


@dataclass
class OperationHypothesis:
    name: str  # e.g. "create", "archive", "move"
    method: str = "POST"
    endpoint_pattern: str = ""  # e.g. "/api/boards" or "/api/boards/{boardId}"
    required_fields: list[str] = field(default_factory=list)
    optional_fields: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    postconditions: list[str] = field(default_factory=list)
    ui_trigger: str = ""  # e.g. "Click 'Create board' button"
    ui_location: str = ""  # e.g. "/boards page, create board modal"
    response_status: int = 200
    error_cases: list[dict[str, Any]] = field(default_factory=list)
    validated: bool = False
    evidence: list[str] = field(default_factory=list)


@dataclass
class RelationshipHypothesis:
    from_entity: str
    to_entity: str
    relation_type: str  # "belongs_to", "has_many", "many_to_many"
    foreign_key: str = ""  # e.g. "boardId"
    cascade_delete: bool = False
    validated: bool = False


@dataclass
class EntityHypothesis:
    name: str  # e.g. "Board"
    plural: str = ""  # e.g. "boards"
    api_prefix: str = ""  # e.g. "/api/boards" or "/1/boards"
    fields: dict[str, FieldHypothesis] = field(default_factory=dict)
    states: list[str] = field(default_factory=list)
    transitions: list[StateTransition] = field(default_factory=list)
    operations: list[OperationHypothesis] = field(default_factory=list)
    relationships: list[RelationshipHypothesis] = field(default_factory=list)
    confidence: float = 0.0  # 0-1 based on validation
    evidence: list[str] = field(default_factory=list)

    def unvalidated_operations(self) -> list[OperationHypothesis]:
        return [op for op in self.operations if not op.validated]

    def validation_score(self) -> float:
        """Fraction of hypotheses that have been experimentally validated."""
        total = len(self.operations) + len(self.transitions) + len(self.fields)
        if total == 0:
            return 0.0
        validated = (
            sum(1 for op in self.operations if op.validated)
            + sum(1 for t in self.transitions if t.validated)
            + sum(1 for f in self.fields.values() if f.validated)
        )
        return validated / total


@dataclass
class Experiment:
    id: str
    entity: str
    hypothesis: str  # what we're testing
    operation: str  # which operation
    script: str  # Playwright script to run (Python code)
    expected: str  # what should happen
    actual: str = ""  # what actually happened (filled after execution)
    passed: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)  # screenshots, API responses
    error: str = ""


@dataclass
class DomainModel:
    """The complete domain model hypothesis."""

    target: str  # e.g. "trello.com"
    entities: dict[str, EntityHypothesis] = field(default_factory=dict)
    version: int = 1
    iteration: int = 0  # how many refinement cycles

    def total_hypotheses(self) -> int:
        return sum(
            len(e.operations) + len(e.transitions) + len(e.fields)
            for e in self.entities.values()
        )

    def validated_hypotheses(self) -> int:
        return sum(
            sum(1 for op in e.operations if op.validated)
            + sum(1 for t in e.transitions if t.validated)
            + sum(1 for f in e.fields.values() if f.validated)
            for e in self.entities.values()
        )

    def overall_confidence(self) -> float:
        total = self.total_hypotheses()
        return self.validated_hypotheses() / total if total > 0 else 0.0

    def save(self, path: Path) -> None:
        """Serialize to JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self._to_dict()
        path.write_text(json.dumps(data, indent=2, default=str))

    def _to_dict(self) -> dict[str, Any]:
        def convert(obj: Any) -> Any:
            if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                return {k: convert(v) for k, v in dataclasses.asdict(obj).items()}
            if isinstance(obj, list):
                return [convert(i) for i in obj]
            if isinstance(obj, dict):
                return {k: convert(v) for k, v in obj.items()}
            return obj

        return convert(self)

    @classmethod
    def load(cls, path: Path) -> DomainModel:
        """Deserialize from JSON."""
        data = json.loads(path.read_text())
        model = cls(
            target=data["target"],
            version=data.get("version", 1),
            iteration=data.get("iteration", 0),
        )
        for name, edata in data.get("entities", {}).items():
            entity = EntityHypothesis(
                name=name,
                plural=edata.get("plural", ""),
                api_prefix=edata.get("api_prefix", ""),
            )
            entity.states = edata.get("states", [])
            entity.confidence = edata.get("confidence", 0.0)
            entity.evidence = edata.get("evidence", [])
            for fname, fdata in edata.get("fields", {}).items():
                entity.fields[fname] = FieldHypothesis(
                    name=fname,
                    field_type=FieldType(fdata.get("field_type", "unknown")),
                    required=fdata.get("required", False),
                    unique=fdata.get("unique", False),
                    validated=fdata.get("validated", False),
                    evidence=fdata.get("evidence", []),
                )
            for opdata in edata.get("operations", []):
                entity.operations.append(
                    OperationHypothesis(
                        name=opdata["name"],
                        method=opdata.get("method", "POST"),
                        endpoint_pattern=opdata.get("endpoint_pattern", ""),
                        required_fields=opdata.get("required_fields", []),
                        optional_fields=opdata.get("optional_fields", []),
                        preconditions=opdata.get("preconditions", []),
                        postconditions=opdata.get("postconditions", []),
                        ui_trigger=opdata.get("ui_trigger", ""),
                        validated=opdata.get("validated", False),
                        evidence=opdata.get("evidence", []),
                    )
                )
            for tdata in edata.get("transitions", []):
                entity.transitions.append(
                    StateTransition(
                        from_state=tdata["from_state"],
                        to_state=tdata["to_state"],
                        operation=tdata["operation"],
                        reversible=tdata.get("reversible", False),
                        validated=tdata.get("validated", False),
                    )
                )
            for rdata in edata.get("relationships", []):
                entity.relationships.append(
                    RelationshipHypothesis(
                        from_entity=rdata["from_entity"],
                        to_entity=rdata["to_entity"],
                        relation_type=rdata["relation_type"],
                        foreign_key=rdata.get("foreign_key", ""),
                        cascade_delete=rdata.get("cascade_delete", False),
                        validated=rdata.get("validated", False),
                    )
                )
            model.entities[name] = entity
        return model
