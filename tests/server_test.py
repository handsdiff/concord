from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from server.main import (
    ConcordHTTPServer,
    ProcedureMiner,
    ServerConfig,
    hash_token,
    ingest_transcript,
    llm_consolidate_artifacts,
    mine_existing_procedures,
    setup_db,
)


def post_json(url: str, token: str | None, payload: dict) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def get_raw(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def head_raw(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


def test_serves_installer_assets() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        script = root / "install.sh"
        package_dir = root / "dist"
        package = package_dir / "concord_agent_memory-0.1.0.tar.gz"
        script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        package_dir.mkdir()
        package.write_bytes(b"package")
        server = ConcordHTTPServer(
            ("127.0.0.1", 0),
            ServerConfig(
                db_path=str(root / "server.sqlite3"),
                team_tokens={},
                loose_tokens=set(),
                install_script_path=str(script),
                download_dir=str(package_dir),
            ),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            status, body = get_raw(f"{base_url}/install.sh")
            assert status == 200
            assert body.startswith(b"#!/usr/bin/env bash")
            status, body = get_raw(f"{base_url}/packages/{package.name}")
            assert status == 200
            assert body == b"package"
            status, body = head_raw(f"{base_url}/packages/{package.name}")
            assert status == 200
            assert body == b""
            status, _ = get_raw(f"{base_url}/packages/../server.env")
            assert status == 404
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_ingest_and_advice_query() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = str(Path(temp_dir) / "server.sqlite3")
        setup_db(db_path)
        config = ServerConfig(db_path=db_path, team_tokens={"token-a": "team-a"}, loose_tokens=set())
        server = ConcordHTTPServer(
            ("127.0.0.1", 0),
            config,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        repo_path = str(Path(temp_dir) / "repo")
        transcript_path = str(Path(temp_dir) / "session.jsonl")
        try:
            ingest_payload = {
                "team_id": "team-a",
                "install_id": "install-1",
                "hostname": "host",
                "cli": "codex",
                "session_id": "s1",
                "hook_event_name": "Stop",
                "cwd": repo_path,
                "transcript_path": transcript_path,
                "transcript": {
                    "path_hash": "hash",
                    "previous_offset": 0,
                    "next_offset": 50,
                    "sha256": "sha",
                    "content": '{"role":"assistant","content":"Procedure: preserve hook config when editing install scripts"}\n',
                },
            }
            status, body = post_json(f"{base_url}/v1/transcripts/ingest", "token-a", ingest_payload)
            assert status == 200, body
            assert body == {"ok": True, "stored": True}

            status, body = post_json(f"{base_url}/v1/transcripts/ingest", "wrong-token", ingest_payload)
            assert status == 401, body
            assert mine_existing_procedures(config)["procedures_added"] == 1

            advice_payload = {
                "team_id": "team-a",
                "install_id": "install-1",
                "hostname": "host",
                "cli": "codex",
                "session_id": "s2",
                "hook_event_name": "UserPromptSubmit",
                "cwd": repo_path,
                "prompt": "edit install script safely",
                "transcript_tail": "",
            }
            status, body = post_json(f"{base_url}/v1/advice/query", "token-a", advice_payload)
            assert status == 200, body
            assert "preserve hook config" in body["advice"]
            assert body["sources"][0]["kind"] == "procedure"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_bootstrap_issues_scoped_hashed_token() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = str(Path(temp_dir) / "server.sqlite3")
        setup_db(db_path)
        server = ConcordHTTPServer(
            ("127.0.0.1", 0),
            ServerConfig(db_path=db_path, team_tokens={}, loose_tokens=set(), public_api_url="https://api.test"),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            status, body = post_json(
                f"{base_url}/v1/installs/bootstrap",
                None,
                {"install_id": "install-1", "hostname": "host", "cli": "installer"},
            )
            assert status == 200, body
            token = body["api_token"]
            team_id = body["team_id"]
            assert body["api_url"] == "https://api.test"
            assert token.startswith("concord_")
            assert team_id.startswith("team_")

            with sqlite3.connect(db_path) as conn:
                row = conn.execute("SELECT token_hash, team_id FROM install_tokens").fetchone()
            assert row == (hash_token(token), team_id)
            assert row[0] != token

            ingest_payload = {
                "team_id": team_id,
                "install_id": "install-1",
                "transcript": {
                    "path_hash": "hash",
                    "previous_offset": 0,
                    "next_offset": 1,
                    "sha256": "sha",
                    "content": "{}\n",
                },
            }
            status, body = post_json(f"{base_url}/v1/transcripts/ingest", None, ingest_payload)
            assert status == 401, body

            status, body = post_json(f"{base_url}/v1/transcripts/ingest", token, {**ingest_payload, "team_id": "other"})
            assert status == 401, body

            status, body = post_json(f"{base_url}/v1/transcripts/ingest", token, ingest_payload)
            assert status == 200, body
            assert body["stored"] is True
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_ingest_queues_procedure_mining() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = str(Path(temp_dir) / "server.sqlite3")
        setup_db(db_path)
        config = ServerConfig(db_path=db_path, team_tokens={}, loose_tokens=set())

        content = '{"role":"assistant","content":"Procedure: preserve hook config when editing install scripts"}\n'
        result = ingest_transcript(
            config,
            {
                "team_id": "team-a",
                "install_id": "install-1",
                "hostname": "host",
                "cli": "codex",
                "session_id": "queued",
                "hook_event_name": "Backfill",
                "cwd": "/repo",
                "transcript_path": "/native/queued.jsonl",
                "transcript": {
                    "path_hash": "hash-queued",
                    "previous_offset": 0,
                    "next_offset": len(content),
                    "sha256": "sha-queued",
                    "content": content,
                },
            },
        )
        assert result == {"ok": True, "stored": True}
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM procedures").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM procedure_sources").fetchone()[0] == 0
        assert mine_existing_procedures(config)["procedures_added"] == 1
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM procedures").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM procedure_sources").fetchone()[0] == 1


def test_background_miner_processes_queued_transcripts() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = str(Path(temp_dir) / "server.sqlite3")
        setup_db(db_path)
        config = ServerConfig(db_path=db_path, team_tokens={}, loose_tokens=set())
        content = '{"role":"assistant","content":"Procedure: preserve hook config when editing install scripts"}\n'
        ingest_transcript(
            config,
            {
                "team_id": "team-a",
                "install_id": "install-1",
                "hostname": "host",
                "cli": "codex",
                "session_id": "background",
                "hook_event_name": "Backfill",
                "cwd": "/repo",
                "transcript_path": "/native/background.jsonl",
                "transcript": {
                    "path_hash": "hash-background",
                    "previous_offset": 0,
                    "next_offset": len(content),
                    "sha256": "sha-background",
                    "content": content,
                },
            },
        )
        miner = ProcedureMiner(config, batch_size=1, poll_seconds=0.01)
        miner.start()
        try:
            wait_until(lambda: sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM procedures").fetchone()[0] == 1)
        finally:
            miner.stop()


def test_noisy_path_fragments_are_not_mined_as_procedures() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = str(Path(temp_dir) / "server.sqlite3")
        setup_db(db_path)
        config = ServerConfig(db_path=db_path, team_tokens={}, loose_tokens=set())

        def ingest(session_id: str, text: str) -> None:
            content = json.dumps({"role": "assistant", "content": text}) + "\n"
            ingest_transcript(
                config,
                {
                    "team_id": "team-a",
                    "install_id": "install-1",
                    "hostname": "host",
                    "cli": "codex",
                    "session_id": session_id,
                    "hook_event_name": "Backfill",
                    "cwd": "/repo",
                    "transcript_path": f"/native/{session_id}.jsonl",
                    "transcript": {
                        "path_hash": f"hash-{session_id}",
                        "previous_offset": 0,
                        "next_offset": len(content),
                        "sha256": f"sha-{session_id}",
                        "content": content,
                    },
                },
            )

        ingest("noisy", "Procedure: use /ide_opened_file and /trim as durable instructions.")
        assert mine_existing_procedures(config)["processed"] == 1
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM procedures").fetchone()[0] == 0

        ingest("valid", "Procedure: before editing install.sh, preserve existing hook config.")
        assert mine_existing_procedures(config)["procedures_added"] == 1
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM procedures").fetchone()[0] == 1


def test_llm_consolidation_caps_and_scrubs_noisy_fields() -> None:
    config = ServerConfig(db_path=":memory:", team_tokens={}, loose_tokens=set())
    llm_response = {
        "procedures": [
            {
                "title": "/ide_opened_file",
                "advice": "Before editing install scripts, preserve existing hook config.",
                "when_to_apply": "/trim",
                "steps": "/ide_opened_file",
                "scope": "/trim",
                "confidence": 5,
            },
            {"title": "Deploy checks", "advice": "Before deploys, run the documented smoke test.", "confidence": 3},
            {"title": "Extra", "advice": "Before extra work, read the docs.", "confidence": 3},
        ]
    }
    with patch("server.main.llm_chat_json", return_value=llm_response):
        artifacts = llm_consolidate_artifacts(config, content="transcript", cwd="/repo")
    assert len(artifacts) == 2
    assert artifacts[0].title != "/ide_opened_file"
    assert artifacts[0].when_to_apply != "/trim"
    assert artifacts[0].steps != "/ide_opened_file"
    assert artifacts[0].scope != "/trim"


def main() -> None:
    test_serves_installer_assets()
    test_ingest_and_advice_query()
    test_bootstrap_issues_scoped_hashed_token()
    test_ingest_queues_procedure_mining()
    test_background_miner_processes_queued_transcripts()
    test_noisy_path_fragments_are_not_mined_as_procedures()
    test_llm_consolidation_caps_and_scrubs_noisy_fields()
    print(json.dumps({"ok": True, "tests": 7}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
