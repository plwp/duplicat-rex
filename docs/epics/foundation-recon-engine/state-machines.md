# State Machines: Foundation & Recon Engine

## 1. Fact Lifecycle

```
                    ┌─────────────────────────┐
                    │                         │
                    ▼                         │
              ┌──────────┐   verify()   ┌─────────┐
  create() ──►│UNVERIFIED├────────────►│ VERIFIED │
              └──────────┘              └─────────┘
                    │                         │
                    │ contradict()             │ contradict()
                    ▼                         ▼
              ┌──────────────┐         ┌──────────────┐
              │ CONTRADICTED │         │ CONTRADICTED │
              └──────────────┘         └──────────────┘
                    │                         │
                    │ supersede()              │ supersede()
                    ▼                         ▼
              (new Fact created with supersedes=this.id)
```

### Transitions

| From | To | Trigger | Guard |
|------|----|---------|-------|
| (none) | UNVERIFIED | `create()` | Fact passes validation (atomic claim, evidence present, authority matches source) |
| UNVERIFIED | VERIFIED | `verify(corroborating_fact_id)` | Corroborating fact exists and has equal or higher authority (using `Authority.rank()`, not lexical comparison). For ANECDOTAL facts, only AUTHORITATIVE corroboration qualifies (see INV-010). |
| UNVERIFIED | CONTRADICTED | `contradict(contradicting_fact_id)` | Contradicting fact exists and has equal or higher authority |
| VERIFIED | CONTRADICTED | `contradict(contradicting_fact_id)` | Contradicting fact has equal or higher authority |
| Any | (superseded) | `supersede(new_fact_id)` | New fact references this fact's ID in its `supersedes` field |

### Invalid Transitions (must be rejected)

| From | To | Reason |
|------|----|--------|
| CONTRADICTED | VERIFIED | CONTRADICTED is terminal. If new evidence resolves the contradiction, create a new Fact with `supersedes` pointing to the contradicted one. This preserves the full provenance chain. |
| CONTRADICTED | UNVERIFIED | CONTRADICTED is terminal. No rollback. |
| VERIFIED | UNVERIFIED | Verification is forward-only. If the corroborating evidence is later contradicted, the fact itself transitions to CONTRADICTED. |

### Key Rules

1. **CONTRADICTED is terminal** for that fact instance. The fact record is preserved for provenance but can never become VERIFIED or UNVERIFIED again.
2. **Supersede, don't edit**: If new evidence resolves a contradiction, create a new Fact with `supersedes=contradicted_fact.id`. This preserves the full evidence trail.
3. **Authority gate on contradiction**: An ANECDOTAL fact cannot contradict an AUTHORITATIVE fact. The contradiction must come from an equal or higher authority tier.
4. **No self-reference**: A fact cannot appear in its own `verified_by` or `contradicted_by` list.
5. **Verification requires evidence**: `verify()` must reference at least one corroborating fact ID. The corroborating fact must exist in the store.
6. **Authoritative cross-verification**: Authoritative facts from live observation (e.g. `browser_explore`, `api_docs`) start as UNVERIFIED like all facts. The orchestrator can auto-verify them post-run when two authoritative modules from the same run produce corroborating facts for the same feature. This uses the standard `verify()` path -- one authoritative fact verifies the other. A lone authoritative fact with no corroboration remains UNVERIFIED until a future run provides one.

### State Machine Trigger to API Mapping

The state machine uses short trigger names for readability. Here is the mapping to the actual `SpecStore` API methods:

| Trigger (state machine) | API Method |
|--------------------------|------------|
| `create()` | `add_fact(fact)` |
| `verify(corroborating_fact_id)` | `update_fact_status(fact_id, VERIFIED, related_fact_ids=[corroborating_fact_id])` |
| `contradict(contradicting_fact_id)` | `update_fact_status(fact_id, CONTRADICTED, related_fact_ids=[contradicting_fact_id])` |
| `supersede(new_fact_id)` | `revise_fact(fact_id, patch, reason)` — creates new fact with `supersedes=fact_id` |
| `create_bundle()` | `create_bundle(target, scope)` |
| `add_facts()` / `remove_facts()` | `revise_bundle(bundle_id, patch)` |
| `validate_bundle()` | `validate_bundle(bundle_id)` — check only, returns `(bool, issues)` |
| `set_bundle_status(VALIDATED)` | `set_bundle_status(bundle_id, VALIDATED)` |
| `reopen()` | `set_bundle_status(bundle_id, DRAFT)` |
| `snapshot_bundle()` | `snapshot_bundle(bundle_id)` |

---

## 2. SpecBundle Lifecycle

```
              ┌───────┐  set_bundle_status(VALIDATED)  ┌───────────┐   snapshot_bundle()   ┌──────────┐
  create() ──►│ DRAFT ├──────────────────────────────►│ VALIDATED ├────────────────────►│ SNAPSHOT │
              └───────┘                                └───────────┘                     └──────────┘
                  │  ▲         ▲                              │
                  │  │         │ validate_bundle()             │ set_bundle_status(DRAFT)
                  │  │         │ (check only, no              │
                  │  │         │  state change)               │
                  │  │ set_bundle_status(DRAFT)                │
                  │  │                                        │
                  │  └────────────────────────────────────────┘
                  │
                  │ add_facts(), remove_facts(), revise_bundle()
                  └──── (stays DRAFT)
```

### Transitions

| From | To | Trigger | Guard |
|------|----|---------|-------|
| (none) | DRAFT | `create_bundle()` | Target is non-empty. Version is set to max(existing for same target+scope_hash)+1. |
| DRAFT | DRAFT | `add_facts()`, `remove_facts()`, `revise_bundle()` | Bundle stays in DRAFT while being modified |
| DRAFT | VALIDATED | `set_bundle_status(VALIDATED)` | Caller must first call `validate_bundle()` which returns `(True, [])`. All fact_ids reference existing facts. No contradicted supporting facts. At least one fact per scoped feature. Schema validation passes. |
| VALIDATED | SNAPSHOT | `snapshot_bundle()` | Bundle is VALIDATED. content_hash computed from canonical manifest. snapshot_at set to current time. |
| VALIDATED | DRAFT | `set_bundle_status(DRAFT)` | Only if NOT yet snapshotted. For corrections before snapshot. This is the "reopen" operation. |

### Invalid Transitions (must be rejected)

| From | To | Reason |
|------|----|--------|
| SNAPSHOT | DRAFT | SNAPSHOT is terminal and immutable. Create a new bundle with `parent_id` pointing to this snapshot. |
| SNAPSHOT | VALIDATED | SNAPSHOT is terminal. Cannot revalidate. |
| SNAPSHOT | (any modification) | content_hash, snapshot_at, fact_ids, spec_items -- nothing can be changed on a snapshot. |
| DRAFT | SNAPSHOT | Must pass through VALIDATED first. Cannot skip validation. |

### Key Rules

1. **SNAPSHOT is terminal and immutable.** To iterate, create a new bundle with `parent_id=snapshot.id` and `version` incremented.
2. **Version monotonicity**: Bundle versions for the same `(target, scope_hash)` are strictly monotonically increasing.
3. **Hash determinism**: The `content_hash` of a SNAPSHOT is deterministic and reproducible. Given the same facts and spec_items, the same hash is always produced regardless of when or where it is computed.
4. **Validation is repeatable**: `validate_bundle()` can be called multiple times on a DRAFT. It does not change state -- it only reports whether the bundle is ready. The actual state transition is performed by `set_bundle_status(VALIDATED)`.
5. **Reopening is lossy**: `set_bundle_status(DRAFT)` on a VALIDATED bundle clears `validated_at`. The bundle must be revalidated before it can be snapshotted. There is no separate `reopen()` method — `set_bundle_status(DRAFT)` handles this case.

---

## 3. Scope Lifecycle

```
              ┌────────┐   resolve()   ┌──────────┐   freeze()   ┌────────┐
  parse() ───►│ PARSED ├────────────►│ RESOLVED ├────────────►│ FROZEN │
              └────────┘              └──────────┘              └────────┘
```

### Transitions

| From | To | Trigger | Guard |
|------|----|---------|-------|
| (none) | PARSED | `parse(raw_input)` | Raw input is non-empty. Feature keys are extracted and normalized to lowercase slugs. |
| PARSED | RESOLVED | `resolve()` | Dependency graph is built. Transitive dependencies are flagged with `inclusion_reason="dependency"`. `requires` graph is verified acyclic. `unknown_features` are populated. |
| RESOLVED | FROZEN | `freeze()` | `scope_hash` is computed. `frozen=True` is set. From this point, no features can be added, removed, or modified. |

### Invalid Transitions (must be rejected)

| From | To | Reason |
|------|----|--------|
| FROZEN | RESOLVED | Frozen scope cannot be unfrozen. Start a new scope for a new run. |
| FROZEN | PARSED | No rollback from frozen. |
| PARSED | FROZEN | Cannot skip resolution. Dependencies must be resolved before freezing. |

### Key Rules

1. **Frozen for the run**: Once a scope is frozen, it is immutable for the entire recon run. Any features discovered during recon that are outside scope go into `unknown_features`, not into the scope itself.
2. **Acyclic graph**: The dependency resolution step must detect and reject cycles in the `requires` graph. `enhances` and `conflicts_with` edges do not participate in cycle detection.
3. **Deterministic hash**: `scope_hash` is computed from the sorted requested features plus the sorted resolved dependency set plus the sorted edge list. Same input always produces the same hash.
4. **New run, new scope**: If the user wants to change scope, they create a new Scope object. The old scope is preserved for provenance.

---

## 4. Recon Module Execution Lifecycle

This is not a persistent state machine but defines the expected phase sequence within a single `run()` call:

```
  init ──► auth ──► discover ──► extract ──► persist ──► complete
```

### Phase Definitions

| Phase | Description | Progress Expectation |
|-------|-------------|---------------------|
| `init` | Module setup, prerequisite checks | 0% |
| `auth` | Credential retrieval and authentication | 5-10% |
| `discover` | Finding pages, endpoints, videos to process | 10-30% |
| `extract` | Extracting facts from discovered sources | 30-90% |
| `persist` | Writing facts to the spec store | 90-95% |
| `complete` | Cleanup, final statistics | 100% |

### Rules

1. **Monotonic progress**: Phase sequence must be monotonic (no going backwards). The orchestrator relies on this for streaming and resume.
2. **Skippable phases**: `auth` can be skipped if the module does not require credentials. Other phases are mandatory.
3. **Error during any phase**: The module captures the error in `ReconResult.errors`, sets `status=PARTIAL` or `FAILED`, and still proceeds to `complete` (for cleanup).
