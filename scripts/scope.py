"""
Scope parser and dependency graph for duplicat-rex.

Parses user-specified scope strings into structured Scope objects with a
dependency graph. Implements the parsed → resolved → frozen lifecycle.

Invariants:
    INV-031: Feature keys are always lowercase hyphen-separated slugs.
    INV-037: Ordering comparisons use ordinal ranks, not lexical.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass, field


def _slugify(name: str) -> str:
    """Normalise an arbitrary feature name to a lowercase hyphen-separated slug.

    Examples:
        "Drag Drop"  → "drag-drop"
        "drag_drop"  → "drag-drop"
        "CardModal"  → "cardmodal"   (no camelCase split by design — keep simple)
        "  Boards  " → "boards"
    """
    # Lowercase, collapse whitespace/underscores/dashes to a single dash,
    # strip leading/trailing dashes.
    slug = name.strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = slug.strip("-")
    return slug


@dataclass
class ScopeFeature:
    """A single feature within a scope.

    Attributes:
        feature:       Canonical name (lowercase hyphen-separated slug). INV-031.
        description:   Optional human-readable description.
        depends_on:    Slugs of features this feature depends on.
        is_dependency: True if auto-detected as a transitive dependency rather
                       than explicitly requested by the user.
        priority:      Ordinal rank — 1=core, 2=important, 3=nice-to-have.
                       Use integer comparisons only (INV-037).
    """

    feature: str
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    is_dependency: bool = False
    priority: int = 1

    def __post_init__(self) -> None:
        # Enforce INV-031: feature must always be a slug.
        self.feature = _slugify(self.feature)
        # Normalise depends_on entries too.
        self.depends_on = [_slugify(d) for d in self.depends_on]


@dataclass
class Scope:
    """Structured scope object produced by parse_scope / add_dependencies / freeze_scope.

    Lifecycle: parsed → resolved (add_dependencies) → frozen (freeze_scope).
    Once frozen, no modifications are allowed.

    Attributes:
        raw_input:        Original user string, preserved verbatim.
        features:         Ordered list of ScopeFeature objects.
        known_exclusions: Feature slugs explicitly out of scope.
        target:           Target system slug (e.g. "trello").
        frozen:           When True, all mutations raise ValueError.
        scope_hash:       Deterministic hash set when the scope is frozen.
    """

    raw_input: str
    features: list[ScopeFeature] = field(default_factory=list)
    known_exclusions: list[str] = field(default_factory=list)
    target: str = ""
    frozen: bool = False
    scope_hash: str = ""

    # ------------------------------------------------------------------ #
    # Query helpers                                                        #
    # ------------------------------------------------------------------ #

    def feature_names(self) -> list[str]:
        """Return all feature slugs in insertion order."""
        return [f.feature for f in self.features]

    def dependency_order(self) -> list[list[str]]:
        """Return features in topological waves (Kahn's algorithm).

        Each wave is a list of feature slugs whose dependencies are all
        satisfied by previous waves. Features with no dependencies come first.

        Returns:
            List of waves. Each wave is a list of slugs.

        Raises:
            ValueError: If a cycle is detected in the dependency graph.
        """
        feature_map = {f.feature: f for f in self.features}
        in_degree: dict[str, int] = {f: 0 for f in feature_map}
        adjacency: dict[str, list[str]] = {f: [] for f in feature_map}

        for feat in self.features:
            for dep in feat.depends_on:
                if dep in feature_map:
                    adjacency[dep].append(feat.feature)
                    in_degree[feat.feature] += 1

        # Kahn's BFS
        queue: deque[str] = deque(
            slug for slug, deg in in_degree.items() if deg == 0
        )
        waves: list[list[str]] = []
        processed = 0

        while queue:
            wave: list[str] = []
            # Drain the current zero-in-degree frontier
            for _ in range(len(queue)):
                node = queue.popleft()
                wave.append(node)
                processed += 1
                for neighbour in adjacency[node]:
                    in_degree[neighbour] -= 1
                    if in_degree[neighbour] == 0:
                        queue.append(neighbour)
            waves.append(wave)

        if processed != len(feature_map):
            raise ValueError(
                "Cycle detected in dependency graph — cannot compute topological order."
            )

        return waves

    def to_dict(self) -> dict:  # type: ignore[type-arg]
        """Serialise to a plain dict (JSON-safe)."""
        return {
            "raw_input": self.raw_input,
            "target": self.target,
            "frozen": self.frozen,
            "scope_hash": self.scope_hash,
            "known_exclusions": self.known_exclusions,
            "features": [
                {
                    "feature": f.feature,
                    "description": f.description,
                    "depends_on": f.depends_on,
                    "is_dependency": f.is_dependency,
                    "priority": f.priority,
                }
                for f in self.features
            ],
        }

    def compute_scope_hash(self) -> str:
        """Compute a deterministic hash from the current feature + dependency set.

        Hash inputs (all sorted for determinism):
        - Requested feature slugs (is_dependency=False)
        - Resolved dependency slugs (is_dependency=True)
        - All dependency edges as (feature, dep) pairs

        Returns:
            16-character hex string (first 64 bits of SHA-256).
        """
        requested = sorted(f.feature for f in self.features if not f.is_dependency)
        dependencies = sorted(f.feature for f in self.features if f.is_dependency)
        edges = sorted(
            (f.feature, dep)
            for f in self.features
            for dep in f.depends_on
        )
        canonical = json.dumps(
            {"requested": requested, "dependencies": dependencies, "edges": edges},
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    # ------------------------------------------------------------------ #
    # Internal mutation guard                                              #
    # ------------------------------------------------------------------ #

    def _assert_mutable(self) -> None:
        if self.frozen:
            raise ValueError(
                "Scope is frozen — no modifications allowed. "
                "Create a new Scope for a new run."
            )


# ------------------------------------------------------------------ #
# Public API                                                          #
# ------------------------------------------------------------------ #


def parse_scope(raw_input: str, target: str = "") -> Scope:
    """Parse a comma-separated scope string into a Scope object.

    Accepts values like "boards, lists, cards, drag-drop" or
    "Drag Drop, Card Modal, Lists".

    REQUIRES:
        raw_input is non-empty.

    ENSURES:
        All feature keys are lowercase hyphen-separated slugs (INV-031).
        No duplicate features in output.

    Args:
        raw_input: User-supplied comma-separated feature list.
        target:    Optional target system slug.

    Returns:
        A Scope in the PARSED state (features extracted, no dependencies yet).

    Raises:
        ValueError: If raw_input is empty or produces no valid features.
    """
    if not raw_input or not raw_input.strip():
        raise ValueError("raw_input must not be empty.")

    seen: set[str] = set()
    features: list[ScopeFeature] = []

    for part in raw_input.split(","):
        slug = _slugify(part)
        if not slug:
            continue
        if slug in seen:
            continue
        seen.add(slug)
        features.append(ScopeFeature(feature=slug))

    if not features:
        raise ValueError(
            f"raw_input {raw_input!r} produced no valid features after normalisation."
        )

    return Scope(raw_input=raw_input, features=features, target=target)


def add_dependencies(
    scope: Scope,
    edges: list[tuple[str, str]],
    known_exclusions: list[str] | None = None,
) -> Scope:
    """Add dependency edges to a scope and flag transitive dependencies.

    Moves the scope from PARSED → RESOLVED state.

    For every edge (feature, dep):
    - If *dep* is not already in scope.features, it is added with
      is_dependency=True to indicate it was auto-included, not requested.
    - Cycles are detected eagerly and raise ValueError immediately.

    REQUIRES:
        scope is not frozen.
        edges is a list of (dependent_slug, dependency_slug) pairs.

    ENSURES:
        scope.features contains all required transitive deps, flagged.
        Dependency graph is acyclic.

    Args:
        scope:            The scope to augment (must not be frozen).
        edges:            List of (feature, dep) slug pairs.
        known_exclusions: Optional list of features known to be out of scope.

    Returns:
        The mutated scope (same object, returned for chaining).

    Raises:
        ValueError: If the scope is frozen or if a cycle is detected.
    """
    scope._assert_mutable()

    feature_map = {f.feature: f for f in scope.features}

    # Normalise edge slugs.
    normalised_edges = [(_slugify(a), _slugify(b)) for a, b in edges]

    # Add any missing dep features (transitive, not user-requested).
    for feat_slug, dep_slug in normalised_edges:
        if dep_slug not in feature_map:
            new_feat = ScopeFeature(feature=dep_slug, is_dependency=True)
            scope.features.append(new_feat)
            feature_map[dep_slug] = new_feat
        # Also ensure the dependent feature exists (it should if scope is parsed first).
        if feat_slug not in feature_map:
            new_feat = ScopeFeature(feature=feat_slug, is_dependency=True)
            scope.features.append(new_feat)
            feature_map[feat_slug] = new_feat

    # Wire up depends_on lists.
    for feat_slug, dep_slug in normalised_edges:
        feat = feature_map[feat_slug]
        if dep_slug not in feat.depends_on:
            feat.depends_on.append(dep_slug)

    # Validate: detect cycles by attempting topological sort.
    try:
        scope.dependency_order()
    except ValueError as exc:
        raise ValueError(
            f"Dependency graph is cyclic — cannot resolve scope. Detail: {exc}"
        ) from exc

    # Record known exclusions.
    if known_exclusions:
        for excl in known_exclusions:
            slug = _slugify(excl)
            if slug and slug not in scope.known_exclusions:
                scope.known_exclusions.append(slug)

    return scope


def freeze_scope(scope: Scope) -> Scope:
    """Freeze a resolved scope for a convergence run.

    Moves the scope from RESOLVED → FROZEN state.

    Once frozen:
    - scope.frozen is True.
    - scope.scope_hash is set (deterministic from features + deps).
    - Any further mutation raises ValueError.

    REQUIRES:
        scope is not already frozen.

    ENSURES:
        scope.frozen == True.
        scope.scope_hash is a non-empty deterministic hex string.

    Args:
        scope: The scope to freeze (must not already be frozen).

    Returns:
        The frozen scope (same object, returned for chaining).

    Raises:
        ValueError: If the scope is already frozen.
    """
    scope._assert_mutable()
    scope.scope_hash = scope.compute_scope_hash()
    scope.frozen = True
    return scope


def detect_unknown_features(
    scope: Scope, known_features: set[str]
) -> list[str]:
    """Return feature slugs in scope that are not in the known feature registry.

    Useful for warning the user that a requested feature may be misspelled or
    not yet catalogued.

    Args:
        scope:          A Scope object (any lifecycle state).
        known_features: Set of canonically known feature slugs.

    Returns:
        List of slugs present in scope but absent from known_features.
        Order matches scope.features insertion order.
    """
    normalised_known = {_slugify(k) for k in known_features}
    return [f.feature for f in scope.features if f.feature not in normalised_known]
