from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from concord.backfill import backfill_transcripts
from concord.config import ConcordConfig, load_state
from concord.hooks import handle_event
from concord.install import install_claude_hooks, install_codex_hooks, main as install_main
from concord.transcripts import read_transcript_delta


class FakeClient:
    def __init__(self) -> None:
        self.ingested: list[dict] = []
        self.advice_response: dict = {}

    def ingest_transcript_delta(self, payload: dict) -> dict:
        self.ingested.append(payload)
        return {"ok": True}

    def query_advice(self, payload: dict) -> dict:
        self.last_advice_payload = payload
        return self.advice_response


def make_config(home: Path) -> ConcordConfig:
    return ConcordConfig(
        home=home,
        api_url="https://api.example.test",
        api_token="token",
        team_id="team",
        install_id="install",
    )


def test_transcript_delta_cursor() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        transcript = root / "session.jsonl"
        transcript.write_text('{"a":1}\n', encoding="utf-8")
        state: dict = {}
        first = read_transcript_delta(str(transcript), state)
        assert first is not None
        assert first.previous_offset == 0
        assert first.next_offset == transcript.stat().st_size
        assert first.content == '{"a":1}\n'


def test_capped_live_delta_reports_tail_offset() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        transcript = root / "session.jsonl"
        transcript.write_text('{"a":1}\n{"b":2}\n{"c":3}\n', encoding="utf-8")
        state: dict = {}
        delta = read_transcript_delta(str(transcript), state, max_bytes=10)
        assert delta is not None
        assert delta.previous_offset == transcript.stat().st_size - 10
        assert delta.next_offset == transcript.stat().st_size
        assert len(delta.content.encode("utf-8")) == 10


def test_post_turn_upload_commits_cursor() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        config = make_config(root / "home")
        transcript = root / "session.jsonl"
        transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
        client = FakeClient()
        event = {
            "hook_event_name": "Stop",
            "session_id": "s1",
            "transcript_path": str(transcript),
            "cwd": str(root),
        }
        output = handle_event(event, config, client)
        assert output == ""
        assert len(client.ingested) == 1
        state = load_state(config)
        assert state["transcript_cursors"]
        output = handle_event(event, config, client)
        assert output == ""
        assert len(client.ingested) == 1


def test_pre_turn_advice_output() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        config = make_config(root / "home")
        transcript = root / "session.jsonl"
        transcript.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
        client = FakeClient()
        client.advice_response = {
            "advice": "Use the deployment checklist before editing release scripts.",
            "severity": "info",
            "sources": [{"kind": "procedure", "title": "Deployment checklist"}],
        }
        output = handle_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "transcript_path": str(transcript),
                "cwd": str(root),
                "prompt": "Ship the release",
            },
            config,
            client,
        )
        parsed = json.loads(output)
        context = parsed["hookSpecificOutput"]["additionalContext"]
        assert "Concord team memory" in context
        assert "Deployment checklist" in context
        assert client.last_advice_payload["transcript_tail"]


def test_hook_installers_merge_without_overwrite() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        home = root / "user"
        config = make_config(root / "concord")
        codex_dir = home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "hooks.json").write_text(
            json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "existing"}]}]}}),
            encoding="utf-8",
        )
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text(
            json.dumps({"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "existing"}]}]}}),
            encoding="utf-8",
        )
        fake_python = str(root / "python")
        with patch.object(Path, "home", return_value=home):
            assert install_codex_hooks(config, fake_python) is True
            assert install_claude_hooks(config, fake_python) is True
        codex = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
        claude = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
        assert codex["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "existing"
        assert any(group["hooks"][0]["command"].endswith("-m concord.hooks") for group in codex["hooks"]["Stop"])
        assert claude["hooks"]["Stop"][0]["hooks"][0]["command"] == "existing"
        assert "SessionEnd" in claude["hooks"]


def test_backfill_uploads_existing_transcripts_once() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        config = make_config(root / "home")
        sessions = root / ".codex" / "sessions"
        sessions.mkdir(parents=True)
        transcript = sessions / "session.jsonl"
        transcript.write_text('{"content":"preserve hook config"}\n{"content":"second line"}\n', encoding="utf-8")
        client = FakeClient()

        result = backfill_transcripts(config, roots=[sessions], client=client, chunk_bytes=25)
        assert result["files"] == 1
        assert result["chunks"] >= 2
        assert len(client.ingested) == result["chunks"]
        assert all(item["hook_event_name"] == "Backfill" for item in client.ingested)
        assert all(item["cli"] == "codex" for item in client.ingested)

        result = backfill_transcripts(config, roots=[sessions], client=client, chunk_bytes=25)
        assert result["chunks"] == 0
        assert len(client.ingested) >= 2


def test_backfill_chunks_preserve_jsonl_boundaries() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        config = make_config(root / "home")
        sessions = root / ".codex" / "sessions"
        sessions.mkdir(parents=True)
        transcript = sessions / "session.jsonl"
        transcript.write_text(
            '{"content":"first line with enough text"}\n'
            '{"content":"second line with enough text"}\n'
            '{"content":"third line with enough text"}\n',
            encoding="utf-8",
        )
        client = FakeClient()

        result = backfill_transcripts(config, roots=[sessions], client=client, chunk_bytes=10)
        assert result["chunks"] == 3
        for payload in client.ingested:
            content = payload["transcript"]["content"]
            assert content.endswith("\n")
            for line in content.splitlines():
                json.loads(line)


def test_install_bootstraps_missing_credentials_without_printing_token() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        home = root / "home"
        with patch.dict(os.environ, {"CONCORD_HOME": str(home), "CONCORD_API_TOKEN": "", "CONCORD_TEAM_ID": ""}):
            with patch("concord.client.ConcordClient.bootstrap_install") as bootstrap:
                bootstrap.return_value = {
                    "api_url": "https://api.example.test",
                    "api_token": "issued-token",
                    "team_id": "team_bootstrap",
                    "install_id": "install-bootstrap",
                }
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    install_main(
                        [
                            "--api-url",
                            "https://api.example.test",
                            "--skip-codex",
                            "--skip-claude",
                            "--skip-backfill",
                        ]
                    )
        config = json.loads((home / "config.json").read_text(encoding="utf-8"))
        result = json.loads(output.getvalue())
        assert config["api_token"] == "issued-token"
        assert config["team_id"] == "team_bootstrap"
        assert result["bootstrapped"] is True
        assert "issued-token" not in output.getvalue()


def test_install_starts_background_backfill_by_default() -> None:
    class FakeProcess:
        pid = 4321

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        home = root / "home"
        sessions = root / "sessions"
        sessions.mkdir()
        with patch.dict(os.environ, {"CONCORD_HOME": str(home), "CONCORD_API_TOKEN": "", "CONCORD_TEAM_ID": ""}):
            with patch("concord.client.ConcordClient.bootstrap_install") as bootstrap:
                bootstrap.return_value = {
                    "api_url": "https://api.example.test",
                    "api_token": "issued-token",
                    "team_id": "team_bootstrap",
                    "install_id": "install-bootstrap",
                }
                with patch("concord.install.subprocess.Popen", return_value=FakeProcess()) as popen:
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        install_main(
                            [
                                "--api-url",
                                "https://api.example.test",
                                "--skip-codex",
                                "--skip-claude",
                                "--backfill-roots",
                                str(sessions),
                            ]
                        )
        result = json.loads(output.getvalue())
        status = json.loads((home / "backfill_status.json").read_text(encoding="utf-8"))
        command = popen.call_args.args[0]
        assert result["backfill_started"]["pid"] == 4321
        assert status["state"] == "queued"
        assert status["pid"] == 4321
        assert command[1:3] == ["-m", "concord.backfill"]
        assert "--max-files" in command
        assert command[command.index("--max-files") + 1] == "0"


def main() -> None:
    test_transcript_delta_cursor()
    test_capped_live_delta_reports_tail_offset()
    test_post_turn_upload_commits_cursor()
    test_pre_turn_advice_output()
    test_hook_installers_merge_without_overwrite()
    test_backfill_uploads_existing_transcripts_once()
    test_backfill_chunks_preserve_jsonl_boundaries()
    test_install_bootstraps_missing_credentials_without_printing_token()
    test_install_starts_background_backfill_by_default()
    print(json.dumps({"ok": True, "tests": 9}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
