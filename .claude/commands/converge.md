# /converge — Convergence Orchestrator

Drive the clone-vs-target parity loop until parity is achieved or a stop condition is met.

## Usage

```
/converge <owner/repo> --target <target_url> [--clone <clone_url>] [--max-iterations N] [--budget USD] [--parity N] [--scope "feat1, feat2"] [--weights "feat:N,feat:N"] [--suite <path>]
```

**Examples:**
```
/converge plwp/trello-clone --target trello.com --max-iterations 10 --budget 50
/converge plwp/trello-clone --target https://trello.com --clone https://clone.example.com --parity 90
/converge plwp/trello-clone --target trello.com --scope "boards, cards, auth" --max-iterations 5
```

**Arguments:**
- `<owner/repo>` — GitHub repo of the clone being built (required)
- `--target` — URL of the canonical target application (required; bare domain resolved to https://)
- `--clone` — URL of the clone under test (default: `http://localhost:3000`)
- `--max-iterations` — Maximum convergence iterations before stopping (default: 10)
- `--budget` — USD cost budget; stops when accumulated cost exceeds this (default: unlimited)
- `--parity` — Target parity percentage to achieve (default: 95.0)
- `--scope` — Comma-separated feature filter (default: all features in the test suite)
- `--weights` — Feature weights as `feature:weight` pairs (default: 1.0 each)
- `--suite` — Path to the generated test suite directory (default: `./output/tests`)

## Steps

### 1. Parse Arguments

Extract from `$ARGUMENTS`:
- `repo` — first positional arg (e.g. `plwp/trello-clone`)
- `target_url` — value of `--target` (prepend `https://` if no scheme)
- `clone_url` — value of `--clone` (default: `http://localhost:3000`)
- `max_iterations` — value of `--max-iterations` (default: 10, must be >= 1)
- `budget` — value of `--budget` as float (default: None = unlimited)
- `target_parity` — value of `--parity` as float (default: 95.0)
- `scope_str` — value of `--scope "..."` (strip surrounding quotes)
- `weights_str` — value of `--weights "..."`, parse as `feature:float` pairs
- `suite_dir` — value of `--suite` (default: `./output/tests`)

Validate: `repo` and `--target` are required. If missing, print usage and stop.

### 2. Locate the Repo Root

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"
```

### 3. Resolve Target Repo

Resolve the clone repo to a local path using chief-wiggum's repo helper:

```bash
CW_HOME=$(python3 scripts/repo.py home 2>/dev/null || echo "$HOME/repos/chief-wiggum")
CLONE_REPO_PATH=$(python3 "$CW_HOME/scripts/repo.py" resolve "$REPO" 2>/dev/null || echo "")
```

If resolution fails, continue without a local repo path (issue creation will use the GitHub API directly).

### 4. Build Scope

```python
from scripts.scope import parse_scope, freeze_scope

if scope_str:
    scope = parse_scope(scope_str, target=target_url)
else:
    # Build a minimal all-features scope from the test suite
    from pathlib import Path
    conformance_dir = Path(suite_dir) / "tests" / "conformance"
    if not conformance_dir.exists():
        conformance_dir = Path(suite_dir)
    features = list({
        f.stem.replace("test_api_", "").replace("test_e2e_", "")
              .replace("test_auth_", "").replace("test_schema_", "")
              .replace("_", "-")
        for f in conformance_dir.glob("test_*.py")
    })
    scope_str_auto = ", ".join(features) if features else "unknown"
    scope = parse_scope(scope_str_auto, target=target_url)
```

### 5. Build Components

```python
import asyncio
from pathlib import Path
from scripts.compare import BehavioralComparator
from scripts.gap_analyzer import GapAnalyzer
from scripts.spec_store import SpecStore
from scripts.converge import ConvergenceConfig, ConvergenceOrchestrator

spec_store = SpecStore(Path(".spec_store"))
comparator = BehavioralComparator(Path(suite_dir))
gap_analyzer = GapAnalyzer(spec_store, Path("convergence_history"))
orchestrator = ConvergenceOrchestrator(spec_store, comparator, gap_analyzer)
```

### 6. Run Convergence Loop

```python
config = ConvergenceConfig(
    target_url=target_url,
    clone_url=clone_url,
    scope=scope,
    max_iterations=max_iterations,
    target_parity=target_parity,
    cost_budget=budget,
    weights=weights,
    repo=repo,
)

report = asyncio.run(orchestrator.run(config))
print(report.format_summary())
```

### 7. Interpret Stop Reason

| Stop reason          | Meaning                                                          | Next step                                 |
|----------------------|------------------------------------------------------------------|-------------------------------------------|
| `parity_achieved`    | Clone reached target parity — behaviorally conformant            | Run `/ship` to create a PR                |
| `max_iterations`     | Ran max iterations without achieving parity                      | Increase `--max-iterations` or fix gaps   |
| `budget_exhausted`   | USD cost budget reached before parity                            | Increase `--budget` or prioritize P1 gaps |
| `no_improvement`     | Parity stalled for 2 consecutive iterations                      | Investigate blocking gaps manually        |
| `all_circuit_breaker`| All remaining gaps triggered circuit breaker (3+ times)          | Address long-standing gaps manually       |

### 8. Report Gaps Remaining

If gaps remain after convergence, list them grouped by severity:

```
P1 gaps (core flow broken):
  - boards::test_boards_create  [iteration_count=3]
```

### 9. Save Full Report

```bash
CW_TMP="$HOME/.chief-wiggum/tmp/$(python3 -c 'import uuid; print(uuid.uuid4())')"
mkdir -p "$CW_TMP"
REPORT_PATH="$CW_TMP/convergence-report.txt"
echo "Full report saved to: $REPORT_PATH"
```

### 10. Final Status

If `stop_reason == "parity_achieved"`:
```
Clone has achieved {final_parity:.1f}% behavioral parity with {target_url}.
  Ready for production. Run /ship to create a pull request.
```

Otherwise:
```
Convergence incomplete. Final parity: {final_parity:.1f}% (target: {target_parity:.1f}%)
  Stop reason: {stop_reason}
  {len(gaps_remaining)} gap(s) remain. See convergence-report.txt for details.
```
