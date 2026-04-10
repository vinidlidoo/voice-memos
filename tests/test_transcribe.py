import json
from datetime import datetime

import pytest

from transcribe import (
    MAX_REQUESTS_PER_MINUTE,
    Memo,
    load_state,
    parse_filename,
    render_markdown,
    should_sleep,
    write_state_atomic,
)

# ---------- parse_filename ----------


def test_parse_filename_valid():
    meta = parse_filename(
        "memo_2026-04-09 17.47.41_47.71464468712184_-122.373724393098.m4a"
    )
    assert meta is not None
    assert meta.date == "2026-04-09"
    assert meta.hhmm == "17:47"
    assert meta.timestamp == datetime(2026, 4, 9, 17, 47, 41)
    assert meta.lat == pytest.approx(47.71464468712184)
    assert meta.lng == pytest.approx(-122.373724393098)


def test_parse_filename_negative_longitude():
    meta = parse_filename("memo_2026-04-09 17.47.41_47.1_-122.5.m4a")
    assert meta is not None
    assert meta.lng == -122.5


def test_parse_filename_non_matching():
    assert parse_filename("random.m4a") is None
    # Underscore instead of space between date and time.
    assert parse_filename("memo_2026-04-09_17.47.41_47.1_-122.5.m4a") is None
    # Wrong extension.
    assert parse_filename("memo_2026-04-09 17.47.41_47.1_-122.5.qta") is None


# ---------- should_sleep ----------


def test_should_sleep_at_or_below_threshold():
    assert should_sleep(1) is False
    assert should_sleep(MAX_REQUESTS_PER_MINUTE) is False


def test_should_sleep_above_threshold():
    assert should_sleep(MAX_REQUESTS_PER_MINUTE + 1) is True


# ---------- render_markdown ----------


def _memo(filename: str, transcription: str) -> Memo:
    return Memo(
        filename=filename, transcription=transcription, meta=parse_filename(filename)
    )


def test_render_markdown_sorts_chronologically():
    memos = [
        _memo("memo_2026-04-09 17.46.00_47.714644_-122.373724.m4a", "Second memo."),
        _memo("memo_2026-04-09 17.45.00_47.714644_-122.373724.m4a", "First memo."),
    ]
    md = render_markdown(memos)
    assert md.startswith("# Voice Memos — 2026-04-09\n")
    assert md.index("First memo.") < md.index("Second memo.")
    assert "## Memo 1 — 17:45 — 47.715, -122.374" in md
    assert "## Memo 2 — 17:46 — 47.715, -122.374" in md


def test_render_markdown_fallback_heading():
    memo = Memo(filename="weird_recording.m4a", transcription="Hello.", meta=None)
    md = render_markdown([memo])
    assert "## Memo 1 — weird_recording.m4a" in md
    assert md.startswith("# Voice Memos\n")


def test_render_markdown_empty_raises():
    with pytest.raises(ValueError):
        render_markdown([])


def test_render_markdown_coord_rounding():
    memo = _memo("memo_2026-04-09 17.45.00_47.7149999_-122.3738.m4a", "x")
    md = render_markdown([memo])
    assert "47.715, -122.374" in md


# ---------- state management ----------


def test_load_state_missing(tmp_path):
    state = load_state(tmp_path / "state.json")
    assert state == {"version": 1, "runs": {}, "files": {}}


def test_load_state_valid(tmp_path):
    path = tmp_path / "state.json"
    data = {
        "version": 1,
        "runs": {},
        "files": {
            "a.m4a": {
                "transcription": "hi",
                "transcribed_at": "2026-04-09T17:30:00Z",
            }
        },
    }
    path.write_text(json.dumps(data))
    assert load_state(path) == data


def test_load_state_corrupt_backs_up_and_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json")
    with pytest.raises(RuntimeError, match="corrupt"):
        load_state(path)
    backups = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(backups) == 1


def test_write_state_atomic_no_tmp_lingers(tmp_path):
    path = tmp_path / "state.json"
    state = {"version": 1, "runs": {}, "files": {}}
    write_state_atomic(path, state)
    assert path.exists()
    assert json.loads(path.read_text()) == state
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_state_atomic_per_file_persistence(tmp_path):
    path = tmp_path / "state.json"
    state = load_state(path)
    state["files"]["a.m4a"] = {
        "transcription": "first",
        "transcribed_at": "2026-04-09T17:30:00Z",
    }
    write_state_atomic(path, state)
    assert load_state(path)["files"]["a.m4a"]["transcription"] == "first"

    state["files"]["b.m4a"] = {
        "transcription": "second",
        "transcribed_at": "2026-04-09T17:31:00Z",
    }
    write_state_atomic(path, state)
    reread = load_state(path)
    assert set(reread["files"].keys()) == {"a.m4a", "b.m4a"}
