# Traceability Matrix: Foundation & Recon Engine

Maps every acceptance criterion from tickets #1-#15 to the test(s) that will verify it.

| Ticket | Acceptance Criterion | Unit Test | Integration Test | E2E Test | Status |
|--------|---------------------|-----------|-----------------|----------|--------|
| #1 | `pyproject.toml` exists with correct dependencies | `test_scaffold.py::test_pyproject_exists` | IT-15 | -- | pending |
| #1 | Directory structure matches ARCHITECTURE.md layout | `test_scaffold.py::test_directory_structure` | IT-15 | -- | pending |
| #1 | Chief-wiggum skills are accessible when working in this repo | `test_scaffold.py::test_cw_settings` | -- | -- | pending |
| #1 | `pip install -e .` succeeds | `test_scaffold.py::test_pip_install` | IT-15 | -- | pending |
| #2 | `scripts/pre-merge-check.sh` exists and is executable | `test_premerge.py::test_script_exists` | IT-15 | -- | pending |
| #2 | Script runs successfully (detecting Python layer) | `test_premerge.py::test_detects_python` | IT-15 | -- | pending |
| #2 | CLAUDE.md references the script | `test_premerge.py::test_claude_md_reference` | -- | -- | pending |
| #3 | `/test` skill exists at `.claude/commands/test.md` | `test_skills.py::test_test_skill_exists` | -- | -- | pending |
| #3 | Runs pytest with appropriate flags | `test_skills.py::test_test_skill_runs_pytest` | -- | -- | pending |
| #3 | Reports pass/fail summary | `test_skills.py::test_test_skill_reports` | -- | -- | pending |
| #3 | Optionally fixes simple failures when asked | `test_skills.py::test_test_skill_fixes` | -- | -- | pending |
| #4 | `/deploy` skill exists at `.claude/commands/deploy.md` | `test_skills.py::test_deploy_skill_exists` | -- | -- | pending |
| #4 | Documents installation steps | `test_skills.py::test_deploy_skill_content` | -- | -- | pending |
| #4 | Placeholder for future PyPI publishing | `test_skills.py::test_deploy_pypi_placeholder` | -- | -- | pending |
| #5 | `scripts/keychain.py` works with system keyring | `test_keychain.py::test_store_retrieve` | IT-06 | -- | pending |
| #5 | CLI commands for list/set/delete | `test_keychain.py::test_cli_list`, `test_cli_set`, `test_cli_delete` | -- | -- | pending |
| #5 | Secrets never appear in logs, env vars, or conversation history | `test_keychain.py::test_no_secret_leakage` | IT-06 | -- | pending |
| #5 | Integration test for store/retrieve/delete cycle | `test_keychain.py::test_full_lifecycle` | IT-06 | -- | pending |
| #6 | `templates/spec-schema.json` validates with JSON Schema Draft 2020-12 | `test_spec_schema.py::test_schema_valid` | -- | -- | pending |
| #6 | `spec_store.py` can create, read, update, snapshot, and hash spec bundles | `test_spec_store.py::test_crud`, `test_snapshot`, `test_hash` | IT-01, IT-04, IT-10 | -- | pending |
| #6 | Provenance chain is maintained through fact-to-spec transitions | `test_spec_store.py::test_provenance_chain` | IT-14 | -- | pending |
| #6 | Unit tests cover schema validation and store operations | `test_spec_store.py::*` | -- | -- | pending |
| #7 | Parses scope strings into structured objects | `test_scope.py::test_parse_scope` | IT-07 | -- | pending |
| #7 | Dependency graph can be built and queried | `test_scope.py::test_dependency_graph` | IT-07 | -- | pending |
| #7 | Transitive dependencies are flagged, not silently included | `test_scope.py::test_transitive_deps_flagged` | IT-07 | -- | pending |
| #7 | Known exclusions are documented | `test_scope.py::test_known_exclusions` | IT-07 | -- | pending |
| #7 | Scope freeze mechanism works | `test_scope.py::test_scope_freeze` | IT-07 | -- | pending |
| #7 | Unit tests for parsing, dependency detection, freeze | `test_scope.py::*` | -- | -- | pending |
| #8 | Can authenticate with target SaaS using keychain credentials | `test_browser_explore.py::test_auth` | IT-05, IT-06 | -- | pending |
| #8 | Captures HTTP request/response pairs with full headers and bodies | `test_browser_explore.py::test_http_capture` | Unit-test-primary (IT-13 covers module execution but does not assert header/body content; artifact verification is module-internal) | -- | pending |
| #8 | Captures WebSocket frames | `test_browser_explore.py::test_ws_capture` | Unit-test-primary (IT-13 fixture server has no WS endpoint; WS capture is validated at module unit-test level) | -- | pending |
| #8 | Takes screenshots at each navigation step | `test_browser_explore.py::test_screenshots` | Unit-test-primary (IT-13 does not assert screenshot artifacts; validated via artifact_uri/artifact_sha256 in unit tests) | -- | pending |
| #8 | Maps user flows as sequences of actions | `test_browser_explore.py::test_user_flows` | Unit-test-primary (user flow sequencing is internal to browser_explore; IT-13 validates fact output, not flow structure) | -- | pending |
| #8 | Outputs structured Facts with source_type=live_app, authority=authoritative | `test_browser_explore.py::test_fact_authority` | IT-02 | -- | pending |
| #8 | Handles timeouts, CAPTCHAs, and rate limiting gracefully | `test_browser_explore.py::test_error_handling` | IT-11 | -- | pending |
| #9 | Can scrape Trello's API docs | `test_api_docs.py::test_scrape_api_docs` | IT-02, IT-13 | -- | pending |
| #9 | Extracts endpoint definitions with request/response schemas | `test_api_docs.py::test_endpoint_extraction` | IT-13 | -- | pending |
| #9 | Identifies auth requirements per endpoint | `test_api_docs.py::test_auth_requirements` | -- | -- | pending |
| #9 | Outputs structured Facts | `test_api_docs.py::test_fact_output` | IT-02 | -- | pending |
| #9 | Handles pagination and nested doc pages | `test_api_docs.py::test_pagination` | -- | -- | pending |
| #10 | Can scrape Trello's help center | `test_help_center.py::test_scrape` | IT-02, IT-13 | -- | pending |
| #10 | Extracts feature descriptions and relationships | `test_help_center.py::test_feature_extraction` | IT-13 | -- | pending |
| #10 | Identifies the user mental model | `test_help_center.py::test_mental_model` | -- | -- | pending |
| #10 | Outputs structured Facts with observational authority | `test_help_center.py::test_fact_authority` | IT-02 | -- | pending |
| #11 | Can find and download Trello tutorial videos | `test_video_transcribe.py::test_find_videos` | -- | -- | pending |
| #11 | Extracts audio efficiently | `test_video_transcribe.py::test_audio_extraction` | -- | -- | pending |
| #11 | Transcription is accurate enough for feature extraction | `test_video_transcribe.py::test_transcription_quality` | -- | -- | pending |
| #11 | LLM extracts feature walkthroughs from transcripts | `test_video_transcribe.py::test_feature_walkthroughs` | -- | -- | pending |
| #11 | Outputs structured Facts | `test_video_transcribe.py::test_fact_output` | IT-02 | -- | pending |
| #12 | Can scrape Trello's marketing and pricing pages | `test_marketing.py::test_scrape` | IT-02 | -- | pending |
| #12 | Extracts feature lists per pricing tier | `test_marketing.py::test_tier_extraction` | -- | -- | pending |
| #12 | Identifies core vs premium features | `test_marketing.py::test_core_vs_premium` | -- | -- | pending |
| #12 | Outputs structured Facts with anecdotal authority | `test_marketing.py::test_fact_authority` | IT-02, IT-09 | -- | pending |
| #13 | Can search and scrape relevant Reddit threads | `test_community.py::test_reddit_scrape` | IT-02 | -- | pending |
| #13 | Filters for high-signal content | `test_community.py::test_signal_filter` | -- | -- | pending |
| #13 | Avoids low-signal noise | `test_community.py::test_noise_rejection` | -- | -- | pending |
| #13 | Outputs structured Facts with anecdotal authority | `test_community.py::test_fact_authority` | IT-02 | -- | pending |
| #13 | Respects rate limits | `test_community.py::test_rate_limiting` | -- | -- | pending |
| #14 | Can scrape Trello's changelog / release notes | `test_changelog.py::test_scrape` | IT-02 | -- | pending |
| #14 | Extracts feature additions, removals, and fixes | `test_changelog.py::test_feature_changes` | -- | -- | pending |
| #14 | Outputs structured Facts | `test_changelog.py::test_fact_output` | IT-02 | -- | pending |
| #15 | Orchestrates all recon modules | `test_orchestrator.py::test_module_discovery` | IT-05 | -- | pending |
| #15 | Runs modules in parallel where possible | `test_orchestrator.py::test_parallel_execution` | Unit-test-primary (IT-05 validates orchestrator execution and result collection but does not assert concurrent timing; parallelism is verified by unit test measuring elapsed time < sum of module durations) | -- | pending |
| #15 | Stores all Facts in spec store with correct provenance | `test_orchestrator.py::test_fact_storage` | IT-05, IT-13 | -- | pending |
| #15 | Reports summary statistics | `test_orchestrator.py::test_summary_report` | IT-05 | -- | pending |
| #15 | Identifies gaps in coverage | `test_orchestrator.py::test_gap_identification` | IT-05 | -- | pending |
| #15 | Supports targeted re-runs for specific features | `test_orchestrator.py::test_targeted_rerun` | -- | -- | pending |

## Cross-Cutting Invariant Coverage

These invariants are verified across multiple tickets rather than being owned by a single ticket:

| Invariant | Verified By | Test Coverage |
|-----------|-------------|---------------|
| INV-001 (Fact has evidence) | #6, #8-#14 | `test_spec_store.py::test_fact_validation`, IT-01 |
| INV-002 (Atomic claim) | #6, #8-#14 | `test_spec_store.py::test_atomic_claim_validation` |
| INV-003 (Fact immutability) | #6 | `test_spec_store.py::test_fact_immutability` |
| INV-004 (Content hash stability) | #6 | `test_spec_store.py::test_hash_stability`, IT-01, IT-12 |
| INV-005 (No self-reference) | #6 | `test_spec_store.py::test_no_self_reference` |
| INV-006 (Fact has run_id) | #6, #15 | `test_spec_store.py::test_run_id_required`, IT-02 |
| INV-007 (Provenance chain) | #6 | `test_spec_store.py::test_provenance_chain`, IT-14 |
| INV-008 (Contradiction evidence) | #6 | `test_spec_store.py::test_contradiction_evidence`, IT-03 |
| INV-009 (Verification evidence) | #6 | `test_spec_store.py::test_verification_evidence`, IT-03 |
| INV-010 (Authority gate on verification) | #6 | `test_spec_store.py::test_authority_gate`, IT-03 |
| INV-011 (Authority from source) | #8-#14 | `test_*.py::test_fact_authority`, IT-02 |
| INV-012 (Contradiction authority gate) | #6 | `test_spec_store.py::test_contradiction_authority`, IT-08 |
| INV-013 (Module-authority consistency) | #15 | `test_orchestrator.py::test_authority_consistency`, IT-02 |
| INV-014 (Bundle facts exist) | #6 | `test_spec_store.py::test_bundle_fact_refs`, IT-04 |
| INV-015 (Spec-to-fact traceability) | #6 | `test_spec_store.py::test_spec_traceability`, IT-04 |
| INV-016 (Snapshot immutability) | #6 | `test_spec_store.py::test_snapshot_immutable`, IT-04, IT-10 |
| INV-017 (Snapshot hash validity) | #6 | `test_spec_store.py::test_snapshot_hash_valid`, IT-04, IT-10 |
| INV-018 (Snapshot hash determinism) | #6 | `test_spec_store.py::test_snapshot_hash_deterministic`, IT-10 |
| INV-019 (Version monotonicity) | #6 | `test_spec_store.py::test_version_monotonic`, IT-10 |
| INV-020 (Module never throws) | #8-#14 | `test_*.py::test_error_handling`, IT-11 |
| INV-021 (Result validity) | #8-#14, #15 | `test_*.py::test_result_structure`, IT-05 |
| INV-022 (Fact ownership) | #8-#14 | `test_*.py::test_fact_ownership`, IT-02 |
| INV-023 (Progress monotonicity) | #8-#14 | `test_*.py::test_progress_phases` |
| INV-024 (Dedup by content hash) | #6 | `test_spec_store.py::test_deduplication`, IT-12 |
| INV-025 (Index consistency) | #6 | `test_spec_store.py::test_index_consistency`, IT-12 |
| INV-026 (Concurrent write safety) | #6 | `test_spec_store.py::test_concurrent_writes`, IT-12 |
| INV-027 (Soft delete only) | #6 | `test_spec_store.py::test_soft_delete` |
| INV-028 (Secrets never in data) | #5, #8-#14 | `test_keychain.py::test_no_leakage`, IT-06 |
| INV-029 (Scoped credential access) | #5, #15 | `test_orchestrator.py::test_credential_scoping`, IT-06, IT-16 |
| INV-030 (Secrets never in env vars) | #5 | `test_keychain.py::test_no_env_vars`, IT-06 |
| INV-031 (Scope key normalization) | #7 | `test_scope.py::test_key_normalization`, IT-07 |
| INV-032 (Bidirectional relations) | #6 | `test_spec_store.py::test_bidirectional_relations`, IT-03 |
| INV-033 (LLM provenance) | #11, #13 | `test_video_transcribe.py::test_llm_provenance`, `test_community.py::test_llm_provenance` |
| INV-034 (SpecItem facts within bundle) | #6 | `test_spec_store.py::test_specitem_fact_subset`, IT-04 |
| INV-035 (Bundle scope boundary) | #6 | `test_spec_store.py::test_bundle_scope_boundary`, IT-17 |
| INV-036 (Snapshot fact completeness) | #6 | `test_spec_store.py::test_snapshot_fact_completeness`, IT-04, IT-10 |
| INV-037 (Authority ordering ordinal) | #6 | `test_spec_store.py::test_authority_ordering`, IT-03, IT-18 |
| INV-038 (Run-level fact consistency) | #15 | `test_orchestrator.py::test_run_id_consistency`, IT-02 |
