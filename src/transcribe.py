from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("voice_memos")

# Rate limiting — tuned to Groq free tier. See plan §Transcription.
MAX_REQUESTS_PER_MINUTE = 20
SLEEP_BETWEEN_REQUESTS_SEC = 3.5

# Groq transcription settings.
GROQ_MODEL = "whisper-large-v3-turbo"
MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024

# Retry policy for Groq API calls.
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SEC = 1.0

STATE_VERSION = 1

FILENAME_RE = re.compile(
    r"^memo_(\d{4}-\d{2}-\d{2}) (\d{2})\.(\d{2})\.(\d{2})_(-?\d+\.\d+)_(-?\d+\.\d+)\.m4a$"
)


@dataclass(frozen=True)
class MemoMeta:
    date: str
    hhmm: str
    timestamp: datetime
    lat: float
    lng: float


@dataclass(frozen=True)
class Memo:
    filename: str
    transcription: str
    meta: MemoMeta | None


def parse_filename(name: str) -> MemoMeta | None:
    m = FILENAME_RE.match(name)
    if not m:
        return None
    date, hh, mm, ss, lat, lng = m.groups()
    timestamp = datetime.strptime(f"{date} {hh}:{mm}:{ss}", "%Y-%m-%d %H:%M:%S")
    return MemoMeta(
        date=date,
        hhmm=f"{hh}:{mm}",
        timestamp=timestamp,
        lat=float(lat),
        lng=float(lng),
    )


def should_sleep(n_files: int) -> bool:
    return n_files > MAX_REQUESTS_PER_MINUTE


def _memo_sort_key(memo: Memo) -> tuple:
    if memo.meta is not None:
        return (0, memo.meta.timestamp)
    return (1, memo.filename)


def render_markdown(memos: list[Memo]) -> str:
    if not memos:
        raise ValueError("render_markdown requires at least one memo")
    ordered = sorted(memos, key=_memo_sort_key)
    header_date = next((m.meta.date for m in ordered if m.meta is not None), None)
    header = f"# Voice Memos — {header_date}" if header_date else "# Voice Memos"
    lines = [header, ""]
    for i, memo in enumerate(ordered, start=1):
        if memo.meta is not None:
            lat = round(memo.meta.lat, 3)
            lng = round(memo.meta.lng, 3)
            heading = f"## Memo {i} — {memo.meta.hhmm} — {lat}, {lng}"
        else:
            heading = f"## Memo {i} — {memo.filename}"
        lines.append(heading)
        lines.append(memo.transcription)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _empty_state() -> dict:
    return {"version": STATE_VERSION, "runs": {}, "files": {}}


def load_state(path: Path) -> dict:
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        backup = path.with_name(
            f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.copy2(path, backup)
        raise RuntimeError(
            f"state.json is corrupt; backed up to {backup}. Refusing to continue."
        ) from exc
    data.setdefault("version", STATE_VERSION)
    data.setdefault("files", {})
    data.setdefault("runs", {})
    return data


def write_state_atomic(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def retry_with_backoff[T](
    func: Callable[[], T],
    *,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_SEC,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `func()` with exponential backoff (base, base*2, base*4, ...).

    Retries up to `max_attempts - 1` times on any Exception, then re-raises.
    `sleep` is injectable so tests don't have to wait.
    """
    for attempt in range(max_attempts):
        try:
            return func()
        except Exception:
            if attempt == max_attempts - 1:
                raise
            sleep(base_delay * (2**attempt))
    # Unreachable: the loop above either returns or raises.
    raise RuntimeError("retry_with_backoff exhausted without returning or raising")


def _default_groq_client():
    from groq import Groq

    return Groq()


def transcribe_file(path: Path, *, client=None) -> str:
    """Transcribe a single audio file via Groq. Raises ValueError if too large."""
    size = path.stat().st_size
    if size > MAX_FILE_SIZE_BYTES:
        raise ValueError(
            f"{path.name} is {size} bytes, exceeds Groq's "
            f"{MAX_FILE_SIZE_BYTES} byte limit. "
            "Chunk with ffmpeg before retrying."
        )
    if client is None:
        client = _default_groq_client()
    with path.open("rb") as f:
        resp = client.audio.transcriptions.create(
            file=(path.name, f.read()),
            model=GROQ_MODEL,
        )
    return resp.text


def discover_audio_files(memo_dir: Path) -> list[Path]:
    """Return .m4a files in memo_dir. Warns about .qta files (skipped)."""
    m4a_files = sorted(memo_dir.glob("*.m4a"))
    qta_files = list(memo_dir.glob("*.qta"))
    if qta_files:
        logger.warning(
            "Skipping %d .qta file(s) — convert with: ffmpeg -i input.qta output.m4a",
            len(qta_files),
        )
    return m4a_files


def _file_sort_key(path: Path) -> tuple:
    meta = parse_filename(path.name)
    if meta is not None:
        return (0, meta.timestamp)
    return (1, path.name)


def _format_run_id(dt_local: datetime) -> str:
    return dt_local.strftime("%Y-%m-%d_%H-%M-%S")


def _format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_clock() -> datetime:
    return datetime.now().astimezone()


def process_new_memos(
    memo_dir: Path,
    state_path: Path,
    output_dir: Path,
    *,
    transcriber: Callable[[Path], str] = transcribe_file,
    clock: Callable[[], datetime] = _default_clock,
    sleep: Callable[[float], None] = time.sleep,
) -> Path | None:
    """Discover new memos, transcribe, persist state, and write a markdown file.

    Returns the absolute path of the written markdown file, or None if nothing
    was written (no new memos, or all transcriptions failed).
    """
    state = load_state(state_path)
    all_files = discover_audio_files(memo_dir)
    new_files = sorted(
        (f for f in all_files if f.name not in state["files"]),
        key=_file_sort_key,
    )

    if not new_files:
        logger.info("No new memos to process.")
        return None

    rate_limited = should_sleep(len(new_files))
    memos: list[Memo] = []

    for i, audio_path in enumerate(new_files):
        logger.info("Transcribing %s", audio_path.name)
        try:
            text = retry_with_backoff(
                lambda p=audio_path: transcriber(p),
                sleep=sleep,
            )
        except Exception as exc:
            logger.error("Skipping %s: %s", audio_path.name, exc)
            continue

        state["files"][audio_path.name] = {
            "transcription": text,
            "transcribed_at": _format_iso_utc(clock()),
        }
        write_state_atomic(state_path, state)

        memos.append(
            Memo(
                filename=audio_path.name,
                transcription=text,
                meta=parse_filename(audio_path.name),
            )
        )

        if rate_limited and i < len(new_files) - 1:
            sleep(SLEEP_BETWEEN_REQUESTS_SEC)

    if not memos:
        logger.warning("All new memos failed to transcribe; no output written.")
        return None

    run_local = clock()
    run_id = _format_run_id(run_local)
    output_path = (output_dir / f"{run_id}.md").resolve()
    output_path.write_text(render_markdown(memos))

    state["runs"][run_id] = {
        "created_at": _format_iso_utc(run_local),
        "files": [m.filename for m in memos],
    }
    write_state_atomic(state_path, state)

    return output_path


def refresh_all_transcriptions(
    memo_dir: Path,
    state_path: Path,
    *,
    transcriber: Callable[[Path], str] = transcribe_file,
    clock: Callable[[], datetime] = _default_clock,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Re-transcribe every file in state['files'], overwriting in place.

    Returns the count of successfully refreshed transcriptions. Files that are
    referenced in state but missing from `memo_dir` are skipped with a warning.
    Entries are overwritten one at a time (not wiped up front), so a crash
    mid-run preserves all completed work.
    """
    state = load_state(state_path)
    filenames = sorted(state["files"].keys())
    if not filenames:
        logger.info("No files to refresh.")
        return 0

    rate_limited = should_sleep(len(filenames))
    refreshed = 0

    for i, name in enumerate(filenames):
        audio_path = memo_dir / name
        if not audio_path.exists():
            logger.warning("Skipping %s: file missing from %s", name, memo_dir)
            continue

        logger.info("Refreshing %s", name)
        try:
            text = retry_with_backoff(
                lambda p=audio_path: transcriber(p),
                sleep=sleep,
            )
        except Exception as exc:
            logger.error("Skipping %s: %s", name, exc)
            continue

        state["files"][name] = {
            "transcription": text,
            "transcribed_at": _format_iso_utc(clock()),
        }
        write_state_atomic(state_path, state)
        refreshed += 1

        if rate_limited and i < len(filenames) - 1:
            sleep(SLEEP_BETWEEN_REQUESTS_SEC)

    return refreshed


def list_runs(state_path: Path) -> list[tuple[str, str, int]]:
    """Return past runs as (run_id, created_at, file_count), oldest first."""
    state = load_state(state_path)
    entries = [
        (run_id, data["created_at"], len(data["files"]))
        for run_id, data in state["runs"].items()
    ]
    entries.sort(key=lambda e: e[1])
    return entries


def regenerate_run(
    run_id: str,
    state_path: Path,
    output_dir: Path,
) -> Path:
    """Re-render markdown for a past run using current transcriptions.

    Raises KeyError if run_id is unknown. Files referenced by the run but no
    longer present in state['files'] are skipped with a warning.
    """
    state = load_state(state_path)
    if run_id not in state["runs"]:
        raise KeyError(f"Unknown run ID: {run_id}")

    memos: list[Memo] = []
    for name in state["runs"][run_id]["files"]:
        entry = state["files"].get(name)
        if entry is None:
            logger.warning(
                "Skipping %s: no transcription in state['files']", name
            )
            continue
        memos.append(
            Memo(
                filename=name,
                transcription=entry["transcription"],
                meta=parse_filename(name),
            )
        )

    if not memos:
        raise RuntimeError(
            f"No memos available for run {run_id}; nothing to render."
        )

    output_path = (output_dir / f"{run_id}.md").resolve()
    output_path.write_text(render_markdown(memos))
    return output_path


# ---------- CLI entry point ----------


DEFAULT_MEMO_DIR = (
    Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/VoiceMemos"
)
DEFAULT_STATE_PATH = Path(__file__).resolve().parent / "state.json"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcribe",
        description="Transcribe iCloud voice memos into a markdown file.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--all",
        action="store_true",
        help="Refresh every existing transcription in place (no markdown written).",
    )
    group.add_argument(
        "--list-runs",
        action="store_true",
        help="Print past runs and exit.",
    )
    group.add_argument(
        "--regenerate",
        metavar="RUN_ID",
        help="Re-render the markdown for a past run using current transcriptions.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    args = _build_parser().parse_args(argv)
    memo_dir = DEFAULT_MEMO_DIR
    state_path = DEFAULT_STATE_PATH
    output_dir = Path.cwd()

    if args.all:
        n = refresh_all_transcriptions(memo_dir, state_path)
        logger.info("Refreshed %d transcriptions", n)
        return 0

    if args.list_runs:
        for run_id, created_at, n in list_runs(state_path):
            print(f"{run_id}  {created_at}  {n} memos")
        return 0

    if args.regenerate:
        try:
            path = regenerate_run(args.regenerate, state_path, output_dir)
        except KeyError as exc:
            logger.error("%s", exc)
            return 1
        print(path)
        return 0

    result = process_new_memos(memo_dir, state_path, output_dir)
    if result is None:
        logger.info("No new memos to process.")
        return 0
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
