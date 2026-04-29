from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TranscriptDelta:
    path: str
    key: str
    previous_offset: int
    next_offset: int
    content: str
    sha256: str

    @property
    def has_content(self) -> bool:
        return bool(self.content)


def transcript_key(path: str) -> str:
    return hashlib.sha256(str(Path(path).expanduser()).encode("utf-8")).hexdigest()


def read_transcript_delta(path: str, state: dict[str, Any], max_bytes: int = 2_000_000) -> TranscriptDelta | None:
    transcript_path = Path(path).expanduser()
    if not transcript_path.exists() or not transcript_path.is_file():
        return None
    key = transcript_key(str(transcript_path))
    cursors = state.setdefault("transcript_cursors", {})
    if not isinstance(cursors, dict):
        cursors = {}
        state["transcript_cursors"] = cursors
    previous_offset = int(cursors.get(key, 0) or 0)
    size = transcript_path.stat().st_size
    if previous_offset < 0 or previous_offset > size:
        previous_offset = 0
    read_start = previous_offset
    if size - read_start > max_bytes:
        read_start = max(0, size - max_bytes)
    with transcript_path.open("rb") as handle:
        handle.seek(read_start)
        raw = handle.read(size - read_start)
    content = raw.decode("utf-8", errors="replace")
    return TranscriptDelta(
        path=str(transcript_path),
        key=key,
        previous_offset=previous_offset,
        next_offset=size,
        content=content,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def iter_transcript_deltas(
    path: str,
    state: dict[str, Any],
    chunk_bytes: int = 1_000_000,
) -> list[TranscriptDelta]:
    transcript_path = Path(path).expanduser()
    if not transcript_path.exists() or not transcript_path.is_file():
        return []
    key = transcript_key(str(transcript_path))
    cursors = state.setdefault("transcript_cursors", {})
    if not isinstance(cursors, dict):
        cursors = {}
        state["transcript_cursors"] = cursors
    start = int(cursors.get(key, 0) or 0)
    size = transcript_path.stat().st_size
    if start < 0 or start > size:
        start = 0
    deltas: list[TranscriptDelta] = []
    with transcript_path.open("rb") as handle:
        while start < size:
            handle.seek(start)
            raw = handle.read(min(chunk_bytes, size - start))
            next_offset = start + len(raw)
            deltas.append(
                TranscriptDelta(
                    path=str(transcript_path),
                    key=key,
                    previous_offset=start,
                    next_offset=next_offset,
                    content=raw.decode("utf-8", errors="replace"),
                    sha256=hashlib.sha256(raw).hexdigest(),
                )
            )
            start = next_offset
    return deltas


def commit_transcript_delta(state: dict[str, Any], delta: TranscriptDelta) -> None:
    cursors = state.setdefault("transcript_cursors", {})
    if not isinstance(cursors, dict):
        cursors = {}
        state["transcript_cursors"] = cursors
    cursors[delta.key] = delta.next_offset


def read_transcript_tail(path: str, max_bytes: int) -> str:
    transcript_path = Path(path).expanduser()
    if not transcript_path.exists() or not transcript_path.is_file():
        return ""
    size = transcript_path.stat().st_size
    with transcript_path.open("rb") as handle:
        handle.seek(max(0, size - max_bytes))
        raw = handle.read()
    return raw.decode("utf-8", errors="replace")
