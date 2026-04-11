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

Exits 0 on success, 1 on any assertion failure. Not a pytest test (filename
is `smoke.py`, not `test_*.py`) so `uv run pytest` does not pick it up.

Usage:
    uv run tests/e2e/smoke.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from transcribe import load_state, main  # noqa: E402

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


def fail(msg: str) -> None:
    print(f"SMOKE FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def run() -> None:
    if not os.environ.get("GROQ_API_KEY"):
        fail("GROQ_API_KEY is not set")

    if not FIXTURE_DIR.is_dir():
        fail(f"fixture dir missing: {FIXTURE_DIR}")
    present = {p.name for p in FIXTURE_DIR.glob("*.m4a")}
    missing = set(ALL_FIXTURES) - present
    if missing:
        fail(f"missing fixture files: {sorted(missing)}")

    with tempfile.TemporaryDirectory(prefix="voice-memos-smoke-") as tmp:
        tmp_path = Path(tmp)
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
        if rc != 0:
            fail(f"main() returned non-zero exit code {rc}")

        md_files = list(output_dir.glob("*.md"))
        if len(md_files) != 1:
            fail(f"expected exactly 1 markdown file, got {len(md_files)}: {md_files}")
        md_text = md_files[0].read_text()

        if not md_text.startswith("# Voice Memos — 2026-04-09"):
            fail(f"unexpected H1: {md_text.splitlines()[0]!r}")

        for n in (1, 2, 3, 4):
            if f"## Memo {n} —" not in md_text:
                fail(f"expected `## Memo {n} —` heading")

        if "## Memo 1 — 17:47" not in md_text:
            fail("expected Memo 1 to use the structured HH:MM heading")
        if "## Memo 2 — 17:47" not in md_text:
            fail("expected Memo 2 to use the structured HH:MM heading")

        if "## Memo 3 — Carkeek Park 11.m4a" not in md_text:
            fail("expected Carkeek Park 11 at Memo 3 (earliest mtime in tier 1)")
        if "## Memo 4 — Carkeek Park 10.m4a" not in md_text:
            fail("expected Carkeek Park 10 at Memo 4 (latest mtime in tier 1)")

        # Whitespace-strip invariant: no line should start with a space.
        # Headings start with "#", empty lines are "", transcription lines
        # should be left-stripped by `transcribe_file`.
        for lineno, line in enumerate(md_text.splitlines(), start=1):
            if line.startswith(" "):
                fail(f"line {lineno} starts with a space: {line!r}")

        lowered = md_text.lower()
        for substr in EXPECTED_SUBSTRINGS:
            if substr not in lowered:
                fail(f"expected substring {substr!r} not found in transcription")

        state = load_state(state_path)
        if set(state["files"].keys()) != set(ALL_FIXTURES):
            fail(f"state['files'] keys mismatch: {sorted(state['files'].keys())}")

        for name, entry in state["files"].items():
            text = entry.get("transcription", "")
            if not text.strip():
                fail(f"empty transcription for {name}")
            if text != text.strip():
                fail(f"stored transcription for {name} is not stripped: {text!r}")
            if not entry.get("transcribed_at", "").endswith("Z"):
                fail(f"transcribed_at not UTC for {name}: {entry.get('transcribed_at')!r}")

        if len(state["runs"]) != 1:
            fail(f"expected exactly 1 run entry, got {len(state['runs'])}")
        run_data = next(iter(state["runs"].values()))
        expected_run_files = sorted(PARSEABLE_FIXTURES) + NON_PARSEABLE_FIXTURES_IN_MTIME_ORDER
        if list(run_data["files"]) != expected_run_files:
            fail(f"run files not in expected order: {run_data['files']}")
        if not run_data["created_at"].endswith("Z"):
            fail(f"run created_at not UTC: {run_data['created_at']!r}")

    print("SMOKE PASS: 4 memos transcribed (2 parseable + 2 fallback), markdown + state OK")


if __name__ == "__main__":
    run()
