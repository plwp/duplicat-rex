# /recon — Recon Orchestrator

Run all recon modules against a target to gather facts and build the spec store.

## Usage

```
/recon <target> --scope "<feature1>, <feature2>, ..." [--modules module1,module2] [--features feat1,feat2]
```

**Examples:**
```
/recon trello.com --scope "boards, lists, cards, drag-drop, labels, members, auth"
/recon trello.com --scope "boards, cards" --modules api_docs,browser_explore
/recon trello.com --scope "boards, cards, labels" --features labels
```

**Arguments:**
- `<target>` — Domain or URL to recon (e.g. `trello.com`)
- `--scope` — Comma-separated list of features to cover (required)
- `--modules` — Optional comma-separated filter: only run these named modules
- `--features` — Optional comma-separated filter: targeted re-run for specific features

## Steps

### 1. Parse Arguments

Extract from `$ARGUMENTS`:
- `target` — the first positional arg (everything before the first `--`)
- `scope` — value of `--scope "..."` (strip surrounding quotes)
- `modules` — optional `--modules` value, split on commas (or None)
- `features` — optional `--features` value, split on commas (or None)

Validate: target and scope are required. If missing, print usage and stop.

### 2. Resolve the Spec Store Path

The spec store lives in the target repo root. Since this skill runs from the
target repo, use the current working directory:

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
```

### 3. Set Up the Session Temp Directory

```bash
CW_TMP="$HOME/.chief-wiggum/tmp/$(python3 -c 'import uuid; print(uuid.uuid4())')"
mkdir -p "$CW_TMP"
```

### 4. Build and Run the Orchestrator

Run the orchestrator directly from the repo root. Build scope, instantiate
SpecStore, and invoke ReconOrchestrator.run() with a progress callback that
prints live module updates:

```python
import asyncio
import sys

sys.path.insert(0, REPO_ROOT)

from scripts.models import Scope, ScopeNode
from scripts.recon.orchestrator import ReconOrchestrator
from scripts.spec_store import SpecStore
import scripts.keychain as kc

# Build scope from --scope string
raw_scope = "<scope>"
features = [s.strip() for s in raw_scope.split(",") if s.strip()]
scope = Scope(
    target="<target>",
    raw_input=raw_scope,
    resolved_features=[ScopeNode(feature=f) for f in features],
    requested_features=[ScopeNode(feature=f) for f in features],
)

store = SpecStore(REPO_ROOT)

def on_progress(p):
    pct = f" ({p.completed}/{p.total})" if p.completed is not None and p.total else ""
    print(f"  [{p.module}] {p.phase}: {p.message}{pct}", flush=True)

orchestrator = ReconOrchestrator(
    spec_store=store,
    keychain=kc,
    progress_callback=on_progress,
)

report = asyncio.run(
    orchestrator.run(
        "<target>",
        scope,
        modules=<modules_or_none>,   # list[str] | None
        features=<features_or_none>, # list[str] | None
    )
)
```

### 5. Print the Report

After the run completes, print a structured summary:

```
--- Recon Report ---
Target:    trello.com
Run ID:    <uuid>
Duration:  42.3s
Total facts gathered: 187

Facts by module:
  api_docs: 94
  browser_explore: 63
  help_center: 18
  ...

Facts by authority:
  authoritative: 157
  observational: 18
  anecdotal: 12

Facts by feature:
  boards: 43
  cards: 38
  ...

Coverage gaps (2 features with no authoritative facts):
  - drag-drop
  - labels

Modules skipped (missing prerequisites): video_transcribe
```

### 6. Report to the User

Summarise the results and suggest next steps:
- If coverage gaps exist: suggest targeted re-runs with `--modules browser_explore`
  or `--modules api_docs` for authoritative coverage; `--modules help_center` or
  `--modules video_transcribe` for observational coverage.
- If all features have authoritative facts: suggest proceeding to spec synthesis.
- If module failures occurred: show error details and suggest fixes (e.g. checking
  credentials with `python3 scripts/keychain.py list`).
