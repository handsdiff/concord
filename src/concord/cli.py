from __future__ import annotations

import argparse
import json

from concord.config import load_config
from concord.hooks import main as hooks_main
from concord.install import main as install_main


def status() -> None:
    config = load_config()
    backfill_status = None
    status_path = config.home / "backfill_status.json"
    if status_path.exists():
        backfill_status = json.loads(status_path.read_text(encoding="utf-8") or "{}")
    print(
        json.dumps(
            {
                "home": str(config.home),
                "api_url": config.api_url,
                "team_id_configured": bool(config.team_id),
                "api_token_configured": bool(config.api_token),
                "install_id": config.install_id,
                "config_path": str(config.config_path),
                "state_path": str(config.state_path),
                "backfill": backfill_status,
            },
            indent=2,
            sort_keys=True,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Concord shared memory for coding agents.")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="Show local Concord configuration.")
    subparsers.add_parser("install", help="Install Concord hooks.")
    hook_parser = subparsers.add_parser("hook", help="Run Concord hook command.")
    hook_parser.add_argument("--event", choices=("auto", "pre-turn", "post-turn"), default="auto")
    return parser


def main() -> None:
    parser = build_parser()
    args, unknown = parser.parse_known_args()
    if args.command == "status":
        status()
    elif args.command == "install":
        install_main(unknown)
    elif args.command == "hook":
        hook_args: list[str] = []
        if args.event:
            hook_args.extend(["--event", args.event])
        hook_args.extend(unknown)
        hooks_main(hook_args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
