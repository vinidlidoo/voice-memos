# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

- **Run the CLI**: `uv run src/transcribe.py [--all | --list-runs | --regenerate RUN_ID] [-o OUTPUT_DIR]`
- **Unit tests**: `uv run pytest`
- **Single test**: `uv run pytest tests/unit/test_transcribe.py::test_name`
- **Lint**: `uv run ruff check`
- **Real-API smoke test** (gated on `GROQ_API_KEY`, costs money): `uv run pytest -m e2e`

## Architecture

Single-file implementation in `src/transcribe.py` — intentional for v1. All core logic (parsing, rate limiting, rendering, state I/O, transcription, retry, main loop, CLI) lives in one module. Tests import pure functions via `pythonpath = ["src"]` in `pyproject.toml`.

**State file** (`src/state.json`, gitignored) has two sections with separated concerns:

- `files` — one entry per audio file, keyed by filename. Written atomically after each successful transcription (crash-safe).
- `runs` — one entry per markdown output, keyed by local-time run ID. Stores the chronological list of filenames that belonged to that run.

The separation matters: `--all` refreshes `files` in place without touching `runs`, and `--regenerate <run_id>` re-renders markdown from the current `files` contents for a past run.

**CLI modes** are mutually exclusive via an argparse group. Default = discover → filter against state → transcribe → render. Alternate modes: `--all`, `--list-runs`, `--regenerate RUN_ID`.

**Dependency injection for testability**: `process_new_memos`, `refresh_all_transcriptions`, and `retry_with_backoff` accept injected `transcriber`, `clock`, and `sleep` parameters (defaulting to real implementations). Unit tests pass fakes; `tests/e2e/smoke.py` uses defaults for a real end-to-end run.

## Gotchas

- `src/state.json` lives next to the script, not at the repo root.
- Default memo directory: `~/Library/Mobile Documents/com~apple~CloudDocs/VoiceMemos/` (iCloud Drive). Legacy `.qta` files are skipped with a warning.
- Run ID (local time) and `state.json`'s `created_at` (UTC) are the same instant in two time zones — not two timestamps.
- `httpx[socks]` is a runtime dep because Claude Code's sandbox routes subprocess traffic through a local SOCKS5 proxy. Direct terminal runs don't need it, but sandboxed runs do.
- Exit codes: `0` success, `1` user error (unknown `--regenerate RUN_ID`), `2` system state error (corrupt `state.json`, malformed run entry).

## Plans and decisions

Architectural decisions are logged in plan files under `plans/active/` (in-flight) and `plans/completed/` (shipped). `plans/index.md` is the progressive-disclosure summary. Before making non-trivial changes, consult the relevant plan's **"Decisions logged during implementation"** section — it captures the *why* behind choices that aren't obvious from the code. Tech debt is tracked separately in `plans/tech-debt-tracker.md`.
