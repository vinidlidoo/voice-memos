import json
from datetime import datetime

import pytest

from transcribe import (
    MAX_FILE_SIZE_BYTES,
    MAX_REQUESTS_PER_MINUTE,
    Memo,
    list_runs,
    load_state,
    main,
    parse_filename,
    process_new_memos,
    refresh_all_transcriptions,
    regenerate_run,
    render_markdown,
    retry_with_backoff,
    should_sleep,
    transcribe_file,
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


# ---------- retry_with_backoff ----------


def test_retry_succeeds_after_two_failures():
    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    result = retry_with_backoff(flaky, sleep=sleeps.append)
    assert result == "ok"
    assert calls["n"] == 3
    # Two sleeps between three attempts: base*1 and base*2.
    assert sleeps == [1.0, 2.0]


def test_retry_reraises_after_max_attempts():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        retry_with_backoff(always_fails, sleep=lambda _: None)
    assert calls["n"] == 3


# ---------- transcribe_file ----------


def test_transcribe_file_rejects_oversized(tmp_path):
    path = tmp_path / "huge.m4a"
    path.write_bytes(b"\x00" * (MAX_FILE_SIZE_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds Groq"):
        transcribe_file(path)


def test_transcribe_file_calls_client_with_model(tmp_path):
    path = tmp_path / "memo.m4a"
    path.write_bytes(b"fake-audio-bytes")

    captured = {}

    class FakeResp:
        text = "hello world"

    class FakeTranscriptions:
        def create(self, *, file, model):
            captured["file"] = file
            captured["model"] = model
            return FakeResp()

    class FakeAudio:
        transcriptions = FakeTranscriptions()

    class FakeClient:
        audio = FakeAudio()

    result = transcribe_file(path, client=FakeClient())
    assert result == "hello world"
    assert captured["model"] == "whisper-large-v3-turbo"
    assert captured["file"] == ("memo.m4a", b"fake-audio-bytes")


# ---------- process_new_memos (main loop) ----------


def _make_env(tmp_path):
    """Set up an isolated memo dir, state path, and output dir under tmp_path."""
    memo_dir = tmp_path / "memos"
    memo_dir.mkdir()
    state_path = tmp_path / "state.json"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    return memo_dir, state_path, out_dir


def _fixed_clock(year=2026, month=4, day=9, hour=17, minute=50):
    fixed = datetime(year, month, day, hour, minute, 0).astimezone()
    return lambda: fixed


def test_process_no_new_memos_returns_none(tmp_path):
    memo_dir, state_path, out_dir = _make_env(tmp_path)

    def boom(_p):
        raise AssertionError("transcriber should not be called")

    result = process_new_memos(
        memo_dir,
        state_path,
        out_dir,
        transcriber=boom,
        clock=_fixed_clock(),
        sleep=lambda _: None,
    )
    assert result is None
    assert list(out_dir.glob("*.md")) == []
    assert load_state(state_path)["runs"] == {}


def test_process_writes_markdown_state_and_run_entry(tmp_path):
    memo_dir, state_path, out_dir = _make_env(tmp_path)
    f1 = "memo_2026-04-09 17.45.00_47.714644_-122.373724.m4a"
    f2 = "memo_2026-04-09 17.46.00_47.714644_-122.373724.m4a"
    (memo_dir / f1).write_bytes(b"a")
    (memo_dir / f2).write_bytes(b"b")

    def fake_transcriber(p):
        return f"text for {p.name}"

    result = process_new_memos(
        memo_dir,
        state_path,
        out_dir,
        transcriber=fake_transcriber,
        clock=_fixed_clock(),
        sleep=lambda _: None,
    )

    assert result is not None
    assert result.name == "2026-04-09_17-50-00.md"
    md = result.read_text()
    assert f"text for {f1}" in md
    assert f"text for {f2}" in md

    state = load_state(state_path)
    assert set(state["files"].keys()) == {f1, f2}
    assert state["files"][f1]["transcribed_at"].endswith("Z")

    run_id = "2026-04-09_17-50-00"
    assert run_id in state["runs"]
    # Files list is in chronological order.
    assert state["runs"][run_id]["files"] == [f1, f2]
    assert state["runs"][run_id]["created_at"].endswith("Z")


def test_process_skips_already_transcribed_files(tmp_path):
    memo_dir, state_path, out_dir = _make_env(tmp_path)
    f1 = "memo_2026-04-09 17.45.00_47.7_-122.3.m4a"
    f2 = "memo_2026-04-09 17.46.00_47.7_-122.3.m4a"
    (memo_dir / f1).write_bytes(b"a")
    (memo_dir / f2).write_bytes(b"b")

    state = load_state(state_path)
    state["files"][f1] = {
        "transcription": "old",
        "transcribed_at": "2026-04-09T17:30:00Z",
    }
    write_state_atomic(state_path, state)

    calls: list[str] = []

    def fake_transcriber(p):
        calls.append(p.name)
        return "new"

    process_new_memos(
        memo_dir,
        state_path,
        out_dir,
        transcriber=fake_transcriber,
        clock=_fixed_clock(),
        sleep=lambda _: None,
    )

    assert calls == [f2]
    state = load_state(state_path)
    assert state["files"][f1]["transcription"] == "old"
    assert state["files"][f2]["transcription"] == "new"
    assert state["runs"]["2026-04-09_17-50-00"]["files"] == [f2]


def test_process_ignores_qta_files(tmp_path):
    memo_dir, state_path, out_dir = _make_env(tmp_path)
    (memo_dir / "memo_2026-04-09 17.45.00_47.7_-122.3.m4a").write_bytes(b"a")
    (memo_dir / "old_recording.qta").write_bytes(b"b")

    calls: list[str] = []

    def fake_transcriber(p):
        calls.append(p.name)
        return "text"

    process_new_memos(
        memo_dir,
        state_path,
        out_dir,
        transcriber=fake_transcriber,
        clock=_fixed_clock(),
        sleep=lambda _: None,
    )
    assert len(calls) == 1
    assert calls[0].endswith(".m4a")


def test_process_all_failures_writes_no_output(tmp_path):
    memo_dir, state_path, out_dir = _make_env(tmp_path)
    (memo_dir / "memo_2026-04-09 17.45.00_47.7_-122.3.m4a").write_bytes(b"a")

    def always_fails(_p):
        raise RuntimeError("network down")

    result = process_new_memos(
        memo_dir,
        state_path,
        out_dir,
        transcriber=always_fails,
        clock=_fixed_clock(),
        sleep=lambda _: None,
    )
    assert result is None
    assert list(out_dir.glob("*.md")) == []
    assert load_state(state_path)["runs"] == {}


# ---------- refresh_all_transcriptions (--all) ----------


def test_refresh_all_overwrites_in_place_and_preserves_missing(tmp_path):
    """Plan §`--all`: overwrite entries in place; entries whose audio file is
    missing from memo_dir are left untouched so a partial crash can't wipe them.
    """
    memo_dir, state_path, _ = _make_env(tmp_path)
    file_a = "memo_2026-04-09 17.45.00_47.7_-122.3.m4a"
    file_b = "memo_2026-04-09 17.46.00_47.7_-122.3.m4a"

    (memo_dir / file_a).write_bytes(b"a")

    state = load_state(state_path)
    state["files"][file_a] = {
        "transcription": "old A",
        "transcribed_at": "2026-04-09T17:30:00Z",
    }
    state["files"][file_b] = {
        "transcription": "old B",
        "transcribed_at": "2026-04-09T17:31:00Z",
    }
    write_state_atomic(state_path, state)

    def fake_transcriber(p):
        return f"new {p.name}"

    n = refresh_all_transcriptions(
        memo_dir,
        state_path,
        transcriber=fake_transcriber,
        clock=_fixed_clock(),
        sleep=lambda _: None,
    )
    assert n == 1

    state = load_state(state_path)
    assert state["files"][file_a]["transcription"] == f"new {file_a}"
    assert state["files"][file_b]["transcription"] == "old B"


def test_refresh_all_does_not_touch_runs_or_write_markdown(tmp_path):
    memo_dir, state_path, out_dir = _make_env(tmp_path)
    file_a = "memo_2026-04-09 17.45.00_47.7_-122.3.m4a"
    (memo_dir / file_a).write_bytes(b"a")

    state = load_state(state_path)
    state["files"][file_a] = {
        "transcription": "old",
        "transcribed_at": "2026-04-09T17:30:00Z",
    }
    state["runs"]["2026-04-09_17-30-00"] = {
        "created_at": "2026-04-09T17:30:00Z",
        "files": [file_a],
    }
    write_state_atomic(state_path, state)

    refresh_all_transcriptions(
        memo_dir,
        state_path,
        transcriber=lambda _p: "new",
        clock=_fixed_clock(),
        sleep=lambda _: None,
    )

    state = load_state(state_path)
    assert list(state["runs"].keys()) == ["2026-04-09_17-30-00"]
    assert list(out_dir.glob("*.md")) == []


def test_refresh_all_retranscribes_every_entry_when_all_present(tmp_path):
    """Plan §Test Plan: `--all` mode selects everything.

    Complements the overwrite-in-place test (which exercises one-present,
    one-missing) by verifying that when all referenced audio files exist
    on disk, every state entry gets retranscribed.
    """
    memo_dir, state_path, _ = _make_env(tmp_path)
    names = [
        "memo_2026-04-09 17.45.00_47.7_-122.3.m4a",
        "memo_2026-04-09 17.46.00_47.7_-122.3.m4a",
        "memo_2026-04-09 17.47.00_47.7_-122.3.m4a",
    ]
    for name in names:
        (memo_dir / name).write_bytes(b"x")

    state = load_state(state_path)
    for name in names:
        state["files"][name] = {
            "transcription": "old",
            "transcribed_at": "2026-04-09T17:30:00Z",
        }
    write_state_atomic(state_path, state)

    calls: list[str] = []

    def fake_transcriber(p):
        calls.append(p.name)
        return f"new {p.name}"

    n = refresh_all_transcriptions(
        memo_dir,
        state_path,
        transcriber=fake_transcriber,
        clock=_fixed_clock(),
        sleep=lambda _: None,
    )
    assert n == len(names)
    assert sorted(calls) == sorted(names)
    state = load_state(state_path)
    for name in names:
        assert state["files"][name]["transcription"] == f"new {name}"


# ---------- list_runs ----------


def test_list_runs_sorted_oldest_first(tmp_path):
    _, state_path, _ = _make_env(tmp_path)
    state = load_state(state_path)
    state["runs"]["2026-04-09_17-30-00"] = {
        "created_at": "2026-04-09T17:30:00Z",
        "files": ["a.m4a", "b.m4a"],
    }
    state["runs"]["2026-04-08_09-00-00"] = {
        "created_at": "2026-04-08T09:00:00Z",
        "files": ["c.m4a"],
    }
    write_state_atomic(state_path, state)

    entries = list_runs(state_path)
    assert entries == [
        ("2026-04-08_09-00-00", 1),
        ("2026-04-09_17-30-00", 2),
    ]


def test_list_runs_empty_state(tmp_path):
    _, state_path, _ = _make_env(tmp_path)
    assert list_runs(state_path) == []


def test_list_runs_malformed_entry_raises_runtime_error(tmp_path):
    """A hand-edited run entry missing `created_at` must surface as a
    `RuntimeError` (so `main()` maps it to exit 2), not an uncaught
    `KeyError` that crashes with a raw traceback.
    """
    _, state_path, _ = _make_env(tmp_path)
    state = load_state(state_path)
    state["runs"]["bad-entry"] = {"files": ["a.m4a"]}  # missing created_at
    write_state_atomic(state_path, state)

    with pytest.raises(RuntimeError, match="malformed run entry"):
        list_runs(state_path)


# ---------- regenerate_run ----------


def test_regenerate_run_uses_current_transcriptions(tmp_path):
    """Regeneration pulls fresh text from state['files'], not the original."""
    _, state_path, out_dir = _make_env(tmp_path)
    file_a = "memo_2026-04-09 17.45.00_47.7_-122.3.m4a"

    state = load_state(state_path)
    state["files"][file_a] = {
        "transcription": "REFRESHED TEXT",
        "transcribed_at": "2026-04-09T17:30:00Z",
    }
    state["runs"]["2026-04-09_17-30-00"] = {
        "created_at": "2026-04-09T17:30:00Z",
        "files": [file_a],
    }
    write_state_atomic(state_path, state)

    path = regenerate_run("2026-04-09_17-30-00", state_path, out_dir)
    assert path.name == "2026-04-09_17-30-00.md"
    assert "REFRESHED TEXT" in path.read_text()


def test_regenerate_run_unknown_id_raises(tmp_path):
    _, state_path, out_dir = _make_env(tmp_path)
    with pytest.raises(KeyError, match="Unknown run ID"):
        regenerate_run("does-not-exist", state_path, out_dir)


def test_regenerate_run_skips_missing_filenames_with_warning(tmp_path, caplog):
    _, state_path, out_dir = _make_env(tmp_path)
    file_a = "memo_2026-04-09 17.45.00_47.7_-122.3.m4a"
    file_b = "memo_2026-04-09 17.46.00_47.7_-122.3.m4a"

    state = load_state(state_path)
    state["files"][file_a] = {
        "transcription": "A text",
        "transcribed_at": "2026-04-09T17:30:00Z",
    }
    state["runs"]["2026-04-09_17-30-00"] = {
        "created_at": "2026-04-09T17:30:00Z",
        "files": [file_a, file_b],
    }
    write_state_atomic(state_path, state)

    with caplog.at_level("WARNING", logger="voice_memos"):
        path = regenerate_run("2026-04-09_17-30-00", state_path, out_dir)

    md = path.read_text()
    assert "A text" in md
    assert file_b not in md
    assert any("no transcription" in r.message for r in caplog.records)


# ---------- main() / error handling ----------


def test_main_corrupt_state_exits_nonzero_and_backs_up(tmp_path, caplog):
    memo_dir, state_path, out_dir = _make_env(tmp_path)
    state_path.write_text("{ not valid json")

    with caplog.at_level("ERROR", logger="voice_memos"):
        rc = main(
            argv=["--list-runs"],
            memo_dir=memo_dir,
            state_path=state_path,
            output_dir=out_dir,
        )
    assert rc == 2
    backups = list(state_path.parent.glob("state.json.corrupt-*"))
    assert len(backups) == 1
    assert any("corrupt" in r.message for r in caplog.records)


def test_main_list_runs_empty_state_is_ok(tmp_path, capsys):
    memo_dir, state_path, out_dir = _make_env(tmp_path)
    rc = main(
        argv=["--list-runs"],
        memo_dir=memo_dir,
        state_path=state_path,
        output_dir=out_dir,
    )
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_main_regenerate_unknown_run_id_exits_1(tmp_path, caplog):
    memo_dir, state_path, out_dir = _make_env(tmp_path)
    with caplog.at_level("ERROR", logger="voice_memos"):
        rc = main(
            argv=["--regenerate", "nope"],
            memo_dir=memo_dir,
            state_path=state_path,
            output_dir=out_dir,
        )
    assert rc == 1
    assert any("Unknown run ID" in r.message for r in caplog.records)


def test_main_silences_groq_logger():
    """M6 decision: noisy third-party libs are capped at WARNING."""
    import logging as _logging

    from transcribe import _configure_logging

    _configure_logging()
    for name in ("groq", "httpx", "httpcore"):
        assert _logging.getLogger(name).level == _logging.WARNING
