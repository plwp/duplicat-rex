# Invariants: Foundation & Recon Engine

These cross-cutting rules must hold at all times across the entire epic. Every ticket's tests and reviews must verify they are not violated. Invariants are grouped by category and numbered for reference in code comments and review checklists.

---

## Data Integrity

**INV-001 — Fact has evidence.** Every persisted Fact has at least one `EvidenceRef` with a non-empty `source_url`. A fact without evidence is not a fact.

**INV-002 — Atomic claim.** Each Fact contains exactly one testable assertion in `claim`. Multi-part claims must be split into separate Facts.

**INV-003 — Fact immutability.** Once a Fact is stored, its content fields (`feature`, `category`, `claim`, `structured_data`, `evidence`, `source_type`, `module_name`) are never mutated. Only lifecycle fields (`status`, `verified_by`, `contradicted_by`) change via `update_fact_status()`. Content corrections create new Facts via `revise_fact()`.

**INV-004 — Content hash stability.** A Fact's `content_hash()` is deterministic and depends only on content fields plus source identifiers. Two calls with the same inputs always produce the same hash, regardless of when or where they are computed.

**INV-005 — No self-reference.** A Fact cannot appear in its own `verified_by` or `contradicted_by` list.

**INV-006 — Fact has run_id.** Every persisted Fact has a non-empty `run_id` linking it to the recon run that produced it.

---

## Provenance

**INV-007 — Provenance chain integrity.** If Fact B has `supersedes=A.id`, then Fact A must exist in the store. Provenance chains must not be broken by deletion or corruption. `get_fact_lineage()` must always be able to traverse back to the root.

**INV-008 — Contradiction requires evidence.** A Fact's `contradicted_by` list is non-empty if and only if `status=CONTRADICTED`. Every ID in `contradicted_by` references an existing Fact.

**INV-009 — Verification requires evidence.** A Fact's `verified_by` list is non-empty if and only if `status=VERIFIED`. Every ID in `verified_by` references an existing Fact with equal or higher authority.

**INV-010 — Only authoritative evidence can verify an anecdotal claim.** An ANECDOTAL fact can only reach `VERIFIED` status if corroborated by at least one AUTHORITATIVE fact. OBSERVATIONAL corroboration can raise confidence but cannot flip status.

---

## Authority & Source Consistency

**INV-011 — Authority derived from source.** A Fact's `authority` is derived from its `source_type` according to the Module-to-Authority mapping table. It is never set freehand. `browser_explore` always produces AUTHORITATIVE facts; `community` always produces ANECDOTAL facts.

**INV-012 — Contradiction authority gate.** A Fact can only be contradicted by a Fact of equal or higher authority. An ANECDOTAL fact cannot contradict an AUTHORITATIVE fact. Specifically: ANECDOTAL can only contradict ANECDOTAL; OBSERVATIONAL can contradict OBSERVATIONAL or ANECDOTAL; AUTHORITATIVE can contradict any tier.

**INV-013 — Module-authority consistency.** Every Fact in a `ReconResult` has `authority` equal to the module's declared `authority` property. The orchestrator must reject results where this invariant is violated.

---

## Bundle

**INV-014 — Bundle facts exist.** Every `fact_id` in a SpecBundle references an existing Fact in the store. Orphan references are not permitted.

**INV-015 — Spec-to-fact traceability.** Every `SpecItem` in a validated or snapshotted bundle must trace back to at least one non-contradicted Fact via `supporting_fact_ids`.

**INV-016 — Snapshot immutability.** A bundle with `status=SNAPSHOT` cannot be modified. Its `content_hash`, `snapshot_at`, `fact_ids`, `spec_items`, and all other fields are set exactly once and never change. Any modification requires creating a new bundle version.

**INV-017 — Snapshot hash validity.** A snapshot's `content_hash` must match the recomputed hash from its canonical manifest (schema_version, scope_hash, sorted spec_items, sorted supporting fact tuples). If any input changes, the hash changes.

**INV-018 — Snapshot hash determinism.** The same validated bundle produces the same `content_hash` on any machine at any time, given the same facts and spec_items. The canonical serialization is fixed and platform-independent.

**INV-019 — Version monotonicity.** Bundle versions for the same `(target, scope_hash)` are strictly monotonically increasing. Creating a new bundle automatically assigns `version = max(existing) + 1`.

---

## Module Contract

**INV-020 — Module never throws.** A recon module's `run()` method never raises an exception. All errors are captured in `ReconResult.errors`. The orchestrator never needs try/except around module calls. Only contract violations or unrecoverable bootstrap failures (detected in `validate_prerequisites()`) may raise.

**INV-021 — Result validity.** Every `ReconResult` has `module` set to the module's `name` and `status` set. If `status=SUCCESS`, at least one Fact is returned. If `status=FAILED`, `facts` is empty.

**INV-022 — Fact ownership.** Every Fact produced by a module has `module_name` set to that module's `name` property and `source_type` set to that module's `source_type` property.

**INV-023 — Progress monotonicity.** Progress events emitted by a module have a monotonically advancing phase sequence: `init -> auth -> discover -> extract -> persist -> complete`. The orchestrator can rely on this for streaming and resume.

---

## Store

**INV-024 — Deduplication by content hash.** The store never contains two Facts with the same `content_hash`. `add_fact()` returns the existing Fact on hash collision. This is enforced at the store level, not the module level.

**INV-025 — Index consistency.** The index (`index.json`) is always consistent with the facts on disk. Every query via the index returns the same results as a full scan would. Any write operation updates the index atomically.

**INV-026 — Concurrent write safety.** The store uses file locking to prevent corruption from concurrent writes. The `add_facts()` batch method acquires a single lock for all operations to minimize contention.

**INV-027 — Soft delete only.** Hard deletion is never performed. `delete_fact()` marks the fact as deleted but preserves it for provenance chain integrity. Facts referenced by SNAPSHOT bundles cannot be deleted even softly.

---

## Credential

**INV-028 — Secrets never in data.** Secret material (API keys, passwords, session tokens, OAuth tokens) and raw auth tokens never enter Facts, SpecBundles, logs, progress messages, or LLM prompts. The keychain module fetches and returns them; they are passed directly to SDK constructors or request headers and discarded after use.

**INV-029 — Scoped credential access.** A recon module only receives credentials it declared in `requires_credentials`. The orchestrator does not pass unrelated secrets. Modules cannot access the keychain directly -- credentials are pre-fetched and injected by the orchestrator.

**INV-030 — Secrets never in env vars.** Credentials are never set as environment variables. They are fetched from the system keychain at call time and passed directly to constructors. This prevents leakage into conversation history, subprocess environments, or crash dumps.

---

## Scope

**INV-031 — Scope key normalization.** Feature keys in Scope objects are always lowercase, hyphen-separated slugs (e.g. "drag-drop", not "Drag Drop" or "drag_drop"). All lookups, dependency edges, and fact feature tags use normalized keys.

**INV-032 — Bidirectional relation consistency.** When `update_fact_status()` adds Fact B to Fact A's `verified_by`, it must also add A to B's `corroborates`. All cross-references between facts are bidirectional and consistent.

**INV-033 — LLM provenance.** Any Fact whose `claim` or `structured_data` was generated or inferred by an LLM (not directly observed) must include the prompt version and raw context snippet in `evidence[].raw_excerpt`. This enables auditing of LLM-synthesised intelligence.

---

## Bundle Completeness

**INV-034 — SpecItem facts within bundle.** Every `fact_id` in a `SpecItem.supporting_fact_ids` must also appear in the parent `SpecBundle.fact_ids`. A spec item cannot reference facts outside its bundle.

**INV-035 — Bundle scope boundary.** A validated or snapshotted bundle must not contain facts or spec items for features outside the bundle's `scope` list. All `Fact.feature` values referenced by `fact_ids` must be a subset of the bundle's scoped features.

**INV-036 — Snapshot fact completeness.** A snapshot file must inline exactly the facts referenced by `bundle.fact_ids` — no more, no fewer. The `compute_hash()` input must use the same fact list as the snapshot contents.

---

## Ordering & Run Consistency

**INV-037 — Authority ordering is ordinal.** Authority comparison uses the rank ordering AUTHORITATIVE (3) > OBSERVATIONAL (2) > ANECDOTAL (1), not lexical string comparison. All guards that say "equal or higher authority" must use `Authority.rank()` or equivalent ordinal comparison. The same applies to Confidence: HIGH (3) > MEDIUM (2) > LOW (1).

**INV-038 — Run-level fact consistency.** All facts in a single `ReconResult` must share the same `run_id` (the `ReconRequest.run_id` that was passed to the module). This extends INV-006 (non-empty run_id) to ensure per-run grouping integrity.
