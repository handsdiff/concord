from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any

from concord.client import ConcordClient, ConcordClientError
from concord.config import ConcordConfig, load_config, load_state, write_state
from concord.transcripts import (
    commit_transcript_delta,
    read_transcript_delta,
    read_transcript_tail,
)


POST_TURN_EVENTS = {"Stop", "SessionEnd", "PostToolBatch", "PostToolUse"}
PRE_TURN_EVENTS = {"UserPromptSubmit"}


def load_hook_event() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def hook_event_name(event: dict[str, Any]) -> str:
    value = event.get("hook_event_name") or event.get("hookEventName") or ""
    return value if isinstance(value, str) else ""


def cli_kind(event: dict[str, Any]) -> str:
    transcript_path = str(event.get("transcript_path") or "")
    if ".claude" in transcript_path:
        return "claude-code"
    if ".codex" in transcript_path:
        return "codex"
    return "unknown"


def base_payload(config: ConcordConfig, event: dict[str, Any]) -> dict[str, Any]:
    return {
        "team_id": config.team_id,
        "install_id": config.install_id,
        "hostname": socket.gethostname(),
        "cli": cli_kind(event),
        "session_id": event.get("session_id") or event.get("sessionId"),
        "hook_event_name": hook_event_name(event),
        "cwd": event.get("cwd"),
        "permission_mode": event.get("permission_mode") or event.get("permissionMode"),
        "transcript_path": event.get("transcript_path") or event.get("transcriptPath"),
    }


def log_nonblocking_error(message: str) -> None:
    print(f"concord hook warning: {message}", file=sys.stderr)


def upload_transcript(config: ConcordConfig, event: dict[str, Any], client: ConcordClient | None = None) -> bool:
    transcript_path = event.get("transcript_path") or event.get("transcriptPath")
    if not isinstance(transcript_path, str) or not transcript_path:
        return False
    state = load_state(config)
    delta = read_transcript_delta(transcript_path, state)
    if delta is None or not delta.has_content:
        return False
    payload = {
        **base_payload(config, event),
        "transcript": {
            "path_hash": delta.key,
            "previous_offset": delta.previous_offset,
            "next_offset": delta.next_offset,
            "sha256": delta.sha256,
            "content": delta.content,
        },
    }
    active_client = client or ConcordClient(config)
    active_client.ingest_transcript_delta(payload)
    commit_transcript_delta(state, delta)
    write_state(config, state)
    return True


def advice_context_from_response(response: dict[str, Any]) -> str:
    advice = response.get("advice") or response.get("additional_context") or response.get("additionalContext")
    if not isinstance(advice, str) or not advice.strip():
        return ""
    severity = response.get("severity")
    title = "Concord team memory"
    if isinstance(severity, str) and severity:
        title = f"{title} ({severity})"
    source_lines: list[str] = []
    sources = response.get("sources")
    if isinstance(sources, list):
        for item in sources[:3]:
            if isinstance(item, dict):
                label = item.get("title") or item.get("id") or item.get("kind")
                if isinstance(label, str) and label:
                    source_lines.append(f"- {label}")
    lines = [title, advice.strip()]
    if source_lines:
        lines.append("Sources:")
        lines.extend(source_lines)
    return "\n".join(lines)


def hook_context(event_name: str, additional_context: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": additional_context,
        }
    }


def query_advice(config: ConcordConfig, event: dict[str, Any], client: ConcordClient | None = None) -> str:
    prompt = event.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return ""
    transcript_path = event.get("transcript_path") or event.get("transcriptPath")
    transcript_tail = ""
    if isinstance(transcript_path, str) and transcript_path:
        transcript_tail = read_transcript_tail(transcript_path, config.transcript_tail_bytes)
    payload = {
        **base_payload(config, event),
        "prompt": prompt,
        "transcript_tail": transcript_tail,
    }
    active_client = client or ConcordClient(config)
    return advice_context_from_response(active_client.query_advice(payload))


def handle_event(event: dict[str, Any], config: ConcordConfig | None = None, client: ConcordClient | None = None) -> str:
    active_config = config or load_config()
    event_name = hook_event_name(event)
    try:
        if event_name in PRE_TURN_EVENTS:
            advice = query_advice(active_config, event, client)
            if advice:
                return json.dumps(hook_context(event_name, advice), ensure_ascii=False, separators=(",", ":"))
            return ""
        if event_name in POST_TURN_EVENTS:
            upload_transcript(active_config, event, client)
            return ""
        return ""
    except (ConcordClientError, OSError, json.JSONDecodeError) as exc:
        log_nonblocking_error(str(exc))
        return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Concord CLI hook.")
    parser.add_argument("--event", choices=("auto", "pre-turn", "post-turn"), default="auto")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    event = load_hook_event()
    if args.event == "pre-turn":
        event["hook_event_name"] = "UserPromptSubmit"
    elif args.event == "post-turn" and not hook_event_name(event):
        event["hook_event_name"] = "Stop"
    output = handle_event(event)
    if output:
        print(output)


if __name__ == "__main__":
    main()
