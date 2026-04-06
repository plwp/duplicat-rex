# /test — Run the Full Test Suite

Run all tests for duplicat-rex, report results clearly, and optionally fix failures.

## Steps

### 1. Locate the repo root

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"
```

### 2. Confirm the test framework

This repo uses **pytest**. Verify it is available:

```bash
python3 -m pytest --version
```

If pytest is not installed, install it:

```bash
pip install pytest pytest-cov
```

### 3. Run linting with ruff

```bash
ruff check .
```

Report any lint errors. If there are errors and the user asked to fix them, run:

```bash
ruff check . --fix
```

Then re-run to confirm clean.

### 4. Run type checking with mypy

```bash
mypy scripts/ --ignore-missing-imports
```

Report any type errors.

### 5. Run the full test suite

```bash
python3 -m pytest -v --tb=short
```

### 6. Report results

After the run, output a clear summary:

```
=== Test Results ===
Status  : PASS / FAIL
Passed  : N
Failed  : N
Errors  : N
Skipped : N
Duration: Xs

=== Lint Results ===
Status  : CLEAN / N issues

=== Type Check ===
Status  : CLEAN / N issues
```

If all tests pass and lint is clean, say so explicitly and stop.

### 7. Fix failures (only if the user asked)

If the user invoked `/test --fix` or explicitly asked you to fix failures:

- Read each failing test's traceback carefully.
- Identify the root cause (implementation bug, missing dependency, outdated fixture, etc.).
- Apply the minimal fix — do not refactor unrelated code.
- Re-run `pytest -v --tb=short` to confirm the fix works.
- Re-run `ruff check .` to confirm lint is still clean.
- If the fix introduces new failures, stop and report rather than chasing further.

### 8. Final status

Report the final pass/fail status. If anything remains broken, list the remaining failures with their error messages so the user knows exactly what needs attention.
