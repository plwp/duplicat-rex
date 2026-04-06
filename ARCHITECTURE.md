# Duplicat-Rex — Architecture Decisions

## What We're Building

An agentic intelligence layer that reverse-engineers SaaS applications and produces full-feature-parity clones. It works by gathering intelligence from every available source (live app exploration, API docs, training videos, marketing, community discussions), synthesising structured specifications, then feeding those specs into chief-wiggum's build pipeline for implementation. A convergence loop compares the clone against the target, identifies gaps, and iterates until parity is achieved. The PoC target is Trello — chosen for its clean API, iconic UI, and topical relevance (Atlassian layoffs). The generated clone can optionally be enhanced with agent-first features that surpass the original.

## Architecture Decisions

### 1. Repo Structure — Separate repo, CW as skill source

Duplicat-rex is its own repo (`plwp/duplicat-rex`) that configures chief-wiggum as a command source. It owns the intelligence/recon pipeline. Chief-wiggum owns the build pipeline. A third repo (per target, e.g. `plwp/trello-clone`) is the generated output.

**Rationale**: Chief-wiggum is core tooling that shouldn't be polluted with experimental diversions. Duplicat-rex adds its own skills (`/recon`, `/duplicate`, `/compare`, `/converge`) and leans on CW's `/plan-epic`, `/architect`, `/implement-wave`, `/close-epic` when it's time to build.

**Artifact lineage**: The build system consumes immutable spec snapshots (versioned, hashed) committed to the output repo under `docs/specs/`. This ensures reproducibility — the output repo always knows exactly which spec version it was built from.

### 2. Language — Python 3.11+

**Rationale**: Best AI/LLM library ecosystem, chief-wiggum patterns are Python, AI models generate better Python than anything else. For a PoC, speed-to-working matters most.

### 3. CLI Framework — Typer

**Rationale**: Modern, type-hinted, excellent UX with minimal boilerplate. Good fit for the exploration-heavy workflow.

### 4. AI Orchestration — Multi-model with structured adjudication

Multi-model consultation (Claude, Codex, Gemini) with a structured reconciliation stage. All models receive the same prompt and evidence bundle. Disagreements are scored and adjudicated against a shared schema, not averaged as prose.

**Rationale**: Proven in chief-wiggum. Different models catch different things. Natural divergence produces better specs than any single model. Structured adjudication prevents hallucination multiplication — one shared schema, one evidence bundle, one reconciliation stage that scores disagreements.

**Cost control**: Full multi-model fan-out only on high-uncertainty or high-impact steps. Lower-confidence steps use a single model with cached results.

### 5. Target Exploration — Multi-source recon with authority ranking

The recon phase gathers intelligence from every available source, ranked by authority:

**Authoritative** (truth sources — these dominate specs):
- **Live app** — Browser-use + Playwright explores UI, intercepts HTTP and WebSocket traffic, maps user flows, captures screenshots
- **API documentation** — Scrape/fetch official API docs (structured endpoints, schemas, auth)

**Observational** (reliable but indirect):
- **Help center / knowledge base** — User-facing docs that explain the mental model and feature behavior
- **Training videos** — Download via yt-dlp, extract audio via ffmpeg, transcribe via whisper, extract feature walkthroughs

**Anecdotal** (hypothesis generators — must be validated against authoritative sources):
- **Marketing & pricing pages** — Feature lists and tier breakdowns reveal what the vendor considers core vs premium
- **Reddit / community forums** — User complaints, workarounds, feature requests reveal what actually matters vs what's marketed
- **Changelog / release notes** — Feature evolution, recent additions and removals
- **Open source components** — SDK docs, power-up/plugin APIs, public repos

Anecdotal sources create candidate facts that must be validated by authoritative observation before becoming specs.

**Rationale**: The live app alone doesn't tell you everything. Docs explain the intended behavior. Training videos show the happy path. Community feedback reveals what's broken or missing. But not all sources are equally trustworthy — ranking by authority prevents spec drift toward stale or anecdotal behavior.

### 6. Secret Management — System keychain

**Rationale**: Chief-wiggum pattern. Secrets never touch env vars. Target SaaS credentials (for authenticated exploration) stored the same way.

### 7. Core Loop — Convergence-based with stop conditions

```
Recon → Spec → Test → Build (via CW) → Compare → Gap Analysis → Loop
```

Each iteration:
1. **Recon** — Gather intelligence from all sources (first pass is comprehensive; subsequent passes are targeted at gaps)
2. **Spec** — LLM synthesises into typed, versioned specs with provenance (see Decision 14)
3. **Test** — Generate test cases from specs. Tests run against both target and clone for direct behavioral comparison.
4. **Build** — Create GitHub issues in the output repo, invoke chief-wiggum's epic flow
5. **Compare** — Behavioral conformance testing (see Decision 15). Not pixel-diff or naive JSON-diff.
6. **Gap Analysis** — Identify what's missing, wrong, or divergent. Prioritise by impact. Circuit breaker: if the same gap persists after 3 iterations, escalate to human or trigger deep recon on that specific feature.
7. **Loop** — Feed gaps back as new issues. Return to step 4 (or step 1 if new areas discovered). Scope is frozen per run — new discoveries are queued, not auto-expanded.

**Stop conditions**:
- Behavioral test suite pass rate ≥ threshold (e.g. 95%)
- Weighted feature coverage meets target
- Zero open P1/P2 gaps
- Max iteration count reached
- Cost budget exhausted

**Rationale**: SaaS apps are too complex to get right in one pass. The convergence loop mirrors how a human would do it — build, check, fix, repeat. The comparison step is the engine that drives the loop. Explicit stop conditions prevent endless refinement. Frozen scope per run prevents drift.

### 8. Default Output Stack — Next.js + Tailwind + Postgres + Redis + Docker

- **Frontend**: Next.js 14+ (App Router) + Tailwind CSS
- **Backend**: Next.js for SSR/frontend + dedicated API server (FastAPI or Express) for business logic, background jobs, and real-time
- **Real-time**: Redis pub/sub + Socket.io (or Supabase Realtime) for collaborative features
- **Database**: PostgreSQL
- **Auth**: NextAuth.js (matches target auth type — email/password, OAuth, etc.)
- **Containerisation**: Docker Compose for local dev, Dockerfiles for deployment

**Rationale**: Trello-class interactivity requires WebSockets, background jobs, and optimistic UI from day one. Next.js API routes are too weak for this. A proper backend with Redis pub/sub handles real-time collaboration, event propagation, and presence. Starting with this avoids a painful mid-project refactor.

### 9. Auth Duplication — Scoped auth matrix

If the target has auth, the clone has auth. The recon phase detects auth type and the clone replicates it.

**v1 auth support matrix** (PoC scope):
- Email/password registration and login
- OAuth (Google, GitHub — most common providers)
- Role-based access (workspace admin, member, guest)
- Session management (JWT or cookies)

**Deferred to v2**:
- SSO / SAML / SCIM
- MFA and recovery flows
- Enterprise RBAC edge cases
- Anti-abuse controls

**Rationale**: Auth is table-stakes for any SaaS clone. But SSO, SCIM, and enterprise RBAC can dominate scope. A declared support matrix keeps v1 focused while making the boundary explicit.

### 10. Deployment — Docker (v1), Cloud Run (v2)

- **v1**: Docker Compose for local and demo deployment
- **v2**: Google Cloud Run (standard GCP stack)

**Rationale**: Docker is universal and sufficient for PoC. GCP Cloud Run is the user's standard production stack.

### 11. Scope Control — User-specified with dependency awareness

The user can specify what to duplicate:
```
duplicat-rex recon trello.com --scope "boards, lists, cards, drag-drop, labels, members, auth"
```

The agent focuses on the specified scope but also identifies **transitive dependencies** — features adjacent to scope that are required for scoped features to work correctly (e.g. "cards" depends on "activity feed" for audit trail). Dependencies are flagged, not silently included or silently omitted.

**Known exclusions** are documented in the output so the system doesn't silently fail on features adjacent to scope.

**Rationale**: "Full feature parity" for Trello means hundreds of features. Scope control lets the user start with core product and expand. Dependency awareness prevents the "it works but notifications are broken" surprise.

### 12. Agent-First Enhancement — Optional improvement layer

The clone can optionally surpass the original with AI-powered features:
- Natural language card/task creation
- Auto-triage and prioritisation
- Smart board queries ("show me everything blocked this sprint")
- AI-powered automation rules

**Rationale**: This is the statement — "your SaaS, rebuilt in weeks, but better." The enhancement layer is optional and comes after parity is achieved.

### 13. PoC Target — Trello

**Rationale**: Clean, well-documented API. Iconic, recognisable UI (boards, lists, cards). Atlassian layoffs make it topical and pointed. Simple enough to be achievable, complex enough to be impressive. Jira was considered but rejected — decades of enterprise feature creep makes it impractical for v1.

### 14. Spec Schema — Typed, versioned, with provenance (NEW — from AI review)

Every fact extracted during recon flows through a structured intermediate representation:

```
Fact → Hypothesis → Spec → Test → Gap
```

Each item in the pipeline carries:
- **Source**: Which recon source produced it (with URL/timestamp)
- **Authority**: Authoritative / Observational / Anecdotal
- **Confidence**: High / Medium / Low
- **Freshness**: When it was last verified
- **Status**: Unverified / Verified / Contradicted
- **Provenance chain**: Which facts led to which specs

The spec schema is defined as JSON Schema (`templates/spec-schema.json`) so that chief-wiggum's build pipeline can consume it unambiguously. Specs are versioned and committed as immutable snapshots to the output repo.

**Rationale**: Without structured provenance, each loop iteration reinterprets noisy text instead of refining stable evidence. The system needs to know *why* it believes something and *how confident* it is, so contradictions and stale data can be resolved systematically.

### 15. Parity Rubric — Behavioral conformance, not diff (NEW — from AI review)

Clone-vs-target comparison uses **behavioral testing**, not pixel-diff or naive JSON-diff:

- **Fixture-based**: Deterministic test scenarios with seeded data (known users, boards, cards, controlled timestamps)
- **Dual-execution**: Same E2E test scripts run against both target and clone
- **Tolerated variance**: IDs, timestamps, ordering, and async timing are excluded from comparison. Only user-visible behavior and data integrity matter.
- **Weighted acceptance criteria**: Core flows (CRUD, auth, drag-drop) weighted higher than edge cases (power-ups, integrations)
- **Semantic conformance**: Does the clone satisfy the user intent captured during recon, not just surface-level similarity?

**Rationale**: Raw diffs produce false deltas (different IDs, timestamps, async timing). Behavioral testing asks "does the user get the same outcome?" which is what parity actually means.

### 16. Real-Time Strategy — WebSocket interception and replication (NEW — from AI review)

Trello's collaborative features (card moves, board updates, presence) run over WebSockets, not REST. The recon phase must intercept WS traffic alongside HTTP.

- **Recon**: Playwright network interception captures both HTTP and WS frames
- **Spec**: WS events are catalogued as event contracts (event name, payload schema, trigger conditions)
- **Build**: Output app includes Redis pub/sub + Socket.io for real-time event propagation
- **Compare**: Real-time behavior tested with multi-user fixtures (two browser sessions, verify event propagation)

**Rationale**: If the recon only sees HTTP, it misses the collaborative heart of the product. The clone would work but feel dead — no real-time updates, no presence, no optimistic UI.

## Patterns from Chief-Wiggum

Carrying forward:
- **Multi-AI consultation + reconciliation** — for spec synthesis and code review
- **Browser-use for exploration** — adapted from E2E validation to target recon
- **Stitch-audit schema diffing** — adapted for clone-vs-target conformance
- **Worktree isolation** — for parallel feature implementation
- **Test-first → implement → review** — via CW's `/implement` pipeline
- **Keychain secret management** — for target SaaS credentials
- **Transcription pipeline** — whisper + ffmpeg for training videos

## Repo Layout (Planned)

```
plwp/duplicat-rex/
├── .claude/
│   ├── commands/             # Duplicat-rex skills
│   │   ├── recon.md          # Intelligence gathering
│   │   ├── duplicate.md      # Full pipeline orchestration
│   │   ├── compare.md        # Clone-vs-target conformance
│   │   └── converge.md       # Gap analysis and loop control
│   └── settings.local.json   # CW as command source
├── scripts/
│   ├── recon/                # Recon modules
│   │   ├── browser_explore.py  # Live app + WS interception
│   │   ├── api_docs.py
│   │   ├── help_center.py
│   │   ├── video_transcribe.py
│   │   ├── marketing.py
│   │   ├── community.py
│   │   └── changelog.py
│   ├── spec_synthesizer.py   # LLM synthesis with provenance
│   ├── compare.py            # Behavioral conformance testing
│   ├── gap_analyzer.py       # Prioritised gap ID + circuit breaker
│   └── scope.py              # Scope parsing + dependency graph
├── templates/
│   ├── spec-schema.json      # Typed spec format (JSON Schema)
│   ├── spec-output.md        # Human-readable spec format
│   ├── comparison-report.md  # Conformance report format
│   ├── parity-rubric.md      # Behavioral acceptance criteria
│   └── recon-prompt.md       # Prompt templates for recon synthesis
├── CLAUDE.md
├── ARCHITECTURE.md
├── README.md
└── pyproject.toml
```

## Open Questions (Deferred)

1. **Adaptive output stack** — How to detect target's tech stack and match it? Deferred to v2.
2. **Rate limiting & anti-bot** — How to handle rate limits and bot detection during recon? Atlassian has sophisticated detection.
3. **Shadow state** — Side effects invisible to external recon (async automations, notifications, search indexing). How to discover these?
4. **Billing/payments** — When/how to duplicate Stripe/payment integrations?
5. **Data migration** — Import real user data from target into clone for comparison and demo purposes.
6. **Visual comparison** — Screenshot diffing as a secondary signal (after behavioral tests pass). Pixel-level or semantic?
7. **Data handling & legal** — Authenticated recon touches cookies, tokens, PII. Need redaction rules, retention policy, and LLM-send policy.
