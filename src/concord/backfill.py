from __future__ import annotations

import argparse
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from concord.client import ConcordClient
from concord.config import ConcordConfig, load_config, load_state, write_state
from concord.transcripts import commit_transcript_delta, iter_transcript_deltas


def transcript_roots(value: str | None = None) -> list[Path]:
    if value:
        return [Path(item).expanduser() for item in value.split(os.pathsep) if item]
    home = Path.home()
    return [home / ".codex" / "sessions", home / ".claude" / "projects"]


def transcript_paths(roots: Iterable[Path], max_files: int) -> list[Path]:
    paths: list[Path] = []
    for root in roots:
        if root.exists():
            paths.extend(path for path in root.rglob("*.jsonl") if path.is_file())
    paths = sorted(set(paths), key=lambda path: path.stat().st_mtime)
    return paths[-max_files:] if max_files > 0 else paths


def cli_kind(path: Path) -> str:
    text = str(path)
    if ".claude" in text:
        return "claude-code"
    if ".codex" in text:
        return "codex"
    return "unknown"


def status_path(config: ConcordConfig) -> Path:
    return config.home / "backfill_status.json"


def write_backfill_status(config: ConcordConfig, payload: dict[str, Any]) -> None:
    config.home.mkdir(parents=True, exist_ok=True)
    data = dict(payload)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = status_path(config)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def backfill_transcripts(
    config: ConcordConfig,
    *,
    roots: list[Path] | None = None,
    client: ConcordClient | None = None,
    max_files: int = 0,
    chunk_bytes: int = 1_000_000,
    status_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int | bool]:
    if not config.api_token or not config.team_id:
        return {"skipped": True, "files": 0, "chunks": 0, "bytes": 0}
    active_client = client or ConcordClient(config)
    state = load_state(config)
    paths = transcript_paths(roots or transcript_roots(os.environ.get("CONCORD_BACKFILL_ROOTS")), max_files)
    files = chunks = byte_count = 0
    if status_callback:
        status_callback({"state": "running", "files_total": len(paths), "files_seen": 0, "files_uploaded": 0, "chunks": 0, "bytes": 0})
    for index, path in enumerate(paths, start=1):
        file_chunks = 0
        for delta in iter_transcript_deltas(str(path), state, chunk_bytes=chunk_bytes):
            if not delta.has_content:
                continue
            payload = {
                "team_id": config.team_id,
                "install_id": config.install_id,
                "hostname": socket.gethostname(),
                "cli": cli_kind(path),
                "session_id": path.stem,
                "hook_event_name": "Backfill",
                "cwd": None,
                "permission_mode": None,
                "transcript_path": str(path),
                "transcript": {
                    "path_hash": delta.key,
                    "previous_offset": delta.previous_offset,
                    "next_offset": delta.next_offset,
                    "sha256": delta.sha256,
                    "content": delta.content,
                },
            }
            active_client.ingest_transcript_delta(payload)
            commit_transcript_delta(state, delta)
            chunks += 1
            file_chunks += 1
            byte_count += delta.next_offset - delta.previous_offset
        if file_chunks:
            files += 1
            write_state(config, state)
        if status_callback:
            status_callback(
                {
                    "state": "running",
                    "files_total": len(paths),
                    "files_seen": index,
                    "files_uploaded": files,
                    "chunks": chunks,
                    "bytes": byte_count,
                }
            )
    return {"skipped": False, "files": files, "chunks": chunks, "bytes": byte_count}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill native CLI transcript JSONL files.")
    parser.add_argument("--roots", default=os.environ.get("CONCORD_BACKFILL_ROOTS"))
    parser.add_argument("--max-files", type=int, default=int(os.environ.get("CONCORD_BACKFILL_MAX_FILES", "0")))
    parser.add_argument("--chunk-bytes", type=int, default=int(os.environ.get("CONCORD_BACKFILL_CHUNK_BYTES", "1000000")))
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = load_config()
    try:
        result = backfill_transcripts(
            config,
            roots=transcript_roots(args.roots),
            max_files=args.max_files,
            chunk_bytes=args.chunk_bytes,
            status_callback=lambda payload: write_backfill_status(config, payload),
        )
        write_backfill_status(config, {"state": "done", **result})
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:
        write_backfill_status(config, {"state": "failed", "error": str(exc)})
        raise


if __name__ == "__main__":
    main()
