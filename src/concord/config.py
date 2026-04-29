from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_API_URL = "https://concord.slate.ceo"
DEFAULT_HOME = "~/.concord"


@dataclass(frozen=True)
class ConcordConfig:
    home: Path
    api_url: str
    api_token: str
    team_id: str
    install_id: str
    advice_timeout_seconds: float = 4.0
    upload_timeout_seconds: float = 10.0
    transcript_tail_bytes: int = 120_000

    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    @property
    def state_path(self) -> Path:
        return self.home / "state.json"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"


def concord_home() -> Path:
    return Path(os.environ.get("CONCORD_HOME", DEFAULT_HOME)).expanduser()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    parsed = json.loads(path.read_text(encoding="utf-8") or "{}")
    return parsed if isinstance(parsed, dict) else {}


def load_config() -> ConcordConfig:
    home = concord_home()
    data = _read_json(home / "config.json")
    api_url = os.environ.get("CONCORD_API_URL") or str(data.get("api_url") or DEFAULT_API_URL)
    api_token = os.environ.get("CONCORD_API_TOKEN") or str(data.get("api_token") or "")
    team_id = os.environ.get("CONCORD_TEAM_ID") or str(data.get("team_id") or "")
    install_id = os.environ.get("CONCORD_INSTALL_ID") or str(data.get("install_id") or "")
    if not install_id:
        install_id = secrets.token_hex(16)
    return ConcordConfig(
        home=home,
        api_url=api_url.rstrip("/"),
        api_token=api_token,
        team_id=team_id,
        install_id=install_id,
        advice_timeout_seconds=float(data.get("advice_timeout_seconds", 4.0)),
        upload_timeout_seconds=float(data.get("upload_timeout_seconds", 10.0)),
        transcript_tail_bytes=int(data.get("transcript_tail_bytes", 120_000)),
    )


def write_config(config: ConcordConfig) -> None:
    config.home.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config.home, 0o700)
    os.chmod(config.log_dir, 0o700)
    payload = {
        "api_url": config.api_url,
        "api_token": config.api_token,
        "team_id": config.team_id,
        "install_id": config.install_id,
        "advice_timeout_seconds": config.advice_timeout_seconds,
        "upload_timeout_seconds": config.upload_timeout_seconds,
        "transcript_tail_bytes": config.transcript_tail_bytes,
    }
    config.config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(config.config_path, 0o600)


def load_state(config: ConcordConfig) -> dict[str, Any]:
    return _read_json(config.state_path)


def write_state(config: ConcordConfig, state: dict[str, Any]) -> None:
    config.home.mkdir(parents=True, exist_ok=True)
    config.state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(config.state_path, 0o600)
