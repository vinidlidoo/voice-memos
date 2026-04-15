# Output path: Obsidian default + `-o` override

## Overview

Today `process_new_memos` and `regenerate_run` write the run markdown to `Path.cwd()` (see `main()` in `src/transcribe.py:462`), using `<run_id>.md` as the filename (e.g. `2026-04-15_09-30-12.md`). That makes the output location implicit and collides visually with Obsidian daily notes (`YYYY-MM-DD.md`) once files land in the vault.

This plan:

1. Changes the default output directory to the Obsidian vault's `Voice/` folder.
2. Adds a `-o / --output-dir PATH` flag that overrides the default for a single invocation.
3. Prefixes the output filename with `vm-` so voice-memo runs never visually collide with Obsidian daily notes. Final form: `vm-YYYY-MM-DD_HH-MM-SS.md`.

Scope is intentionally narrow — no config file, no env-var support, no per-run metadata changes.

## Decisions

- **Default path:** `/Users/vincent/Obsidian Vault/Voice/` — the vault folder already exists (confirmed) and matches TODO.md item 3's later reference to "Obsidian's Vault Voice/".
- **Constant form:** Expose as `DEFAULT_OUTPUT_DIR` module-level constant alongside `DEFAULT_MEMO_DIR` and `DEFAULT_STATE_PATH`. Hard-coded absolute path in v1 — consistent with how `DEFAULT_MEMO_DIR` hard-codes the iCloud path. If a second user ever needs this tool, we revisit.
- **Flag name:** `-o / --output-dir` (short + long). Takes a path; argparse converts to `Path`.
- **Precedence:** CLI `-o` > `DEFAULT_OUTPUT_DIR`. No env var in this iteration.
- **Applies to both write paths:** default run *and* `--regenerate RUN_ID`. `--all` writes no markdown, so it's unaffected.
- **Filename prefix:** `vm-` (voice memo). Applied at the single call site where the output filename is built — `(output_dir / f"vm-{run_id}.md")` in both `process_new_memos` and `regenerate_run`. The `run_id` itself (stored in `state.json`'s `runs` key) stays unprefixed so existing state files keep working and `--regenerate <run_id>` still takes the bare timestamp.
- **Directory creation:** If the target directory doesn't exist, create it (`mkdir(parents=True, exist_ok=True)`) before writing. Matches existing `write_state_atomic` behavior.
- **`main()` signature:** Keep the `output_dir: Path | None = None` test hook. Resolution order inside `main()` becomes: explicit kwarg > `args.output_dir` > `DEFAULT_OUTPUT_DIR`. Tests keep passing a `tmp_path`; the new default only kicks in when nothing is passed and no flag is set.

## Implementation steps

1. **`src/transcribe.py`**
   - Add `DEFAULT_OUTPUT_DIR = Path.home() / "Obsidian Vault" / "Voice"` near the other `DEFAULT_*` constants.
   - Add `-o / --output-dir` argument to `_build_parser()` with `type=Path`, `default=None`, help text.
   - In `main()`, replace `if output_dir is None: output_dir = Path.cwd()` with the precedence chain above; also `output_dir.mkdir(parents=True, exist_ok=True)` before dispatching to a write path.
   - Change the two `f"{run_id}.md"` call sites (`process_new_memos`, `regenerate_run`) to `f"vm-{run_id}.md"`.
2. **Tests (`tests/unit/test_transcribe.py`)**
   - Add a test that `main()` with no `output_dir` kwarg and no `-o` writes to `DEFAULT_OUTPUT_DIR` — use monkeypatch to point `DEFAULT_OUTPUT_DIR` at a tmp_path so the test doesn't touch the real vault.
   - Add a test that `-o /some/tmp/path` wins over the default.
   - Update existing tests that assert on the output filename to expect the `vm-` prefix.
   - Confirm other existing tests (which pass `output_dir=tmp_path` as a kwarg) still pass unchanged.
3. **Docs**
   - Update `CLAUDE.md` "Commands" line to show the new flag.
   - Update `plans/active/001-v1-implementation.md` §Output to note the new default + flag (one-line reference back here; don't duplicate).
   - Update `plans/index.md` with this plan's entry.
   - Tick both TODO.md items once shipped.

## Open questions

- Should the default be `~/Obsidian Vault/Voice/` or a dated subfolder like `Voice/YYYY/`? v1 keeps it flat — filenames are already `YYYY-MM-DD_HH-MM-SS.md`, so Obsidian's file explorer handles ordering fine.
- If the Obsidian vault moves (different Mac, different user), `DEFAULT_OUTPUT_DIR` is wrong. Acceptable for a single-user v1; revisit when/if multi-machine support lands.

## Out of scope

- Env-var configuration (`VOICE_MEMOS_OUTPUT_DIR`).
- Config file.
- TODO.md item 3 ("trigger daily-note creation in Obsidian's Vault Voice/") — that's a separate workflow that consumes the output, not produces it.
