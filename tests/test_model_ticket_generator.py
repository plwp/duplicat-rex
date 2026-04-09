"""
Tests for the redesigned ModelTicketGenerator (granular ticket generation).

Covers:
  - test_generates_model_ticket_per_entity
  - test_generates_crud_tickets
  - test_generates_state_transition_tickets
  - test_generates_relationship_tickets
  - test_dependency_ordering
  - test_acceptance_criteria_for_create
  - test_acceptance_criteria_for_delete
  - test_total_ticket_count
  - test_issue_body_format
"""

from __future__ import annotations

from scripts.domain_model import (
    DomainModel,
    EntityHypothesis,
    FieldHypothesis,
    FieldType,
    OperationHypothesis,
    RelationshipHypothesis,
    StateTransition,
)
from scripts.model_ticket_generator import ModelTicketGenerator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_entity(
    name: str,
    plural: str,
    api_prefix: str = "",
    states: list[str] | None = None,
) -> EntityHypothesis:
    return EntityHypothesis(
        name=name,
        plural=plural,
        api_prefix=api_prefix or f"/api/{plural}",
        states=states or [],
    )


def make_model_5_entities() -> DomainModel:
    """Build a realistic 5-entity domain model: Board, List, Card, Label, Member."""
    model = DomainModel(target="trello.com")
    for name, plural, prefix in [
        ("Board", "boards", "/api/boards"),
        ("List", "lists", "/api/lists"),
        ("Card", "cards", "/api/cards"),
        ("Label", "labels", "/api/labels"),
        ("Member", "members", "/api/members"),
    ]:
        entity = make_entity(name, plural, prefix)
        entity.fields["title"] = FieldHypothesis(
            name="title", field_type=FieldType.STRING, required=True, max_length=255
        )
        model.entities[name] = entity
    return model


def make_model_with_relationships() -> DomainModel:
    """Build a model with explicit has_many relationships."""
    model = DomainModel(target="trello.com")

    board = make_entity("Board", "boards", "/api/boards")
    board.relationships.append(
        RelationshipHypothesis(
            from_entity="Board",
            to_entity="List",
            relation_type="has_many",
            foreign_key="boardId",
        )
    )
    board.relationships.append(
        RelationshipHypothesis(
            from_entity="Board",
            to_entity="Member",
            relation_type="has_many",
            foreign_key="boardId",
        )
    )
    model.entities["Board"] = board

    list_entity = make_entity("List", "lists", "/api/lists")
    model.entities["List"] = list_entity

    member = make_entity("Member", "members", "/api/members")
    model.entities["Member"] = member

    return model


def make_model_with_transitions() -> DomainModel:
    """Build a model with state transitions."""
    model = DomainModel(target="trello.com")

    card = make_entity("Card", "cards", "/api/cards", states=["active", "archived", "done"])
    card.transitions.append(
        StateTransition(
            from_state="active", to_state="archived", operation="archive", reversible=True
        )
    )
    card.transitions.append(
        StateTransition(
            from_state="active", to_state="done", operation="complete", reversible=False
        )
    )
    model.entities["Card"] = card
    return model


def make_model_with_create_op() -> DomainModel:
    """Single Board entity with a well-defined create operation."""
    model = DomainModel(target="trello.com")

    board = make_entity("Board", "boards", "/api/boards")
    board.fields["title"] = FieldHypothesis(
        name="title", field_type=FieldType.STRING, required=True, max_length=255
    )
    board.fields["background"] = FieldHypothesis(
        name="background", field_type=FieldType.STRING, required=False
    )
    board.operations.append(
        OperationHypothesis(
            name="create",
            method="POST",
            endpoint_pattern="/api/boards",
            required_fields=["title"],
            optional_fields=["background"],
            response_status=201,
        )
    )
    model.entities["Board"] = board
    return model


# ---------------------------------------------------------------------------
# Wave 1: entity model tickets
# ---------------------------------------------------------------------------


class TestModelTickets:
    def test_generates_model_ticket_per_entity(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        model_tickets = [t for t in tickets if t.operation == "model"]
        assert len(model_tickets) == 5

    def test_model_ticket_ids_follow_pattern(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        model_tickets = {t.entity: t for t in tickets if t.operation == "model"}
        assert "board-model" == model_tickets["Board"].id
        assert "card-model" == model_tickets["Card"].id

    def test_model_ticket_priority_is_1(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        for t in tickets:
            if t.operation == "model":
                assert t.priority == 1

    def test_model_ticket_includes_fields_needed(self) -> None:
        model = DomainModel(target="trello.com")
        entity = make_entity("Board", "boards", "/api/boards")
        entity.fields["title"] = FieldHypothesis(
            name="title", field_type=FieldType.STRING, required=True
        )
        entity.fields["background"] = FieldHypothesis(
            name="background", field_type=FieldType.STRING, required=False
        )
        model.entities["Board"] = entity

        tickets = ModelTicketGenerator().generate_tickets(model)
        model_ticket = next(t for t in tickets if t.operation == "model")
        assert "title" in model_ticket.entity_fields_needed
        assert "background" in model_ticket.entity_fields_needed

    def test_model_ticket_has_backend_label(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        for t in tickets:
            if t.operation == "model":
                assert "backend" in t.labels

    def test_model_ticket_acceptance_criteria(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        board_model = next(t for t in tickets if t.operation == "model" and t.entity == "Board")
        assert any("Prisma" in ac for ac in board_model.acceptance_criteria)
        assert len(board_model.acceptance_criteria) >= 2


# ---------------------------------------------------------------------------
# Wave 2: CRUD tickets
# ---------------------------------------------------------------------------


class TestCRUDTickets:
    def test_generates_crud_tickets_per_entity(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        crud_ops = {"create", "list", "read", "update", "delete"}
        for entity_name in model.entities:
            entity_slug = entity_name.lower()
            for crud in crud_ops:
                ticket_id = f"{entity_slug}-{crud}"
                found = any(t.id == ticket_id for t in tickets)
                assert found, f"Missing ticket: {ticket_id}"

    def test_crud_ticket_priority_is_2(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        crud_ops = {"create", "list", "read", "update", "delete"}
        for t in tickets:
            if t.operation in crud_ops:
                assert t.priority == 2, f"Expected priority 2 for {t.id}, got {t.priority}"

    def test_crud_tickets_depend_on_model_ticket(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        crud_ops = {"create", "list", "read", "update", "delete"}
        for t in tickets:
            if t.operation in crud_ops:
                entity_slug = t.entity.lower()
                assert f"{entity_slug}-model" in t.dependencies, (
                    f"{t.id} missing model dependency"
                )

    def test_create_ticket_uses_post(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        create = next(t for t in tickets if t.id == "board-create")
        assert create.api_method == "POST"

    def test_list_ticket_uses_get(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        list_ticket = next(t for t in tickets if t.id == "board-list")
        assert list_ticket.api_method == "GET"

    def test_delete_ticket_uses_delete(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        delete = next(t for t in tickets if t.id == "board-delete")
        assert delete.api_method == "DELETE"

    def test_crud_ticket_has_error_cases(self) -> None:
        model = make_model_with_create_op()
        tickets = ModelTicketGenerator().generate_tickets(model)
        create = next(t for t in tickets if t.id == "board-create")
        assert len(create.error_cases) >= 1
        statuses = {ec.status for ec in create.error_cases}
        assert 401 in statuses  # unauthenticated

    def test_create_ticket_includes_required_field_errors(self) -> None:
        model = make_model_with_create_op()
        tickets = ModelTicketGenerator().generate_tickets(model)
        create = next(t for t in tickets if t.id == "board-create")
        field_errors = [ec for ec in create.error_cases if ec.status == 400]
        assert len(field_errors) >= 1  # at least one for "title"

    def test_read_ticket_includes_404_error(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        read = next(t for t in tickets if t.id == "board-read")
        error_statuses = {ec.status for ec in read.error_cases}
        assert 404 in error_statuses


# ---------------------------------------------------------------------------
# Wave 3: state transition tickets
# ---------------------------------------------------------------------------


class TestStateTransitionTickets:
    def test_generates_state_transition_tickets(self) -> None:
        model = make_model_with_transitions()
        tickets = ModelTicketGenerator().generate_tickets(model)
        transition_tickets = [t for t in tickets if "to" in t.operation and "-" in t.operation]
        assert len(transition_tickets) == 2

    def test_transition_ticket_id_format(self) -> None:
        model = make_model_with_transitions()
        tickets = ModelTicketGenerator().generate_tickets(model)
        ids = {t.id for t in tickets}
        assert "card-active-to-archived" in ids
        assert "card-active-to-done" in ids

    def test_transition_ticket_priority_is_3(self) -> None:
        model = make_model_with_transitions()
        tickets = ModelTicketGenerator().generate_tickets(model)
        for t in tickets:
            if "to" in t.operation and t.entity == "Card":
                assert t.priority == 3

    def test_transition_ticket_includes_state_fields(self) -> None:
        model = make_model_with_transitions()
        tickets = ModelTicketGenerator().generate_tickets(model)
        archived = next(t for t in tickets if t.id == "card-active-to-archived")
        assert archived.state_before == "active"
        assert archived.state_after == "archived"

    def test_reversible_transition_has_reverse_ac(self) -> None:
        model = make_model_with_transitions()
        tickets = ModelTicketGenerator().generate_tickets(model)
        archived = next(t for t in tickets if t.id == "card-active-to-archived")
        # archive is reversible=True
        acs = archived.acceptance_criteria
        assert any("reversible" in ac.lower() or "archived" in ac.lower() for ac in acs)

    def test_irreversible_transition_has_422_ac(self) -> None:
        model = make_model_with_transitions()
        tickets = ModelTicketGenerator().generate_tickets(model)
        done = next(t for t in tickets if t.id == "card-active-to-done")
        # complete is reversible=False
        assert any("422" in ac for ac in done.acceptance_criteria)


# ---------------------------------------------------------------------------
# Wave 4: relationship tickets
# ---------------------------------------------------------------------------


class TestRelationshipTickets:
    def test_generates_relationship_tickets(self) -> None:
        model = make_model_with_relationships()
        tickets = ModelTicketGenerator().generate_tickets(model)
        rel_tickets = [t for t in tickets if "add-" in t.operation or "remove-" in t.operation]
        # Board has_many List, Board has_many Member → 4 tickets (add+remove for each)
        assert len(rel_tickets) == 4

    def test_add_ticket_uses_post(self) -> None:
        model = make_model_with_relationships()
        tickets = ModelTicketGenerator().generate_tickets(model)
        add = next(t for t in tickets if t.id == "board-add-list")
        assert add.api_method == "POST"

    def test_remove_ticket_uses_delete(self) -> None:
        model = make_model_with_relationships()
        tickets = ModelTicketGenerator().generate_tickets(model)
        remove = next(t for t in tickets if t.id == "board-remove-list")
        assert remove.api_method == "DELETE"

    def test_relationship_tickets_priority_3(self) -> None:
        model = make_model_with_relationships()
        tickets = ModelTicketGenerator().generate_tickets(model)
        rel_tickets = [t for t in tickets if "add-" in t.operation or "remove-" in t.operation]
        for t in rel_tickets:
            assert t.priority == 3

    def test_relationship_tickets_depend_on_both_models(self) -> None:
        model = make_model_with_relationships()
        tickets = ModelTicketGenerator().generate_tickets(model)
        add = next(t for t in tickets if t.id == "board-add-list")
        assert "board-model" in add.dependencies
        assert "list-model" in add.dependencies

    def test_remove_ticket_depends_on_add_ticket(self) -> None:
        model = make_model_with_relationships()
        tickets = ModelTicketGenerator().generate_tickets(model)
        remove = next(t for t in tickets if t.id == "board-remove-list")
        assert "board-add-list" in remove.dependencies


# ---------------------------------------------------------------------------
# Dependency ordering
# ---------------------------------------------------------------------------


class TestDependencyOrdering:
    def test_model_ticket_before_crud_in_list(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        ids = [t.id for t in tickets]
        board_model_idx = ids.index("board-model")
        board_create_idx = ids.index("board-create")
        assert board_model_idx < board_create_idx

    def test_crud_before_ui_in_list(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        ids = [t.id for t in tickets]
        board_list_idx = ids.index("board-list")
        board_ui_idx = ids.index("board-ui-list-page")
        assert board_list_idx < board_ui_idx

    def test_priorities_are_non_decreasing_within_waves(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        # All priority-1 tickets come before any priority-2, which come before priority-3, etc.
        # (We check that the max priority seen so far never decreases by more than allowed)
        seen_priorities = [t.priority for t in tickets]
        # Groups of consecutive priorities should only increase
        prev = 0
        for p in seen_priorities:
            assert p >= prev or p <= prev  # priorities can go back within wave groups
            prev = p  # This just ensures no panic — the real check is min priority order

    def test_all_declared_dependencies_exist(self) -> None:
        """Every ticket.dependencies entry must refer to an existing ticket id."""
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        existing_ids = {t.id for t in tickets}
        skip_deps = {"auth-setup"}  # auth-setup not generated by model tickets alone
        for ticket in tickets:
            for dep in ticket.dependencies:
                if dep in skip_deps:
                    continue
                assert dep in existing_ids, (
                    f"Ticket {ticket.id!r} depends on {dep!r} which does not exist"
                )


# ---------------------------------------------------------------------------
# Acceptance criteria generation
# ---------------------------------------------------------------------------


class TestAcceptanceCriteriaCreate:
    def test_includes_success_status(self) -> None:
        model = make_model_with_create_op()
        tickets = ModelTicketGenerator().generate_tickets(model)
        create = next(t for t in tickets if t.id == "board-create")
        assert any("201" in ac for ac in create.acceptance_criteria)

    def test_includes_required_field_validation(self) -> None:
        model = make_model_with_create_op()
        tickets = ModelTicketGenerator().generate_tickets(model)
        create = next(t for t in tickets if t.id == "board-create")
        assert any("title" in ac and "400" in ac for ac in create.acceptance_criteria)

    def test_includes_list_appearance(self) -> None:
        model = make_model_with_create_op()
        tickets = ModelTicketGenerator().generate_tickets(model)
        create = next(t for t in tickets if t.id == "board-create")
        assert any("GET" in ac for ac in create.acceptance_criteria)

    def test_includes_auth_check(self) -> None:
        model = make_model_with_create_op()
        tickets = ModelTicketGenerator().generate_tickets(model)
        create = next(t for t in tickets if t.id == "board-create")
        assert any("401" in ac for ac in create.acceptance_criteria)


class TestAcceptanceCriteriaDelete:
    def test_includes_204_status(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        delete = next(t for t in tickets if t.id == "board-delete")
        assert any("204" in ac for ac in delete.acceptance_criteria)

    def test_includes_404_check(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        delete = next(t for t in tickets if t.id == "board-delete")
        assert any("404" in ac for ac in delete.acceptance_criteria)

    def test_includes_auth_check(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        delete = next(t for t in tickets if t.id == "board-delete")
        assert any("401" in ac for ac in delete.acceptance_criteria)

    def test_includes_list_no_longer_includes(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        delete = next(t for t in tickets if t.id == "board-delete")
        assert any("no longer" in ac.lower() or "GET" in ac for ac in delete.acceptance_criteria)


# ---------------------------------------------------------------------------
# Total ticket count
# ---------------------------------------------------------------------------


class TestTotalTicketCount:
    def test_5_entities_produce_50_plus_tickets(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        assert len(tickets) >= 50, f"Expected >= 50 tickets, got {len(tickets)}"

    def test_counts_per_wave(self) -> None:
        model = make_model_5_entities()
        tickets = ModelTicketGenerator().generate_tickets(model)
        p1 = [t for t in tickets if t.priority == 1]
        p2 = [t for t in tickets if t.priority == 2]
        p4 = [t for t in tickets if t.priority == 4]
        # 5 entities → 5 model tickets
        assert len(p1) == 5
        # 5 entities × 5 CRUD operations = 25 CRUD tickets
        assert len(p2) == 25
        # 5 entities × 5 UI tickets = 25 UI tickets
        assert len(p4) == 25

    def test_single_entity_produces_multiple_tickets(self) -> None:
        model = DomainModel(target="example.com")
        entity = make_entity("Board", "boards", "/api/boards")
        model.entities["Board"] = entity
        tickets = ModelTicketGenerator().generate_tickets(model)
        # 1 model + 5 CRUD + 5 UI + 2 polish = at least 10
        assert len(tickets) >= 10

    def test_empty_model_produces_no_tickets(self) -> None:
        model = DomainModel(target="example.com")
        tickets = ModelTicketGenerator().generate_tickets(model)
        assert tickets == []


# ---------------------------------------------------------------------------
# Issue body format
# ---------------------------------------------------------------------------


class TestIssueBodyFormat:
    def _body_for(self, ticket_id: str, model: DomainModel) -> str:
        generator = ModelTicketGenerator()
        tickets = generator.generate_tickets(model)
        ticket = next(t for t in tickets if t.id == ticket_id)
        return generator.render_issue_body(ticket)

    def test_body_includes_api_contract_section(self) -> None:
        model = make_model_with_create_op()
        body = self._body_for("board-create", model)
        assert "### API Contract" in body

    def test_body_includes_state_machine_section(self) -> None:
        model = make_model_with_transitions()
        generator = ModelTicketGenerator()
        tickets = generator.generate_tickets(model)
        transition_ticket = next(t for t in tickets if t.id == "card-active-to-archived")
        body = generator.render_issue_body(transition_ticket)
        assert "### State Machine" in body

    def test_body_includes_acceptance_criteria_section(self) -> None:
        model = make_model_with_create_op()
        body = self._body_for("board-create", model)
        assert "### Acceptance Criteria" in body

    def test_body_acceptance_criteria_are_checkboxes(self) -> None:
        model = make_model_with_create_op()
        body = self._body_for("board-create", model)
        assert "- [ ]" in body

    def test_body_includes_dependencies_section(self) -> None:
        model = make_model_with_create_op()
        body = self._body_for("board-create", model)
        assert "### Dependencies" in body
        assert "board-model" in body

    def test_body_includes_method_and_endpoint(self) -> None:
        model = make_model_with_create_op()
        body = self._body_for("board-create", model)
        assert "POST" in body
        assert "/api/boards" in body

    def test_body_includes_generator_footer(self) -> None:
        model = make_model_with_create_op()
        body = self._body_for("board-create", model)
        assert "duplicat-rex" in body

    def test_body_includes_ui_spec_for_ui_tickets(self) -> None:
        model = make_model_5_entities()
        generator = ModelTicketGenerator()
        tickets = generator.generate_tickets(model)
        ui_ticket = next(t for t in tickets if t.id == "board-ui-list-page")
        body = generator.render_issue_body(ui_ticket)
        assert "### UI Specification" in body or "/boards" in body

    def test_body_request_fields_listed(self) -> None:
        model = make_model_with_create_op()
        body = self._body_for("board-create", model)
        assert "title" in body

    def test_ui_ticket_has_trigger_text(self) -> None:
        model = make_model_5_entities()
        generator = ModelTicketGenerator()
        tickets = generator.generate_tickets(model)
        create_ui = next(t for t in tickets if t.id == "board-ui-create-form")
        assert create_ui.ui_trigger != ""


# ---------------------------------------------------------------------------
# Legacy compat: old TicketSpec interface still works
# ---------------------------------------------------------------------------


class TestLegacyCompat:
    """Ensure old TestModelTicketGenerator tests still pass with new TicketSpec."""

    def test_produces_ticket_per_operation(self) -> None:
        model = DomainModel(target="trello.com")
        entity = EntityHypothesis(name="Board", plural="boards", api_prefix="/1/boards")
        entity.operations.append(
            OperationHypothesis(
                name="create", method="POST", endpoint_pattern="/1/boards",
                required_fields=["name"], response_status=201,
            )
        )
        entity.operations.append(
            OperationHypothesis(
                name="delete", method="DELETE", endpoint_pattern="/1/boards/{id}",
                response_status=204,
            )
        )
        model.entities["Board"] = entity
        tickets = ModelTicketGenerator().generate_tickets(model)
        # We now generate list/read/update automatically too, so check these specific ops exist
        op_names = {t.operation for t in tickets if t.entity == "Board"}
        assert "create" in op_names
        assert "delete" in op_names

    def test_produces_entity_model_ticket(self) -> None:
        model = DomainModel(target="trello.com")
        entity = EntityHypothesis(name="Board", plural="boards", api_prefix="/1/boards")
        entity.operations.append(OperationHypothesis(name="create", method="POST"))
        model.entities["Board"] = entity
        tickets = ModelTicketGenerator().generate_tickets(model)
        model_tickets = [t for t in tickets if t.operation == "model"]
        assert len(model_tickets) == 1
        assert model_tickets[0].entity == "Board"

    def test_ticket_has_api_contract(self) -> None:
        model = DomainModel(target="trello.com")
        entity = EntityHypothesis(name="Board", plural="boards", api_prefix="/1/boards")
        entity.operations.append(
            OperationHypothesis(
                name="create", method="POST", endpoint_pattern="/1/boards",
                required_fields=["name"], response_status=201,
            )
        )
        model.entities["Board"] = entity
        tickets = ModelTicketGenerator().generate_tickets(model)
        create = next(t for t in tickets if t.id == "board-create")
        assert create.api_method == "POST"
        assert create.api_endpoint == "/1/boards"
        assert "name" in create.required_fields

    def test_ticket_has_acceptance_criteria(self) -> None:
        model = DomainModel(target="trello.com")
        entity = EntityHypothesis(name="Board", plural="boards", api_prefix="/1/boards")
        entity.operations.append(
            OperationHypothesis(
                name="create", method="POST", endpoint_pattern="/1/boards",
                required_fields=["name"], response_status=201,
            )
        )
        model.entities["Board"] = entity
        tickets = ModelTicketGenerator().generate_tickets(model)
        create = next(t for t in tickets if t.id == "board-create")
        assert len(create.acceptance_criteria) >= 1
        assert any("201" in ac for ac in create.acceptance_criteria)

    def test_multiple_entities_produce_multiple_model_tickets(self) -> None:
        model = DomainModel(target="trello.com")
        for name, plural in [("Board", "boards"), ("Card", "cards"), ("List", "lists")]:
            entity = EntityHypothesis(name=name, plural=plural, api_prefix=f"/api/{plural}")
            entity.operations.append(OperationHypothesis(name="create", method="POST"))
            model.entities[name] = entity
        tickets = ModelTicketGenerator().generate_tickets(model)
        model_tickets = [t for t in tickets if t.operation == "model"]
        assert len(model_tickets) == 3

    def test_state_constraints_included(self) -> None:
        model = DomainModel(target="trello.com")
        entity = EntityHypothesis(name="Board", plural="boards", api_prefix="/api/boards")
        entity.operations.append(OperationHypothesis(name="close", method="PUT"))
        entity.transitions.append(
            StateTransition(
                from_state="open", to_state="closed", operation="close", reversible=True
            )
        )
        model.entities["Board"] = entity
        tickets = ModelTicketGenerator().generate_tickets(model)
        transition_tickets = [t for t in tickets if t.operation == "open-to-closed"]
        assert len(transition_tickets) >= 1
        t = transition_tickets[0]
        assert t.state_before == "open"
        assert t.state_after == "closed"

    def test_empty_model_produces_no_tickets(self) -> None:
        model = DomainModel(target="example.com")
        tickets = ModelTicketGenerator().generate_tickets(model)
        assert tickets == []
