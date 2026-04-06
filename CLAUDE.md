# Duplicat-Rex — Agentic SaaS Reverse-Engineering Engine

An intelligence layer that reverse-engineers SaaS applications and produces full-feature-parity deployed clones. Uses chief-wiggum as the build engine.

## What This Repo Is

Duplicat-rex owns the **intelligence pipeline**: reconnaissance, spec synthesis, behavioral comparison, and gap analysis. It does NOT generate code directly — it produces structured specifications and GitHub issues that chief-wiggum's build pipeline (`/plan-epic`, `/architect`, `/implement-wave`, `/close-epic`) consumes.

Three repos in play:
1. **duplicat-rex** (this repo) — intelligence/recon engine
2. **chief-wiggum** — build engine (configured as skill source)
3. **output repo** (e.g. `plwp/trello-clone`) — the generated app, one per target

## Tech Stack

- **Language**: Python 3.11+
- **CLI**: Typer
- **AI**: Multi-model (Claude, Codex, Gemini) with structured adjudication
- **Browser automation**: Browser-use + Playwright
- **Transcription**: whisper + ffmpeg + yt-dlp
- **Secrets**: System keychain (never env vars) — same pattern as chief-wiggum

## Default Output Stack

Generated apps use:
- Next.js 14+ (App Router) + Tailwind CSS (frontend)
- FastAPI or Express (backend API + business logic)
- PostgreSQL (database)
- Redis pub/sub + Socket.io (real-time / collaborative features)
- NextAuth.js (auth)
- Docker Compose (deployment)

## Core Loop

```
Recon → Spec → Test → Build (via CW) → Compare → Gap Analysis → Loop
```

See `ARCHITECTURE.md` for full details on each step, stop conditions, and the convergence model.

## Key Principles

- **Intelligence, not implementation**: This repo figures out WHAT to build. Chief-wiggum figures out HOW.
- **Source authority matters**: Authoritative (live app, API docs) > Observational (help center, training videos) > Anecdotal (Reddit, marketing). Anecdotal sources generate hypotheses, not specs.
- **Typed specs with provenance**: Every fact carries source, confidence, freshness. The system knows WHY it believes something.
- **Behavioral parity, not pixel parity**: Comparison is "does the user get the same outcome?", not "do the pixels match?"
- **Scope is explicit**: User declares scope. Dependencies are flagged. Scope is frozen per convergence run.
- **Stop conditions are real**: Pass rate threshold, max iterations, cost budget. No infinite loops.
- **Scripts are Python**: All helpers are Python — no bash scripts.
- **Secrets never touch env vars**: Fetched from macOS Keychain at call time via `keychain.py`.

## Repo Layout

```
.claude/commands/        # Skills: /recon, /duplicate, /compare, /converge
scripts/
├── recon/               # Recon modules (browser, API docs, videos, community)
├── spec_synthesizer.py  # LLM synthesis with provenance
├── compare.py           # Behavioral conformance testing
├── gap_analyzer.py      # Gap identification + circuit breaker
└── scope.py             # Scope parsing + dependency graph
templates/               # Spec schemas, prompt templates, report formats
```

## Usage

```bash
# Recon a target SaaS
/recon trello.com --scope "boards, lists, cards, drag-drop, labels, members, auth"

# Full duplication pipeline (recon → spec → build → compare → loop)
/duplicate trello.com --output plwp/trello-clone --scope "boards, lists, cards"

# Compare clone against target
/compare plwp/trello-clone --target trello.com

# Run gap analysis and feed back into build
/converge plwp/trello-clone --target trello.com
```

## Pre-Merge Checks

Run `scripts/pre-merge-check.sh` before merging any PR. This script auto-detects project layers and runs their test/lint/build commands.

## Required Tools

- `claude` - Claude Code CLI
- `codex` - OpenAI Codex CLI
- `gemini` - Google Gemini CLI
- `gh` - GitHub CLI
- `ffmpeg` - Media processing
- `whisper` - Transcription
- `playwright` - Browser automation
- `yt-dlp` - Video download
