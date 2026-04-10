# Voice Memos Transcription Pipeline

## Overview

CLI tool that transcribes voice memos from iCloud Drive into a single markdown file. Designed for capturing thoughts while walking — daily notes, tasks, research ideas — and producing structured output for downstream processing.

## Workflow

```
iPhone (Action Button → Shortcut) → iCloud Drive → transcribe.py → markdown
```

1. Record via iOS Shortcut assigned to Action Button
2. Shortcut saves `.m4a` to `iCloud Drive/VoiceMemos/` with filename: `memo_YYYY-MM-DD HH.mm.ss_LAT_LONG.m4a`
3. Files sync to Mac at `~/Library/Mobile Documents/com~apple~CloudDocs/VoiceMemos/`
4. Run `transcribe.py` to produce a markdown file with all new transcriptions

## Input

- **Directory:** `~/Library/Mobile Documents/com~apple~CloudDocs/VoiceMemos/`
- **Format:** `.m4a` only (legacy `.qta` files from Voice Memos app converted manually via ffmpeg)
- **Filename pattern:** `memo_2026-04-09 17.47.41_47.71464468712184_-122.373724393098.m4a`
  - Structure: `memo_<YYYY-MM-DD> <HH.mm.ss>_<LAT>_<LONG>.m4a`
  - Note: space between date and time components
  - Parsing regex: `memo_(\d{4}-\d{2}-\d{2}) (\d{2}\.\d{2}\.\d{2})_(-?\d+\.\d+)_(-?\d+\.\d+)\.m4a`
- **Non-matching filenames:** Process anyway (transcribe the audio), but skip metadata extraction. Log a warning. Fallback heading format in markdown: `## Memo N — <filename>` (filename used verbatim, no time/coords).
- **`.qta` files in directory:** Log a warning to stderr ("Skipping N .qta files — convert with: ffmpeg -i input.qta output.m4a"), do not process.

### iCloud Sync Handling

Files may appear in the directory as iCloud stubs (not yet downloaded). macOS transparently downloads stubs when they're read, so we don't need to explicitly force downloads — simply opening the file triggers the download via the FileProvider framework.

- Wrap file reads in try/except to catch I/O errors from failed downloads (offline, iCloud issues)
- Skip with a warning on read failure, continue with other files

## Transcription

- **API:** Groq — Whisper Large v3 Turbo (`whisper-large-v3-turbo`)
- **Why Groq:** Fast inference, simple API, competitive pricing
- **Auth:** `GROQ_API_KEY` read from environment (already exported in `.zshrc`)
- **Language:** Auto-detect (Whisper handles multilingual well enough)
- **File size limit:** Groq enforces 25 MB max. Validate before sending; error with a clear message if exceeded.
- **Rate limiting:** Configurable via module-level constants (not hardcoded inline), so tier/provider changes are a one-line edit:
  - `MAX_REQUESTS_PER_MINUTE = 20` (Groq free tier)
  - `SLEEP_BETWEEN_REQUESTS_SEC = 3.5` (derived: 60/20 + headroom)
  - Rule: only sleep when total files to process exceeds `MAX_REQUESTS_PER_MINUTE`. For smaller runs, fire as fast as possible.
  - Back-to-back runs totaling >20 in 60 seconds is ignored in v1 — not a realistic scenario. The 7,200 audio-seconds/hour limit is also ignored — walking memos are well under.
- **Retry backoff:** Exponential starting at 1s (1s → 2s → 4s), max 3 attempts.

## Output

Single markdown file per run containing only newly transcribed memos. Named by generation timestamp in **local time**: `2026-04-09_17-30-00.md`.

Memos sorted chronologically by filename timestamp.

```markdown
# Voice Memos — 2026-04-09

## Memo 1 — 17:45 — 47.714, -122.373
<transcription text>

## Memo 2 — 17:46 — 47.714, -122.373
<transcription text>
```

- **Heading time (`HH:MM`):** extracted from the filename (local time the recording was made).
- **Heading coordinates:** rounded to 3 decimal places (~110m precision) for readability. Full precision is preserved in the filename and state.
- **Empty run:** if no new memos are found, print a message to stderr and exit 0 without writing a file.
- **Output directory:** current working directory (no `--output` flag in v1).
- **Stdout:** print the absolute path of the written markdown file on success, so callers can pipe (e.g. `open "$(uv run transcribe.py)"`).

## Incremental Processing

Track processed files and past runs in `state.json`. Two top-level sections with separated concerns:

- **`files`** — transcriptions (audio → text). One entry per audio file.
- **`runs`** — markdown outputs (groups of files → markdown file). One entry per generated output.

```json
{
  "version": 1,
  "runs": {
    "2026-04-09_17-30-00": {
      "created_at": "2026-04-09T17:30:00Z",
      "files": [
        "memo_2026-04-09 17.25.12_47.71464468712184_-122.373724393098.m4a",
        "memo_2026-04-09 17.27.03_47.71501234567890_-122.374012345678.m4a"
      ]
    }
  },
  "files": {
    "memo_2026-04-09 17.25.12_47.71464468712184_-122.373724393098.m4a": {
      "transcription": "The actual transcription text...",
      "transcribed_at": "2026-04-09T17:30:15Z"
    }
  }
}
```

### `files` section

- **Key:** filename (not path — all files are in the same directory). Filenames from the Shortcut are unique by construction (per-second timestamp + GPS coords), so no hash is needed. The full filename is used verbatim as the key, with full-precision GPS.
- **Lookup:** loaded into a Python dict, giving O(1) "already processed?" checks.
- **Transcription stored:** enables regenerating past output from state.
- **`transcribed_at`:** ISO 8601 UTC (with `Z` suffix).
- **Write after each file:** `state.json` is updated after each successful transcription, not at end of batch. Writes are atomic via tmp-file-then-`os.replace` to prevent corruption on crash mid-write. A crash mid-run loses zero completed work.

### `runs` section

- **Key:** the run ID, which is the same as the markdown output filename without `.md` (local-time timestamp, e.g. `2026-04-09_17-30-00`). Natural, human-readable, no extra identifier needed.
- **`created_at`:** ISO 8601 UTC when the run was created.
- **`files`:** ordered list of filenames that belonged to that run. Regeneration pulls the current transcription from the `files` section and re-renders the markdown.
- **Empty runs** (zero new memos) are NOT tracked — if no markdown is written, no run entry is created.
- **Written atomically** alongside `files` updates.

### `--all` semantics

`--all` is a **transcription refresh** operation, not a run-creation operation:

- Re-transcribes every file currently in `files`, overwriting the `transcription` field in place.
- Does **not** touch `runs`.
- Does **not** write any markdown output.
- Prints `Refreshed N transcriptions` to stderr on completion.
- Entries are overwritten in place (not wiped up front), so a crash mid-run preserves all existing entries.
- To regenerate markdown with the refreshed transcriptions, separately call `--regenerate <run_id>` on the runs you care about.

### Regeneration semantics

`--regenerate <run_id>` re-renders the markdown for a past run using the **current** transcriptions in `files`. If `--all` was used since the original run, the regenerated markdown reflects the new transcriptions, not the originals (acceptable tradeoff — transcriptions are idempotent in practice).

If a filename referenced in `runs[run_id].files` is no longer present in `files`, skip it with a warning.

## Error Handling

| Failure | Behavior |
|---------|----------|
| Groq 429 (rate limited) | Retry with exponential backoff, max 3 attempts |
| Groq 5xx (server error) | Retry with backoff, max 3 attempts |
| Network offline | Fail fast with clear message |
| Partial run (crash mid-batch) | State already persisted per-file; re-run picks up where it left off |
| Corrupt/empty audio file | Skip with warning, continue processing remaining files |
| `state.json` corrupted | Back up to `state.json.corrupt-<timestamp>`, exit with an error. Never silently reprocess — the user must explicitly re-run (with `--all` if they want to rebuild state). |
| File exceeds 25 MB | Skip with error message suggesting ffmpeg chunking |

## CLI Interface

```
uv run transcribe.py [--all | --list-runs | --regenerate RUN_ID]
```

Each flag is mutually exclusive with the others. Default (no flags) is the common path.

| Command | Transcribes | Updates `files` | Creates run entry | Writes markdown |
|---|---|---|---|---|
| `transcribe.py` (default) | only new | yes | yes (new) | yes |
| `transcribe.py --all` | all existing | yes | no | no |
| `transcribe.py --list-runs` | no | no | no | no |
| `transcribe.py --regenerate RUN_ID` | no | no | no | yes (for that run) |

- **Default:** process new (unprocessed) files, create a new run entry, output markdown to cwd.
- **`--all`:** refresh all transcriptions in place (see Incremental Processing).
- **`--list-runs`:** print one line per past run to stdout: `<run_id>  <created_at>  <file_count> memos`. Sorted chronologically, newest last.
- **`--regenerate RUN_ID`:** re-render the markdown for a past run into cwd using current transcriptions. Output filename is `<run_id>.md` — same as the original run — overwriting if it exists. The run_id *is* the identity of the run, so regeneration lands at the same path (predictable for scripts). Exits non-zero if `RUN_ID` is unknown.
- **Progress:** print each filename to stderr as it is processed.
- **Stdout contract:** on successful markdown write (default or `--regenerate`), print the absolute path of the markdown file. On `--list-runs`, print the run list. On `--all`, nothing to stdout (progress goes to stderr).

## Design Decisions

- **One file per run, not per memo.** The unit of work is the session (e.g., a walk), not the individual recording. Keeping all memos together preserves context across recordings and lets downstream processing split by topic rather than by arbitrary recording boundaries.
- **Python over bash.** Although v1 is simple enough for bash, the roadmap includes Claude Agent SDK integration for intelligent post-processing (routing thoughts to daily notes, tasks, research). The Python Agent SDK is the most mature.
- **Processed files stay in place.** We do not move processed files to a subdirectory. `state.json` is the single source of truth for processed status — splitting state between filesystem location and JSON introduces sync bugs (what if a move fails mid-run?). Directory listing is cheap even at thousands of files.
- **`--all` is a last resort at scale.** After a few months of daily use, reprocessing everything becomes a long, rate-limited grind against Groq's 20 RPM limit. Protect `state.json` carefully (it is our durable record of all transcriptions) and treat `--all` as a recovery tool, not a routine operation.
- **Separate `files` and `runs` in state.** Transcriptions (audio → text) and markdown outputs (groups of files → markdown file) are distinct concerns. Separating them means `--all` can refresh transcriptions without affecting past run records, and past markdown files can be regenerated on demand from `state.json` if lost. This also keeps `--all` from doing two things at once.

### Decisions logged during implementation

- **M1 — Ruff ruleset:** `E, F, I, UP, B, SIM` with line-length 100, target `py312`. Broad enough to catch real issues (bugs via `B`, simplifications via `SIM`, modernizations via `UP`, import order via `I`) without being pedantic. Revisit if it gets noisy.
- **M1 — pyproject.toml layout, not `uv init`:** Wrote `pyproject.toml` by hand rather than running `uv init`, because `uv init` assumes a package layout and we have a single-script `src/transcribe.py` that doesn't need to be packaged. Configured `[tool.pytest.ini_options] pythonpath = ["src"]` so tests can `import transcribe` without a package.
- **M1 — Placeholder test kept in `tests/test_transcribe.py`:** `pytest` exits with code 5 when no tests are collected, which fails our `pytest && ruff` check chain. A trivial `test_module_imports` keeps the scaffold green and will be replaced by real tests in M2.
- **M1 — `.gitignore` for `.md`:** Top-level `*.md` is ignored (generated transcription outputs land in cwd), with explicit allow-list exceptions for `plans/*.md`, `CLAUDE.md`, `README.md`.
- **M2 — `MemoMeta` + `Memo` as frozen dataclasses.** `MemoMeta` holds parsed filename metadata (date, hhmm, timestamp, lat, lng); `Memo` is the rendering unit (filename + transcription + optional meta). Frozen → hashable and cheap to copy, reads cleanly in tests.
- **M2 — `render_markdown` is pure, no clock.** Derives the H1 date from the first parseable memo. If no memos are parseable, the H1 is just `# Voice Memos` (no date) — keeps the function free of `datetime.now()` and therefore trivially testable. All-unparseable runs are an edge case that shouldn't dictate the signature.
- **M2 — Non-parseable memos sort last.** Sort key is `(0, timestamp)` for parseable memos and `(1, filename)` for non-parseable ones, so fallback memos cluster at the end in filename order. Plan didn't specify; picking a deterministic order now avoids surprises later.
- **M2 — `load_state` tolerates partial state shape.** `setdefault`s `version`, `files`, `runs` after loading so older/hand-edited state files still work. The corrupt path backs up with `shutil.copy2` (preserves mtime) using a local-time `YYYYMMDD-HHMMSS` suffix and raises `RuntimeError` — caller decides how to surface it.

## Test Plan

Split into fast unit tests (no network, no API calls) and a manual smoke test (real Groq API).

### Testability Requirements

To keep unit tests clean, the implementation must extract these as testable units:

- `should_sleep(n_files) -> bool` — named function, not an inlined `if` in the main loop
- A transcription callable passed into the main loop (or a module-level symbol that tests can swap) — enables testing the loop without touching the Groq API
- `parse_filename(name)`, `load_state(path)`, `write_state_atomic(path, state)`, `render_markdown(memos)` as pure functions

### Unit Tests (`pytest`)

Pure logic only — no network, no filesystem state beyond `tmp_path`.

- **Filename parsing**
  - Valid filename → correct date, time, lat, long extracted
  - Negative longitude parsed correctly
  - Non-matching filename → returns `None` for metadata (fallback path)
- **State management**
  - Loading a valid `state.json` → correct dict
  - Loading a missing `state.json` → empty dict
  - Loading a corrupt `state.json` → backup file created, error raised
  - Atomic write: after `write_state_atomic()` succeeds, no tmp file lingers
  - Writing state after each file → file on disk reflects last successful transcription
- **Filtering**
  - Files in state are skipped; files not in state are selected
  - `--all` mode selects everything
  - `.qta` files in the directory are excluded from the processing list
- **`--all` overwrite semantics**
  - Start with state containing entries A and B; run `--all` against only A; assert B is still present in state. Guards the crash-safety design decision.
  - `--all` does not create a new run entry and does not write markdown (assert `runs` dict unchanged and no file written).
- **Runs section**
  - Default run creates a new `runs[run_id]` entry with `created_at` and the list of filenames in chronological order.
  - Empty default run (no new memos) does NOT create a run entry.
- **Regeneration**
  - `--regenerate <run_id>` re-renders markdown using current transcriptions; output matches what the original run would produce if transcriptions were unchanged.
  - `--regenerate` with an unknown run ID exits non-zero with an error.
  - `--regenerate` where a referenced filename is missing from `files` skips it with a warning.
- **List runs**
  - `--list-runs` prints one line per run, sorted chronologically, with run ID, created_at, and file count.
- **Markdown rendering**
  - Correct heading format (`## Memo N — HH:MM — LAT, LONG`)
  - Non-matching filename renders with fallback heading (`## Memo N — <filename>`)
  - Coordinates rounded to 3 decimal places
  - Memos sorted chronologically by filename timestamp
  - Empty input → no file written, stderr message
- **Rate limit gating**
  - `should_sleep(n_files)` returns `False` for ≤ `MAX_REQUESTS_PER_MINUTE`, `True` above it
- **Retry/backoff**
  - Pass a fake transcribe function that raises twice then returns; assert the retry helper returns the final value and called the fake 3 times. Catches infinite loops, wrong sleep durations, or swallowed final exceptions.

### Manual Smoke Test

Run once end-to-end with real API before declaring v1 done:

1. Record 2 short memos via the iOS Shortcut, let them sync to Mac
2. Run `uv run transcribe.py`
3. Verify:
   - Output markdown file is created in cwd with correct filename format
   - **Absolute path of the markdown file is printed to stdout** (not stderr, not prefixed with anything) — test with `open "$(uv run transcribe.py)"`
   - Transcriptions are non-empty and plausible
   - `state.json` contains both entries with UTC timestamps
   - Re-running produces no new file and prints "no new memos" to stderr
   - Running with `--all` re-transcribes both, prints "Refreshed 2 transcriptions" to stderr, and does NOT write a markdown file
   - `--list-runs` shows the original run
   - `--regenerate <run_id>` recreates the markdown file with the refreshed transcriptions and prints its path to stdout

### Out of Scope for v1

- Mocked Groq API tests — brittle, low value; the smoke test covers the integration
- iCloud stub download tests — hard to simulate; covered by the smoke test if we record while offline and process later

## V2 Ideas

- Reverse geocoding (lat/long → city/neighborhood) in memo headings
- Smarter output filename convention
- SQLite for state tracking (better corruption/partial write handling than JSON)
- Migrate CLI from `argparse` to `typer` when the surface grows (subcommands, more flags)
- Agent post-processing (see below)

## Future: Agent Post-Processing

Downstream step using Claude Agent SDK to:

- Read the combined transcription markdown
- Classify each thought (daily note, task, research, idea)
- Route to appropriate destination (Obsidian vault, task manager, etc.)

This is out of scope for v1 but informs the choice of Python and project structure.

## Logging

Use Python's stdlib `logging` module for all progress/status/diagnostic output. Keep it minimal for v1:

```python
import logging
import sys

logger = logging.getLogger("voice_memos")
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stderr,
)
```

- **Single logger** named `voice_memos`. No per-submodule loggers at v1 scale.
- **stderr stream** so stdout contract (absolute markdown path) stays clean.
- **Levels used:** `INFO` for progress (`Transcribing ...`, `Refreshed N transcriptions`), `WARNING` for skips (`.qta` files, non-matching filenames), `ERROR` for failures.
- **Third-party libs:** when Groq SDK or (future) Claude Agent SDK emit noisy logs, silence them with `logging.getLogger("groq").setLevel(logging.WARNING)` etc.
- **No `-v` / `-q` flags in v1.** Add later if needed — trivially a level override.
- **Out of scope:** file handlers, JSON/structured logging, custom filters.

## Tech Stack

- Python 3.12+
- `uv` for dependency management
- `groq` Python SDK
- stdlib `logging` (see Logging section)
- `ffmpeg` (manual one-time conversion of legacy `.qta` files only)
- `claude-agent-sdk` (future, for post-processing)

### Dev dependencies

- `pytest` — unit tests
- `ruff` — lint + format (single tool, no separate formatter). Type checker deferred until the codebase grows.

## Project Structure

```
voice-memos/
├── plans/
│   └── 001-v1-implementation.md
├── src/
│   ├── transcribe.py        # Main CLI script (single file for v1)
│   └── state.json           # Processed file tracking (gitignored)
├── tests/
│   └── test_transcribe.py   # Unit tests import from transcribe
├── pyproject.toml
└── .gitignore
```

Source lives under `src/` from the start — v1 is a single file, but the project is expected to grow (Agent SDK post-processing, etc.), and moving files later is churn. Invocation becomes `uv run src/transcribe.py` (or a `[project.scripts]` entry later). Tests import via a path tweak or by configuring `pyproject.toml` (`[tool.pytest.ini_options] pythonpath = ["src"]`), then `from transcribe import parse_filename, ...`. The repo is new — initialize with `git init` and `uv init` as the first milestone.

`state.json` sits next to the script in `src/` rather than at the repo root. It's gitignored — keeping it adjacent to the code that owns it avoids a top-level file that looks like project metadata.

## Milestones

Track progress through these checkpoints. Each milestone should leave the repo in a runnable, committable state.

- [x] **M1 — Project scaffold.** `git init`, `uv init`, `pyproject.toml` with `groq` dep, `pytest` + `ruff` dev deps, and `pythonpath = ["src"]` for pytest. `.gitignore` covers `src/state.json`, `__pycache__`, `.venv`, generated `.md` outputs. Empty `src/transcribe.py` and `tests/test_transcribe.py`. `uv run pytest` runs (zero tests). `uv run ruff check` passes.
- [x] **M2 — Pure functions + unit tests.** Implement `parse_filename`, `should_sleep`, `render_markdown`, `load_state`, `write_state_atomic` with their unit tests. No Groq calls yet. All tests green.
- [ ] **M3 — Transcription + retry.** Groq client wrapper with exponential backoff retry helper. Unit-test the retry helper with a fake callable (two failures → success).
- [ ] **M4 — Main loop (default path).** Wire filename discovery → filter against state → transcribe → per-file atomic state write → markdown render → stdout path print. Unit tests for filtering, runs-entry creation, empty-run behavior.
- [ ] **M5 — Other CLI modes.** `--all`, `--list-runs`, `--regenerate RUN_ID` with their unit tests (including `--all` overwrite semantics and regenerate-with-missing-file warning).
- [ ] **M6 — Logging + error handling polish.** Wire the stdlib logger, silence `groq` noise, verify warnings/errors route correctly. Handle corrupt `state.json` (backup + exit).
- [ ] **M7 — Smoke test (together).** First run against real Groq API with 2 real memos. Verify stdout contract, state shape, re-run behavior, `--all`, `--list-runs`, `--regenerate`. Declare v1 done.
