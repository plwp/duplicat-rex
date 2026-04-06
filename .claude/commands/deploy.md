# /deploy — Deploy Duplicat-Rex

Install, configure, or publish duplicat-rex.

## Usage
```
/deploy [local|publish]
```

## Steps

### 1. Detect mode

- If no argument or `local`: run local installation
- If `publish`: run PyPI publishing (future)

### 2. Local Installation

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"
```

Install in editable mode:

```bash
pip install -e ".[dev]"
```

Verify the installation:

```bash
python3 -c "import scripts; print('Import OK')"
```

### 3. Verify Configuration

Check that chief-wiggum is configured as a command source:

```bash
cat .claude/settings.local.json
```

Expected: `{"commandDirs": ["~/repos/chief-wiggum/.claude/commands"]}`

### 4. Verify Required Tools

Check that required external tools are available:

```bash
which claude codex gemini gh ffmpeg yt-dlp 2>/dev/null
python3 -m playwright --version 2>/dev/null || echo "playwright: not installed"
python3 -c "import whisper" 2>/dev/null && echo "whisper: OK" || echo "whisper: not installed"
```

Report which tools are available and which are missing.

### 5. Verify Keychain

Check that the keychain module is accessible and required secrets are configured:

```bash
python3 -c "import keyring; print('keyring: OK')" 2>/dev/null || echo "keyring: not installed"
```

### 6. Report

```
=== Deployment Status ===
Mode     : local
Install  : OK / FAIL
Config   : OK / MISSING
Tools    : N/M available
Keychain : OK / FAIL
```

### 7. PyPI Publishing (future)

TODO: When ready for distribution:
- Bump version in pyproject.toml
- Build: `python3 -m build`
- Upload: `twine upload dist/*`
- Verify: `pip install duplicat-rex`
