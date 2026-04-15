# Date-grouped markdown output

## Overview

`render_markdown` assumes every memo in a run shares one date. That held for v1 (walking-memo runs were same-day), but today's run spanned 5 days (2026-04-10 → 2026-04-14). The resulting `vm-*.md` reads:

```
# Voice Memos — 2026-04-10     ← taken from *first* memo; wrong for cross-day runs
## Memo 1 — 20:26 — …
## Memo 2 — 20:28 — …
...
## Memo 17 — 10:33 — …         ← which day?
```

The reader can't tell which memo belongs to which day without cross-referencing filenames in `state.json`.

## Target output shape

```
# Voice Memos — 2026-04-15 13:40         ← run timestamp (local), minute precision

## 2026-04-10

### Memo 1 — 20:26 — 47.715, -122.374
<transcription>

### Memo 2 — 20:28 — 47.715, -122.374
<transcription>

## 2026-04-11

### Memo 4 — 11:16 — …
<transcription>

...

## Unknown date                          ← only present if non-parseable memos exist

### Memo 17 — weird_recording.m4a
<transcription>
```

### Decisions

- **H1 = run timestamp, not memo date.** Format `YYYY-MM-DD HH:MM` (local time, minute precision — seconds are noise in a heading). Derived from the `run_datetime` passed by the caller.
- **H2 = date (`YYYY-MM-DD`)**, one per date group, inserted whenever the date changes in the (already-sorted) memo list.
- **H3 = memo entry** (was H2). Heading format unchanged otherwise: `### Memo N — HH:MM — lat, lng` for parseable, `### Memo N — <filename>` for fallback.
- **Memo numbering is continuous across date groups** (1..N for the whole run). Rationale: a memo can be referenced by number ("Memo 7") without ambiguity, and skimming the file the number reflects recording order. Resetting per date would make "Memo 2" mean different things in the same file.
- **Non-parseable memos cluster under a trailing `## Unknown date` section.** Matches the existing tier-1 sort behavior (non-parseable memos already render after parseable ones via `_file_sort_key`). Header only appears if the section is non-empty. Skip entirely if *all* memos are non-parseable — fall back to a single un-grouped list under the H1 (see edge cases).
- **Single-date runs still get the `## YYYY-MM-DD` H2.** Slightly heavier for the common case but keeps the format uniform. Readers parsing the file can always assume "date = nearest preceding H2".
- **Tight spacing between sections.** One blank line before each H2, no trailing blank line at the file end — matches current `render_markdown` style.

### Edge cases

- **All memos non-parseable, no parseable memos at all.** There's no date to group by. Emit H1 + no H2s + H3 memo entries directly (current flat layout, minus the date in H1). No "Unknown date" header needed since everything is unknown.
- **Single memo, parseable.** H1 + one H2 + one H3. A bit ceremonial but consistent — not worth a special case.
- **Memos at the same timestamp (tied keys).** Already handled by `_file_sort_key`; just preserve input order.

## Implementation steps

1. **`src/transcribe.py` — `render_markdown` signature change**
   - Add required `run_datetime: datetime` parameter. (Required, not optional: the H1 depends on it; a sensible default doesn't exist.)
   - Build H1 as `f"# Voice Memos — {run_datetime.strftime('%Y-%m-%d %H:%M')}"`.
   - Iterate memos, tracking `current_date`. When `memo.meta.date` differs from `current_date`, emit a blank line + `## {date}` + blank line. Non-parseable memos are emitted last under `## Unknown date` (unless *all* memos are non-parseable — then skip the header).
   - Bump memo heading from `##` to `###`.
2. **Call sites**
   - `process_new_memos`: pass `run_local` (already in scope).
   - `regenerate_run`: parse `run_id` back into a `datetime` via `datetime.strptime(run_id, "%Y-%m-%d_%H-%M-%S")`. run_id is local time — no tz conversion needed for display.
3. **Tests (`tests/unit/test_transcribe.py`)**
   - Update `test_render_markdown_preserves_input_order` to expect H3 and pass `run_datetime`.
   - Update `test_render_markdown_fallback_heading` (non-parseable only: expect flat H1 + H3, no H2).
   - Update `test_render_markdown_coord_rounding`.
   - Add `test_render_markdown_multi_day_grouping`: memos across 3 dates → 3 H2 sections, continuous numbering, correct order.
   - Add `test_render_markdown_mixed_parseable_and_fallback`: 2 parseable + 1 non-parseable → 1 date H2 + trailing `## Unknown date`.
   - Extend `tests/e2e/test_smoke.py` to cover the multi-date path:
     - A 5th parseable fixture exists from an earlier date: `memo_2026-04-08 10.15.00_47.71464468712184_-122.373724393098.m4a` (copy of the 17.47.41 fixture; audio identical so `EXPECTED_SUBSTRINGS` still matches). Add it to `PARSEABLE_FIXTURES` and to `ALL_FIXTURES`.
     - Expected rendered structure: H1 with run timestamp, `## 2026-04-08` section containing the 04-08 memo, `## 2026-04-09` section containing the two 04-09 memos, then `## Unknown date` with the two Carkeek memos.
     - Update H1 assertion to match the run-timestamp format (regex on `r"^# Voice Memos — \d{4}-\d{2}-\d{2} \d{2}:\d{2}$"`).
     - Update memo-heading assertions from `##` to `###`.
     - Memo numbering is continuous: Memo 1 = 04-08, Memo 2/3 = 04-09, Memo 4/5 = Carkeek.
4. **Docs**
   - Update `plans/completed/001-v1-implementation.md` §Output? No — leave historical records alone; this plan is the new source of truth.
   - Update `plans/index.md` with this plan's entry.

## Out of scope

- Anchors / deep-linking to specific memos.
- Frontmatter (YAML block at the top) for Obsidian. Separate plan if we want it.
- TOC generation.
- Changing `state.json` structure. The run_id and per-file entries are unaffected; only rendering changes.

## Risk / verification

- Existing `regenerate_run` for *old* runs must still work. Since the run_id format is unchanged and `state['files']` entries are unchanged, re-rendering produces the new layout from the same data — that's the feature, not a regression.
- E2E smoke must be updated in the same commit as the render change, or the e2e target breaks.
