"""End-to-end smoke test against the real Groq API.

Runs the full transcribe.py default path against committed audio fixtures
under `tests/e2e/fixtures/smoke/`, using a throwaway memo_dir, state.json,
and output dir under $TMPDIR. Gated on `GROQ_API_KEY`.

Covers both filename paths:
- Two **parseable** fixtures (`memo_YYYY-MM-DD HH.MM.SS_LAT_LONG.m4a`)
  exercise the structured-heading path.
- Two **non-parseable** fixtures (`Carkeek Park 10/11.m4a`) exercise the
  fallback-heading path and the mtime-based sort for tier 1.

Fixtures are copied into the temp memo_dir so `os.utime` can pin
deterministic mtimes on the non-parseable ones without mutating the
committed files.

Marked `e2e` so the default `pytest` run skips it. Opt in with:
    uv run pytest -m e2e
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from transcribe import load_state, main

HERE = Path(__file__).resolve().parent
FIXTURE_DIR = HERE / "fixtures" / "smoke"

PARSEABLE_FIXTURES = [
    "memo_2026-04-09 17.47.13_47.71463839736928_-122.373747134786.m4a",
    "memo_2026-04-09 17.47.41_47.71464468712184_-122.373724393098.m4a",
]

# Ordered by intended "recording order" — the smoke pins mtimes in this
# order so the mtime-based sort for tier 1 non-parseable memos produces
# this sequence in the rendered markdown.
NON_PARSEABLE_FIXTURES_IN_MTIME_ORDER = [
    "Carkeek Park 11.m4a",  # earlier mtime → renders first within tier 1
    "Carkeek Park 10.m4a",  # later  mtime → renders second within tier 1
]

ALL_FIXTURES = PARSEABLE_FIXTURES + NON_PARSEABLE_FIXTURES_IN_MTIME_ORDER

# Substrings known to be stable in Whisper output for these fixtures. Keyed
# off distinctive words rather than exact transcriptions to survive the
# usual ±1% model variance.
EXPECTED_SUBSTRINGS = ["working", "exciting", "technical", "memos"]


@pytest.mark.e2e
def test_smoke(tmp_path):
    if not os.environ.get("GROQ_API_KEY"):
        pytest.skip("GROQ_API_KEY is not set")

    assert FIXTURE_DIR.is_dir(), f"fixture dir missing: {FIXTURE_DIR}"
    present = {p.name for p in FIXTURE_DIR.glob("*.m4a")}
    missing = set(ALL_FIXTURES) - present
    assert not missing, f"missing fixture files: {sorted(missing)}"

    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    state_path = tmp_path / "state.json"
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    # Copy fixtures into the temp memo_dir so we can pin mtimes without
    # mutating committed files.
    for name in ALL_FIXTURES:
        shutil.copy2(FIXTURE_DIR / name, memo_dir / name)

    # Pin deterministic mtimes on the non-parseable fixtures so the
    # tier-1 mtime sort produces a stable order.
    base = 1_700_000_000
    for i, name in enumerate(NON_PARSEABLE_FIXTURES_IN_MTIME_ORDER):
        t = base + i
        os.utime(memo_dir / name, (t, t))

    rc = main(
        argv=[],
        memo_dir=memo_dir,
        state_path=state_path,
        output_dir=output_dir,
    )
    assert rc == 0, f"main() returned non-zero exit code {rc}"

    md_files = list(output_dir.glob("*.md"))
    assert len(md_files) == 1, f"expected exactly 1 markdown file, got {md_files}"
    assert md_files[0].name.startswith("vm-"), f"expected vm- prefix: {md_files[0].name}"
    md_text = md_files[0].read_text()

    assert md_text.startswith("# Voice Memos — 2026-04-09"), (
        f"unexpected H1: {md_text.splitlines()[0]!r}"
    )

    for n in (1, 2, 3, 4):
        assert f"## Memo {n} —" in md_text, f"expected `## Memo {n} —` heading"

    assert "## Memo 1 — 17:47" in md_text, "expected Memo 1 to use the structured HH:MM heading"
    assert "## Memo 2 — 17:47" in md_text, "expected Memo 2 to use the structured HH:MM heading"

    assert "## Memo 3 — Carkeek Park 11.m4a" in md_text, (
        "expected Carkeek Park 11 at Memo 3 (earliest mtime in tier 1)"
    )
    assert "## Memo 4 — Carkeek Park 10.m4a" in md_text, (
        "expected Carkeek Park 10 at Memo 4 (latest mtime in tier 1)"
    )

    # Whitespace-strip invariant: no line should start with a space.
    # Headings start with "#", empty lines are "", transcription lines
    # should be left-stripped by `transcribe_file`.
    for lineno, line in enumerate(md_text.splitlines(), start=1):
        assert not line.startswith(" "), f"line {lineno} starts with a space: {line!r}"

    lowered = md_text.lower()
    for substr in EXPECTED_SUBSTRINGS:
        assert substr in lowered, f"expected substring {substr!r} not found in transcription"

    state = load_state(state_path)
    assert set(state["files"].keys()) == set(ALL_FIXTURES), (
        f"state['files'] keys mismatch: {sorted(state['files'].keys())}"
    )

    for name, entry in state["files"].items():
        text = entry.get("transcription", "")
        assert text.strip(), f"empty transcription for {name}"
        assert text == text.strip(), f"stored transcription for {name} is not stripped: {text!r}"
        assert entry.get("transcribed_at", "").endswith("Z"), (
            f"transcribed_at not UTC for {name}: {entry.get('transcribed_at')!r}"
        )

    assert len(state["runs"]) == 1, f"expected exactly 1 run entry, got {len(state['runs'])}"
    run_data = next(iter(state["runs"].values()))
    expected_run_files = sorted(PARSEABLE_FIXTURES) + NON_PARSEABLE_FIXTURES_IN_MTIME_ORDER
    assert list(run_data["files"]) == expected_run_files, (
        f"run files not in expected order: {run_data['files']}"
    )
    assert run_data["created_at"].endswith("Z"), (
        f"run created_at not UTC: {run_data['created_at']!r}"
    )
