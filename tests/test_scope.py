"""Tests for scripts/scope.py — ticket #7: Scope Parser with Dependency Graph."""
import pytest

from scripts.scope import (
    Scope,
    ScopeFeature,
    _slugify,
    add_dependencies,
    detect_unknown_features,
    freeze_scope,
    parse_scope,
)

# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_lowercase(self) -> None:
        assert _slugify("Boards") == "boards"

    def test_spaces_to_dashes(self) -> None:
        assert _slugify("Drag Drop") == "drag-drop"

    def test_underscores_to_dashes(self) -> None:
        assert _slugify("drag_drop") == "drag-drop"

    def test_mixed_whitespace(self) -> None:
        assert _slugify("  Card  Modal  ") == "card-modal"

    def test_already_slug(self) -> None:
        assert _slugify("drag-drop") == "drag-drop"

    def test_multiple_separators_collapsed(self) -> None:
        assert _slugify("a  __  b") == "a-b"

    def test_strips_leading_trailing_dashes(self) -> None:
        assert _slugify("-boards-") == "boards"

    def test_empty_string(self) -> None:
        assert _slugify("") == ""

    def test_whitespace_only(self) -> None:
        assert _slugify("   ") == ""

    def test_numbers_preserved(self) -> None:
        assert _slugify("oauth2") == "oauth2"


# ---------------------------------------------------------------------------
# ScopeFeature
# ---------------------------------------------------------------------------

class TestScopeFeature:
    def test_slug_normalised_on_init(self) -> None:
        sf = ScopeFeature(feature="Drag Drop")
        assert sf.feature == "drag-drop"

    def test_depends_on_slugified(self) -> None:
        sf = ScopeFeature(feature="cards", depends_on=["Drag Drop", "lists"])
        assert sf.depends_on == ["drag-drop", "lists"]

    def test_default_not_dependency(self) -> None:
        sf = ScopeFeature(feature="boards")
        assert sf.is_dependency is False

    def test_default_priority(self) -> None:
        sf = ScopeFeature(feature="boards")
        assert sf.priority == 1


# ---------------------------------------------------------------------------
# parse_scope
# ---------------------------------------------------------------------------

class TestParseScope:
    def test_simple_csv(self) -> None:
        scope = parse_scope("boards, lists, cards")
        assert scope.feature_names() == ["boards", "lists", "cards"]

    def test_mixed_case(self) -> None:
        scope = parse_scope("Boards, LISTS, Cards")
        assert scope.feature_names() == ["boards", "lists", "cards"]

    def test_spaces_in_names(self) -> None:
        scope = parse_scope("drag drop, card modal")
        assert scope.feature_names() == ["drag-drop", "card-modal"]

    def test_whitespace_trimmed(self) -> None:
        scope = parse_scope("  boards  ,  lists  ")
        assert scope.feature_names() == ["boards", "lists"]

    def test_duplicates_deduplicated(self) -> None:
        scope = parse_scope("boards, boards, lists")
        assert scope.feature_names() == ["boards", "lists"]

    def test_raw_input_preserved(self) -> None:
        raw = "Boards, Drag Drop"
        scope = parse_scope(raw)
        assert scope.raw_input == raw

    def test_target_set(self) -> None:
        scope = parse_scope("boards", target="trello")
        assert scope.target == "trello"

    def test_not_frozen_on_parse(self) -> None:
        scope = parse_scope("boards")
        assert scope.frozen is False

    def test_empty_input_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_scope("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_scope("   ")

    def test_all_empty_tokens_raises(self) -> None:
        # only separators, no actual features
        with pytest.raises(ValueError):
            parse_scope(",,,")

    def test_underscore_normalised(self) -> None:
        scope = parse_scope("drag_drop")
        assert scope.feature_names() == ["drag-drop"]

    def test_single_feature(self) -> None:
        scope = parse_scope("boards")
        assert len(scope.features) == 1
        assert scope.features[0].feature == "boards"
        assert scope.features[0].is_dependency is False


# ---------------------------------------------------------------------------
# add_dependencies
# ---------------------------------------------------------------------------

class TestAddDependencies:
    def test_edge_wired_into_depends_on(self) -> None:
        scope = parse_scope("cards")
        add_dependencies(scope, [("cards", "lists")])
        cards = next(f for f in scope.features if f.feature == "cards")
        assert "lists" in cards.depends_on

    def test_missing_dep_added_as_transitive(self) -> None:
        scope = parse_scope("cards")
        add_dependencies(scope, [("cards", "lists")])
        names = scope.feature_names()
        assert "lists" in names
        lists_feat = next(f for f in scope.features if f.feature == "lists")
        assert lists_feat.is_dependency is True

    def test_existing_feature_not_flagged(self) -> None:
        scope = parse_scope("cards, lists")
        add_dependencies(scope, [("cards", "lists")])
        # lists was user-requested, so should remain is_dependency=False
        lists_feat = next(f for f in scope.features if f.feature == "lists")
        assert lists_feat.is_dependency is False

    def test_chained_transitive_deps(self) -> None:
        scope = parse_scope("drag-drop")
        add_dependencies(scope, [("drag-drop", "cards"), ("cards", "lists")])
        names = scope.feature_names()
        assert "cards" in names
        assert "lists" in names

    def test_known_exclusions_stored(self) -> None:
        scope = parse_scope("boards")
        add_dependencies(scope, [], known_exclusions=["enterprise-sso"])
        assert "enterprise-sso" in scope.known_exclusions

    def test_exclusions_slugified(self) -> None:
        scope = parse_scope("boards")
        add_dependencies(scope, [], known_exclusions=["Enterprise SSO"])
        assert "enterprise-sso" in scope.known_exclusions

    def test_exclusions_not_duplicated(self) -> None:
        scope = parse_scope("boards")
        add_dependencies(scope, [], known_exclusions=["enterprise-sso"])
        add_dependencies(scope, [], known_exclusions=["enterprise-sso"])
        assert scope.known_exclusions.count("enterprise-sso") == 1

    def test_frozen_scope_raises(self) -> None:
        scope = parse_scope("boards")
        freeze_scope(scope)
        with pytest.raises(ValueError, match="frozen"):
            add_dependencies(scope, [("boards", "lists")])

    def test_edges_normalised(self) -> None:
        scope = parse_scope("Drag Drop")
        add_dependencies(scope, [("Drag Drop", "Cards")])
        drag = next(f for f in scope.features if f.feature == "drag-drop")
        assert "cards" in drag.depends_on

    def test_returns_same_scope_object(self) -> None:
        scope = parse_scope("boards")
        result = add_dependencies(scope, [])
        assert result is scope


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

class TestCycleDetection:
    def test_simple_cycle_raises(self) -> None:
        scope = parse_scope("a, b")
        with pytest.raises(ValueError, match="[Cc]ycl"):
            add_dependencies(scope, [("a", "b"), ("b", "a")])

    def test_self_loop_raises(self) -> None:
        scope = parse_scope("a")
        with pytest.raises(ValueError, match="[Cc]ycl"):
            add_dependencies(scope, [("a", "a")])

    def test_longer_cycle_raises(self) -> None:
        scope = parse_scope("a, b, c")
        with pytest.raises(ValueError, match="[Cc]ycl"):
            add_dependencies(scope, [("a", "b"), ("b", "c"), ("c", "a")])

    def test_no_cycle_does_not_raise(self) -> None:
        scope = parse_scope("a, b, c")
        # Should not raise
        add_dependencies(scope, [("a", "b"), ("b", "c")])


# ---------------------------------------------------------------------------
# dependency_order (topological waves)
# ---------------------------------------------------------------------------

class TestDependencyOrder:
    def test_no_deps_single_wave(self) -> None:
        scope = parse_scope("boards, lists, cards")
        waves = scope.dependency_order()
        # All features in a single wave (no edges set)
        assert len(waves) == 1
        assert set(waves[0]) == {"boards", "lists", "cards"}

    def test_linear_chain_three_waves(self) -> None:
        scope = parse_scope("a, b, c")
        add_dependencies(scope, [("b", "a"), ("c", "b")])
        waves = scope.dependency_order()
        assert len(waves) == 3
        assert waves[0] == ["a"]
        assert waves[1] == ["b"]
        assert waves[2] == ["c"]

    def test_diamond_dependency(self) -> None:
        # a → b, a → c, d → b, d → c
        scope = parse_scope("a, b, c, d")
        add_dependencies(scope, [("a", "b"), ("a", "c"), ("d", "b"), ("d", "c")])
        waves = scope.dependency_order()
        # b and c must come before a and d
        all_features = [f for wave in waves for f in wave]
        assert all_features.index("b") < all_features.index("a")
        assert all_features.index("c") < all_features.index("a")
        assert all_features.index("b") < all_features.index("d")
        assert all_features.index("c") < all_features.index("d")

    def test_cycle_raises_in_dependency_order(self) -> None:
        # Build a scope that somehow got cycles (bypassing add_dependencies guard).
        scope = Scope(raw_input="a, b")
        scope.features = [
            ScopeFeature(feature="a", depends_on=["b"]),
            ScopeFeature(feature="b", depends_on=["a"]),
        ]
        with pytest.raises(ValueError, match="[Cc]ycl"):
            scope.dependency_order()

    def test_transitive_dep_included_in_order(self) -> None:
        scope = parse_scope("drag-drop")
        add_dependencies(scope, [("drag-drop", "cards"), ("cards", "lists")])
        waves = scope.dependency_order()
        all_features = [f for wave in waves for f in wave]
        assert all_features.index("lists") < all_features.index("cards")
        assert all_features.index("cards") < all_features.index("drag-drop")


# ---------------------------------------------------------------------------
# Transitive dependency flagging
# ---------------------------------------------------------------------------

class TestTransitiveDependencyFlagging:
    def test_user_requested_not_flagged(self) -> None:
        scope = parse_scope("boards, lists")
        add_dependencies(scope, [("boards", "lists")])
        lists_feat = next(f for f in scope.features if f.feature == "lists")
        # lists was explicitly requested — should NOT be is_dependency
        assert lists_feat.is_dependency is False

    def test_auto_added_dep_flagged(self) -> None:
        scope = parse_scope("drag-drop")
        add_dependencies(scope, [("drag-drop", "cards")])
        cards_feat = next(f for f in scope.features if f.feature == "cards")
        assert cards_feat.is_dependency is True

    def test_chained_transitive_all_flagged(self) -> None:
        scope = parse_scope("drag-drop")
        add_dependencies(scope, [("drag-drop", "cards"), ("cards", "lists")])
        for name in ("cards", "lists"):
            feat = next(f for f in scope.features if f.feature == name)
            assert feat.is_dependency is True, f"{name} should be flagged as dependency"


# ---------------------------------------------------------------------------
# freeze_scope
# ---------------------------------------------------------------------------

class TestFreezeScope:
    def test_frozen_flag_set(self) -> None:
        scope = parse_scope("boards")
        freeze_scope(scope)
        assert scope.frozen is True

    def test_scope_hash_populated(self) -> None:
        scope = parse_scope("boards")
        freeze_scope(scope)
        assert scope.scope_hash != ""

    def test_mutation_after_freeze_raises(self) -> None:
        scope = parse_scope("boards")
        freeze_scope(scope)
        with pytest.raises(ValueError, match="frozen"):
            scope._assert_mutable()

    def test_double_freeze_raises(self) -> None:
        scope = parse_scope("boards")
        freeze_scope(scope)
        with pytest.raises(ValueError, match="frozen"):
            freeze_scope(scope)

    def test_add_features_after_freeze_raises(self) -> None:
        scope = parse_scope("boards")
        freeze_scope(scope)
        with pytest.raises(ValueError, match="frozen"):
            add_dependencies(scope, [("boards", "lists")])

    def test_returns_same_scope(self) -> None:
        scope = parse_scope("boards")
        result = freeze_scope(scope)
        assert result is scope


# ---------------------------------------------------------------------------
# Scope hash determinism
# ---------------------------------------------------------------------------

class TestScopeHashDeterminism:
    def test_same_features_same_hash(self) -> None:
        scope1 = parse_scope("boards, lists, cards")
        scope2 = parse_scope("boards, lists, cards")
        assert scope1.compute_scope_hash() == scope2.compute_scope_hash()

    def test_different_features_different_hash(self) -> None:
        scope1 = parse_scope("boards, lists")
        scope2 = parse_scope("boards, cards")
        assert scope1.compute_scope_hash() != scope2.compute_scope_hash()

    def test_hash_includes_deps(self) -> None:
        scope1 = parse_scope("drag-drop")
        scope2 = parse_scope("drag-drop")
        add_dependencies(scope2, [("drag-drop", "cards")])
        assert scope1.compute_scope_hash() != scope2.compute_scope_hash()

    def test_hash_stable_after_freeze(self) -> None:
        scope = parse_scope("boards, lists")
        pre_hash = scope.compute_scope_hash()
        freeze_scope(scope)
        assert scope.scope_hash == pre_hash

    def test_hash_insertion_order_independent(self) -> None:
        # Features requested in different order should produce the same hash
        # if they end up with the same requested vs dependency classification.
        scope1 = parse_scope("boards, lists")
        add_dependencies(scope1, [("boards", "lists")])

        scope2 = parse_scope("lists, boards")
        add_dependencies(scope2, [("boards", "lists")])

        # Both have same requested set {boards, lists} with same edge — hash must match.
        assert scope1.compute_scope_hash() == scope2.compute_scope_hash()

    def test_hash_is_hex_string(self) -> None:
        scope = parse_scope("boards")
        h = scope.compute_scope_hash()
        assert all(c in "0123456789abcdef" for c in h)
        assert len(h) == 16


# ---------------------------------------------------------------------------
# detect_unknown_features
# ---------------------------------------------------------------------------

class TestDetectUnknownFeatures:
    def test_all_known(self) -> None:
        scope = parse_scope("boards, lists")
        unknown = detect_unknown_features(scope, {"boards", "lists", "cards"})
        assert unknown == []

    def test_some_unknown(self) -> None:
        scope = parse_scope("boards, lists, typo-feature")
        unknown = detect_unknown_features(scope, {"boards", "lists"})
        assert unknown == ["typo-feature"]

    def test_all_unknown(self) -> None:
        scope = parse_scope("foo, bar")
        unknown = detect_unknown_features(scope, set())
        assert set(unknown) == {"foo", "bar"}

    def test_known_features_normalised(self) -> None:
        scope = parse_scope("drag-drop")
        # Pass known features in non-slug form — should still match.
        unknown = detect_unknown_features(scope, {"Drag Drop"})
        assert unknown == []

    def test_transitive_deps_also_checked(self) -> None:
        scope = parse_scope("drag-drop")
        add_dependencies(scope, [("drag-drop", "cards")])
        # cards is a transitive dep not in known set
        unknown = detect_unknown_features(scope, {"drag-drop"})
        assert "cards" in unknown


# ---------------------------------------------------------------------------
# Scope.to_dict
# ---------------------------------------------------------------------------

class TestToDict:
    def test_round_trip_keys(self) -> None:
        scope = parse_scope("boards, lists", target="trello")
        d = scope.to_dict()
        assert d["raw_input"] == "boards, lists"
        assert d["target"] == "trello"
        assert d["frozen"] is False
        assert d["scope_hash"] == ""
        assert len(d["features"]) == 2

    def test_frozen_reflected(self) -> None:
        scope = parse_scope("boards")
        freeze_scope(scope)
        d = scope.to_dict()
        assert d["frozen"] is True
        assert d["scope_hash"] != ""

    def test_exclusions_in_dict(self) -> None:
        scope = parse_scope("boards")
        add_dependencies(scope, [], known_exclusions=["enterprise-sso"])
        d = scope.to_dict()
        assert "enterprise-sso" in d["known_exclusions"]


# ---------------------------------------------------------------------------
# Full lifecycle integration
# ---------------------------------------------------------------------------

class TestFullLifecycle:
    def test_parsed_resolved_frozen(self) -> None:
        scope = parse_scope("drag-drop, boards", target="trello")
        assert not scope.frozen

        add_dependencies(
            scope,
            [("drag-drop", "cards"), ("cards", "lists"), ("boards", "lists")],
            known_exclusions=["enterprise-sso"],
        )
        assert "lists" in scope.feature_names()
        assert "cards" in scope.feature_names()

        waves = scope.dependency_order()
        all_features = [f for wave in waves for f in wave]
        # lists must come first (no deps)
        assert all_features.index("lists") < all_features.index("cards")
        assert all_features.index("cards") < all_features.index("drag-drop")

        freeze_scope(scope)
        assert scope.frozen
        assert scope.scope_hash

        with pytest.raises(ValueError, match="frozen"):
            add_dependencies(scope, [("boards", "cards")])

    def test_frozen_scope_cannot_skip_resolve(self) -> None:
        # Guard: must not allow PARSED → FROZEN shortcut through add_dependencies check.
        scope = parse_scope("boards")
        # Manually freeze without resolving — should still work (no required guard here),
        # but further mutation is blocked.
        freeze_scope(scope)
        with pytest.raises(ValueError):
            add_dependencies(scope, [("boards", "lists")])
