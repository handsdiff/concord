from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any

from concord.config import ConcordConfig, load_config, write_config


HOOK_COMMAND = "CONCORD_HOME={home} {python} -m concord.hooks"
PRE_TURN_HOOK_TIMEOUT_SECONDS = 8
POST_TURN_HOOK_TIMEOUT_SECONDS = 10


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    parsed = json.loads(path.read_text(encoding="utf-8") or "{}")
    return parsed if isinstance(parsed, dict) else {}


def _write_json(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, mode)


def _append_hook(data: dict[str, Any], event_name: str, command: str, timeout: int, matcher: str | None = None) -> bool:
    hooks = data.setdefault("hooks", {})
    groups = hooks.setdefault(event_name, [])
    if not isinstance(groups, list):
        groups = []
        hooks[event_name] = groups
    for group in groups:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks", []):
            if isinstance(hook, dict) and hook.get("type") == "command" and hook.get("command") == command:
                if int(hook.get("timeout") or 0) < timeout:
                    hook["timeout"] = timeout
                    return True
                return False
    group: dict[str, Any] = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
    if matcher is not None:
        group["matcher"] = matcher
    groups.append(group)
    return True


def install_codex_hooks(config: ConcordConfig, python: str) -> bool:
    path = Path.home() / ".codex" / "hooks.json"
    data = _read_json(path)
    command = HOOK_COMMAND.format(home=str(config.home), python=python)
    changed = False
    changed = _append_hook(data, "UserPromptSubmit", command, PRE_TURN_HOOK_TIMEOUT_SECONDS) or changed
    changed = _append_hook(data, "Stop", command, POST_TURN_HOOK_TIMEOUT_SECONDS) or changed
    if changed:
        _write_json(path, data)
    return changed


def install_claude_hooks(config: ConcordConfig, python: str) -> bool:
    path = Path.home() / ".claude" / "settings.json"
    data = _read_json(path)
    command = HOOK_COMMAND.format(home=str(config.home), python=python)
    changed = False
    changed = _append_hook(data, "UserPromptSubmit", command, PRE_TURN_HOOK_TIMEOUT_SECONDS, matcher="") or changed
    changed = _append_hook(data, "Stop", command, POST_TURN_HOOK_TIMEOUT_SECONDS, matcher="") or changed
    changed = _append_hook(data, "SessionEnd", command, POST_TURN_HOOK_TIMEOUT_SECONDS, matcher="") or changed
    if changed:
        _write_json(path, data)
    return changed


def start_background_backfill(config: ConcordConfig, python: str, roots: str | None, max_files: int) -> dict[str, Any]:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config.log_dir, 0o700)
    log_path = config.log_dir / "backfill.log"
    command = [python, "-m", "concord.backfill", "--max-files", str(max_files)]
    if roots:
        command.extend(["--roots", roots])
    env = os.environ.copy()
    env["CONCORD_HOME"] = str(config.home)
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    from concord.backfill import status_path, write_backfill_status

    write_backfill_status(
        config,
        {
            "state": "queued",
            "pid": process.pid,
            "log": str(log_path),
            "max_files": max_files,
        },
    )
    return {"pid": process.pid, "log": str(log_path), "status": str(status_path(config))}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install Concord hooks.")
    parser.add_argument("--api-url", default=os.environ.get("CONCORD_API_URL"))
    parser.add_argument("--api-token", default=os.environ.get("CONCORD_API_TOKEN"))
    parser.add_argument("--team-id", default=os.environ.get("CONCORD_TEAM_ID"))
    parser.add_argument("--python", default=os.environ.get("CONCORD_PYTHON") or os.sys.executable)
    parser.add_argument("--skip-codex", action="store_true")
    parser.add_argument("--skip-claude", action="store_true")
    parser.add_argument("--skip-bootstrap", action="store_true")
    parser.add_argument("--skip-backfill", action="store_true")
    parser.add_argument("--foreground-backfill", action="store_true")
    parser.add_argument("--backfill-roots", default=os.environ.get("CONCORD_BACKFILL_ROOTS"))
    parser.add_argument("--backfill-max-files", type=int, default=int(os.environ.get("CONCORD_BACKFILL_MAX_FILES", "0")))
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    current = load_config()
    config = ConcordConfig(
        home=current.home,
        api_url=(args.api_url or current.api_url).rstrip("/"),
        api_token=args.api_token or current.api_token,
        team_id=args.team_id or current.team_id,
        install_id=current.install_id,
        advice_timeout_seconds=current.advice_timeout_seconds,
        upload_timeout_seconds=current.upload_timeout_seconds,
        transcript_tail_bytes=current.transcript_tail_bytes,
    )
    result: dict[str, Any] = {
        "config": str(config.config_path),
        "bootstrapped": False,
        "codex_hooks_changed": False,
        "claude_hooks_changed": False,
    }
    if (not config.api_token or not config.team_id) and not args.skip_bootstrap:
        from concord.client import ConcordClient

        bootstrap = ConcordClient(config).bootstrap_install(
            {
                "install_id": config.install_id,
                "hostname": socket.gethostname(),
                "cli": "installer",
            }
        )
        api_token = bootstrap.get("api_token")
        team_id = bootstrap.get("team_id")
        if not isinstance(api_token, str) or not api_token or not isinstance(team_id, str) or not team_id:
            raise SystemExit("bootstrap response did not include api_token and team_id")
        config = ConcordConfig(
            home=config.home,
            api_url=str(bootstrap.get("api_url") or config.api_url).rstrip("/"),
            api_token=api_token,
            team_id=team_id,
            install_id=str(bootstrap.get("install_id") or config.install_id),
            advice_timeout_seconds=config.advice_timeout_seconds,
            upload_timeout_seconds=config.upload_timeout_seconds,
            transcript_tail_bytes=config.transcript_tail_bytes,
        )
        result["bootstrapped"] = True
    write_config(config)
    if not args.skip_codex:
        result["codex_hooks_changed"] = install_codex_hooks(config, args.python)
    if not args.skip_claude:
        result["claude_hooks_changed"] = install_claude_hooks(config, args.python)
    if not args.skip_backfill:
        try:
            if args.foreground_backfill:
                from concord.backfill import backfill_transcripts, transcript_roots, write_backfill_status

                result["backfill"] = backfill_transcripts(
                    config,
                    roots=transcript_roots(args.backfill_roots),
                    max_files=args.backfill_max_files,
                    status_callback=lambda payload: write_backfill_status(config, payload),
                )
                write_backfill_status(config, {"state": "done", **result["backfill"]})
            else:
                result["backfill_started"] = start_background_backfill(
                    config,
                    args.python,
                    args.backfill_roots,
                    args.backfill_max_files,
                )
        except Exception as exc:
            result["backfill_error"] = str(exc)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
