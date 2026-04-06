# /compare — Behavioral Conformance Comparator

Run dual-execution conformance tests against a target and its clone, then report the parity score.

## Usage

```
/compare <target_url> <clone_url> [--suite <path>] [--scope "<features>"] [--weights "feat:N,feat:N"]
```

**Examples:**
```
/compare https://trello.com https://my-clone.example.com --suite ./output/tests
/compare https://trello.com https://clone.example.com --scope "boards, cards, auth"
/compare https://trello.com https://clone.example.com --weights "auth:3,boards:2,cards:1"
```

**Arguments:**
- `<target_url>` — URL of the canonical target application (required)
- `<clone_url>` — URL of the clone under test (required)
- `--suite` — Path to the generated test suite directory (default: `./output/tests`)
- `--scope` — Optional comma-separated feature filter (runs only these features)
- `--weights` — Optional feature weights as `feature:weight` pairs (default: 1.0 each)

## Steps

### 1. Parse Arguments

Extract from `$ARGUMENTS`:
- `target_url` — first positional arg
- `clone_url` — second positional arg
- `suite` — value of `--suite` (default: `./output/tests`)
- `scope` — value of `--scope "..."` (strip surrounding quotes), split on commas
- `weights` — value of `--weights "..."`, parse as `feature:float` pairs

Validate: target_url and clone_url are required. If missing, print usage and stop.

### 2. Locate the Repo Root

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"
```

### 3. Verify Test Suite Exists

Check that the suite directory contains conformance test files:

```bash
ls "${SUITE_DIR}/tests/conformance/test_*.py" 2>/dev/null || ls "${SUITE_DIR}/test_*.py" 2>/dev/null
```

If no test files are found, report:
```
No conformance tests found in <suite_dir>.
Run /recon and /generate-tests first to build the test suite.
```
Then stop.

### 4. Run the Comparator

```python
import asyncio
from pathlib import Path
from scripts.compare import BehavioralComparator, format_report

comparator = BehavioralComparator(Path("<suite_dir>"))

# If --scope was provided, build a minimal Scope object
scope = None  # or a Scope filtered to the requested features

# If --weights were provided, parse into dict[str, float]
weights = None  # or {"auth": 3.0, "boards": 2.0}

result = asyncio.run(comparator.compare(
    "<target_url>",
    "<clone_url>",
    scope=scope,
    weights=weights,
))

print(format_report(result))
```

Run this as:
```bash
python3 -c "
import asyncio
from pathlib import Path
from scripts.compare import BehavioralComparator, format_report

comparator = BehavioralComparator(Path('${SUITE_DIR}'))
result = asyncio.run(comparator.compare('${TARGET_URL}', '${CLONE_URL}'))
print(format_report(result))
"
```

### 5. Interpret the Report

The report shows:

```
======================================================================
BEHAVIORAL CONFORMANCE REPORT
======================================================================
Target: https://trello.com
Clone:  https://my-clone.example.com

Overall Parity Score: 87.5%

Score by Feature:
  auth                           [################....] 80.0%
  boards                         [####################] 100.0%
  cards                          [##################..] 90.0%

Tests run: 24
  Passed (both):  21
  Failed (clone):  3  <- conformance gaps
  Errors (target baseline unavailable):  0

----------------------------------------------------------------------
CONFORMANCE GAPS - Action Required:
----------------------------------------------------------------------

[auth] test_auth_auth_0
  Test:   test_auth_auth_0
  Target: PASS
  Clone:  FAIL
  ...
```

**Parity score interpretation:**
- 95-100%: Excellent - clone is behaviorally equivalent
- 80-94%:  Good - minor gaps, targeted fixes needed
- 60-79%:  Moderate - significant behavioral divergence
- <60%:    Poor - clone is not conformant; major re-work required

### 6. Report to User

Output the full conformance report. Highlight:
1. Overall parity score (is the clone conformant?)
2. Lowest-scoring features (where to focus clone implementation work)
3. Each conformance gap with the test name and actionable remediation hint

If parity_score >= 95, declare the clone behaviorally conformant.
If parity_score < 95, list the failing tests and recommend re-running after fixes.

### 7. Save Report (Optional)

If the user wants to save the report for later reference:

```bash
CW_TMP="$HOME/.chief-wiggum/tmp/$(python3 -c 'import uuid; print(uuid.uuid4())')"
mkdir -p "$CW_TMP"
REPORT_PATH="$CW_TMP/conformance-report.txt"
echo "Report saved to: $REPORT_PATH"
```
