# /duplicate — Full SaaS Duplication Pipeline

Run the complete duplicat-rex pipeline: recon → spec synthesis → test generation → convergence loop.

## Usage
```
/duplicate <target_url> --output <owner/repo> --scope "<features>" [--max-iterations N] [--budget N] [--skip-browser-use]
```

## Parameters
- `target_url`: The SaaS application to duplicate (e.g. `trello.com`)
- `--output`: GitHub repo for the generated clone (e.g. `plwp/abuello`)
- `--scope`: Comma-separated features to duplicate (e.g. `"boards, lists, cards, drag-drop, labels, members, auth"`)
- `--max-iterations`: Maximum convergence iterations (default: 10)
- `--budget`: Cost budget in USD (optional)
- `--skip-browser-use`: Skip browser-based exploration

## Pipeline Steps

### Step 1: Setup
- Parse scope string into structured Scope object
- Create output repo if it doesn't exist (`gh repo create`)
- Initialize spec store

### Step 2: Reconnaissance
Run `/recon` against the target:
```bash
# Internally calls ReconOrchestrator with all 7 modules:
# browser_explore, api_docs, help_center, video_transcribe,
# marketing, community, changelog
```

### Step 3: Spec Synthesis
Run the spec synthesizer with multi-AI consultation:
- Group facts by feature
- Consult Codex + Gemini for each feature
- Reconcile into structured specs
- Flag contradictions for review

### Step 4: Snapshot & Commit
- Validate the spec bundle
- Create immutable snapshot with content hash
- Commit specs to the output repo under `.specstore/`

### Step 5: Test Generation
Generate dual-execution test cases:
- API conformance tests
- E2E Playwright tests
- Auth scenario tests
- Schema validation tests
- Commit tests to output repo under `tests/conformance/`

### Step 6: Build (via Chief-Wiggum)
Create GitHub issues in the output repo from specs. In production, invoke:
```
/plan-epic <output_repo>
/architect <output_repo> --epic "Epic: ..."
/implement-wave <output_repo> --epic "Epic: ..."
```

### Step 7: Convergence Loop
Run `/converge` to iterate toward parity:
- Compare clone against target (behavioral conformance)
- Identify gaps
- Create issues for gaps
- Repeat until stop condition met

### Stop Conditions
- Parity score >= 95% (configurable)
- Max iterations reached
- Cost budget exhausted
- No improvement for 2 consecutive iterations
- All remaining gaps are circuit-breaker (3+ iterations stuck)

### Step 8: Report
Output final report:
- Parity score achieved
- Iterations completed
- Total cost
- Duration
- Remaining gaps (if any)

## Example

```bash
/duplicate trello.com --output plwp/abuello --scope "boards, lists, cards, drag-drop, labels, members, auth" --max-iterations 10 --budget 100
```

## Notes
- The convergence loop currently creates issues but doesn't auto-invoke chief-wiggum. In production, it will call `/implement-wave` automatically.
- Scope is frozen for each convergence run. New feature discoveries are queued.
- All specs are versioned and immutable once snapshotted.
