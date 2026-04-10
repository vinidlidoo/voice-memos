"""End-to-end smoke test against the real Groq API.

Runs the full transcribe.py default path against committed audio fixtures
under `tests/fixtures/smoke/`, using a throwaway state.json and output dir
under $TMPDIR. Gated on `GROQ_API_KEY`.

Exits 0 on success, 1 on any assertion failure. Not a pytest test (filename
is `smoke.py`, not `test_*.py`) so `uv run pytest` does not pick it up.

Usage:
    uv run tests/smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from transcribe import load_state, main  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "smoke"

# The two committed fixture files were recorded at 17:47:13 and 17:47:41 on
# 2026-04-09. Sorted alphabetically equals sorted chronologically here, which
# simplifies the ordering assertion below.
EXPECTED_FILES = sorted(
    [
        "memo_2026-04-09 17.47.13_47.71463839736928_-122.373747134786.m4a",
        "memo_2026-04-09 17.47.41_47.71464468712184_-122.373724393098.m4a",
    ]
)

# Substrings known to be stable in Whisper output for these fixtures. One per
# memo — enough to prove both transcriptions made it into the markdown without
# being brittle to the model's usual ±1% variance.
EXPECTED_SUBSTRINGS = ["working", "exciting"]


def fail(msg: str) -> None:
    print(f"SMOKE FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def run() -> None:
    if not os.environ.get("GROQ_API_KEY"):
        fail("GROQ_API_KEY is not set")

    if not FIXTURE_DIR.is_dir():
        fail(f"fixture dir missing: {FIXTURE_DIR}")
    present = {p.name for p in FIXTURE_DIR.glob("*.m4a")}
    missing = set(EXPECTED_FILES) - present
    if missing:
        fail(f"missing fixture files: {sorted(missing)}")

    with tempfile.TemporaryDirectory(prefix="voice-memos-smoke-") as tmp:
        tmp_path = Path(tmp)
        state_path = tmp_path / "state.json"
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        rc = main(
            argv=[],
            memo_dir=FIXTURE_DIR,
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

        if "## Memo 1 —" not in md_text or "## Memo 2 —" not in md_text:
            fail("expected two memo headings in markdown")

        lowered = md_text.lower()
        for substr in EXPECTED_SUBSTRINGS:
            if substr not in lowered:
                fail(f"expected substring {substr!r} not found in transcription")

        state = load_state(state_path)
        file_keys = sorted(state["files"].keys())
        if file_keys != EXPECTED_FILES:
            fail(f"state['files'] keys mismatch: {file_keys}")

        for name, entry in state["files"].items():
            if not entry.get("transcription", "").strip():
                fail(f"empty transcription for {name}")
            if not entry.get("transcribed_at", "").endswith("Z"):
                fail(f"transcribed_at not UTC for {name}: {entry.get('transcribed_at')!r}")

        if len(state["runs"]) != 1:
            fail(f"expected exactly 1 run entry, got {len(state['runs'])}")
        run_data = next(iter(state["runs"].values()))
        if list(run_data["files"]) != EXPECTED_FILES:
            fail(f"run files not chronological: {run_data['files']}")
        if not run_data["created_at"].endswith("Z"):
            fail(f"run created_at not UTC: {run_data['created_at']!r}")

    print("SMOKE PASS: 2 memos transcribed, markdown rendered, state shape OK")


if __name__ == "__main__":
    run()
