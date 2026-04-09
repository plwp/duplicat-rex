"""
ModelTicketGenerator — generate granular implementation tickets from a validated DomainModel.

Produces 100+ tickets covering all waves:
  Wave 1 (priority 1) — Entity Prisma model tickets
  Wave 2 (priority 2) — Core CRUD per entity (create, list, read, update, delete)
  Wave 3 (priority 3) — State transitions
  Wave 4 (priority 3) — Relationship operations (add/remove child from parent)
  Wave 5 (priority 4) — UI pages (list page, detail page, operation triggers)
  Wave 6 (priority 5) — Edge cases and polish (validation, permissions, errors)

Each ticket contains:
  - API contract (method, endpoint, request/response fields, error cases)
  - UI specification (trigger, location, components)
  - State machine constraints (preconditions, postconditions, transitions)
  - Concrete acceptance criteria (verifiable statements)
  - Dependency declarations (ticket IDs this ticket depends on)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scripts.domain_model import DomainModel, EntityHypothesis, OperationHypothesis

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class FieldSpec:
    """Describes a single API request or response field."""

    name: str
    field_type: str  # "string", "boolean", "integer", etc.
    required: bool
    validation: str = ""  # e.g. "non-empty, max 255 chars"


@dataclass
class ErrorCase:
    """An expected error response."""

    status: int
    condition: str  # e.g. "title is empty"
    response: str  # e.g. '{"error": "Title is required"}'


@dataclass
class TicketSpec:
    """A single granular implementation ticket derived from a domain model."""

    # Identity
    id: str  # e.g. "board-create"
    title: str  # e.g. "Board creation — POST /api/boards with title and background"
    entity: str  # e.g. "Board"
    operation: str  # e.g. "create", "model", "list", "ui-list-page", etc.
    priority: int  # 1=foundation, 2=core CRUD, 3=state/relationships, 4=UI, 5=polish
    dependencies: list[str] = field(default_factory=list)  # ticket IDs

    # API contract
    api_method: str = ""
    api_endpoint: str = ""
    request_fields: list[FieldSpec] = field(default_factory=list)
    response_fields: list[FieldSpec] = field(default_factory=list)
    error_cases: list[ErrorCase] = field(default_factory=list)

    # UI specification
    ui_trigger: str = ""
    ui_location: str = ""
    ui_components: list[str] = field(default_factory=list)
    screenshot_ref: str = ""

    # State machine
    preconditions: list[str] = field(default_factory=list)
    postconditions: list[str] = field(default_factory=list)
    state_before: str = ""
    state_after: str = ""

    # Acceptance criteria (verifiable)
    acceptance_criteria: list[str] = field(default_factory=list)

    # Metadata
    entity_fields_needed: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    validated: bool = False

    # Legacy compat — used by old tests
    @property
    def required_fields(self) -> list[str]:
        return [f.name for f in self.request_fields if f.required]

    @property
    def optional_fields(self) -> list[str]:
        return [f.name for f in self.request_fields if not f.required]

    @property
    def state_constraints(self) -> list[str]:
        """Backwards-compat: return state transitions as strings."""
        constraints = []
        if self.state_before and self.state_after:
            constraints.append(f"{self.state_before} → {self.state_after}")
        return constraints

    @property
    def validation_rules(self) -> list[str]:
        """Backwards-compat: return field validation rules as strings."""
        rules = []
        for f in self.request_fields:
            if f.required:
                rules.append(f"{f.name} is required")
            if f.validation:
                rules.append(f"{f.name}: {f.validation}")
        return rules

    @property
    def preconditions_list(self) -> list[str]:
        return self.preconditions

    @property
    def postconditions_list(self) -> list[str]:
        return self.postconditions


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class ModelTicketGenerator:
    """Generate granular implementation tickets from a DomainModel."""

    def generate_tickets(self, model: DomainModel) -> list[TicketSpec]:
        """
        Generate all tickets in dependency-wave order:

          Wave 1 — Prisma model tickets (one per entity)
          Wave 2 — Core CRUD (list, read, create, update, delete per entity)
          Wave 3 — State transitions
          Wave 4 — Relationship operations
          Wave 5 — UI pages (list page, detail page, operation triggers)
          Wave 6 — Edge cases and polish
        """
        tickets: list[TicketSpec] = []

        # Wave 1: entity model tickets
        wave1 = self._wave1_model_tickets(model)
        tickets.extend(wave1)

        # Wave 2: core CRUD
        wave2 = self._wave2_crud_tickets(model)
        tickets.extend(wave2)

        # Wave 3: state transitions
        wave3 = self._wave3_state_transition_tickets(model)
        tickets.extend(wave3)

        # Wave 4: relationship operations
        wave4 = self._wave4_relationship_tickets(model)
        tickets.extend(wave4)

        # Wave 5: UI pages
        wave5 = self._wave5_ui_tickets(model)
        tickets.extend(wave5)

        # Wave 6: edge cases
        wave6 = self._wave6_edge_case_tickets(model)
        tickets.extend(wave6)

        return tickets

    # ------------------------------------------------------------------
    # Wave 1 — Entity Prisma model tickets
    # ------------------------------------------------------------------

    def _wave1_model_tickets(self, model: DomainModel) -> list[TicketSpec]:
        tickets = []
        for entity in model.entities.values():
            ticket = self._entity_model_ticket(entity)
            tickets.append(ticket)
        return tickets

    def _entity_model_ticket(self, entity: EntityHypothesis) -> TicketSpec:
        entity_slug = entity.name.lower()
        ticket_id = f"{entity_slug}-model"

        # Determine if needs auth dependency
        has_user_field = any(
            fname in ("userId", "user_id", "ownerId") or "user" in fname.lower()
            for fname in entity.fields
        )
        deps = ["auth-setup"] if has_user_field else []

        request_fields = [
            FieldSpec(
                name=fname,
                field_type=str(fh.field_type),
                required=fh.required,
                validation=", ".join(
                    [v for v in [
                        f"min_length: {fh.min_length}" if fh.min_length is not None else "",
                        f"max_length: {fh.max_length}" if fh.max_length is not None else "",
                        f"one of: {', '.join(fh.enum_values)}" if fh.enum_values else "",
                    ] if v]
                ),
            )
            for fname, fh in entity.fields.items()
        ]

        ac = [
            f"{entity.name} Prisma model is defined with all discovered fields",
            "Field types match observed API responses",
            "Migration runs without errors: npx prisma migrate dev",
        ]
        if entity.relationships:
            rel_desc = ", ".join(
                f"{r.relation_type} {r.to_entity}" for r in entity.relationships
            )
            ac.append(f"Relationships are implemented: {rel_desc}")
        if entity.states:
            ac.append(f"State column supports values: {', '.join(entity.states)}")

        return TicketSpec(
            id=ticket_id,
            title=f"Define {entity.name} Prisma model — fields, relations, indexes",
            entity=entity.name,
            operation="model",
            priority=1,
            dependencies=deps,
            api_endpoint=entity.api_prefix,
            request_fields=request_fields,
            acceptance_criteria=ac,
            entity_fields_needed=list(entity.fields.keys()),
            labels=["backend", "database", "prisma"],
            evidence=list(entity.evidence),
            validated=entity.confidence >= 0.5,
        )

    # ------------------------------------------------------------------
    # Wave 2 — Core CRUD tickets
    # ------------------------------------------------------------------

    def _wave2_crud_tickets(self, model: DomainModel) -> list[TicketSpec]:
        tickets = []
        for entity in model.entities.values():
            entity_slug = entity.name.lower()
            model_dep = f"{entity_slug}-model"

            # Index existing custom ops by name to use their endpoint/field data
            op_by_name: dict[str, OperationHypothesis] = {
                op.name: op for op in entity.operations
            }

            # Standard CRUD operations to generate
            crud_ops = ["create", "list", "read", "update", "delete"]
            for crud in crud_ops:
                ticket = self._crud_ticket(entity, crud, model_dep, op_by_name)
                tickets.append(ticket)

            # Also generate non-standard operations (those not in the CRUD set)
            standard = {"create", "read", "update", "delete", "list"}
            for op in entity.operations:
                if op.name not in standard:
                    ticket = self._operation_to_ticket(entity, op, model_dep)
                    tickets.append(ticket)

        return tickets

    def _crud_ticket(
        self,
        entity: EntityHypothesis,
        crud: str,
        model_dep: str,
        op_by_name: dict[str, OperationHypothesis],
    ) -> TicketSpec:
        entity_slug = entity.name.lower()
        plural = entity.plural or f"{entity_slug}s"
        api_prefix = entity.api_prefix or f"/api/{plural}"

        # Use existing op data if available, otherwise synthesise defaults
        op = op_by_name.get(crud)

        if crud == "create":
            method = op.method if op else "POST"
            endpoint = op.endpoint_pattern if op else api_prefix
            status = op.response_status if op else 201
            req_fields = list(op.required_fields) if op else []
            opt_fields = list(op.optional_fields) if op else []
            ac = self._generate_acceptance_criteria_create(entity, endpoint, req_fields, status)
            _unauth_resp = '{"error": "Unauthorized"}'
            _unauth = ErrorCase(status=401, condition="not authenticated", response=_unauth_resp)
            error_cases = [_unauth]
            for f in req_fields:
                _msg = f'{{"error": "{f.capitalize()} is required"}}'
                error_cases.append(ErrorCase(status=400, condition=f"{f} is empty", response=_msg))

        elif crud == "list":
            method = "GET"
            endpoint = op.endpoint_pattern if op else api_prefix
            status = op.response_status if op else 200
            req_fields = []
            opt_fields = []
            ac = self._generate_acceptance_criteria_list(entity, endpoint)
            _unauth_resp = '{"error": "Unauthorized"}'
            _unauth = ErrorCase(status=401, condition="not authenticated", response=_unauth_resp)
            error_cases = [_unauth]

        elif crud == "read":
            method = "GET"
            endpoint = op.endpoint_pattern if op else f"{api_prefix}/{{id}}"
            status = op.response_status if op else 200
            req_fields = []
            opt_fields = []
            ac = self._generate_acceptance_criteria_read(entity, endpoint)
            error_cases = self._standard_error_cases()

        elif crud == "update":
            method = op.method if op else "PATCH"
            endpoint = op.endpoint_pattern if op else f"{api_prefix}/{{id}}"
            status = op.response_status if op else 200
            req_fields = list(op.required_fields) if op else []
            opt_fields = list(op.optional_fields) if op else []
            ac = self._generate_acceptance_criteria_update(entity, method, endpoint, status)
            error_cases = self._standard_error_cases()

        else:  # delete
            method = op.method if op else "DELETE"
            endpoint = op.endpoint_pattern if op else f"{api_prefix}/{{id}}"
            status = op.response_status if op else 204
            req_fields = []
            opt_fields = []
            ac = self._generate_acceptance_criteria_delete(entity, method, endpoint, status)
            error_cases = self._standard_error_cases()

        all_fields = (
            [FieldSpec(name=f, field_type="string", required=True) for f in req_fields]
            + [FieldSpec(name=f, field_type="string", required=False) for f in opt_fields]
        )

        crud_label_map = {
            "create": "Create",
            "list": "List",
            "read": "Read (single)",
            "update": "Update",
            "delete": "Delete",
        }
        title = f"{entity.name} {crud_label_map.get(crud, crud)} — {method} {endpoint}"

        ui_trigger = op.ui_trigger if op else ""
        ui_location = op.ui_location if op else ""
        preconditions = list(op.preconditions) if op else ["User is authenticated"]
        postconditions = list(op.postconditions) if op else []

        return TicketSpec(
            id=f"{entity_slug}-{crud}",
            title=title,
            entity=entity.name,
            operation=crud,
            priority=2,
            dependencies=[model_dep],
            api_method=method,
            api_endpoint=endpoint,
            request_fields=all_fields,
            error_cases=error_cases,
            ui_trigger=ui_trigger,
            ui_location=ui_location,
            preconditions=preconditions,
            postconditions=postconditions,
            acceptance_criteria=ac,
            labels=self._labels_for_crud(crud),
            evidence=list(op.evidence) if op else [],
            validated=op.validated if op else False,
        )

    def _operation_to_ticket(
        self, entity: EntityHypothesis, op: OperationHypothesis, model_dep: str
    ) -> TicketSpec:
        """Convert a non-standard OperationHypothesis to a TicketSpec."""
        entity_slug = entity.name.lower()

        state_constraints_for_op = [
            t for t in entity.transitions if t.operation == op.name
        ]
        state_before = state_constraints_for_op[0].from_state if state_constraints_for_op else ""
        state_after = state_constraints_for_op[0].to_state if state_constraints_for_op else ""

        req_fields = [
            FieldSpec(name=f, field_type="string", required=True) for f in op.required_fields
        ]
        opt_fields = [
            FieldSpec(name=f, field_type="string", required=False) for f in op.optional_fields
        ]

        ac = [f"{op.method} {op.endpoint_pattern} returns {op.response_status}"]
        if op.required_fields:
            ac.append(f"Request includes required fields: {', '.join(op.required_fields)}")
        if op.postconditions:
            ac.extend(op.postconditions)
        if op.ui_trigger:
            ac.append(f"UI: {op.ui_trigger}")

        error_cases = [
            ErrorCase(
                status=e.get("status", 400),
                condition=e.get("condition", ""),
                response=e.get("response", ""),
            )
            for e in op.error_cases
        ]

        return TicketSpec(
            id=f"{entity_slug}-{op.name}",
            title=f"{entity.name} {op.name} — {op.method} {op.endpoint_pattern}",
            entity=entity.name,
            operation=op.name,
            priority=2,
            dependencies=[model_dep],
            api_method=op.method,
            api_endpoint=op.endpoint_pattern,
            request_fields=req_fields + opt_fields,
            error_cases=error_cases,
            ui_trigger=op.ui_trigger,
            ui_location=op.ui_location,
            preconditions=list(op.preconditions),
            postconditions=list(op.postconditions),
            state_before=state_before,
            state_after=state_after,
            acceptance_criteria=ac,
            labels=["backend", "api"],
            evidence=list(op.evidence),
            validated=op.validated,
        )

    # ------------------------------------------------------------------
    # Wave 3 — State transition tickets
    # ------------------------------------------------------------------

    def _wave3_state_transition_tickets(self, model: DomainModel) -> list[TicketSpec]:
        tickets = []
        for entity in model.entities.values():
            entity_slug = entity.name.lower()
            model_dep = f"{entity_slug}-model"
            crud_dep = f"{entity_slug}-update"

            for transition in entity.transitions:
                ticket_id = f"{entity_slug}-{transition.from_state}-to-{transition.to_state}"
                api_prefix = entity.api_prefix or f"/api/{entity.plural or entity_slug + 's'}"
                endpoint = f"{api_prefix}/{{id}}"

                from_s = transition.from_state
                to_s = transition.to_state
                ac = [
                    f"PATCH {endpoint} with transition payload returns 200",
                    f"{entity.name} state changes from '{from_s}' to '{to_s}'",
                    f"GET {endpoint} reflects new state '{to_s}'",
                ]
                if transition.reversible:
                    ac.append(
                        f"Transition is reversible: '{to_s}' → '{from_s}' is also valid"
                    )
                else:
                    ac.append(f"Transition '{to_s}' → '{from_s}' returns 422")

                tickets.append(TicketSpec(
                    id=ticket_id,
                    title=f"{entity.name} {from_s}→{to_s} — PATCH {endpoint}",
                    entity=entity.name,
                    operation=f"{from_s}-to-{to_s}",
                    priority=3,
                    dependencies=[model_dep, crud_dep],
                    api_method="PATCH",
                    api_endpoint=endpoint,
                    preconditions=[
                        "User is authenticated",
                        f"{entity.name} is in '{from_s}' state",
                    ],
                    postconditions=[f"{entity.name} is in '{to_s}' state"],
                    state_before=from_s,
                    state_after=to_s,
                    acceptance_criteria=ac,
                    labels=["backend", "api", "state-machine"],
                    validated=transition.validated,
                ))

        return tickets

    # ------------------------------------------------------------------
    # Wave 4 — Relationship operation tickets
    # ------------------------------------------------------------------

    def _wave4_relationship_tickets(self, model: DomainModel) -> list[TicketSpec]:
        tickets = []
        for entity in model.entities.values():
            entity_slug = entity.name.lower()

            for rel in entity.relationships:
                if rel.relation_type == "has_many":
                    child_slug = rel.to_entity.lower()
                    child_model_dep = f"{child_slug}-model"
                    parent_model_dep = f"{entity_slug}-model"

                    api_prefix = entity.api_prefix or f"/api/{entity.plural or entity_slug + 's'}"
                    child_prefix = rel.to_entity.lower() + "s"
                    endpoint = f"{api_prefix}/{{id}}/{child_prefix}"

                    # Add child to parent
                    add_id = f"{entity_slug}-add-{child_slug}"
                    ac_add = [
                        f"POST {endpoint} with valid data returns 201",
                        f"Response includes the created {rel.to_entity} linked to {entity.name}",
                        f"GET {endpoint} includes the new {rel.to_entity}",
                        f"POST {endpoint} without authentication returns 401",
                    ]
                    tickets.append(TicketSpec(
                        id=add_id,
                        title=f"Add {rel.to_entity} to {entity.name} — POST {endpoint}",
                        entity=entity.name,
                        operation=f"add-{child_slug}",
                        priority=3,
                        dependencies=[parent_model_dep, child_model_dep],
                        api_method="POST",
                        api_endpoint=endpoint,
                        preconditions=["User is authenticated", f"{entity.name} exists"],
                        postconditions=[f"{rel.to_entity} is linked to {entity.name}"],
                        acceptance_criteria=ac_add,
                        labels=["backend", "api", "relationships"],
                    ))

                    # Remove child from parent
                    remove_id = f"{entity_slug}-remove-{child_slug}"
                    remove_endpoint = f"{endpoint}/{{childId}}"
                    ac_remove = [
                        f"DELETE {remove_endpoint} returns 204",
                        f"GET {endpoint} no longer includes the removed {rel.to_entity}",
                        f"DELETE {remove_endpoint} with non-existent ID returns 404",
                        f"DELETE {remove_endpoint} without authentication returns 401",
                    ]
                    remove_title = (
                        f"Remove {rel.to_entity} from {entity.name}"
                        f" — DELETE {remove_endpoint}"
                    )
                    tickets.append(TicketSpec(
                        id=remove_id,
                        title=remove_title,
                        entity=entity.name,
                        operation=f"remove-{child_slug}",
                        priority=3,
                        dependencies=[parent_model_dep, child_model_dep, add_id],
                        api_method="DELETE",
                        api_endpoint=remove_endpoint,
                        preconditions=[
                            "User is authenticated",
                            f"{entity.name} exists",
                            f"{rel.to_entity} is linked to {entity.name}",
                        ],
                        postconditions=[f"{rel.to_entity} is no longer linked to {entity.name}"],
                        acceptance_criteria=ac_remove,
                        labels=["backend", "api", "relationships"],
                    ))

        return tickets

    # ------------------------------------------------------------------
    # Wave 5 — UI page tickets
    # ------------------------------------------------------------------

    def _wave5_ui_tickets(self, model: DomainModel) -> list[TicketSpec]:
        tickets = []
        for entity in model.entities.values():
            entity_slug = entity.name.lower()
            plural = entity.plural or f"{entity_slug}s"
            crud_list_dep = f"{entity_slug}-list"
            crud_read_dep = f"{entity_slug}-read"
            crud_create_dep = f"{entity_slug}-create"
            crud_update_dep = f"{entity_slug}-update"
            crud_delete_dep = f"{entity_slug}-delete"

            # List/grid page
            list_page_id = f"{entity_slug}-ui-list-page"
            ac_list_page = [
                f"/{plural} page renders without errors",
                f"Page displays all {plural} belonging to the user",
                f"Each {entity.name} card shows its key fields",
                "Page is accessible without JS errors in the browser console",
                f"/{plural} returns HTTP 200",
            ]
            tickets.append(TicketSpec(
                id=list_page_id,
                title=f"{entity.name} list page — /{plural}",
                entity=entity.name,
                operation="ui-list-page",
                priority=4,
                dependencies=[crud_list_dep],
                ui_location=f"/{plural}",
                ui_components=[f"{entity.name}Card", f"{entity.name}Grid"],
                acceptance_criteria=ac_list_page,
                labels=["frontend", "ui"],
            ))

            # Detail/edit page
            detail_page_id = f"{entity_slug}-ui-detail-page"
            ac_detail_page = [
                f"/{plural}/{{id}} page renders the {entity.name} details",
                "All fields are displayed correctly",
                f"/{plural}/{{id}} for a non-existent ID shows 404 page",
                "Page is accessible without JS errors in the browser console",
            ]
            tickets.append(TicketSpec(
                id=detail_page_id,
                title=f"{entity.name} detail page — /{plural}/{{id}}",
                entity=entity.name,
                operation="ui-detail-page",
                priority=4,
                dependencies=[crud_read_dep],
                ui_location=f"/{plural}/{{id}}",
                ui_components=[f"{entity.name}Detail", f"{entity.name}Header"],
                acceptance_criteria=ac_detail_page,
                labels=["frontend", "ui"],
            ))

            # Create modal / form trigger
            create_ui_id = f"{entity_slug}-ui-create-form"
            ac_create_ui = [
                f"'Create {entity.name}' button is visible on /{plural} page",
                "Clicking the button opens a modal or form",
                "Form validates required fields before submitting",
                "Successful submission closes the form and shows the new item",
                "Failed submission shows inline error messages",
            ]
            tickets.append(TicketSpec(
                id=create_ui_id,
                title=f"{entity.name} create form — UI trigger on /{plural}",
                entity=entity.name,
                operation="ui-create-form",
                priority=4,
                dependencies=[crud_create_dep, list_page_id],
                ui_trigger=f"Click 'Create {entity.name}' button on /{plural}",
                ui_location=f"/{plural}",
                ui_components=[f"Create{entity.name}Modal", f"{entity.name}Form"],
                acceptance_criteria=ac_create_ui,
                labels=["frontend", "ui"],
            ))

            # Edit form trigger
            edit_ui_id = f"{entity_slug}-ui-edit-form"
            ac_edit_ui = [
                f"'Edit' button or link is visible on the {entity.name} detail page",
                "Clicking opens an edit form pre-populated with current values",
                "Saving updates the {entity.name} and reflects changes in the UI",
                "Cancelling discards changes",
            ]
            tickets.append(TicketSpec(
                id=edit_ui_id,
                title=f"{entity.name} edit form — UI trigger on /{plural}/{{id}}",
                entity=entity.name,
                operation="ui-edit-form",
                priority=4,
                dependencies=[crud_update_dep, detail_page_id],
                ui_trigger=f"Click 'Edit' on /{plural}/{{id}}",
                ui_location=f"/{plural}/{{id}}",
                ui_components=[f"Edit{entity.name}Modal", f"{entity.name}Form"],
                acceptance_criteria=ac_edit_ui,
                labels=["frontend", "ui"],
            ))

            # Delete confirmation
            delete_ui_id = f"{entity_slug}-ui-delete-confirm"
            ac_delete_ui = [
                f"'Delete' button is visible on the {entity.name} detail or list page",
                "Clicking shows a confirmation dialog",
                "Confirming deletes the item and redirects to the list page",
                "Cancelling keeps the item intact",
            ]
            tickets.append(TicketSpec(
                id=delete_ui_id,
                title=f"{entity.name} delete confirmation — UI trigger",
                entity=entity.name,
                operation="ui-delete-confirm",
                priority=4,
                dependencies=[crud_delete_dep, detail_page_id],
                ui_trigger=f"Click 'Delete' on /{plural}/{{id}}",
                ui_location=f"/{plural}/{{id}}",
                ui_components=["ConfirmDeleteModal"],
                acceptance_criteria=ac_delete_ui,
                labels=["frontend", "ui"],
            ))

        return tickets

    # ------------------------------------------------------------------
    # Wave 6 — Edge cases and polish
    # ------------------------------------------------------------------

    def _wave6_edge_case_tickets(self, model: DomainModel) -> list[TicketSpec]:
        tickets = []
        for entity in model.entities.values():
            entity_slug = entity.name.lower()
            crud_dep = f"{entity_slug}-create"

            # Validation rules
            validation_fields = [
                (fname, fh) for fname, fh in entity.fields.items()
                if fh.required or fh.max_length is not None or fh.enum_values
            ]
            if validation_fields:
                validation_id = f"{entity_slug}-validation"
                ac_validation: list[str] = []
                for fname, fh in validation_fields:
                    if fh.required:
                        ac_validation.append(f"POST without '{fname}' returns 400")
                    if fh.max_length is not None:
                        ac_validation.append(
                            f"POST with '{fname}' > {fh.max_length} chars returns 400"
                        )
                    if fh.enum_values:
                        ac_validation.append(
                            f"POST with invalid '{fname}' value returns 400"
                        )
                if ac_validation:
                    tickets.append(TicketSpec(
                        id=validation_id,
                        title=f"{entity.name} field validation — required fields and constraints",
                        entity=entity.name,
                        operation="validation",
                        priority=5,
                        dependencies=[crud_dep],
                        api_method="POST",
                        api_endpoint=entity.api_prefix or f"/api/{entity.plural or entity_slug + 's'}",  # noqa: E501
                        acceptance_criteria=ac_validation,
                        labels=["backend", "validation"],
                    ))

            # Permission checks
            perm_id = f"{entity_slug}-permissions"
            api_prefix = entity.api_prefix or f"/api/{entity.plural or entity_slug + 's'}"
            ac_perms = [
                f"GET {api_prefix} without authentication returns 401",
                f"POST {api_prefix} without authentication returns 401",
                f"PATCH {api_prefix}/{{id}} for another user's {entity.name} returns 403",
                f"DELETE {api_prefix}/{{id}} for another user's {entity.name} returns 403",
            ]
            tickets.append(TicketSpec(
                id=perm_id,
                title=f"{entity.name} permission checks — auth and ownership",
                entity=entity.name,
                operation="permissions",
                priority=5,
                dependencies=[crud_dep],
                acceptance_criteria=ac_perms,
                labels=["backend", "security"],
            ))

        return tickets

    # ------------------------------------------------------------------
    # Acceptance criteria generators
    # ------------------------------------------------------------------

    def _generate_acceptance_criteria_create(
        self,
        entity: EntityHypothesis,
        endpoint: str,
        required_fields: list[str],
        status: int,
    ) -> list[str]:
        acs = [
            f"POST {endpoint} with valid data returns {status}",
            f"Response includes the created {entity.name} with all fields including id",
        ]
        for field_name in required_fields:
            acs.append(f"POST without '{field_name}' returns 400 with error message")
        acs.append(f"Created {entity.name} appears in GET {entity.api_prefix or endpoint} list")
        acs.append("POST without authentication returns 401")
        return acs

    def _generate_acceptance_criteria_list(
        self,
        entity: EntityHypothesis,
        endpoint: str,
    ) -> list[str]:
        return [
            f"GET {endpoint} returns 200 with array of {entity.name} objects",
            f"Response only includes {entity.name} objects belonging to the authenticated user",
            "Response is an array (even if empty)",
            "GET without authentication returns 401",
        ]

    def _generate_acceptance_criteria_read(
        self,
        entity: EntityHypothesis,
        endpoint: str,
    ) -> list[str]:
        return [
            f"GET {endpoint} returns 200 with the {entity.name} and all fields",
            "GET with non-existent ID returns 404",
            "GET without authentication returns 401",
            f"Response includes all expected {entity.name} fields",
        ]

    def _generate_acceptance_criteria_update(
        self,
        entity: EntityHypothesis,
        method: str,
        endpoint: str,
        status: int,
    ) -> list[str]:
        return [
            f"{method} {endpoint} with valid data returns {status}",
            f"Response includes the updated {entity.name} with new values",
            f"{method} with non-existent ID returns 404",
            f"{method} without authentication returns 401",
            "Only provided fields are updated; others are unchanged",
        ]

    def _generate_acceptance_criteria_delete(
        self,
        entity: EntityHypothesis,
        method: str,
        endpoint: str,
        status: int,
    ) -> list[str]:
        api_prefix = entity.api_prefix or endpoint.rsplit("/", 2)[0]
        return [
            f"{method} {endpoint} returns {status}",
            f"GET {api_prefix} no longer includes the deleted {entity.name}",
            f"{method} with non-existent ID returns 404",
            f"{method} without authentication returns 401",
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _standard_error_cases(self) -> list[ErrorCase]:
        """Return the standard 401 + 404 error cases used by read/update/delete."""
        return [
            ErrorCase(
                status=401,
                condition="not authenticated",
                response='{"error": "Unauthorized"}',
            ),
            ErrorCase(
                status=404,
                condition="ID does not exist",
                response='{"error": "Not found"}',
            ),
        ]

    def _labels_for_crud(self, crud: str) -> list[str]:
        if crud in ("list", "read"):
            return ["backend", "api"]
        if crud in ("create", "update", "delete"):
            return ["backend", "api", "frontend"]
        return ["backend", "api"]

    # ------------------------------------------------------------------
    # GitHub issue body renderer
    # ------------------------------------------------------------------

    def render_issue_body(self, ticket: TicketSpec) -> str:
        """Render a GitHub issue body from a TicketSpec."""
        lines: list[str] = []

        lines.append(f"## {ticket.entity}: {ticket.operation}")
        lines.append("")

        # API contract
        if ticket.api_method or ticket.api_endpoint:
            lines.append("### API Contract")
            if ticket.api_method:
                lines.append(f"- **Method:** {ticket.api_method}")
            if ticket.api_endpoint:
                lines.append(f"- **Endpoint:** {ticket.api_endpoint}")
            if ticket.request_fields:
                lines.append("- **Request body:**")
                for f in ticket.request_fields:
                    req_str = "required" if f.required else "optional"
                    val_str = f" — {f.validation}" if f.validation else ""
                    lines.append(f"  - `{f.name}` ({f.field_type}, {req_str}){val_str}")
            if ticket.response_fields:
                lines.append("- **Response body:**")
                for f in ticket.response_fields:
                    lines.append(f"  - `{f.name}` ({f.field_type})")
            if ticket.error_cases:
                lines.append("- **Error responses:**")
                for ec in ticket.error_cases:
                    lines.append(f"  - {ec.status}: {ec.condition}")
            lines.append("")

        # UI specification
        if ticket.ui_trigger or ticket.ui_location or ticket.ui_components:
            lines.append("### UI Specification")
            if ticket.ui_trigger:
                lines.append(f"- **Trigger:** {ticket.ui_trigger}")
            if ticket.ui_location:
                lines.append(f"- **Location:** {ticket.ui_location}")
            if ticket.ui_components:
                lines.append(f"- **Components:** {', '.join(ticket.ui_components)}")
            if ticket.screenshot_ref:
                ref = ticket.screenshot_ref
                lines.append(f"- **Screenshot:** [{ref}]({ref})")
            lines.append("")

        # State machine
        if ticket.preconditions or ticket.state_before or ticket.postconditions:
            lines.append("### State Machine")
            if ticket.preconditions:
                for pre in ticket.preconditions:
                    lines.append(f"- **Precondition:** {pre}")
            if ticket.state_before and ticket.state_after:
                sb, sa = ticket.state_before, ticket.state_after
                lines.append(f"- **State transition:** {sb} → {sa}")
            elif ticket.state_before:
                lines.append(f"- **State before:** {ticket.state_before}")
            if ticket.postconditions:
                for post in ticket.postconditions:
                    lines.append(f"- **Postcondition:** {post}")
            lines.append("")

        # Acceptance criteria
        lines.append("### Acceptance Criteria")
        for ac in ticket.acceptance_criteria:
            lines.append(f"- [ ] {ac}")
        lines.append("")

        # Dependencies
        if ticket.dependencies:
            lines.append("### Dependencies")
            for dep in ticket.dependencies:
                lines.append(f"- Depends on: #{dep}")
            lines.append("")

        lines.append("---")
        lines.append("*Generated by duplicat-rex from validated domain model*")

        return "\n".join(lines)
