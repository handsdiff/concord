from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.main import ServerConfig, ingest_transcript, mine_existing_procedures, query_advice, setup_db


TEAM_ID = "team-eval"
INSTALL_ID = "install-eval"
FIXTURE_DIR = Path(__file__).with_name("fixtures")


@dataclass(frozen=True)
class Case:
    name: str
    prompt: str
    expect_advice: bool
    must_include: tuple[str, ...] = ()
    must_not_include: tuple[str, ...] = ()
    prompt_must_not_include: tuple[str, ...] = ()
    cwd: str = "/repo"
    category: str = "general"


def jsonl_message(role: str, text: str) -> str:
    return json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": role,
                "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}],
            },
        },
        separators=(",", ":"),
    )


def add_transcript(
    config: ServerConfig,
    session_id: str,
    content: str,
    cwd: str = "/repo",
    cli: str = "codex",
) -> None:
    ingest_transcript(
        config,
        {
            "team_id": TEAM_ID,
            "install_id": INSTALL_ID,
            "hostname": "eval",
            "cli": cli,
            "session_id": session_id,
            "hook_event_name": "Backfill",
            "cwd": cwd,
            "transcript_path": f"/native/{session_id}.jsonl",
            "transcript": {
                "path_hash": f"hash-{session_id}",
                "previous_offset": 0,
                "next_offset": len(content.encode("utf-8")),
                "sha256": f"sha-{session_id}",
                "content": content,
            },
        },
    )


def ask(config: ServerConfig, prompt: str, cwd: str = "/repo") -> dict:
    return query_advice(
        config,
        {
            "team_id": TEAM_ID,
            "install_id": INSTALL_ID,
            "hostname": "eval",
            "cli": "codex",
            "session_id": "eval-query",
            "hook_event_name": "UserPromptSubmit",
            "cwd": cwd,
            "prompt": prompt,
            "transcript_tail": "",
        },
    )


def assert_clean_advice(case: Case, advice: str) -> None:
    assert len(advice) <= 900, case.name
    forbidden = ("{\"", "\\\"", "assistant:", "decision:", "payload", "timestamp", "response_item", "hook_event_name")
    for item in forbidden:
        assert item not in advice, (case.name, item, advice)
    normalized = re.sub(r"[`*]+", "", advice.lower())
    for expected in case.must_include:
        assert expected.lower() in normalized, (case.name, expected, advice)
    for forbidden_item in case.must_not_include:
        assert forbidden_item.lower() not in normalized, (case.name, forbidden_item, advice)
    for prompt_forbidden in case.prompt_must_not_include:
        assert prompt_forbidden.lower() not in case.prompt.lower(), (case.name, prompt_forbidden, case.prompt)


def fixture_content(transcript: dict[str, Any]) -> str:
    if isinstance(transcript.get("content"), str):
        return transcript["content"]
    lines = transcript.get("lines")
    if not isinstance(lines, list):
        raise AssertionError(f"fixture transcript missing lines: {transcript.get('session_id')}")
    return "\n".join(json.dumps(line, separators=(",", ":")) for line in lines) + "\n"


def add_fixture(config: ServerConfig, path: Path) -> list[Case]:
    payload = json.loads(path.read_text())
    for transcript in payload.get("transcripts", []):
        add_transcript(
            config,
            str(transcript["session_id"]),
            fixture_content(transcript),
            cwd=str(transcript.get("cwd") or "/repo"),
            cli=str(transcript.get("cli") or "codex"),
        )
    cases: list[Case] = []
    for item in payload.get("cases", []):
        cases.append(
            Case(
                name=f"{path.stem}:{item['name']}",
                prompt=str(item["prompt"]),
                expect_advice=bool(item["expect_advice"]),
                must_include=tuple(item.get("must_include", ())),
                must_not_include=tuple(item.get("must_not_include", ())),
                prompt_must_not_include=tuple(item.get("prompt_must_not_include", ())),
                cwd=str(item.get("cwd") or "/repo"),
                category=str(item.get("category") or path.stem),
            )
        )
    return cases


def run_eval() -> dict[str, int]:
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = str(Path(temp_dir) / "quality.sqlite3")
        setup_db(db_path)
        config = ServerConfig(db_path=db_path, team_tokens={}, loose_tokens=set())
        add_transcript(
            config,
            "release-policy",
            "\n".join(
                [
                    jsonl_message("user", "Can you update the release scripts?"),
                    jsonl_message(
                        "assistant",
                        "Decision: before editing release scripts, run scripts/release_check.py and preserve existing hook config.",
                    ),
                ]
            )
            + "\n",
        )
        add_transcript(
            config,
            "onboarding-incident",
            jsonl_message(
                "assistant",
                "Incident: Discord onboarding failure is usually blocked by bot_pool having no available rows; seed bot_pool before debugging OAuth.",
            )
            + "\n",
        )
        add_transcript(
            config,
            "onboarding-broad-noise",
            jsonl_message(
                "assistant",
                "Discord onboarding launch page copy shipped with a one-click button and visitor-facing headline updates.",
            )
            + "\n",
        )
        add_transcript(
            config,
            "frontend-style",
            jsonl_message("assistant", "For frontend spacing, keep cards at 8px radius and verify mobile text wrapping.")
            + "\n",
        )
        add_transcript(
            config,
            "quality-eval-procedure",
            jsonl_message(
                "assistant",
                "Procedure: after retrieval changes, run PYTHONPATH=. python3 evals/concord_quality_eval.py and fail on noisy JSON advice.",
            )
            + "\n",
        )
        add_transcript(
            config,
            "old-noisy-injection",
            jsonl_message(
                "assistant",
                'Concord team memory (info) Relevant prior transcript signals: - {"timestamp":"2026","type":"event_msg","payload":{"message":"old noisy advice"}}',
            )
            + "\n",
        )
        add_transcript(
            config,
            "prior-user-question",
            jsonl_message("user", "how can we actually eval whether concord is useful?")
            + "\n",
        )
        add_transcript(
            config,
            "live-noisy-peer-fact",
            jsonl_message(
                "assistant",
                "There are 4 peers: brain, test-e2e, user, and 1436148981. The CLI session would use user. Telegram uses the chat ID as the peer name. This means memory from Telegram will not be shared with the CLI.",
            )
            + "\n",
        )

        cases = [
            Case(
                name="surfaces_release_policy",
                prompt="Deploy release scripts safely",
                expect_advice=True,
                must_include=("release_check.py", "preserve existing hook config"),
            ),
            Case(
                name="surfaces_onboarding_incident",
                prompt="Debug Discord onboarding failure",
                expect_advice=True,
                must_include=("bot_pool", "available rows"),
            ),
            Case(
                name="stays_silent_for_irrelevant_prompt",
                prompt="Choose a lunch place near the office",
                expect_advice=False,
            ),
            Case(
                name="surfaces_quality_eval_procedure_without_self_noise",
                prompt="Evaluate Concord retrieval quality without noisy JSON",
                expect_advice=True,
                must_include=("evals/concord_quality_eval.py", "noisy JSON advice"),
            ),
            Case(
                name="does_not_echo_already_known_context",
                prompt="Before editing release scripts, run scripts/release_check.py and preserve existing hook config.",
                expect_advice=False,
            ),
            Case(
                name="stays_silent_for_meta_injection_complaint",
                prompt="thats weird since when i casually use my cli i can see whats being injected and its usually unhelpful, how do you square that away?",
                expect_advice=False,
            ),
        ]
        fixture_cases = 0
        if FIXTURE_DIR.exists():
            for path in sorted(FIXTURE_DIR.glob("*.json")):
                loaded_cases = add_fixture(config, path)
                fixture_cases += len(loaded_cases)
                cases.extend(loaded_cases)
        mine_existing_procedures(config)

        passed = 0
        by_category: dict[str, int] = {}
        for case in cases:
            response = ask(config, case.prompt, cwd=case.cwd)
            advice = str(response.get("advice") or "")
            if case.expect_advice:
                assert advice, case.name
                assert response.get("sources"), case.name
                assert_clean_advice(case, advice)
            else:
                assert not advice, (case.name, advice)
            passed += 1
            by_category[case.category] = by_category.get(case.category, 0) + 1
        return {"passed": passed, "cases": len(cases), "fixture_cases": fixture_cases, "categories": by_category}


def main() -> None:
    print(json.dumps({"ok": True, **run_eval()}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
