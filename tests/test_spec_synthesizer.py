"""
Tests for scripts/spec_synthesizer.py

Covers:
- Fact grouping by feature
- Synthesis prompt generation
- Contradiction flagging
- SpecItem creation with provenance
- SpecBundle assembly
- Multi-AI disabled mode (single model)
- INV-015, INV-034, INV-035 invariants
- Mock all external AI calls
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.models import (
    Authority,
    BundleStatus,
    Confidence,
    EvidenceRef,
    Fact,
    FactCategory,
    FactStatus,
    SourceType,
)
from scripts.spec_store import SpecStore
from scripts.spec_synthesizer import (
    AIResponse,
    SpecSynthesizer,
    SynthesisError,
    _confidence_from_facts,
    _parse_spec_content,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_evidence(url: str = "https://example.com") -> EvidenceRef:
    return EvidenceRef(
        source_url=url,
        source_title="Test source",
        captured_at="2024-01-15T10:00:00+00:00",
    )


def make_fact(
    *,
    feature: str = "boards",
    claim: str = "Boards have a title field",
    category: FactCategory = FactCategory.UI_COMPONENT,
    authority: Authority = Authority.OBSERVATIONAL,
    confidence: Confidence = Confidence.MEDIUM,
    status: FactStatus = FactStatus.UNVERIFIED,
    contradicts: list[str] | None = None,
    contradicted_by: list[str] | None = None,
    source_type: SourceType = SourceType.HELP_CENTER,
) -> Fact:
    return Fact(
        feature=feature,
        category=category,
        claim=claim,
        evidence=[make_evidence()],
        source_type=source_type,
        authority=authority,
        confidence=confidence,
        status=status,
        contradicts=contradicts or [],
        contradicted_by=contradicted_by or [],
    )


# Reusable JSON response body for mocked AI calls (avoids E501 line-length violations).
_EMPTY_SPEC_JSON = (
    '{"feature": "f", "summary": "s", "api_contracts": [], "ui_patterns": [],'
    ' "data_models": [], "state_machines": [], "business_rules": [], "open_questions": []}'
)


def _spec_json(feature: str = "boards", summary: str = "s") -> str:
    """Return a minimal valid spec JSON string."""
    return (
        f'{{"feature": "{feature}", "summary": "{summary}",'
        ' "api_contracts": [], "ui_patterns": [],'
        ' "data_models": [], "state_machines": [], "business_rules": [], "open_questions": []}'
    )


def _spec_json_with_questions(feature: str, questions: list[str]) -> str:
    """Return a minimal valid spec JSON string with open_questions."""
    qs = ", ".join(f'"{q}"' for q in questions)
    return (
        f'{{"feature": "{feature}", "summary": "s",'
        f' "api_contracts": [], "ui_patterns": [],'
        f' "data_models": [], "state_machines": [], "business_rules": [],'
        f' "open_questions": [{qs}]}}'
    )


class MockScope:
    """Simple scope stand-in for tests."""

    def __init__(self, features: list[str], scope_hash: str = "testhash"):
        self.features_list = features
        self.scope_hash = scope_hash
        self.id = "scope-id-test"

    def feature_names(self) -> list[str]:
        return self.features_list


@pytest.fixture
def store(tmp_path):
    return SpecStore(tmp_path)


@pytest.fixture
def synthesizer(store):
    return SpecSynthesizer(spec_store=store, keychain=MagicMock(), cw_home="/fake/cw_home")


# ---------------------------------------------------------------------------
# Unit tests: _group_facts_by_feature
# ---------------------------------------------------------------------------


class TestGroupFactsByFeature:
    def test_groups_by_feature(self, synthesizer):
        facts = [
            make_fact(feature="boards", claim="Claim A"),
            make_fact(feature="lists", claim="Claim B"),
            make_fact(feature="boards", claim="Claim C"),
        ]
        groups = synthesizer._group_facts_by_feature(facts)
        assert set(groups.keys()) == {"boards", "lists"}
        assert len(groups["boards"]) == 2
        assert len(groups["lists"]) == 1

    def test_authority_sort_order(self, synthesizer):
        """AUTHORITATIVE facts should appear first within each group."""
        facts = [
            make_fact(feature="boards", claim="Anecdotal claim", authority=Authority.ANECDOTAL),
            make_fact(feature="boards", claim="Auth claim", authority=Authority.AUTHORITATIVE),
            make_fact(feature="boards", claim="Obs claim", authority=Authority.OBSERVATIONAL),
        ]
        groups = synthesizer._group_facts_by_feature(facts)
        ordered = groups["boards"]
        assert ordered[0].authority == Authority.AUTHORITATIVE
        assert ordered[1].authority == Authority.OBSERVATIONAL
        assert ordered[2].authority == Authority.ANECDOTAL

    def test_empty_facts(self, synthesizer):
        groups = synthesizer._group_facts_by_feature([])
        assert groups == {}

    def test_single_feature(self, synthesizer):
        facts = [make_fact(feature="cards") for _ in range(5)]
        groups = synthesizer._group_facts_by_feature(facts)
        assert list(groups.keys()) == ["cards"]
        assert len(groups["cards"]) == 5


# ---------------------------------------------------------------------------
# Unit tests: _build_synthesis_prompt
# ---------------------------------------------------------------------------


class TestBuildSynthesisPrompt:
    def test_prompt_contains_feature(self, synthesizer):
        facts = [make_fact(feature="boards", claim="Boards have titles")]
        prompt = synthesizer._build_synthesis_prompt("boards", facts)
        assert "boards" in prompt

    def test_prompt_contains_claims(self, synthesizer):
        facts = [make_fact(feature="boards", claim="Boards have titles")]
        prompt = synthesizer._build_synthesis_prompt("boards", facts)
        assert "Boards have titles" in prompt

    def test_prompt_contains_authority(self, synthesizer):
        facts = [make_fact(feature="boards", authority=Authority.AUTHORITATIVE)]
        prompt = synthesizer._build_synthesis_prompt("boards", facts)
        assert "authoritative" in prompt

    def test_prompt_contains_json_template(self, synthesizer):
        facts = [make_fact(feature="boards")]
        prompt = synthesizer._build_synthesis_prompt("boards", facts)
        assert "api_contracts" in prompt
        assert "ui_patterns" in prompt
        assert "data_models" in prompt
        assert "state_machines" in prompt

    def test_prompt_with_structured_data(self, synthesizer):
        facts = [
            Fact(
                feature="boards",
                category=FactCategory.API_ENDPOINT,
                claim="GET /boards returns list",
                evidence=[make_evidence()],
                source_type=SourceType.API_DOCS,
                structured_data={"method": "GET", "path": "/boards"},
                authority=Authority.AUTHORITATIVE,
            )
        ]
        prompt = synthesizer._build_synthesis_prompt("boards", facts)
        assert "GET" in prompt
        assert "/boards" in prompt

    def test_prompt_multiple_facts(self, synthesizer):
        facts = [
            make_fact(feature="lists", claim=f"Claim {i}")
            for i in range(3)
        ]
        prompt = synthesizer._build_synthesis_prompt("lists", facts)
        assert "Claim 0" in prompt
        assert "Claim 1" in prompt
        assert "Claim 2" in prompt


# ---------------------------------------------------------------------------
# Unit tests: _flag_contradictions
# ---------------------------------------------------------------------------


class TestFlagContradictions:
    def test_no_contradictions(self, synthesizer):
        facts = [make_fact(feature="boards") for _ in range(3)]
        result = synthesizer._flag_contradictions(facts)
        assert result == []

    def test_explicit_contradicts_link(self, synthesizer):
        fact_a = make_fact(feature="boards", claim="Claim A")
        fact_b = make_fact(
            feature="boards",
            claim="Claim B",
            contradicts=[fact_a.id],
        )
        result = synthesizer._flag_contradictions([fact_a, fact_b])
        assert len(result) == 1
        assert result[0]["fact_id_a"] == fact_b.id
        assert result[0]["fact_id_b"] == fact_a.id
        assert result[0]["resolution"] == "human_review_required"

    def test_contradicted_by_link(self, synthesizer):
        fact_a = make_fact(feature="boards", claim="Active claim")
        fact_b = make_fact(
            feature="boards",
            claim="Contradicted claim",
            status=FactStatus.CONTRADICTED,
            contradicted_by=[fact_a.id],
        )
        result = synthesizer._flag_contradictions([fact_a, fact_b])
        assert len(result) == 1
        assert result[0]["status"] == "contradicted"

    def test_pair_deduplication(self, synthesizer):
        """Same pair should only appear once."""
        fact_a = make_fact(feature="boards", claim="Claim A")
        fact_b = make_fact(
            feature="boards",
            claim="Claim B",
            contradicts=[fact_a.id],
            status=FactStatus.CONTRADICTED,
            contradicted_by=[fact_a.id],
        )
        result = synthesizer._flag_contradictions([fact_a, fact_b])
        # Should deduplicate — only one entry for this pair
        pairs = [frozenset([r["fact_id_a"], r["fact_id_b"]]) for r in result]
        assert len(pairs) == len(set(pairs))


# ---------------------------------------------------------------------------
# Unit tests: _confidence_from_facts
# ---------------------------------------------------------------------------


class TestConfidenceFromFacts:
    def test_authoritative_gives_high(self):
        facts = [make_fact(authority=Authority.AUTHORITATIVE)]
        assert _confidence_from_facts(facts) == Confidence.HIGH

    def test_two_observational_gives_high(self):
        facts = [make_fact(authority=Authority.OBSERVATIONAL) for _ in range(2)]
        assert _confidence_from_facts(facts) == Confidence.HIGH

    def test_one_observational_gives_medium(self):
        facts = [make_fact(authority=Authority.OBSERVATIONAL)]
        assert _confidence_from_facts(facts) == Confidence.MEDIUM

    def test_anecdotal_gives_low(self):
        facts = [make_fact(authority=Authority.ANECDOTAL)]
        assert _confidence_from_facts(facts) == Confidence.LOW


# ---------------------------------------------------------------------------
# Unit tests: _parse_spec_content
# ---------------------------------------------------------------------------


class TestParseSpecContent:
    def test_parses_json_block(self):
        raw = '```json\n{"summary": "test", "feature": "boards"}\n```'
        result = _parse_spec_content(raw, "boards", "synthesised_spec")
        assert result["summary"] == "test"

    def test_parses_bare_json(self):
        raw = '{"summary": "bare json", "feature": "boards"}'
        result = _parse_spec_content(raw, "boards", "synthesised_spec")
        assert result["summary"] == "bare json"

    def test_falls_back_to_plain_text(self):
        raw = "This is a plain text response without JSON."
        result = _parse_spec_content(raw, "boards", "synthesised_spec")
        assert "summary" in result
        assert "parse_note" in result

    def test_malformed_json_falls_back(self):
        raw = "```json\n{broken json\n```"
        result = _parse_spec_content(raw, "boards", "synthesised_spec")
        assert "parse_note" in result


# ---------------------------------------------------------------------------
# Integration tests: synthesize (multi-AI disabled)
# ---------------------------------------------------------------------------


class TestSynthesizeLocalMode:
    def _add_scoped_facts(self, store: SpecStore, feature: str, n: int = 2) -> list[Fact]:
        facts = []
        for i in range(n):
            f = make_fact(
                feature=feature,
                claim=f"{feature} claim {i}",
                authority=Authority.OBSERVATIONAL,
            )
            stored = store.add_fact(f)
            facts.append(stored)
        return facts

    def test_synthesize_returns_bundle(self, store, synthesizer):
        self._add_scoped_facts(store, "boards", n=2)
        scope = MockScope(["boards"])
        bundle = asyncio.run(
            synthesizer.synthesize("trello.com", scope, use_multi_ai=False)
        )
        assert isinstance(bundle, object)
        assert bundle.target == "trello.com"
        assert bundle.status == BundleStatus.DRAFT

    def test_bundle_contains_spec_items(self, store, synthesizer):
        self._add_scoped_facts(store, "boards", n=2)
        scope = MockScope(["boards"])
        bundle = asyncio.run(
            synthesizer.synthesize("trello.com", scope, use_multi_ai=False)
        )
        assert len(bundle.spec_items) == 1
        assert bundle.spec_items[0].feature == "boards"

    def test_spec_item_provenance(self, store, synthesizer):
        """SpecItem.supporting_fact_ids must be subset of bundle.fact_ids (INV-034)."""
        self._add_scoped_facts(store, "boards", n=3)
        scope = MockScope(["boards"])
        bundle = asyncio.run(
            synthesizer.synthesize("trello.com", scope, use_multi_ai=False)
        )
        bundle_fact_ids = set(bundle.fact_ids)
        for item in bundle.spec_items:
            assert set(item.supporting_fact_ids).issubset(bundle_fact_ids), (
                "INV-034: supporting_fact_ids not subset of bundle.fact_ids"
            )

    def test_spec_item_has_at_least_one_fact(self, store, synthesizer):
        """INV-015: every SpecItem must trace back to at least one fact."""
        self._add_scoped_facts(store, "boards", n=2)
        scope = MockScope(["boards"])
        bundle = asyncio.run(
            synthesizer.synthesize("trello.com", scope, use_multi_ai=False)
        )
        for item in bundle.spec_items:
            assert len(item.supporting_fact_ids) >= 1, (
                f"INV-015: SpecItem for {item.feature!r} has no supporting facts"
            )

    def test_bundle_scope_matches(self, store, synthesizer):
        """INV-035: bundle scope should only include requested features."""
        self._add_scoped_facts(store, "boards", n=2)
        self._add_scoped_facts(store, "lists", n=1)
        scope = MockScope(["boards"])  # lists NOT in scope
        bundle = asyncio.run(
            synthesizer.synthesize("trello.com", scope, use_multi_ai=False)
        )
        # Only boards facts should be in the bundle
        bundle_features = {item.feature for item in bundle.spec_items}
        assert "lists" not in bundle_features

    def test_multi_feature_scope(self, store, synthesizer):
        self._add_scoped_facts(store, "boards", n=2)
        self._add_scoped_facts(store, "lists", n=2)
        scope = MockScope(["boards", "lists"])
        bundle = asyncio.run(
            synthesizer.synthesize("trello.com", scope, use_multi_ai=False)
        )
        item_features = {item.feature for item in bundle.spec_items}
        assert "boards" in item_features
        assert "lists" in item_features

    def test_empty_scope_raises(self, store, synthesizer):
        scope = MockScope([])
        with pytest.raises(SynthesisError, match="no features"):
            asyncio.run(
                synthesizer.synthesize("trello.com", scope, use_multi_ai=False)
            )

    def test_no_facts_for_scope_produces_empty_bundle(self, store, synthesizer):
        """Scope with no matching facts should produce a bundle with no spec items."""
        scope = MockScope(["missing-feature"])
        bundle = asyncio.run(
            synthesizer.synthesize("trello.com", scope, use_multi_ai=False)
        )
        assert bundle.spec_items == []
        assert bundle.fact_ids == []

    def test_bundle_notes_contain_run_id(self, store, synthesizer):
        self._add_scoped_facts(store, "boards", n=1)
        scope = MockScope(["boards"])
        bundle = asyncio.run(
            synthesizer.synthesize(
                "trello.com", scope, run_id="test-run-123", use_multi_ai=False
            )
        )
        assert "test-run-123" in bundle.notes

    def test_contradiction_flagged_in_spec(self, store, synthesizer):
        """Contradicted facts should appear as open_questions in spec content."""
        fact_a = store.add_fact(
            make_fact(feature="boards", claim="Boards support 100 cards")
        )
        fact_b_raw = make_fact(
            feature="boards",
            claim="Boards support only 10 cards",
            contradicts=[fact_a.id],
        )
        store.add_fact(fact_b_raw)
        scope = MockScope(["boards"])
        bundle = asyncio.run(
            synthesizer.synthesize("trello.com", scope, use_multi_ai=False)
        )
        # At least one spec item should have contradiction info
        assert len(bundle.spec_items) == 1
        content = bundle.spec_items[0].content
        # Local synthesis includes contradictions key
        assert "contradictions" in content or "open_questions" in content


# ---------------------------------------------------------------------------
# Integration tests: synthesize (multi-AI mocked)
# ---------------------------------------------------------------------------


class TestSynthesizeMultiAI:
    def _add_facts(self, store: SpecStore, feature: str) -> list[Fact]:
        facts = []
        for i in range(2):
            f = make_fact(feature=feature, claim=f"Claim {i}")
            facts.append(store.add_fact(f))
        return facts

    def test_multi_ai_calls_both_tools(self, store, synthesizer):
        """When use_multi_ai=True, both codex and gemini should be consulted."""
        self._add_facts(store, "boards")
        scope = MockScope(["boards"])

        responses = [
            AIResponse(tool="codex", content=_spec_json("boards", "Codex summary")),
            AIResponse(tool="gemini", content=_spec_json("boards", "Gemini summary")),
        ]
        mock_fn = AsyncMock(side_effect=responses)
        with patch("scripts.spec_synthesizer._consult_ai_async", mock_fn):
            bundle = asyncio.run(
                synthesizer.synthesize("trello.com", scope, use_multi_ai=True)
            )

        assert len(bundle.spec_items) == 1
        content = bundle.spec_items[0].content
        assert "consulted_tools" in content

    def test_multi_ai_reconciles_open_questions(self, store, synthesizer):
        """open_questions from all AI responses should be merged."""
        self._add_facts(store, "boards")
        scope = MockScope(["boards"])

        responses = [
            AIResponse(
                tool="codex",
                content=_spec_json_with_questions("boards", ["Question from Codex"]),
            ),
            AIResponse(
                tool="gemini",
                content=_spec_json_with_questions("boards", ["Question from Gemini"]),
            ),
        ]
        mock_fn = AsyncMock(side_effect=responses)
        with patch("scripts.spec_synthesizer._consult_ai_async", mock_fn):
            bundle = asyncio.run(
                synthesizer.synthesize("trello.com", scope, use_multi_ai=True)
            )

        content = bundle.spec_items[0].content
        questions = content.get("open_questions", [])
        assert any("Codex" in str(q) for q in questions)
        assert any("Gemini" in str(q) for q in questions)

    def test_multi_ai_handles_tool_failure(self, store, synthesizer):
        """If one AI tool fails, synthesis should still succeed using the other."""
        self._add_facts(store, "boards")
        scope = MockScope(["boards"])

        responses = [
            AIResponse(tool="codex", content="", error="codex timed out"),
            AIResponse(tool="gemini", content=_spec_json("boards", "Gemini only")),
        ]
        mock_fn = AsyncMock(side_effect=responses)
        with patch("scripts.spec_synthesizer._consult_ai_async", mock_fn):
            bundle = asyncio.run(
                synthesizer.synthesize("trello.com", scope, use_multi_ai=True)
            )

        assert len(bundle.spec_items) == 1
        content = bundle.spec_items[0].content
        assert "consultation_errors" in content

    def test_multi_ai_all_tools_fail_produces_stub(self, store, synthesizer):
        """If all AI tools fail, a minimal stub spec should be produced."""
        self._add_facts(store, "boards")
        scope = MockScope(["boards"])

        responses = [
            AIResponse(tool="codex", content="", error="timeout"),
            AIResponse(tool="gemini", content="", error="timeout"),
        ]
        mock_fn = AsyncMock(side_effect=responses)
        with patch("scripts.spec_synthesizer._consult_ai_async", mock_fn):
            bundle = asyncio.run(
                synthesizer.synthesize("trello.com", scope, use_multi_ai=True)
            )

        assert len(bundle.spec_items) == 1
        content = bundle.spec_items[0].content
        assert "consultation_errors" in content

    def test_inv034_supported_by_parallel_tasks(self, store, synthesizer):
        """INV-034 should hold even with parallel synthesis tasks."""
        for feature in ["boards", "lists", "cards"]:
            for i in range(2):
                store.add_fact(make_fact(feature=feature, claim=f"{feature} claim {i}"))
        scope = MockScope(["boards", "lists", "cards"])

        def make_ok_response(tool: str) -> AIResponse:
            return AIResponse(tool=tool, content=_EMPTY_SPEC_JSON)

        responses = []
        for _ in range(3):  # 3 features × 2 tools
            responses.extend([make_ok_response("codex"), make_ok_response("gemini")])

        mock_fn = AsyncMock(side_effect=responses)
        with patch("scripts.spec_synthesizer._consult_ai_async", mock_fn):
            bundle = asyncio.run(
                synthesizer.synthesize("trello.com", scope, use_multi_ai=True)
            )

        bundle_fact_ids = set(bundle.fact_ids)
        for item in bundle.spec_items:
            assert set(item.supporting_fact_ids).issubset(bundle_fact_ids), (
                f"INV-034 violated for feature {item.feature!r}"
            )


# ---------------------------------------------------------------------------
# Local synthesis content structure
# ---------------------------------------------------------------------------


class TestLocalSynthesisContent:
    def test_api_endpoint_goes_to_api_contracts(self, synthesizer):
        facts = [
            Fact(
                feature="boards",
                category=FactCategory.API_ENDPOINT,
                claim="GET /boards returns boards list",
                evidence=[make_evidence()],
                source_type=SourceType.API_DOCS,
                authority=Authority.AUTHORITATIVE,
            )
        ]
        content = synthesizer._local_synthesis("boards", facts, [])
        assert len(content["api_contracts"]) == 1
        assert content["api_contracts"][0]["claim"] == "GET /boards returns boards list"

    def test_ui_component_goes_to_ui_patterns(self, synthesizer):
        facts = [make_fact(category=FactCategory.UI_COMPONENT, claim="Board has a header")]
        content = synthesizer._local_synthesis("boards", facts, [])
        assert len(content["ui_patterns"]) == 1

    def test_data_model_goes_to_data_models(self, synthesizer):
        facts = [make_fact(category=FactCategory.DATA_MODEL, claim="Board has id, title, color")]
        content = synthesizer._local_synthesis("boards", facts, [])
        assert len(content["data_models"]) == 1

    def test_business_rule_goes_to_business_rules(self, synthesizer):
        facts = [make_fact(category=FactCategory.BUSINESS_RULE, claim="Only admins can delete")]
        content = synthesizer._local_synthesis("boards", facts, [])
        assert "Only admins can delete" in content["business_rules"]

    def test_contradiction_in_open_questions(self, synthesizer):
        contradictions = [{
            "fact_id_a": "abc",
            "claim_a": "Claim A",
            "authority_a": "authoritative",
            "fact_id_b": "def",
            "claim_b": "Claim B",
            "authority_b": "observational",
            "resolution": "human_review_required",
        }]
        content = synthesizer._local_synthesis("boards", [], contradictions)
        assert len(content["open_questions"]) == 1
        assert "abc" in content["open_questions"][0]

    def test_synthesis_mode_is_local(self, synthesizer):
        content = synthesizer._local_synthesis("boards", [], [])
        assert content["synthesis_mode"] == "local"
