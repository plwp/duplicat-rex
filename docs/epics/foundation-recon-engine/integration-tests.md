# Integration Tests: Foundation & Recon Engine

These tests validate that the full system works end-to-end. They are run by `/close-epic` after all tickets are merged.

## Testing Infrastructure

### Mock SaaS Server

All integration tests that exercise recon modules use a **local fixture server** (a lightweight HTTP/WS server serving deterministic test data) rather than hitting live targets. This ensures:
- Tests are fast and deterministic
- No external dependencies or rate limiting
- Fixtures can simulate edge cases (auth failures, partial responses, contradictions)
- Tests can run in CI without credentials

The fixture server is implemented as a pytest fixture that starts a local HTTP server serving static HTML, JSON, and WebSocket endpoints from `tests/fixtures/`.

---

## Test Specifications

### IT-01: Round-Trip Fact Persistence

```
GIVEN a Fact created by browser_explore module with:
    - feature="boards"
    - category=UI_COMPONENT
    - claim="Board list shows all workspace boards"
    - One EvidenceRef with source_url and selector
WHEN stored via spec_store.add_fact()
THEN spec_store.get_fact(id) returns an identical Fact
AND the fact appears in query_facts(feature="boards", module="browser_explore")
AND the fact's content_hash is stable across serialize/deserialize
AND the fact appears in the store's index.json under both feature and module keys
```

### IT-02: Multi-Module Fact Ingestion

```
GIVEN stub implementations of at least 3 recon modules:
    - browser_explore (AUTHORITATIVE)
    - api_docs (AUTHORITATIVE)
    - community (ANECDOTAL)
AND a shared Scope with features=["boards", "cards"]
AND a local fixture server serving test data for each module
WHEN all three modules run against the fixture server
THEN the spec store contains facts from all three modules
AND facts are correctly categorized by authority (AUTHORITATIVE, AUTHORITATIVE, ANECDOTAL)
AND facts for the same feature from different modules can be queried together
AND no duplicate facts exist (by content_hash)
AND each fact has run_id set to the same recon run ID
AND each fact has module_name matching its source module
```

### IT-03: Fact Lifecycle Transitions

```
GIVEN a fact with status=UNVERIFIED
WHEN verify() is called with a valid corroborating fact ID of equal or higher authority
THEN status becomes VERIFIED and verified_by contains the corroborating ID

GIVEN a fact with status=VERIFIED
WHEN contradict() is called with a valid contradicting fact ID of equal authority
THEN status becomes CONTRADICTED and contradicted_by contains the contradicting ID

GIVEN a fact with status=CONTRADICTED
WHEN verify() is attempted
THEN the operation is rejected with ValueError (CONTRADICTED is terminal)

GIVEN a fact with status=UNVERIFIED (ANECDOTAL authority)
WHEN verify() is called with a corroborating fact that is also ANECDOTAL
THEN the operation is rejected (only authoritative evidence can verify anecdotal claims)
```

### IT-04: Bundle Lifecycle End-to-End

```
GIVEN a spec store with 20+ facts across 3 features from 3 modules
WHEN a bundle is created with those features
AND facts are added to the bundle
AND validate_bundle() is called
THEN validation passes (all facts exist, none contradicted, at least one per feature)
AND set_bundle_status(VALIDATED) succeeds
AND snapshot_bundle() produces a snapshot file
AND the snapshot file contains the bundle metadata and all facts inlined
AND the snapshot's content_hash matches recomputation from the same facts
AND the bundle cannot be modified after snapshot (any attempt raises ValueError)
```

### IT-05: Orchestrator Module Discovery and Execution

```
GIVEN the orchestrator (#15) and at least 2 registered modules
AND a parsed and frozen Scope
AND a local fixture server
WHEN the orchestrator runs a recon pass
THEN it discovers all registered modules
AND calls validate_prerequisites() on each
AND calls run() on each with correct credentials from the keychain
AND collects all ReconResults
AND stores all facts in the spec store via add_facts()
AND reports aggregate statistics (total facts, by module, by authority, by feature)
AND identifies gaps (features in scope with no authoritative facts)
```

### IT-06: Credential Flow (Keychain to Module)

```
GIVEN a module that requires_credentials = ["target.test-app.api-key"]
AND the credential exists in the keychain under the "duplicat-rex" service
WHEN the orchestrator prepares to run the module
THEN it fetches the credential from the keychain
AND passes it to the module's run() via the credentials parameter
AND the credential never appears in:
    - Any Fact's claim, structured_data, or evidence
    - Any ReconProgress message
    - Any log output
    - Any ReconError message
```

### IT-07: Scope Parsing and Dependency Resolution

```
GIVEN scope input "boards, lists, cards, drag-drop, labels"
WHEN parsed by the scope parser (#7)
THEN the resulting Scope object contains all 5 features as ScopeNodes
AND all feature keys are normalized to lowercase slugs
AND transitive dependencies are detected and flagged (inclusion_reason="dependency")
AND dependency_order() returns valid topological waves with no circular dependencies
AND known_exclusions lists features adjacent to scope that are not included
AND scope_hash is deterministic (same input always produces same hash)
AND after freeze(), no features can be added or removed
```

### IT-08: Contradiction Detection and Resolution

```
GIVEN two facts about the same feature:
    - Fact A (AUTHORITATIVE, browser_explore): "Board names have a 200 char limit"
    - Fact B (ANECDOTAL, community): "Board names have no length limit"
WHEN both are stored
AND find_contradictions() is called
THEN the pair (A, B) is returned

WHEN contradict() is called on B with A as the contradicting fact
THEN B's status becomes CONTRADICTED
AND A remains in its current status (AUTHORITATIVE fact is not affected)
AND the provenance chain is intact (B.contradicted_by contains A.id)
```

### IT-09: Marketing Fact Contradicted by Live App Fact

```
GIVEN a marketing module fact (ANECDOTAL):
    "Trello supports unlimited boards on the free plan"
AND a browser_explore fact (AUTHORITATIVE):
    "Free plan shows 'Upgrade to create more boards' after 10 boards"
WHEN both facts are stored and contradiction is detected
THEN the marketing fact is marked CONTRADICTED
AND the browser_explore fact remains UNVERIFIED or VERIFIED
AND the final spec bundle for the "boards" feature uses the authoritative fact
AND the contradicted marketing fact is excluded from the validated bundle
```

### IT-10: Snapshot Integrity and Diffing

```
GIVEN bundle v1 is snapshotted with 15 facts
AND bundle v2 is created with parent_id=v1.id
AND v2 adds 5 new facts and includes 13 of v1's facts (2 superseded)
AND v2 is validated and snapshotted
WHEN diff_snapshots(v1.id, v2.id) is called
THEN the diff result has keys {added, removed, changed}
AND added contains 5 facts, removed contains 2 facts, changed contains 13 facts
AND both snapshots remain independently valid (immutable)
AND v1.content_hash has not changed since v1 was snapshotted
AND v2.content_hash differs from v1.content_hash
AND v2.version == v1.version + 1
```

### IT-11: Partial Failure Resilience

```
GIVEN 3 recon modules where one (community) will fail (simulated network error)
WHEN the orchestrator runs all three
THEN the orchestrator receives ReconResults from all three
AND the failed module returns status=FAILED with errors describing the failure
AND the other two modules' facts are still stored in the spec store
AND the orchestrator reports the partial failure without crashing
AND the orchestrator's summary shows which modules succeeded and which failed
AND the run can be resumed later targeting only the failed module
```

### IT-12: Store Deduplication Under Concurrent Writes

```
GIVEN two modules producing facts with identical content
    (same claim, same feature, same source URL, same module)
WHEN both modules' results are stored concurrently via add_facts()
THEN only one fact with that content_hash exists in the store
AND both add_fact() calls return the same fact (the winner)
AND the store index is consistent (no orphan references)
AND the index fact count matches the actual file count in facts/
```

### IT-13: Full Recon Path (Extract, Store, Synthesise, Snapshot)

```
GIVEN a local fixture server simulating a simple target app with:
    - 2 HTML pages (login, dashboard)
    - 1 API endpoint (/api/boards)
    - 1 help center page
AND a scope of ["boards", "auth"]
WHEN the full recon pipeline runs:
    1. browser_explore extracts UI facts from the fixture pages
    2. api_docs extracts API facts from the fixture endpoint
    3. help_center extracts observational facts from the fixture help page
    4. Facts are stored in the spec store
    5. A bundle is created, validated, and snapshotted
THEN the snapshot contains facts from all three sources
AND every fact has correct authority and source_type
AND the bundle has at least one fact per scoped feature
AND the snapshot file is self-contained (bundle + all facts inlined)
AND the snapshot's content_hash is valid
```

### IT-14: Provenance Chain Traversal

```
GIVEN the following fact chain:
    1. Fact A (ANECDOTAL, marketing): "Trello has 3 pricing tiers"
    2. Fact B supersedes A (OBSERVATIONAL, help_center): "Trello has 4 pricing tiers"
    3. Fact C contradicts B (AUTHORITATIVE, browser_explore): "Trello has 5 pricing tiers (Free, Standard, Premium, Enterprise, special)"
    4. Fact D supersedes B (OBSERVATIONAL, updated): "Trello has 5 pricing tiers"
WHEN get_fact_lineage(D.id) is called
THEN the chain [A, B, D] is returned in chronological order

WHEN querying the spec store for feature="pricing"
THEN only non-contradicted, non-superseded facts appear in default queries
AND the full chain is accessible via lineage traversal
```

### IT-16: Multi-Module Credential Injection

```
GIVEN 2 recon modules with different credential requirements:
    - browser_explore requires ["target.test-app.username", "target.test-app.password"]
    - api_docs requires ["target.test-app.api-key"]
AND all credentials exist in the keychain under the "duplicat-rex" service
WHEN the orchestrator runs both modules
THEN browser_explore receives only its declared credentials in services.credentials
AND api_docs receives only its declared credentials in services.credentials
AND neither module receives the other module's credentials (INV-029)
AND credentials never appear in any Fact, ReconProgress, or log output (INV-028)
```

### IT-17: Frozen Scope vs Out-of-Scope Discovery

```
GIVEN a frozen scope with features=["boards", "cards"]
AND a recon module that discovers facts about "labels" (not in scope)
WHEN the module runs and returns facts for "boards", "cards", and "labels"
THEN facts for "boards" and "cards" are stored normally
AND the "labels" feature appears in scope.unknown_features
AND "labels" facts are stored but flagged as out-of-scope in the coverage report
AND the scope remains frozen (no new features added to scope)
AND validate_bundle() for a bundle scoped to ["boards", "cards"] does not
    include "labels" facts (INV-035)
```

### IT-18: Authority Boundary — OBSERVATIONAL Cannot Verify ANECDOTAL

```
GIVEN an ANECDOTAL fact (community module): "Boards support custom backgrounds"
AND an OBSERVATIONAL fact (help_center module) corroborating the same claim
WHEN update_fact_status(anecdotal_fact, VERIFIED, related_fact_ids=[observational_fact.id])
    is attempted
THEN the operation is rejected (INV-010: only AUTHORITATIVE can verify ANECDOTAL)

WHEN the OBSERVATIONAL corroboration is recorded via a confidence update instead
THEN the anecdotal fact's confidence rises (e.g. LOW -> MEDIUM)
AND the anecdotal fact's status remains UNVERIFIED
AND the anecdotal fact's corroborates list includes the observational fact

WHEN an AUTHORITATIVE fact (browser_explore) later corroborates the claim
AND update_fact_status(anecdotal_fact, VERIFIED, related_fact_ids=[authoritative_fact.id])
    is called
THEN the anecdotal fact becomes VERIFIED
```

### IT-15: Pre-Merge Check Validates All Layers

```
GIVEN the complete repo scaffold (#1) with pre-merge check (#2)
WHEN scripts/pre-merge-check.sh is run
THEN it detects the Python project
AND runs pytest
AND runs any configured linters (ruff, mypy)
AND exits 0 only if all checks pass
AND exits non-zero if any check fails
```
