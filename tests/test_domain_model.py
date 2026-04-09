"""
Tests for the scientific recon domain model components.

Covers:
  - DomainModel serialization roundtrip (save/load)
  - EntityHypothesis.validation_score()
  - HypothesisBuilder entity extraction from API paths
  - HypothesisBuilder relationship inference from URL nesting
  - ModelRefiner marks validated after passing experiments
  - ModelRefiner updates hypothesis after failing experiments
  - ModelTicketGenerator produces one ticket per operation
  - DomainModel.overall_confidence()
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from scripts.domain_model import (
    DomainModel,
    EntityHypothesis,
    Experiment,
    FieldHypothesis,
    FieldType,
    OperationHypothesis,
    RelationshipHypothesis,
    StateTransition,
)
from scripts.hypothesis_builder import HypothesisBuilder
from scripts.model_refiner import ModelRefiner
from scripts.model_ticket_generator import ModelTicketGenerator, TicketSpec
from scripts.models import Authority, EvidenceRef, Fact, FactCategory, SourceType


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_api_fact(
    path: str,
    method: str = "GET",
    feature: str = "boards",
    response_body: dict | None = None,
) -> Fact:
    sd: dict = {"method": method, "url": f"https://trello.com{path}"}
    if response_body:
        sd["response_body"] = response_body
    return Fact(
        feature=feature,
        category=FactCategory.API_ENDPOINT,
        claim=f"{method} {path}",
        evidence=[EvidenceRef(source_url=f"https://trello.com{path}")],
        source_type=SourceType.LIVE_APP,
        authority=Authority.AUTHORITATIVE,
        structured_data=sd,
    )


def make_entity(name: str, plural: str, api_prefix: str = "") -> EntityHypothesis:
    return EntityHypothesis(
        name=name,
        plural=plural,
        api_prefix=api_prefix or f"/{plural}",
    )


def make_model_with_entity(
    entity_name: str = "Board",
    plural: str = "boards",
    n_ops: int = 2,
    n_validated: int = 1,
) -> DomainModel:
    model = DomainModel(target="trello.com")
    entity = make_entity(entity_name, plural)
    for i in range(n_ops):
        entity.operations.append(
            OperationHypothesis(
                name=f"op{i}",
                method="GET",
                validated=(i < n_validated),
            )
        )
    model.entities[entity_name] = entity
    return model


def make_experiment(
    entity: str = "Board",
    operation: str = "create",
    passed: bool = True,
    actual: str = "HTTP 201",
    error: str = "",
) -> Experiment:
    return Experiment(
        id="test-exp-1",
        entity=entity,
        hypothesis=f"{entity}.{operation} returns 201",
        operation=operation,
        script="",
        expected="HTTP 201",
        actual=actual,
        passed=passed,
        error=error,
    )


# ---------------------------------------------------------------------------
# DomainModel serialization roundtrip
# ---------------------------------------------------------------------------


class TestDomainModelSerialization:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        model = DomainModel(target="trello.com", iteration=3)
        entity = EntityHypothesis(
            name="Board",
            plural="boards",
            api_prefix="/1/boards",
            states=["open", "closed"],
            confidence=0.75,
        )
        entity.fields["name"] = FieldHypothesis(
            name="name",
            field_type=FieldType.STRING,
            required=True,
            validated=True,
        )
        entity.operations.append(
            OperationHypothesis(
                name="create",
                method="POST",
                endpoint_pattern="/1/boards",
                required_fields=["name"],
                validated=True,
            )
        )
        entity.transitions.append(
            StateTransition(
                from_state="open",
                to_state="closed",
                operation="close",
                reversible=False,
                validated=False,
            )
        )
        entity.relationships.append(
            RelationshipHypothesis(
                from_entity="List",
                to_entity="Board",
                relation_type="belongs_to",
                foreign_key="boardId",
            )
        )
        model.entities["Board"] = entity

        path = tmp_path / "model.json"
        model.save(path)
        assert path.exists()

        loaded = DomainModel.load(path)
        assert loaded.target == "trello.com"
        assert loaded.iteration == 3
        assert "Board" in loaded.entities

        loaded_entity = loaded.entities["Board"]
        assert loaded_entity.name == "Board"
        assert loaded_entity.plural == "boards"
        assert loaded_entity.api_prefix == "/1/boards"
        assert loaded_entity.states == ["open", "closed"]
        assert loaded_entity.confidence == 0.75

        assert "name" in loaded_entity.fields
        assert loaded_entity.fields["name"].field_type == FieldType.STRING
        assert loaded_entity.fields["name"].required is True
        assert loaded_entity.fields["name"].validated is True

        assert len(loaded_entity.operations) == 1
        assert loaded_entity.operations[0].name == "create"
        assert loaded_entity.operations[0].validated is True
        assert loaded_entity.operations[0].required_fields == ["name"]

        assert len(loaded_entity.transitions) == 1
        assert loaded_entity.transitions[0].from_state == "open"
        assert loaded_entity.transitions[0].to_state == "closed"

        assert len(loaded_entity.relationships) == 1
        assert loaded_entity.relationships[0].relation_type == "belongs_to"
        assert loaded_entity.relationships[0].foreign_key == "boardId"

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        model = DomainModel(target="example.com")
        path = tmp_path / "deep" / "nested" / "model.json"
        model.save(path)
        assert path.exists()

    def test_empty_model_roundtrip(self, tmp_path: Path) -> None:
        model = DomainModel(target="empty.com")
        path = tmp_path / "empty.json"
        model.save(path)
        loaded = DomainModel.load(path)
        assert loaded.target == "empty.com"
        assert loaded.entities == {}


# ---------------------------------------------------------------------------
# EntityHypothesis.validation_score()
# ---------------------------------------------------------------------------


class TestEntityValidationScore:
    def test_all_unvalidated(self) -> None:
        entity = make_entity("Board", "boards")
        entity.operations.append(OperationHypothesis(name="create", validated=False))
        entity.operations.append(OperationHypothesis(name="read", validated=False))
        assert entity.validation_score() == 0.0

    def test_all_validated(self) -> None:
        entity = make_entity("Board", "boards")
        entity.operations.append(OperationHypothesis(name="create", validated=True))
        entity.fields["name"] = FieldHypothesis(name="name", validated=True)
        entity.transitions.append(
            StateTransition(from_state="open", to_state="closed", operation="close", validated=True)
        )
        assert entity.validation_score() == 1.0

    def test_partial_validation(self) -> None:
        entity = make_entity("Board", "boards")
        entity.operations.append(OperationHypothesis(name="create", validated=True))
        entity.operations.append(OperationHypothesis(name="delete", validated=False))
        # 1 out of 2 validated
        assert entity.validation_score() == pytest.approx(0.5)

    def test_empty_entity(self) -> None:
        entity = make_entity("Board", "boards")
        assert entity.validation_score() == 0.0


# ---------------------------------------------------------------------------
# DomainModel.overall_confidence()
# ---------------------------------------------------------------------------


class TestOverallConfidence:
    def test_no_hypotheses(self) -> None:
        model = DomainModel(target="example.com")
        assert model.overall_confidence() == 0.0

    def test_partial_confidence(self) -> None:
        model = make_model_with_entity(n_ops=4, n_validated=2)
        assert model.overall_confidence() == pytest.approx(0.5)

    def test_full_confidence(self) -> None:
        model = make_model_with_entity(n_ops=3, n_validated=3)
        assert model.overall_confidence() == 1.0

    def test_counts_across_entities(self) -> None:
        model = DomainModel(target="example.com")
        for name, plural, n_ops, n_val in [
            ("Board", "boards", 4, 2),
            ("Card", "cards", 2, 2),
        ]:
            entity = make_entity(name, plural)
            for i in range(n_ops):
                entity.operations.append(
                    OperationHypothesis(name=f"op{i}", validated=(i < n_val))
                )
            model.entities[name] = entity
        # 4 validated / 6 total = 0.666...
        assert model.overall_confidence() == pytest.approx(4 / 6)


# ---------------------------------------------------------------------------
# HypothesisBuilder — entity extraction from API paths
# ---------------------------------------------------------------------------


class TestHypothesisBuilderEntities:
    def test_extracts_single_entity(self) -> None:
        facts = [make_api_fact("/1/boards")]
        model = HypothesisBuilder().build(facts, "trello.com")
        assert "Board" in model.entities
        assert model.entities["Board"].plural == "boards"

    def test_extracts_multiple_entities(self) -> None:
        facts = [
            make_api_fact("/1/boards", feature="boards"),
            make_api_fact("/1/cards", feature="cards"),
            make_api_fact("/1/lists", feature="lists"),
        ]
        model = HypothesisBuilder().build(facts, "trello.com")
        assert "Board" in model.entities
        assert "Card" in model.entities
        assert "List" in model.entities

    def test_ignores_version_prefix(self) -> None:
        facts = [make_api_fact("/api/v2/users")]
        model = HypothesisBuilder().build(facts, "example.com")
        assert "User" in model.entities
        # "api" and "v2" should not be entities
        assert "Api" not in model.entities
        assert "V2" not in model.entities

    def test_ignores_id_segments(self) -> None:
        facts = [make_api_fact("/1/boards/abc123/lists")]
        model = HypothesisBuilder().build(facts, "trello.com")
        assert "Board" in model.entities
        assert "List" in model.entities
        # "abc123" is an ID, should not be an entity
        assert "Abc123" not in model.entities

    def test_empty_facts(self) -> None:
        model = HypothesisBuilder().build([], "example.com")
        assert model.entities == {}

    def test_non_api_facts_ignored(self) -> None:
        fact = Fact(
            feature="boards",
            category=FactCategory.UI_COMPONENT,
            claim="Board header component",
            evidence=[EvidenceRef(source_url="https://trello.com/boards")],
            source_type=SourceType.LIVE_APP,
            authority=Authority.AUTHORITATIVE,
        )
        model = HypothesisBuilder().build([fact], "trello.com")
        # No API endpoint facts — no entities extracted from paths
        # (Standard CRUD will not be added either since no entities)
        assert len(model.entities) == 0


# ---------------------------------------------------------------------------
# HypothesisBuilder — relationship inference from URL nesting
# ---------------------------------------------------------------------------


class TestHypothesisBuilderRelationships:
    def test_infers_belongs_to_from_nested_url(self) -> None:
        facts = [
            make_api_fact("/1/boards", feature="boards"),
            make_api_fact("/1/boards/{boardId}/lists", feature="lists"),
        ]
        model = HypothesisBuilder().build(facts, "trello.com")

        assert "List" in model.entities
        list_entity = model.entities["List"]
        belongs_to = [
            r for r in list_entity.relationships if r.relation_type == "belongs_to"
        ]
        assert len(belongs_to) >= 1
        assert any(r.to_entity == "Board" for r in belongs_to)

    def test_infers_has_many_on_parent(self) -> None:
        facts = [
            make_api_fact("/1/boards", feature="boards"),
            make_api_fact("/1/boards/{boardId}/lists", feature="lists"),
        ]
        model = HypothesisBuilder().build(facts, "trello.com")

        board_entity = model.entities.get("Board")
        assert board_entity is not None
        has_many = [
            r for r in board_entity.relationships if r.relation_type == "has_many"
        ]
        assert any(r.to_entity == "List" for r in has_many)

    def test_no_self_relationship(self) -> None:
        facts = [make_api_fact("/1/boards/{boardId}/boards")]
        model = HypothesisBuilder().build(facts, "trello.com")
        board = model.entities.get("Board")
        if board:
            self_rels = [
                r for r in board.relationships if r.from_entity == r.to_entity
            ]
            assert len(self_rels) == 0


# ---------------------------------------------------------------------------
# HypothesisBuilder — standard CRUD hypotheses
# ---------------------------------------------------------------------------


class TestHypothesisBuilderCRUD:
    def test_adds_crud_for_entity(self) -> None:
        facts = [make_api_fact("/1/boards")]
        model = HypothesisBuilder().build(facts, "trello.com")
        board = model.entities["Board"]
        op_names = {op.name for op in board.operations}
        assert "create" in op_names
        assert "read" in op_names
        assert "update" in op_names
        assert "delete" in op_names

    def test_crud_ops_start_unvalidated(self) -> None:
        facts = [make_api_fact("/1/boards")]
        model = HypothesisBuilder().build(facts, "trello.com")
        board = model.entities["Board"]
        for op in board.operations:
            # Only standard CRUD hypotheses from _add_standard_crud_hypotheses
            # are always unvalidated initially
            if "standard CRUD hypothesis" in op.evidence:
                assert op.validated is False

    def test_no_duplicate_ops(self) -> None:
        # POST /1/boards observed twice → still only one "create" op
        facts = [
            make_api_fact("/1/boards", method="POST"),
            make_api_fact("/1/boards", method="POST"),
        ]
        model = HypothesisBuilder().build(facts, "trello.com")
        board = model.entities["Board"]
        create_ops = [op for op in board.operations if op.name == "create"]
        assert len(create_ops) == 1


# ---------------------------------------------------------------------------
# ModelRefiner
# ---------------------------------------------------------------------------


class TestModelRefiner:
    def _model_with_op(
        self, op_name: str = "create", validated: bool = False
    ) -> DomainModel:
        model = DomainModel(target="trello.com")
        entity = make_entity("Board", "boards")
        entity.operations.append(
            OperationHypothesis(name=op_name, method="POST", validated=validated)
        )
        model.entities["Board"] = entity
        return model

    def test_marks_validated_after_passing_experiment(self) -> None:
        model = self._model_with_op("create", validated=False)
        exp = make_experiment(entity="Board", operation="create", passed=True)
        refined = ModelRefiner().refine(model, [exp])
        op = refined.entities["Board"].operations[0]
        assert op.validated is True

    def test_increments_iteration(self) -> None:
        model = self._model_with_op()
        model.iteration = 2
        refined = ModelRefiner().refine(model, [])
        assert refined.iteration == 3

    def test_updates_hypothesis_after_failing_experiment(self) -> None:
        model = self._model_with_op("create", validated=False)
        exp = make_experiment(
            entity="Board",
            operation="create",
            passed=False,
            actual="HTTP 500",
            error="Internal Server Error",
        )
        refined = ModelRefiner().refine(model, [exp])
        op = refined.entities["Board"].operations[0]
        assert op.validated is False
        # Evidence should record the failure
        assert any("FAILED" in e for e in op.evidence)

    def test_updates_response_status_on_failure(self) -> None:
        model = self._model_with_op("create", validated=False)
        exp = Experiment(
            id="x1",
            entity="Board",
            hypothesis="test",
            operation="create",
            script="",
            expected="HTTP 201",
            actual="HTTP 409",
            passed=False,
            evidence={"status_code": 409},
        )
        refined = ModelRefiner().refine(model, [exp])
        op = refined.entities["Board"].operations[0]
        assert op.response_status == 409

    def test_unknown_entity_does_not_crash(self) -> None:
        model = DomainModel(target="trello.com")
        exp = make_experiment(entity="UnknownEntity", operation="create", passed=True)
        # Should not raise
        ModelRefiner().refine(model, [exp])

    def test_updates_entity_confidence(self) -> None:
        model = DomainModel(target="trello.com")
        entity = make_entity("Board", "boards")
        entity.operations.append(OperationHypothesis(name="create", validated=False))
        entity.operations.append(OperationHypothesis(name="read", validated=False))
        model.entities["Board"] = entity

        exp = make_experiment(entity="Board", operation="create", passed=True)
        refined = ModelRefiner().refine(model, [exp])
        assert refined.entities["Board"].confidence == pytest.approx(0.5)

    def test_multiple_experiments_accumulate(self) -> None:
        model = DomainModel(target="trello.com")
        entity = make_entity("Board", "boards")
        entity.operations.append(OperationHypothesis(name="create", validated=False))
        entity.operations.append(OperationHypothesis(name="delete", validated=False))
        model.entities["Board"] = entity

        experiments = [
            make_experiment(entity="Board", operation="create", passed=True),
            make_experiment(entity="Board", operation="delete", passed=True),
        ]
        refined = ModelRefiner().refine(model, experiments)
        for op in refined.entities["Board"].operations:
            assert op.validated is True


# ---------------------------------------------------------------------------
# ModelTicketGenerator
# ---------------------------------------------------------------------------


class TestModelTicketGenerator:
    def _model_with_two_ops(self) -> DomainModel:
        model = DomainModel(target="trello.com")
        entity = EntityHypothesis(name="Board", plural="boards", api_prefix="/1/boards")
        entity.operations.append(
            OperationHypothesis(
                name="create",
                method="POST",
                endpoint_pattern="/1/boards",
                required_fields=["name"],
                response_status=201,
            )
        )
        entity.operations.append(
            OperationHypothesis(
                name="delete",
                method="DELETE",
                endpoint_pattern="/1/boards/{id}",
                response_status=204,
            )
        )
        model.entities["Board"] = entity
        return model

    def test_produces_ticket_per_operation(self) -> None:
        model = self._model_with_two_ops()
        tickets = ModelTicketGenerator().generate_tickets(model)
        op_tickets = [t for t in tickets if t.operation != "model"]
        assert len(op_tickets) == 2
        op_names = {t.operation for t in op_tickets}
        assert op_names == {"create", "delete"}

    def test_produces_entity_model_ticket(self) -> None:
        model = self._model_with_two_ops()
        tickets = ModelTicketGenerator().generate_tickets(model)
        model_tickets = [t for t in tickets if t.operation == "model"]
        assert len(model_tickets) == 1
        assert model_tickets[0].entity == "Board"

    def test_ticket_has_api_contract(self) -> None:
        model = self._model_with_two_ops()
        tickets = ModelTicketGenerator().generate_tickets(model)
        create = next(t for t in tickets if t.operation == "create")
        assert create.api_method == "POST"
        assert create.api_endpoint == "/1/boards"
        assert "name" in create.required_fields

    def test_ticket_has_acceptance_criteria(self) -> None:
        model = self._model_with_two_ops()
        tickets = ModelTicketGenerator().generate_tickets(model)
        create = next(t for t in tickets if t.operation == "create")
        assert len(create.acceptance_criteria) >= 1
        assert any("201" in ac for ac in create.acceptance_criteria)

    def test_multiple_entities_produce_multiple_model_tickets(self) -> None:
        model = DomainModel(target="trello.com")
        for name, plural in [("Board", "boards"), ("Card", "cards"), ("List", "lists")]:
            entity = make_entity(name, plural)
            entity.operations.append(OperationHypothesis(name="create", method="POST"))
            model.entities[name] = entity

        tickets = ModelTicketGenerator().generate_tickets(model)
        model_tickets = [t for t in tickets if t.operation == "model"]
        op_tickets = [t for t in tickets if t.operation != "model"]
        assert len(model_tickets) == 3
        assert len(op_tickets) == 3

    def test_state_constraints_included(self) -> None:
        model = DomainModel(target="trello.com")
        entity = make_entity("Board", "boards")
        entity.operations.append(OperationHypothesis(name="close", method="PUT"))
        entity.transitions.append(
            StateTransition(
                from_state="open",
                to_state="closed",
                operation="close",
                reversible=True,
            )
        )
        model.entities["Board"] = entity

        tickets = ModelTicketGenerator().generate_tickets(model)
        close_ticket = next(t for t in tickets if t.operation == "close")
        assert len(close_ticket.state_constraints) >= 1
        assert any("open" in sc and "closed" in sc for sc in close_ticket.state_constraints)

    def test_empty_model_produces_no_tickets(self) -> None:
        model = DomainModel(target="example.com")
        tickets = ModelTicketGenerator().generate_tickets(model)
        assert tickets == []
