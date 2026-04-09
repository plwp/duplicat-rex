"""
HypothesisBuilder — build initial DomainModel from observed facts.

Parses API endpoint facts to discover entities, response bodies for fields,
UI facts for operations, and infers relationships from URL nesting.
All hypotheses start with validated=False.
"""

from __future__ import annotations

import re
from typing import Any

from scripts.domain_model import (
    DomainModel,
    EntityHypothesis,
    FieldHypothesis,
    FieldType,
    OperationHypothesis,
    RelationshipHypothesis,
)
from scripts.models import Fact, FactCategory

# Map HTTP methods to operation names
_METHOD_TO_OP: dict[str, str] = {
    "GET": "read",
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}

# Standard CRUD operations to hypothesize for every discovered entity
_CRUD_OPS = [
    ("create", "POST", 201),
    ("read", "GET", 200),
    ("update", "PUT", 200),
    ("delete", "DELETE", 204),
]

# Segments that are not entity names (version prefixes, fixed path segments)
_IGNORED_SEGMENTS = frozenset(["api", "v1", "v2", "v3", "1", "2", "3", "rest"])

# Infrastructure path segments that are not product entities
_INFRASTRUCTURE_PREFIXES = frozenset({
    # Gateway/infrastructure
    "gateway", "consent", "consenthub", "session", "heartbeat",
    "object-resolver", "resolve", "tap-delivery", "personalization",
    "accessible-product", "flagcdn", "graphql", "batch",
    # Short noise segments
    "px", "wa", "td", "tr", "v", "u", "b", "w",
    # Analytics/tracking
    "li_sync", "activity", "activityi",
    # Known non-product paths
    "home", "inbox", "planner", "pricing", "butler-automation",
    "power-ups", "integrations", "templates",
    # Atlassian internal
    "slack", "trello", "atlassian-connect", "migration",
    "connect-fields-migration-test-app", "connect-read-only-date",
    "app-software", "product-listing", "developer-space",
    # Cookie/consent
    "cookies", "cooky", "cookys",
})

# Regex to detect ID-like path segments: {id}, :id, numeric, UUID-like,
# or alphanumeric strings containing digits (likely IDs, not resource names).
# Resource names are typically pure alphabetic (e.g. "boards", "cards").
_ID_SEGMENT_RE = re.compile(
    r"^\{.*\}$"  # {boardId}
    r"|^:[a-zA-Z]"  # :id, :boardId
    r"|^\d+$"  # 123
    r"|^[a-f0-9]{8}-[a-f0-9]{4}"  # UUID prefix
    r"|^[a-f0-9]{24}$"  # MongoDB ObjectId
    r"|^[a-zA-Z]*\d+[a-zA-Z0-9]*$",  # any segment containing digits (e.g. abc123)
)


def _is_id_segment(segment: str) -> bool:
    return bool(_ID_SEGMENT_RE.match(segment))


def _to_entity_name(plural: str) -> str:
    """Convert a plural API resource name to a singular entity name."""
    plural = plural.lower()
    if plural.endswith("ies"):
        return plural[:-3].capitalize() + "y"
    if plural.endswith("ses") or plural.endswith("xes") or plural.endswith("zes"):
        return plural[:-2].capitalize()
    if plural.endswith("s"):
        return plural[:-1].capitalize()
    return plural.capitalize()


def _parse_api_path(path: str) -> list[str]:
    """
    Extract ordered resource segments from an API path.

    /1/boards/{boardId}/lists  →  ["boards", "lists"]
    /api/v1/cards              →  ["cards"]
    """
    parts = [p for p in path.strip("/").split("/") if p]
    resources = []
    for part in parts:
        if part in _IGNORED_SEGMENTS:
            continue
        if _is_id_segment(part):
            continue
        resources.append(part)
    return resources


def _is_product_entity(name: str) -> bool:
    """Filter out infrastructure path segments that aren't product entities."""
    name_lower = name.lower()
    if name_lower in _INFRASTRUCTURE_PREFIXES:
        return False
    if len(name_lower) <= 2:  # skip single/double letter paths
        return False
    if ":" in name or "." in name:  # skip UUIDs, domains, file extensions
        return False
    if re.match(r"^[a-f0-9-]{8,}$", name_lower):  # skip hex IDs/UUIDs
        return False
    return True


class HypothesisBuilder:
    """Build an initial DomainModel from a list of observed Facts."""

    def build(
        self,
        facts: list[Fact],
        target: str,
        seed_entities: list[dict[str, Any]] | None = None,
    ) -> DomainModel:
        model = DomainModel(target=target)
        if seed_entities:
            self._seed_entities(seed_entities, model)
        self._extract_entities_from_api_paths(facts, model)
        self._extract_fields_from_responses(facts, model)
        self._extract_operations_from_facts(facts, model)
        self._infer_relationships(model)
        self._add_standard_crud_hypotheses(model)
        return model

    def _seed_entities(
        self, seeds: list[dict[str, Any]], model: DomainModel
    ) -> None:
        """Pre-populate the model with known entities."""
        for seed in seeds:
            name = seed["name"]
            if name not in model.entities:
                entity = EntityHypothesis(
                    name=name,
                    plural=seed.get("plural", name.lower() + "s"),
                    api_prefix=seed.get("api_prefix", f"/api/{name.lower()}s"),
                    states=seed.get("states", []),
                    evidence=["seeded from domain knowledge"],
                )
                # Add seed fields
                for field_name, field_type in seed.get("fields", {}).items():
                    entity.fields[field_name] = FieldHypothesis(
                        name=field_name,
                        field_type=FieldType(field_type) if field_type in FieldType.__members__.values() else FieldType.STRING,
                        evidence=["seeded"],
                    )
                # Add seed relationships
                for rel in seed.get("relationships", []):
                    entity.relationships.append(RelationshipHypothesis(
                        from_entity=name,
                        to_entity=rel["to"],
                        relation_type=rel.get("type", "has_many"),
                    ))
                model.entities[name] = entity

    # ------------------------------------------------------------------
    # Phase 1: discover entities from URL patterns
    # ------------------------------------------------------------------

    def _extract_entities_from_api_paths(
        self, facts: list[Fact], model: DomainModel
    ) -> None:
        """Parse API endpoint URLs to discover entity resource names."""
        for fact in facts:
            if fact.category != FactCategory.API_ENDPOINT:
                continue

            # Prefer the URL from structured_data, fall back to evidence
            path = self._api_path_from_fact(fact)
            if not path:
                continue

            resources = _parse_api_path(path)
            for resource in resources:
                if not _is_product_entity(resource):
                    continue
                if resource not in model.entities:
                    entity_name = _to_entity_name(resource)
                    entity = EntityHypothesis(
                        name=entity_name,
                        plural=resource,
                        api_prefix=self._api_prefix_from_path(path, resource),
                        evidence=[fact.claim],
                    )
                    model.entities[entity_name] = entity
                else:
                    # Look up by entity name to accumulate evidence
                    entity_name = _to_entity_name(resource)
                    if entity_name in model.entities:
                        model.entities[entity_name].evidence.append(fact.claim)

    def _api_path_from_fact(self, fact: Fact) -> str:
        """Extract the API path from a fact's structured_data or evidence URL."""
        sd = fact.structured_data
        # browser_explore stores method/url in structured_data
        if "url" in sd:
            from urllib.parse import urlparse
            return urlparse(str(sd["url"])).path
        if "path" in sd:
            return str(sd["path"])
        if "endpoint" in sd:
            return str(sd["endpoint"])
        # Fall back to the first evidence source_url
        if fact.evidence:
            from urllib.parse import urlparse
            return urlparse(fact.evidence[0].source_url).path
        return ""

    def _api_prefix_from_path(self, full_path: str, resource: str) -> str:
        """Extract the prefix up to and including the resource segment."""
        parts = full_path.strip("/").split("/")
        idx = next((i for i, p in enumerate(parts) if p == resource), None)
        if idx is None:
            return f"/{resource}"
        prefix_parts = parts[: idx + 1]
        return "/" + "/".join(prefix_parts)

    # ------------------------------------------------------------------
    # Phase 2: discover fields from API response bodies
    # ------------------------------------------------------------------

    def _extract_fields_from_responses(
        self, facts: list[Fact], model: DomainModel
    ) -> None:
        """Parse response bodies in structured_data to discover entity fields."""
        for fact in facts:
            if fact.category not in (
                FactCategory.API_ENDPOINT,
                FactCategory.DATA_MODEL,
            ):
                continue

            sd = fact.structured_data
            response_body: dict[str, Any] | None = sd.get("response_body") or sd.get(
                "response"
            )
            if not isinstance(response_body, dict):
                continue

            # Try to match response to an entity by path
            path = self._api_path_from_fact(fact)
            entity = self._entity_for_path(path, model)
            if entity is None:
                continue

            for field_name, value in response_body.items():
                if field_name not in entity.fields:
                    entity.fields[field_name] = FieldHypothesis(
                        name=field_name,
                        field_type=self._infer_field_type(value),
                        evidence=[fact.claim],
                    )

    def _entity_for_path(
        self, path: str, model: DomainModel
    ) -> EntityHypothesis | None:
        """Find the most specific entity that matches this API path."""
        resources = _parse_api_path(path)
        if not resources:
            return None
        # Use the last resource segment (most specific)
        entity_name = _to_entity_name(resources[-1])
        return model.entities.get(entity_name)

    def _infer_field_type(self, value: Any) -> FieldType:
        if isinstance(value, bool):
            return FieldType.BOOLEAN
        if isinstance(value, int):
            return FieldType.INTEGER
        if isinstance(value, float):
            return FieldType.FLOAT
        if isinstance(value, str):
            # Rough datetime detection
            if re.match(r"\d{4}-\d{2}-\d{2}", value):
                return FieldType.DATETIME
            return FieldType.STRING
        return FieldType.UNKNOWN

    # ------------------------------------------------------------------
    # Phase 3: extract operations from observed facts
    # ------------------------------------------------------------------

    def _extract_operations_from_facts(
        self, facts: list[Fact], model: DomainModel
    ) -> None:
        """Convert API endpoint facts into OperationHypothesis entries."""
        for fact in facts:
            if fact.category != FactCategory.API_ENDPOINT:
                continue

            sd = fact.structured_data
            method = str(sd.get("method", "")).upper()
            if not method or method not in _METHOD_TO_OP:
                continue

            path = self._api_path_from_fact(fact)
            entity = self._entity_for_path(path, model)
            if entity is None:
                continue

            op_name = _METHOD_TO_OP[method]

            # Don't duplicate — check by (name, method, endpoint_pattern)
            existing = next(
                (
                    op
                    for op in entity.operations
                    if op.name == op_name and op.endpoint_pattern == path
                ),
                None,
            )
            if existing is not None:
                existing.evidence.append(fact.claim)
                continue

            op = OperationHypothesis(
                name=op_name,
                method=method,
                endpoint_pattern=path,
                evidence=[fact.claim],
            )

            # Capture UI trigger from UI_COMPONENT facts if claim mentions operation
            entity.operations.append(op)

        # Also scan UI_COMPONENT facts for operation triggers
        for fact in facts:
            if fact.category != FactCategory.UI_COMPONENT:
                continue
            claim_lower = fact.claim.lower()
            for entity in model.entities.values():
                for op in entity.operations:
                    if op.name in claim_lower and not op.ui_trigger:
                        op.ui_trigger = fact.claim

    # ------------------------------------------------------------------
    # Phase 4: infer relationships from URL nesting
    # ------------------------------------------------------------------

    def _infer_relationships(self, model: DomainModel) -> None:
        """
        From URL patterns like /boards/{id}/lists, infer that List belongs_to Board.
        Adds RelationshipHypothesis to the child entity and a has_many to the parent.
        """
        # Collect all (parent_resource, child_resource) pairs seen across entity api_prefixes
        # We'll detect nesting by checking if an entity's api_prefix contains another entity's
        # plural as a parent segment followed by an ID segment.

        plurals = {e.plural: e for e in model.entities.values() if e.plural}

        for entity in list(model.entities.values()):
            prefix = entity.api_prefix
            if not prefix:
                continue
            parts = prefix.strip("/").split("/")
            # Look for pattern: .../parent_plural/{id}/child_plural
            for i, part in enumerate(parts):
                if part in plurals and part != entity.plural:
                    # Check if there's an ID segment between this parent and the child
                    # The child is our current entity; if parent appears before child in path
                    parent_entity = plurals[part]
                    if parent_entity.name == entity.name:
                        continue
                    # Check if child already has this relationship
                    already = any(
                        r.from_entity == entity.name
                        and r.to_entity == parent_entity.name
                        for r in entity.relationships
                    )
                    if not already:
                        fk = parent_entity.plural.rstrip("s") + "Id"
                        entity.relationships.append(
                            RelationshipHypothesis(
                                from_entity=entity.name,
                                to_entity=parent_entity.name,
                                relation_type="belongs_to",
                                foreign_key=fk,
                            )
                        )
                    # Add has_many on the parent side
                    already_parent = any(
                        r.from_entity == parent_entity.name
                        and r.to_entity == entity.name
                        and r.relation_type == "has_many"
                        for r in parent_entity.relationships
                    )
                    if not already_parent:
                        parent_entity.relationships.append(
                            RelationshipHypothesis(
                                from_entity=parent_entity.name,
                                to_entity=entity.name,
                                relation_type="has_many",
                            )
                        )

    # ------------------------------------------------------------------
    # Phase 5: add standard CRUD hypotheses for every entity
    # ------------------------------------------------------------------

    def _add_standard_crud_hypotheses(self, model: DomainModel) -> None:
        """
        For each entity that lacks a given CRUD operation, add an unvalidated hypothesis.
        Only adds if an operation with that name doesn't already exist.
        """
        for entity in model.entities.values():
            existing_names = {op.name for op in entity.operations}
            for op_name, method, status in _CRUD_OPS:
                if op_name in existing_names:
                    continue
                if method in ("GET", "DELETE"):
                    endpoint = f"{entity.api_prefix}/{{id}}"
                elif method == "POST":
                    endpoint = entity.api_prefix
                else:
                    endpoint = f"{entity.api_prefix}/{{id}}"

                entity.operations.append(
                    OperationHypothesis(
                        name=op_name,
                        method=method,
                        endpoint_pattern=endpoint,
                        response_status=status,
                        evidence=["standard CRUD hypothesis"],
                    )
                )
