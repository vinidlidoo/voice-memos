from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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
