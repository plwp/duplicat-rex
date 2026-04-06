"""
SpecSynthesizer — takes raw Facts from the spec store and synthesises
structured specifications via multi-AI consultation.

Design:
- Facts are ranked by authority (AUTHORITATIVE first) when building the synthesis prompt.
- Multi-AI consultation uses chief-wiggum's consult_ai.py functions directly.
- Each SpecItem carries supporting_fact_ids for provenance (INV-015).
- Contradictions between facts are flagged but not auto-resolved.
- Output is a SpecBundle in DRAFT status — caller decides when to validate/snapshot.
- The synthesis prompt asks the AI to produce structured output: API contracts
  (REQUIRES/ENSURES), data models, state machines, UI patterns.

Invariants:
    INV-015: Every SpecItem traces back to at least one non-contradicted Fact.
    INV-034: SpecItem.supporting_fact_ids must be subset of SpecBundle.fact_ids.
    INV-035: Bundle must not contain facts outside scope.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from typing import Any

from scripts.models import (
    Authority,
    BundleStatus,
    Confidence,
    Fact,
    FactStatus,
    SpecBundle,
    SpecItem,
)
from scripts.spec_store import SpecStore

# ---------------------------------------------------------------------------
# Authority rank for sorting (AUTHORITATIVE first)
# ---------------------------------------------------------------------------

_AUTHORITY_ORDER = {
    Authority.AUTHORITATIVE: 0,
    Authority.OBSERVATIONAL: 1,
    Authority.ANECDOTAL: 2,
}


# ---------------------------------------------------------------------------
# SynthesisError
# ---------------------------------------------------------------------------


class SynthesisError(Exception):
    """Raised for unrecoverable synthesis failures."""


# ---------------------------------------------------------------------------
# AIConsultation helpers
# ---------------------------------------------------------------------------


@dataclass
class AIResponse:
    """Response from a single AI consultation."""

    tool: str
    content: str
    error: str | None = None


def _consult_codex(prompt: str) -> str:
    """Call codex CLI in read-only sandbox."""
    cmd = [
        "codex", "exec", "--sandbox", "read-only",
        "-c", 'model_reasoning_effort="high"',
        "-",
    ]
    result = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True,
        check=True, timeout=600,
    )
    return result.stdout


def _consult_gemini(prompt: str) -> str:
    """Call gemini CLI."""
    cmd = ["gemini", "--yolo", "--output-format", "text", "-p", ""]
    result = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True,
        check=True, timeout=1200,
    )
    return result.stdout


async def _consult_ai_async(tool: str, prompt: str) -> AIResponse:
    """Run an AI consultation in a thread pool to avoid blocking the event loop."""
    fn = {"codex": _consult_codex, "gemini": _consult_gemini}.get(tool)
    if fn is None:
        return AIResponse(tool=tool, content="", error=f"Unknown tool: {tool}")

    loop = asyncio.get_event_loop()
    try:
        content = await loop.run_in_executor(None, fn, prompt)
        return AIResponse(tool=tool, content=content)
    except subprocess.TimeoutExpired:
        return AIResponse(tool=tool, content="", error=f"{tool} timed out")
    except subprocess.CalledProcessError as exc:
        return AIResponse(tool=tool, content="", error=f"{tool} error: {exc.stderr or exc}")
    except Exception as exc:  # noqa: BLE001
        return AIResponse(tool=tool, content="", error=f"{tool} unexpected error: {exc}")


# ---------------------------------------------------------------------------
# Prompt builder helpers
# ---------------------------------------------------------------------------


def _confidence_from_facts(facts: list[Fact]) -> Confidence:
    """
    Derive synthesised confidence from supporting facts.

    - HIGH if any authoritative fact, or >=2 observational facts that agree.
    - MEDIUM if a single authoritative fact or observational facts.
    - LOW otherwise.
    """
    auth_count = sum(1 for f in facts if f.authority == Authority.AUTHORITATIVE)
    obs_count = sum(1 for f in facts if f.authority == Authority.OBSERVATIONAL)

    if auth_count >= 1 or obs_count >= 2:
        return Confidence.HIGH
    if auth_count == 1 or obs_count >= 1:
        return Confidence.MEDIUM
    return Confidence.LOW


def _parse_spec_content(raw: str, feature: str, spec_type: str) -> dict[str, Any]:
    """
    Extract structured content from AI response.

    Attempts to parse JSON blocks. Falls back to plain text summary.
    """
    # Try fenced JSON block first
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try bare JSON object
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    # Fall back to plain text content
    return {
        "summary": raw.strip()[:4000],
        "feature": feature,
        "spec_type": spec_type,
        "parse_note": "AI response was not JSON — stored as plain text summary",
    }


# ---------------------------------------------------------------------------
# SpecSynthesizer
# ---------------------------------------------------------------------------


class SpecSynthesizer:
    """
    Synthesises structured SpecItems from raw Facts via multi-AI consultation.

    Usage:
        synthesizer = SpecSynthesizer(spec_store, keychain, cw_home)
        bundle = await synthesizer.synthesize("trello.com", scope)
    """

    def __init__(self, spec_store: SpecStore, keychain: Any, cw_home: str) -> None:
        self.spec_store = spec_store
        self.keychain = keychain
        self.cw_home = cw_home

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def synthesize(
        self,
        target: str,
        scope: Any,
        *,
        run_id: str | None = None,
        use_multi_ai: bool = True,
    ) -> SpecBundle:
        """
        Synthesise a SpecBundle for the given target and scope.

        1. Query spec_store for all non-contradicted facts for scoped features.
        2. Group facts by feature.
        3. For each feature group, synthesize a SpecItem:
           - If use_multi_ai: consult Codex + Gemini in parallel.
           - Reconcile into a single structured spec.
           - Record which fact IDs support each spec item.
        4. Create a SpecBundle with all spec items.
        5. Validate invariants and return.

        REQUIRES: scope has at least one feature.
        ENSURES: returned bundle is DRAFT status.
        ENSURES: every SpecItem.supporting_fact_ids ⊆ bundle.fact_ids (INV-034).
        ENSURES: bundle only contains facts for features within scope (INV-035).
        """
        run_id = run_id or str(uuid.uuid4())

        # Determine scope feature keys — support both models.Scope and scope.Scope
        if hasattr(scope, "feature_keys"):
            feature_keys = scope.feature_keys()
        elif hasattr(scope, "feature_names"):
            feature_keys = scope.feature_names()
        else:
            raise SynthesisError(f"Cannot extract feature keys from scope: {type(scope)}")

        if not feature_keys:
            raise SynthesisError("Scope has no features — nothing to synthesise")

        # 1. Gather all non-contradicted, non-deleted facts for scoped features
        all_facts: list[Fact] = []
        for feature in feature_keys:
            facts = self.spec_store.get_facts_for_feature(feature)
            all_facts.extend(facts)

        # INV-035: scope guard — reject any fact not in scope
        scope_set = set(feature_keys)
        all_facts = [f for f in all_facts if f.feature in scope_set]

        # 2. Group by feature
        grouped = self._group_facts_by_feature(all_facts)

        # 3. Synthesise spec items per feature
        spec_items: list[SpecItem] = []
        tasks = [
            self._synthesize_feature(feature, facts, use_multi_ai=use_multi_ai)
            for feature, facts in grouped.items()
            if facts  # skip empty groups
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                # Non-fatal: log and continue
                print(f"Warning: spec synthesis error: {result}", file=sys.stderr)
            elif isinstance(result, SpecItem):
                spec_items.append(result)

        # 4. Assemble bundle
        all_fact_ids = sorted({f.id for f in all_facts})
        scope_hash = scope.scope_hash if hasattr(scope, "scope_hash") else ""
        scope_id = scope.id if hasattr(scope, "id") else ""

        bundle = SpecBundle(
            target=target,
            scope_id=scope_id,
            scope_hash=scope_hash,
            scope=feature_keys,
            status=BundleStatus.DRAFT,
            spec_items=spec_items,
            fact_ids=all_fact_ids,
            notes=f"Synthesised by SpecSynthesizer run_id={run_id}",
        )

        # 5. Validate invariants
        self._validate_invariants(bundle, all_facts, scope_set)

        return bundle

    # ------------------------------------------------------------------
    # Internal: grouping
    # ------------------------------------------------------------------

    def _group_facts_by_feature(self, facts: list[Fact]) -> dict[str, list[Fact]]:
        """
        Group facts by feature key.

        Within each group, facts are sorted by authority rank (AUTHORITATIVE first),
        then by confidence rank (HIGH first), then by created_at ascending.
        """
        groups: dict[str, list[Fact]] = {}
        for fact in facts:
            groups.setdefault(fact.feature, []).append(fact)

        # Sort each group: authority desc, confidence desc, created_at asc
        for feature in groups:
            groups[feature].sort(
                key=lambda f: (
                    _AUTHORITY_ORDER.get(f.authority, 99),      # lower = better
                    -(f.confidence.rank()),                      # higher rank first
                    f.created_at,
                )
            )
        return groups

    # ------------------------------------------------------------------
    # Internal: per-feature synthesis
    # ------------------------------------------------------------------

    async def _synthesize_feature(
        self,
        feature: str,
        facts: list[Fact],
        *,
        use_multi_ai: bool,
    ) -> SpecItem:
        """
        Synthesise a SpecItem for a single feature.

        If use_multi_ai=True, consults Codex and Gemini in parallel and
        reconciles their outputs. Otherwise uses a local synthesis only.
        """
        prompt = self._build_synthesis_prompt(feature, facts)
        fact_ids = [f.id for f in facts]
        confidence = _confidence_from_facts(facts)

        contradictions = self._flag_contradictions(facts)

        if use_multi_ai:
            # Consult Codex and Gemini in parallel
            responses = await asyncio.gather(
                _consult_ai_async("codex", prompt),
                _consult_ai_async("gemini", prompt),
                return_exceptions=False,
            )
            content = self._reconcile_responses(feature, responses, contradictions)
        else:
            # Single-model synthesis: derive spec from the prompt itself
            content = self._local_synthesis(feature, facts, contradictions)

        return SpecItem(
            feature=feature,
            spec_type="synthesised_spec",
            content=content,
            supporting_fact_ids=fact_ids,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Internal: prompt builder
    # ------------------------------------------------------------------

    def _build_synthesis_prompt(self, feature: str, facts: list[Fact]) -> str:
        """
        Build the synthesis prompt for a feature.

        Facts are ranked by authority (AUTHORITATIVE first).
        The prompt requests structured output: API contracts (REQUIRES/ENSURES),
        data models, state machines, and UI patterns.
        """
        facts_text = "\n\n".join(
            f"[Fact {i + 1}] authority={f.authority} confidence={f.confidence} "
            f"category={f.category}\n"
            f"claim: {f.claim}\n"
            f"source: {f.evidence[0].source_url if f.evidence else 'unknown'}"
            + (
                f"\nstructured_data: {json.dumps(f.structured_data, indent=2)}"
                if f.structured_data
                else ""
            )
            for i, f in enumerate(facts)
        )

        return f"""You are a software specification synthesizer. Given the following
observed facts about the "{feature}" feature of a target application, produce a
structured specification in JSON format.

## Facts (ranked by authority, AUTHORITATIVE first)

{facts_text}

## Output Format

Return ONLY a JSON object (no prose before or after) with this structure:

```json
{{
  "feature": "{feature}",
  "summary": "One-sentence description of the feature",
  "api_contracts": [
    {{
      "endpoint": "METHOD /path",
      "requires": ["precondition 1", "precondition 2"],
      "ensures": ["postcondition 1", "postcondition 2"],
      "data_model": {{}}
    }}
  ],
  "ui_patterns": [
    {{
      "component": "component name",
      "behavior": "description",
      "states": ["state1", "state2"]
    }}
  ],
  "data_models": [
    {{
      "entity": "entity name",
      "fields": {{}},
      "constraints": []
    }}
  ],
  "state_machines": [
    {{
      "entity": "entity name",
      "states": [],
      "transitions": []
    }}
  ],
  "business_rules": [],
  "open_questions": [],
  "confidence_notes": "explanation of confidence level"
}}
```

Prioritize AUTHORITATIVE facts. Note any contradictions in open_questions.
"""

    # ------------------------------------------------------------------
    # Internal: contradiction flagging
    # ------------------------------------------------------------------

    def _flag_contradictions(self, facts: list[Fact]) -> list[dict[str, Any]]:
        """
        Identify contradictions within a feature's facts.

        Returns a list of contradiction descriptors. Contradictions are flagged
        for human review — they are NOT auto-resolved.
        """
        contradictions: list[dict[str, Any]] = []

        # Check explicit contradiction links
        fact_by_id = {f.id: f for f in facts}
        seen_pairs: set[frozenset[str]] = set()

        for fact in facts:
            for contra_id in fact.contradicts:
                pair = frozenset([fact.id, contra_id])
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                other = fact_by_id.get(contra_id)
                contradictions.append({
                    "fact_id_a": fact.id,
                    "claim_a": fact.claim,
                    "authority_a": str(fact.authority),
                    "fact_id_b": contra_id,
                    "claim_b": other.claim if other else "(not in current set)",
                    "authority_b": str(other.authority) if other else "unknown",
                    "resolution": "human_review_required",
                })

        # Also check facts with CONTRADICTED status
        for fact in facts:
            if fact.status == FactStatus.CONTRADICTED:
                for contra_id in fact.contradicted_by:
                    pair = frozenset([fact.id, contra_id])
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    other = fact_by_id.get(contra_id)
                    contradictions.append({
                        "fact_id_a": fact.id,
                        "claim_a": fact.claim,
                        "authority_a": str(fact.authority),
                        "status": "contradicted",
                        "fact_id_b": contra_id,
                        "claim_b": other.claim if other else "(not in current set)",
                        "authority_b": str(other.authority) if other else "unknown",
                        "resolution": "human_review_required",
                    })

        return contradictions

    # ------------------------------------------------------------------
    # Internal: reconciliation
    # ------------------------------------------------------------------

    def _reconcile_responses(
        self,
        feature: str,
        responses: list[AIResponse],
        contradictions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        Reconcile responses from multiple AI tools into a single spec.

        Strategy:
        - Parse each response as structured JSON.
        - Merge top-level fields, preferring the first successful parse.
        - Collect all open_questions and business_rules from all responses.
        - Append contradiction flags to open_questions.
        - Record which tools responded and any errors.
        """
        parsed: list[dict[str, Any]] = []
        consultation_errors: list[str] = []

        for resp in responses:
            if resp.error:
                consultation_errors.append(f"{resp.tool}: {resp.error}")
                continue
            content = _parse_spec_content(resp.content, feature, "synthesised_spec")
            content["_source_tool"] = resp.tool
            parsed.append(content)

        if not parsed:
            # All consultations failed — produce a minimal stub
            return {
                "feature": feature,
                "summary": f"Synthesis failed — no AI responses for {feature}",
                "api_contracts": [],
                "ui_patterns": [],
                "data_models": [],
                "state_machines": [],
                "business_rules": [],
                "open_questions": [f"Consultation errors: {consultation_errors}"],
                "contradictions": contradictions,
                "consultation_errors": consultation_errors,
            }

        # Start with the first successful response as the base
        base = dict(parsed[0])
        base.pop("_source_tool", None)

        # Merge additional open_questions and business_rules from subsequent responses
        all_open_questions: list[Any] = list(base.get("open_questions", []))
        all_business_rules: list[Any] = list(base.get("business_rules", []))

        for extra in parsed[1:]:
            for q in extra.get("open_questions", []):
                if q not in all_open_questions:
                    all_open_questions.append(q)
            for r in extra.get("business_rules", []):
                if r not in all_business_rules:
                    all_business_rules.append(r)

        # Append contradiction flags
        for c in contradictions:
            oq = (
                f"Contradiction between fact {c['fact_id_a']} ({c['claim_a']!r}) "
                f"and fact {c['fact_id_b']} ({c['claim_b']!r}) — human review required"
            )
            if oq not in all_open_questions:
                all_open_questions.append(oq)

        base["open_questions"] = all_open_questions
        base["business_rules"] = all_business_rules
        base["contradictions"] = contradictions
        base["consulted_tools"] = [r.tool for r in responses if not r.error]
        if consultation_errors:
            base["consultation_errors"] = consultation_errors

        return base

    def _local_synthesis(
        self,
        feature: str,
        facts: list[Fact],
        contradictions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        Produce a spec without external AI — used when use_multi_ai=False.

        Groups facts by category and summarises claims into the spec structure.
        """
        api_contracts: list[dict[str, Any]] = []
        ui_patterns: list[dict[str, Any]] = []
        data_models: list[dict[str, Any]] = []
        state_machines: list[dict[str, Any]] = []
        business_rules: list[str] = []

        from scripts.models import FactCategory

        for fact in facts:
            cat = fact.category
            if cat == FactCategory.API_ENDPOINT:
                api_contracts.append({
                    "claim": fact.claim,
                    "structured_data": fact.structured_data,
                    "authority": str(fact.authority),
                    "fact_id": fact.id,
                })
            elif cat == FactCategory.UI_COMPONENT:
                ui_patterns.append({
                    "claim": fact.claim,
                    "authority": str(fact.authority),
                    "fact_id": fact.id,
                })
            elif cat == FactCategory.DATA_MODEL:
                data_models.append({
                    "claim": fact.claim,
                    "structured_data": fact.structured_data,
                    "authority": str(fact.authority),
                    "fact_id": fact.id,
                })
            elif cat in (FactCategory.USER_FLOW, FactCategory.WS_EVENT):
                state_machines.append({
                    "claim": fact.claim,
                    "category": str(cat),
                    "authority": str(fact.authority),
                    "fact_id": fact.id,
                })
            elif cat == FactCategory.BUSINESS_RULE:
                business_rules.append(fact.claim)

        open_questions: list[str] = []
        for c in contradictions:
            open_questions.append(
                f"Contradiction between fact {c['fact_id_a']} ({c['claim_a']!r}) "
                f"and fact {c['fact_id_b']} ({c['claim_b']!r}) — human review required"
            )

        summary_claims = [f.claim for f in facts[:3]]
        summary = f"Feature '{feature}' — {len(facts)} facts synthesised locally."
        if summary_claims:
            summary += " Key claims: " + "; ".join(summary_claims[:2]) + "."

        return {
            "feature": feature,
            "summary": summary,
            "api_contracts": api_contracts,
            "ui_patterns": ui_patterns,
            "data_models": data_models,
            "state_machines": state_machines,
            "business_rules": business_rules,
            "open_questions": open_questions,
            "contradictions": contradictions,
            "synthesis_mode": "local",
        }

    # ------------------------------------------------------------------
    # Internal: invariant validation
    # ------------------------------------------------------------------

    def _validate_invariants(
        self,
        bundle: SpecBundle,
        facts: list[Fact],
        scope_set: set[str],
    ) -> None:
        """
        Validate INV-015, INV-034, INV-035.

        Raises SynthesisError if any invariant is violated.
        """
        bundle_fact_ids = set(bundle.fact_ids)
        fact_ids_in_scope = {f.id for f in facts if f.feature in scope_set}

        # INV-035: bundle must not contain facts outside scope
        out_of_scope = bundle_fact_ids - fact_ids_in_scope
        if out_of_scope:
            raise SynthesisError(
                f"INV-035 violated: bundle contains {len(out_of_scope)} fact(s) "
                f"outside scope: {sorted(out_of_scope)[:5]}"
            )

        # INV-034 + INV-015: every SpecItem's supporting_fact_ids ⊆ bundle.fact_ids
        # and at least one supporting fact
        for item in bundle.spec_items:
            if not item.supporting_fact_ids:
                raise SynthesisError(
                    f"INV-015 violated: SpecItem for feature={item.feature!r} "
                    f"has no supporting_fact_ids"
                )
            extra = set(item.supporting_fact_ids) - bundle_fact_ids
            if extra:
                raise SynthesisError(
                    f"INV-034 violated: SpecItem for feature={item.feature!r} "
                    f"references fact IDs not in bundle.fact_ids: {sorted(extra)[:5]}"
                )
