from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


STOPWORDS = {
    "about",
    "after",
    "again",
    "already",
    "also",
    "and",
    "assistant",
    "away",
    "before",
    "being",
    "but",
    "can",
    "casually",
    "concord",
    "could",
    "did",
    "discord",
    "do",
    "does",
    "for",
    "first",
    "from",
    "have",
    "help",
    "here",
    "how",
    "inspect",
    "into",
    "know",
    "look",
    "need",
    "new",
    "please",
    "prior",
    "relevant",
    "see",
    "should",
    "since",
    "square",
    "system",
    "than",
    "that",
    "the",
    "their",
    "there",
    "this",
    "tool",
    "use",
    "user",
    "usually",
    "want",
    "what",
    "when",
    "where",
    "whether",
    "who",
    "why",
    "without",
    "with",
    "would",
    "weird",
    "you",
    "your",
}

NOISY_TOKENS = {
    "content",
    "decision",
    "event_msg",
    "function_call",
    "function_call_output",
    "hook_event_name",
    "json",
    "noisy",
    "payload",
    "response_item",
    "timestamp",
    "type",
}

STRONG_SIGNAL_TERMS = {
    "blocked",
    "bug",
    "decision",
    "deploy",
    "failure",
    "fix",
    "incident",
    "injection",
    "must",
    "preserve",
    "problem",
    "remember",
    "require",
    "verify",
}

PROCEDURAL_SIGNAL_TERMS = {
    "compose",
    "docker",
    "docs",
    "journalctl",
    "memory",
    "readme",
    "repo",
    "ssh",
    "sibling",
    "systemctl",
    "transcript",
    "transcripts",
}

TEXT_KEYS = ("message", "text", "input_text", "output_text", "output")

NOISY_EXCERPT_FRAGMENTS = (
    "concord team memory",
    "apply_patch verification failed",
    "failed to find expected lines",
    "hookSpecificOutput",
    "dashboard-spacing prompt",
    "deterministic eval exposed",
    "prompt stays silent",
    "stays silent for",
    "<oai-mem-citation",
    "memory_summary begins",
    "epistemic preflight",
    "relevant prior transcript signals",
    "the following is the codex agent history",
    "treat the transcript",
    "untrusted evidence",
    "\"risk_level\"",
    "\"user_authorization\"",
    "{\"timestamp\"",
    "\"payload\"",
    "\\\"timestamp\\\"",
    "\\\"payload\\\"",
)

NOISY_PATH_FRAGMENTS = {
    "/ide_opened_file",
    "/trim",
}

RESOURCE_TERMS = (
    "artifact",
    "artifacts",
    "claude memory",
    "codex memory",
    "docs",
    "documentation",
    "historical session",
    "historical sessions",
    "logs",
    "markdown",
    "md files",
    "memory",
    "memory files",
    "readme",
    "session logs",
    "transcript",
    "transcripts",
)

ACTION_TERMS = (
    "access",
    "check",
    "consult",
    "find",
    "inspect",
    "look at",
    "look through",
    "read",
    "refer",
    "search",
    "use",
)

QUESTION_PREFIXES = (
    "can ",
    "could ",
    "how ",
    "i need ",
    "i want ",
    "what ",
    "when ",
    "where ",
    "why ",
)

PROGRESS_PREFIXES = (
    "i'll ",
    "i’ll ",
    "i will ",
    "i've ",
    "i’ve ",
    "i have ",
    "i'm ",
    "i’m ",
    "next i ",
)


class BadRequest(ValueError):
    pass


class TooManyRequests(ValueError):
    pass


@dataclass(frozen=True)
class ServerConfig:
    db_path: str
    team_tokens: dict[str, str]
    loose_tokens: set[str]
    public_api_url: str = ""
    bootstrap_limit_per_hour: int = 30
    max_body_bytes: int = 5_000_000
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 8.0
    install_script_path: str = "install.sh"
    download_dir: str = "dist"


@dataclass(frozen=True)
class ProcedureArtifact:
    title: str
    advice: str
    when_to_apply: str
    when_not_to_apply: str = ""
    steps: str = ""
    evidence: str = ""
    scope: str = ""
    confidence: int = 2


def parse_team_tokens(value: str) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for item in value.split(","):
        team_id, sep, token = item.strip().partition(":")
        if sep and team_id and token:
            tokens[token] = team_id
    return tokens


def parse_loose_tokens(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def random_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18).replace('-', '_')}"


def connect(db_path: str, timeout: float = 5.0) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def setup_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS teams (
              id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              name TEXT NOT NULL,
              source TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS install_tokens (
              token_hash TEXT PRIMARY KEY,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              revoked_at TEXT,
              team_id TEXT NOT NULL,
              install_id TEXT NOT NULL,
              hostname TEXT,
              cli TEXT,
              FOREIGN KEY(team_id) REFERENCES teams(id)
            );
            CREATE INDEX IF NOT EXISTS idx_install_tokens_install
              ON install_tokens(install_id);
            CREATE TABLE IF NOT EXISTS bootstrap_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              ip TEXT NOT NULL,
              install_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bootstrap_events_ip_recent
              ON bootstrap_events(ip, created_at);

            CREATE TABLE IF NOT EXISTS transcript_deltas (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              team_id TEXT NOT NULL,
              install_id TEXT NOT NULL,
              hostname TEXT,
              cli TEXT,
              session_id TEXT,
              hook_event_name TEXT,
              cwd TEXT,
              permission_mode TEXT,
              transcript_path TEXT,
              path_hash TEXT NOT NULL,
              previous_offset INTEGER NOT NULL,
              next_offset INTEGER NOT NULL,
              sha256 TEXT NOT NULL,
              content TEXT NOT NULL,
              UNIQUE(team_id, install_id, path_hash, next_offset, sha256)
            );
            CREATE INDEX IF NOT EXISTS idx_transcript_team_recent
              ON transcript_deltas(team_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_transcript_team_cwd_recent
              ON transcript_deltas(team_id, cwd, id DESC);

            CREATE TABLE IF NOT EXISTS advice_queries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              team_id TEXT NOT NULL,
              install_id TEXT,
              hostname TEXT,
              cli TEXT,
              session_id TEXT,
              hook_event_name TEXT,
              cwd TEXT,
              prompt TEXT NOT NULL,
              transcript_tail TEXT,
              advice TEXT
            );

            CREATE TABLE IF NOT EXISTS procedures (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              team_id TEXT NOT NULL,
              source_delta_id INTEGER,
              cli TEXT,
              session_id TEXT,
              cwd TEXT,
              source_label TEXT,
              trigger_text TEXT,
              procedure TEXT NOT NULL,
              terms TEXT NOT NULL,
              confidence INTEGER NOT NULL DEFAULT 1,
              UNIQUE(team_id, source_delta_id, procedure)
            );
            CREATE INDEX IF NOT EXISTS idx_procedures_team_recent
              ON procedures(team_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_procedures_team_cwd_recent
              ON procedures(team_id, cwd, id DESC);

            CREATE TABLE IF NOT EXISTS procedure_sources (
              transcript_delta_id INTEGER PRIMARY KEY,
              processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        ensure_procedure_columns(conn)


def ensure_procedure_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(procedures)").fetchall()}
    additions = {
        "title": "TEXT NOT NULL DEFAULT ''",
        "when_to_apply": "TEXT NOT NULL DEFAULT ''",
        "when_not_to_apply": "TEXT NOT NULL DEFAULT ''",
        "steps": "TEXT NOT NULL DEFAULT ''",
        "evidence": "TEXT NOT NULL DEFAULT ''",
        "scope": "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE procedures ADD COLUMN {name} {ddl}")


def require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise BadRequest(f"{key} is required")
    return value


def bearer_token(header: str | None) -> str:
    if not header:
        return ""
    prefix = "Bearer "
    return header[len(prefix) :].strip() if header.startswith(prefix) else ""


def authorized(config: ServerConfig, auth_header: str | None, payload: dict[str, Any]) -> bool:
    token = bearer_token(auth_header)
    if not token:
        return False
    team_id = payload.get("team_id")
    if isinstance(team_id, str) and config.team_tokens.get(token) == team_id:
        return True
    if token in config.loose_tokens:
        return True
    if isinstance(team_id, str):
        with connect(config.db_path) as conn:
            row = conn.execute(
                """
                SELECT team_id FROM install_tokens
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (hash_token(token),),
            ).fetchone()
        return bool(row and secrets.compare_digest(str(row["team_id"]), team_id))
    return False


def bootstrap_install(config: ServerConfig, payload: dict[str, Any], ip: str) -> dict[str, Any]:
    install_id = str(payload.get("install_id") or secrets.token_hex(16))[:128]
    hostname = payload.get("hostname") if isinstance(payload.get("hostname"), str) else None
    cli = payload.get("cli") if isinstance(payload.get("cli"), str) else None
    token = "concord_" + secrets.token_urlsafe(32)
    team_id = random_id("team")
    with connect(config.db_path) as conn:
        recent = conn.execute(
            """
            SELECT COUNT(*) FROM bootstrap_events
            WHERE ip = ? AND created_at >= datetime('now', '-1 hour')
            """,
            (ip,),
        ).fetchone()[0]
        if int(recent) >= config.bootstrap_limit_per_hour:
            raise TooManyRequests("bootstrap rate limit exceeded")
        conn.execute("INSERT INTO bootstrap_events (ip, install_id) VALUES (?, ?)", (ip, install_id))
        conn.execute(
            "INSERT INTO teams (id, name, source) VALUES (?, ?, ?)",
            (team_id, "Anonymous install", "bootstrap"),
        )
        conn.execute(
            """
            INSERT INTO install_tokens (token_hash, team_id, install_id, hostname, cli)
            VALUES (?, ?, ?, ?, ?)
            """,
            (hash_token(token), team_id, install_id, hostname, cli),
        )
    response = {"team_id": team_id, "api_token": token, "install_id": install_id}
    if config.public_api_url:
        response["api_url"] = config.public_api_url
    return response


def ingest_transcript(config: ServerConfig, payload: dict[str, Any]) -> dict[str, Any]:
    team_id = require_str(payload, "team_id")
    install_id = require_str(payload, "install_id")
    transcript = payload.get("transcript")
    if not isinstance(transcript, dict):
        raise BadRequest("transcript is required")
    content = require_str(transcript, "content")
    path_hash = require_str(transcript, "path_hash")
    sha256 = require_str(transcript, "sha256")
    previous_offset = int(transcript.get("previous_offset", 0) or 0)
    next_offset = int(transcript.get("next_offset", 0) or 0)
    stored = False
    with connect(config.db_path) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO transcript_deltas (
              team_id, install_id, hostname, cli, session_id, hook_event_name,
              cwd, permission_mode, transcript_path, path_hash, previous_offset,
              next_offset, sha256, content
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                team_id,
                install_id,
                payload.get("hostname"),
                payload.get("cli"),
                payload.get("session_id"),
                payload.get("hook_event_name"),
                payload.get("cwd"),
                payload.get("permission_mode"),
                payload.get("transcript_path"),
                path_hash,
                previous_offset,
                next_offset,
                sha256,
                content,
            ),
        )
        stored = cursor.rowcount == 1
    return {"ok": True, "stored": stored}


def normalize_token(token: str) -> str:
    if token == "tracing":
        return "trace"
    if len(token) > 5 and token.endswith("ing"):
        token = token[:-3]
        if len(token) > 3 and token[-1] == token[-2]:
            token = token[:-1]
    if len(token) > 4 and token.endswith("ies"):
        token = token[:-3] + "y"
    elif len(token) > 4 and token.endswith("s"):
        token = token[:-1]
    return token


def tokens(text: str) -> set[str]:
    values = set()
    for item in re.findall(r"[a-z0-9]{3,}", text.lower()):
        normalized = normalize_token(item)
        if normalized not in STOPWORDS and normalized not in NOISY_TOKENS:
            values.add(normalized)
    return values


def compact_line(line: str, max_chars: int = 300) -> str:
    line = " ".join(line.strip().split())
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 3].rstrip() + "..."


def strip_extracted_text(text: str) -> str:
    text = re.sub(r"^(assistant|user|tool output):\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"^(decision|incident|note|remember):\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^i read [^.]*\.\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^yes\.\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^yes,?\s+that makes sense\.\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^yeah,?\s+that's a known failure mode[^.]*\.\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+i(?:'|’)m\s+[^.]*\.", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+i am\s+[^.]*\.", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+i(?:'|’)ll\s+[^.]*\.", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+i have enough context[^.]*\.", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+next i\s+[^.]*\.", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(do you want|would you like|should i)\b[^?]*\?", "", text, flags=re.IGNORECASE)
    return text.strip()


def clean_extracted_text(text: str, max_chars: int = 300) -> str:
    return compact_line(strip_extracted_text(text), max_chars=max_chars)


def extraction_noise(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return True
    if len(lowered) > 1800:
        return True
    if lowered.startswith("# agents.md instructions") or lowered.startswith("<environment_context>"):
        return True
    if lowered.startswith(PROGRESS_PREFIXES):
        return True
    return any(fragment in lowered for fragment in NOISY_EXCERPT_FRAGMENTS)


def extract_user_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") in {"tool_result", "image"}:
                continue
            part = extract_user_content(item)
            if part:
                parts.append(part)
        return " ".join(parts)
    if isinstance(value, dict):
        payload = value.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "user_message":
            message = payload.get("message")
            return message if isinstance(message, str) else ""
        message = value.get("message")
        if isinstance(message, dict | list):
            message_text = extract_user_content(message)
            if message_text:
                return message_text
        content = value.get("content")
        if content is not None:
            return extract_user_content(content)
        for key in TEXT_KEYS:
            part = value.get(key)
            if isinstance(part, str) and part.strip():
                return part
    return ""


def transcript_line_role_and_text(line: str) -> tuple[str, str]:
    stripped = line.strip()
    if not stripped:
        return "", ""
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(parsed, dict):
        return "", ""
    if parsed.get("type") == "event_msg":
        payload = parsed.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "user_message":
            return "user", strip_extracted_text(extract_user_content(parsed))
        return "", ""
    if transcript_line_payload_type(parsed) in {"function_call", "function_call_output"}:
        return "", ""
    role = transcript_line_role(parsed).lower()
    if role == "user":
        return "user", strip_extracted_text(extract_user_content(parsed))
    if role == "assistant":
        return "assistant", raw_transcript_line_text(line)
    return "", ""


def has_resource_instruction(text: str) -> bool:
    lowered = text.lower()
    if extraction_noise(text):
        return False
    has_action = any(term in lowered for term in ACTION_TERMS)
    has_resource = any(term in lowered for term in RESOURCE_TERMS)
    return has_action and has_resource


def extract_paths(text: str) -> list[str]:
    paths: list[str] = []
    for match in re.findall(r"(?:\.\.?/|/)[A-Za-z0-9._~/-]+", text):
        item = match.rstrip(".,:;)\']}")
        if (
            item
            and item.lower() not in NOISY_PATH_FRAGMENTS
            and item not in paths
            and "token" not in item.lower()
        ):
            paths.append(item)
    return paths[:3]


def has_noisy_path_fragment(text: str) -> bool:
    lowered = text.lower()
    for fragment in NOISY_PATH_FRAGMENTS:
        if re.search(rf"(?<![a-z0-9_.-]){re.escape(fragment)}(?![a-z0-9_.-])", lowered):
            return True
    return False


def procedure_text_noise(text: str) -> bool:
    return extraction_noise(text) or noisy_excerpt(text) or has_noisy_path_fragment(text)


def clean_artifact_field(value: Any, fallback: str, max_chars: int) -> str:
    cleaned = clean_extracted_text(str(value or ""), max_chars=max_chars)
    if not cleaned or procedure_text_noise(cleaned):
        return fallback
    return cleaned


def title_from_text(text: str) -> str:
    words = [item for item in re.findall(r"[A-Za-z0-9._/-]+", text) if item.lower() not in STOPWORDS]
    return compact_line(" ".join(words[:8]) or "Procedure", max_chars=80)


def scope_from_context(cwd: str | None, text: str) -> str:
    parts = []
    if cwd:
        parts.append(cwd)
    parts.extend(extract_paths(text))
    return compact_line(" ".join(dict.fromkeys(parts)), max_chars=240)


def artifact_from_advice(
    advice: str,
    *,
    trigger_text: str | None,
    cwd: str | None,
    evidence: str,
    confidence: int,
) -> ProcedureArtifact | None:
    advice = clean_extracted_text(advice, max_chars=650)
    if procedure_text_noise(advice):
        return None
    when = clean_extracted_text(" ".join(part for part in (trigger_text, evidence) if part), max_chars=500)
    if not when or procedure_text_noise(when):
        when = advice
    return ProcedureArtifact(
        title=title_from_text(advice),
        advice=advice,
        when_to_apply=when,
        when_not_to_apply="Do not inject for unrelated work or when the prompt already contains this guidance.",
        steps=advice,
        evidence=compact_line(evidence, max_chars=700),
        scope=scope_from_context(cwd, advice + " " + when),
        confidence=confidence,
    )


def bounded_confidence(value: Any, default: int = 3) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return default


def procedure_from_user_correction(text: str, trigger: str | None, cwd: str | None) -> ProcedureArtifact | None:
    if not has_resource_instruction(text):
        return None
    lowered = f"{trigger or ''} {text} {cwd or ''}".lower()
    paths = extract_paths(text + " " + (trigger or ""))
    if any(term in lowered for term in ("container", "docker", "env", "recreate", "upgrade")):
        advice = "Before recreating containers, read the repo README and use the documented Docker Compose project and env-file workflow."
    elif any(term in lowered for term in ("hosted service", "service", "server", "nginx", "systemd", "host")):
        target = f" `{paths[0]}`" if paths else " the relevant repo"
        advice = f"Before hosting services on this machine, inspect{target} docs, memory, and artifacts for the established systemd, nginx, and journalctl workflow."
    elif any(term in lowered for term in ("previous", "prior", "remember", "memory", "logs", "historical")):
        advice = "Before answering questions about prior workflows, inspect repo-local Claude/Codex memory files and historical sessions/logs instead of relying only on the current prompt."
    elif paths:
        joined = ", ".join(f"`{path}`" for path in paths[:3])
        advice = f"Before similar work, inspect {joined} and follow the documented procedure there."
    else:
        advice = "Before similar work, consult the relevant README, docs, memory files, artifacts, or transcripts that describe the established workflow."
    return artifact_from_advice(advice, trigger_text=trigger, cwd=cwd, evidence=text, confidence=3)


def direct_assistant_artifact(text: str, cwd: str | None) -> ProcedureArtifact | None:
    if extraction_noise(text):
        return None
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered.startswith("procedure:"):
        return artifact_from_advice(stripped.split(":", 1)[1].strip(), trigger_text=None, cwd=cwd, evidence=stripped, confidence=4)
    if lowered.startswith(("decision:", "incident:")):
        return artifact_from_advice(stripped.split(":", 1)[1].strip(), trigger_text=None, cwd=cwd, evidence=stripped, confidence=3)
    text_terms = tokens(stripped)
    has_named_tool = bool(re.search(r"\b[a-z]+[0-9][a-z0-9_-]*\b", lowered))
    procedural = bool(text_terms.intersection(STRONG_SIGNAL_TERMS | PROCEDURAL_SIGNAL_TERMS) or has_named_tool)
    action = any(
        marker in lowered
        for marker in (
            " available ",
            " before ",
            " blocked ",
            " prefer ",
            " preserve ",
            " roll",
            " run ",
            " seed ",
            " verified",
            " zero ",
            "should pivot",
            "store transcript",
        )
    )
    if lowered.startswith("before ") and len(text_terms) >= 4:
        return artifact_from_advice(stripped, trigger_text=None, cwd=cwd, evidence=stripped, confidence=3)
    if procedural and action:
        return artifact_from_advice(stripped, trigger_text=None, cwd=cwd, evidence=stripped, confidence=2)
    return None


def llm_chat_json(
    config: ServerConfig,
    messages: list[dict[str, str]],
    max_tokens: int = 900,
    timeout_seconds: float | None = None,
) -> dict[str, Any] | None:
    if not (config.llm_base_url and config.llm_api_key and config.llm_model):
        return None
    url = config.llm_base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    body = json.dumps(
        {
            "model": config.llm_model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {config.llm_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "concord-llm/1.0",
        },
    )
    try:
        timeout = timeout_seconds if timeout_seconds is not None else config.llm_timeout_seconds
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        fence = re.fullmatch(r"\s*```(?:json)?\s*([\s\S]*?)\s*```\s*", content or "")
        if fence:
            content = fence.group(1)
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except (KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError, TimeoutError):
        return None


def llm_consolidate_artifacts(
    config: ServerConfig | None,
    *,
    content: str,
    cwd: str | None,
) -> list[ProcedureArtifact]:
    if config is None:
        return []
    compact = "\n".join(compact_line(line, max_chars=900) for line in content.splitlines()[:80])
    parsed = llm_chat_json(
        config,
        [
            {
                "role": "system",
                "content": (
                    "Extract durable team procedures from coding-agent transcripts. "
                    "Return JSON {\"procedures\":[{title,advice,when_to_apply,when_not_to_apply,steps,evidence,scope,confidence}]}. "
                    "Only include reusable operational knowledge that would change a future agent's next action. "
                    "Skip status updates, raw JSON, one-off facts, and advice already obvious from the prompt."
                ),
            },
            {"role": "user", "content": f"cwd={cwd or ''}\n\n{compact}"},
        ],
    )
    if not parsed or not isinstance(parsed.get("procedures"), list):
        return []
    artifacts: list[ProcedureArtifact] = []
    for item in parsed["procedures"][:2]:
        if not isinstance(item, dict):
            continue
        artifact = artifact_from_advice(
            str(item.get("advice") or ""),
            trigger_text=str(item.get("when_to_apply") or ""),
            cwd=cwd,
            evidence=str(item.get("evidence") or ""),
            confidence=bounded_confidence(item.get("confidence")),
        )
        if artifact:
            title = clean_artifact_field(item.get("title"), artifact.title, 80)
            when_to_apply = clean_artifact_field(item.get("when_to_apply"), artifact.when_to_apply, 500)
            when_not_to_apply = clean_artifact_field(item.get("when_not_to_apply"), artifact.when_not_to_apply, 500)
            steps = clean_artifact_field(item.get("steps"), artifact.steps, 700)
            scope = clean_artifact_field(item.get("scope"), artifact.scope, 240)
            artifacts.append(
                ProcedureArtifact(
                    title=title,
                    advice=artifact.advice,
                    when_to_apply=when_to_apply,
                    when_not_to_apply=when_not_to_apply,
                    steps=steps,
                    evidence=artifact.evidence,
                    scope=scope,
                    confidence=artifact.confidence,
                )
            )
    return artifacts


def insert_procedure(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    source_delta_id: int,
    cli: str | None,
    session_id: str | None,
    cwd: str | None,
    trigger_text: str | None,
    artifact: ProcedureArtifact,
) -> bool:
    procedure = clean_extracted_text(artifact.advice, max_chars=650)
    if procedure_text_noise(procedure):
        return False
    term_text = " ".join(
        part
        for part in (
            artifact.title,
            trigger_text or "",
            artifact.when_to_apply,
            artifact.when_not_to_apply,
            artifact.steps,
            artifact.scope,
            procedure,
            cwd or "",
        )
        if part
    )
    term_values = sorted(tokens(term_text))
    if len(term_values) < 3:
        return False
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO procedures (
          team_id, source_delta_id, cli, session_id, cwd, source_label,
          trigger_text, procedure, terms, confidence, title, when_to_apply,
          when_not_to_apply, steps, evidence, scope
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            team_id,
            source_delta_id,
            cli,
            session_id,
            cwd,
            f"{cli or 'agent'} transcript {session_id or source_delta_id}",
            compact_line(trigger_text or "", max_chars=500),
            procedure,
            " ".join(term_values),
            artifact.confidence,
            artifact.title,
            artifact.when_to_apply,
            artifact.when_not_to_apply,
            artifact.steps,
            artifact.evidence,
            artifact.scope,
        ),
    )
    return cursor.rowcount == 1


def mine_procedures_for_delta(
    conn: sqlite3.Connection,
    *,
    config: ServerConfig | None = None,
    team_id: str,
    source_delta_id: int,
    cli: str | None,
    session_id: str | None,
    cwd: str | None,
    content: str,
) -> int:
    added = 0
    for artifact in llm_consolidate_artifacts(config, content=content, cwd=cwd):
        if insert_procedure(
            conn,
            team_id=team_id,
            source_delta_id=source_delta_id,
            cli=cli,
            session_id=session_id,
            cwd=cwd,
            trigger_text=artifact.when_to_apply,
            artifact=artifact,
        ):
            added += 1
    previous_user = ""
    for line in content.splitlines():
        role, text = transcript_line_role_and_text(line)
        if not text or extraction_noise(text):
            continue
        if role == "user":
            artifact = procedure_from_user_correction(text, previous_user, cwd)
            if artifact and insert_procedure(
                conn,
                team_id=team_id,
                source_delta_id=source_delta_id,
                cli=cli,
                session_id=session_id,
                cwd=cwd,
                trigger_text=previous_user,
                artifact=artifact,
            ):
                added += 1
            previous_user = text
        elif role == "assistant":
            artifact = direct_assistant_artifact(text, cwd)
            if artifact and insert_procedure(
                conn,
                team_id=team_id,
                source_delta_id=source_delta_id,
                cli=cli,
                session_id=session_id,
                cwd=cwd,
                trigger_text=None,
                artifact=artifact,
            ):
                added += 1
    conn.execute(
        "INSERT OR IGNORE INTO procedure_sources (transcript_delta_id) VALUES (?)",
        (source_delta_id,),
    )
    return added


def mine_existing_procedures(
    config: ServerConfig,
    team_id: str | None = None,
    limit: int = 0,
    prefer_recent: bool = False,
) -> dict[str, int]:
    params: list[Any] = []
    team_clause = ""
    if team_id:
        team_clause = "AND team_id = ?"
        params.append(team_id)
    limit_clause = "" if limit <= 0 else f"LIMIT {int(limit)}"
    order_clause = "id ASC"
    if prefer_recent:
        order_clause = "CASE WHEN hook_event_name = 'Backfill' THEN 1 ELSE 0 END, id DESC"
    with connect(config.db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT id, team_id, cli, session_id, cwd, content
            FROM transcript_deltas
            WHERE id NOT IN (SELECT transcript_delta_id FROM procedure_sources)
            {team_clause}
            ORDER BY {order_clause}
            {limit_clause}
            """,
            params,
        ).fetchall()
        added = 0
        for row in rows:
            added += mine_procedures_for_delta(
                conn,
                config=config,
                team_id=str(row["team_id"]),
                source_delta_id=int(row["id"]),
                cli=row["cli"],
                session_id=row["session_id"],
                cwd=row["cwd"],
                content=str(row["content"]),
            )
    return {"processed": len(rows), "procedures_added": added}


class ProcedureMiner:
    def __init__(self, config: ServerConfig, *, batch_size: int = 10, poll_seconds: float = 1.0):
        self.config = config
        self.batch_size = max(1, int(batch_size))
        self.poll_seconds = max(0.05, float(poll_seconds))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="concord-procedure-miner", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = mine_existing_procedures(self.config, limit=self.batch_size, prefer_recent=True)
                if int(result["processed"]) == 0:
                    self._stop.wait(self.poll_seconds)
            except Exception as exc:
                print(f"concord procedure miner warning: {type(exc).__name__}: {exc}", file=sys.stderr)
                self._stop.wait(self.poll_seconds)


def row_text(row: sqlite3.Row, key: str) -> str:
    try:
        value = row[key]
    except (KeyError, IndexError):
        return ""
    return value if isinstance(value, str) else ""


def informative_terms(text: str) -> set[str]:
    return {term for term in tokens(text) if len(term) >= 4 and term not in STRONG_SIGNAL_TERMS}


def reference_terms(text: str) -> set[str]:
    refs = set()
    for path in extract_paths(text):
        refs.update(tokens(path))
    for item in re.findall(r"`([^`]{2,120})`", text):
        refs.update(tokens(item))
    return refs


def procedure_hit_score(prompt: str, transcript_tail: str, row: sqlite3.Row, cwd: str | None) -> tuple[bool, int]:
    current_text = f"{prompt}\n{transcript_tail}"
    current_terms = informative_terms(current_text)
    if not current_terms:
        return False, 0
    advice = clean_extracted_text(str(row["procedure"]), max_chars=650)
    if procedure_text_noise(advice):
        return False, 0
    apply_text = " ".join(
        part
        for part in (
            row_text(row, "title"),
            row_text(row, "when_to_apply"),
            row_text(row, "steps"),
            row_text(row, "scope"),
            row_text(row, "trigger_text"),
            str(row["terms"] or ""),
            advice,
        )
        if part
    )
    apply_terms = informative_terms(apply_text)
    advice_terms = informative_terms(advice)
    overlap = current_terms.intersection(apply_terms)
    novel = advice_terms - current_terms
    refs_novel = reference_terms(advice) - reference_terms(current_text)
    if len(novel) < 2 and not refs_novel:
        return False, 0
    broad_singletons = {"agent", "concord", "herme", "repo", "server", "service", "system"}
    if len(overlap) < 2 and not (len(overlap) == 1 and next(iter(overlap)) not in broad_singletons and len(next(iter(overlap))) >= 6):
        return False, 0
    row_cwd = row_text(row, "cwd")
    cwd_match = bool(cwd and row_cwd and (cwd == row_cwd or cwd.startswith(row_cwd.rstrip("/") + "/")))
    scoped = cwd_match or bool(cwd and row_text(row, "scope") and cwd in row_text(row, "scope"))
    if not scoped and len(overlap) < 3:
        return False, 0
    prompt_lower = prompt.lower()
    if any(term in prompt_lower for term in ("lunch", "restaurant", "dinner")) and not overlap.intersection(PROCEDURAL_SIGNAL_TERMS):
        return False, 0
    ref_bonus = min(len(refs_novel) * 8, 24) if len(overlap) >= 2 else 0
    overlap_bonus = len(overlap) * 12 + max(0, len(overlap) - 2) * 12
    score = overlap_bonus + min(len(novel), 12) * 3 + ref_bonus + int(row["confidence"] or 1) * 6
    if scoped:
        score += 20
    if row_text(row, "when_to_apply"):
        score += 8
    return True, score


def llm_filter_hits(config: ServerConfig, prompt: str, cwd: str | None, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not hits:
        return []
    parsed = llm_chat_json(
        config,
        [
            {
                "role": "system",
                "content": (
                    "Choose whether Concord should inject one prior procedure before a coding agent acts. "
                    "Return JSON {\"inject\":true|false,\"id\":\"...\"}. Inject only if the advice is novel, relevant, "
                    "and likely to change the next action. Prefer silence over noisy generic advice."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"cwd": cwd, "prompt": prompt, "candidates": hits[:5]}, ensure_ascii=False),
            },
        ],
        max_tokens=200,
        timeout_seconds=min(config.llm_timeout_seconds, 3.0),
    )
    if not parsed:
        return hits[:1]
    if not parsed.get("inject"):
        return []
    chosen = str(parsed.get("id") or "")
    for hit in hits:
        if str(hit.get("id")) == chosen:
            return [hit]
    return hits[:1]


def top_procedure_hits(
    config: ServerConfig,
    team_id: str,
    cwd: str | None,
    prompt: str,
    transcript_tail: str = "",
) -> list[dict[str, Any]]:
    prompt_terms = tokens(prompt)
    if not prompt_terms:
        return []
    with connect(config.db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, cli, session_id, cwd, source_label,
                   trigger_text, procedure, terms, confidence, title, when_to_apply,
                   when_not_to_apply, steps, evidence, scope
            FROM procedures
            WHERE team_id = ?
            ORDER BY id DESC
            LIMIT 2000
            """,
            (team_id,),
        ).fetchall()
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for row in rows:
        keep, score = procedure_hit_score(prompt, transcript_tail, row, cwd)
        if keep:
            scored.append(
                (
                    score,
                    int(row["id"]),
                    {
                        "id": row["id"],
                        "created_at": row["created_at"],
                        "cli": row["cli"],
                        "session_id": row["session_id"],
                        "cwd": row["cwd"],
                        "excerpt": clean_extracted_text(str(row["procedure"]), max_chars=650),
                        "source_label": row["source_label"],
                        "title": row_text(row, "title"),
                    },
                )
            )
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, _, hit in scored:
        if hit["excerpt"] in seen or too_similar_to_seen(str(hit["excerpt"]), seen):
            continue
        seen.add(str(hit["excerpt"]))
        hits.append(hit)
        if len(hits) == 5:
            break
    return llm_filter_hits(config, prompt, cwd, hits)


def extract_content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(part for item in value if (part := extract_content_text(item)))
    if isinstance(value, dict):
        if isinstance(value.get("payload"), dict):
            payload_text = extract_content_text(value["payload"])
            if payload_text:
                return payload_text
        if isinstance(value.get("message"), dict | list):
            message_text = extract_content_text(value["message"])
            if message_text:
                return message_text
        if "role" in value and "content" in value:
            content = extract_content_text(value.get("content"))
            return content
        for key in TEXT_KEYS:
            part = value.get(key)
            if isinstance(part, str) and part.strip():
                return part
        if "content" in value:
            return extract_content_text(value.get("content"))
    return ""


def transcript_line_role(value: dict[str, Any]) -> str:
    payload = value.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("role"), str):
        return payload["role"]
    message = value.get("message")
    if isinstance(message, dict) and isinstance(message.get("role"), str):
        return message["role"]
    role = value.get("role")
    return role if isinstance(role, str) else ""


def transcript_line_payload_type(value: dict[str, Any]) -> str:
    payload = value.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("type"), str):
        return payload["type"]
    item_type = value.get("type")
    return item_type if isinstance(item_type, str) else ""


def raw_transcript_line_text(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return strip_extracted_text(stripped)
    if isinstance(parsed, dict):
        if transcript_line_role(parsed).lower() == "user":
            return ""
        if parsed.get("type") == "event_msg":
            return ""
        if transcript_line_payload_type(parsed) in {"function_call", "function_call_output"}:
            return ""
    return strip_extracted_text(extract_content_text(parsed))


def transcript_line_excerpts(line: str) -> list[str]:
    text = raw_transcript_line_text(line)
    if not text:
        return []
    chunks = re.split(r"(?m)\n\s*(?:[-*]|\d+\.)\s+", text)
    if len(chunks) == 1:
        chunks = re.split(r"\n{2,}", text)
    excerpts = [compact_line(chunk) for chunk in chunks if len(tokens(chunk)) >= 2]
    return excerpts or [compact_line(text)]


def transcript_line_text(line: str) -> str:
    excerpts = transcript_line_excerpts(line)
    return excerpts[0] if excerpts else ""


def noisy_excerpt(excerpt: str) -> bool:
    lowered = excerpt.strip().lower()
    if not lowered:
        return True
    if lowered.startswith("{") or lowered.startswith("["):
        return True
    if any(fragment in lowered for fragment in NOISY_EXCERPT_FRAGMENTS):
        return True
    if lowered.endswith("?") or (
        lowered.startswith(QUESTION_PREFIXES) and not lowered.startswith(("when working ", "when in "))
    ):
        return True
    if lowered.startswith(PROGRESS_PREFIXES):
        return True
    return False


def useful_hit(prompt_terms: set[str], excerpt: str) -> tuple[bool, int]:
    if noisy_excerpt(excerpt):
        return False, 0
    excerpt_terms = tokens(excerpt)
    if not excerpt_terms:
        return False, 0
    overlap = prompt_terms.intersection(excerpt_terms)
    novel = excerpt_terms - prompt_terms
    if not overlap or len(novel) < 2:
        return False, 0
    specific_overlap = {term for term in overlap if term not in STRONG_SIGNAL_TERMS and len(term) >= 6}
    strong_signal = specific_overlap and excerpt_terms.intersection(STRONG_SIGNAL_TERMS)
    if len(overlap) < 2 and not strong_signal:
        return False, 0
    coverage = len(overlap) / max(1, len(prompt_terms))
    if coverage < 0.2 and len(overlap) < 3 and not strong_signal:
        return False, 0
    procedural_boost = len(excerpt_terms.intersection(PROCEDURAL_SIGNAL_TERMS)) * 12
    score = len(overlap) * 10 + min(len(novel), 8) + len(overlap.intersection(STRONG_SIGNAL_TERMS)) * 8
    score += procedural_boost
    return True, score


def too_similar_to_seen(excerpt: str, seen: set[str]) -> bool:
    excerpt_terms = tokens(excerpt)
    if not excerpt_terms:
        return True
    for item in seen:
        seen_terms = tokens(item)
        denominator = max(1, min(len(excerpt_terms), len(seen_terms)))
        if len(excerpt_terms.intersection(seen_terms)) / denominator >= 0.7:
            return True
    return False


def top_transcript_hits(config: ServerConfig, team_id: str, cwd: str | None, prompt: str) -> list[dict[str, Any]]:
    prompt_tokens = tokens(prompt)
    if not prompt_tokens:
        return []
    with connect(config.db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, received_at, cli, session_id, cwd, content
            FROM transcript_deltas
            WHERE team_id = ? AND (? IS NULL OR cwd = ?)
            ORDER BY id DESC
            LIMIT 100
            """,
            (team_id, cwd, cwd),
        ).fetchall()
        if cwd and not rows:
            rows = conn.execute(
                """
                SELECT id, received_at, cli, session_id, cwd, content
                FROM transcript_deltas
                WHERE team_id = ?
                ORDER BY id DESC
                LIMIT 100
                """,
                (team_id,),
            ).fetchall()
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for row in rows:
        for line in str(row["content"]).splitlines():
            lowered_line = line.lower()
            if not any(term in lowered_line for term in prompt_tokens):
                continue
            for excerpt in transcript_line_excerpts(line):
                keep, score = useful_hit(prompt_tokens, excerpt)
                if keep:
                    scored.append(
                        (
                            score,
                            int(row["id"]),
                            {
                                "id": row["id"],
                                "received_at": row["received_at"],
                                "cli": row["cli"],
                                "session_id": row["session_id"],
                                "cwd": row["cwd"],
                                "excerpt": excerpt,
                            },
                        )
                    )
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_sessions: dict[str, int] = {}
    for _, _, hit in scored:
        if hit["excerpt"] in seen or too_similar_to_seen(str(hit["excerpt"]), seen):
            continue
        session_key = f"{hit['cli'] or 'agent'}:{hit['session_id'] or hit['id']}"
        if seen_sessions.get(session_key, 0) >= 2:
            continue
        seen.add(hit["excerpt"])
        seen_sessions[session_key] = seen_sessions.get(session_key, 0) + 1
        hits.append(hit)
        if len(hits) == 2:
            break
    return hits


def query_advice(config: ServerConfig, payload: dict[str, Any]) -> dict[str, Any]:
    team_id = require_str(payload, "team_id")
    prompt = require_str(payload, "prompt")
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    transcript_tail = payload.get("transcript_tail") if isinstance(payload.get("transcript_tail"), str) else ""
    procedure_hits = top_procedure_hits(config, team_id, cwd, prompt, transcript_tail)
    transcript_hits: list[dict[str, Any]] = []
    hits = procedure_hits or transcript_hits
    advice = ""
    response: dict[str, Any] = {}
    if hits:
        advice = "\n".join(f"- {hit['excerpt']}" for hit in hits)
        source_kind = "procedure" if procedure_hits else "transcript_delta"
        response = {
            "advice": advice,
            "severity": "info",
            "sources": [
                {
                    "kind": source_kind,
                    "id": str(hit["id"]),
                    "title": str(hit.get("source_label") or f"{hit['cli'] or 'agent'} transcript {hit['session_id'] or hit['id']}"),
                }
                for hit in hits
            ],
        }
    try:
        with connect(config.db_path, timeout=0.25) as conn:
            conn.execute(
                """
                INSERT INTO advice_queries (
                  team_id, install_id, hostname, cli, session_id, hook_event_name,
                  cwd, prompt, transcript_tail, advice
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    team_id,
                    payload.get("install_id"),
                    payload.get("hostname"),
                    payload.get("cli"),
                    payload.get("session_id"),
                    payload.get("hook_event_name"),
                    cwd,
                    prompt,
                    transcript_tail,
                    advice,
                ),
            )
    except sqlite3.OperationalError:
        pass
    return response


class ConcordHTTPServer(ThreadingHTTPServer):
    config: ServerConfig

    def __init__(self, address: tuple[str, int], config: ServerConfig):
        self.config = config
        super().__init__(address, ConcordHandler)


class ConcordHandler(BaseHTTPRequestHandler):
    server: ConcordHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def write_json(self, status: int, payload: dict[str, Any], include_body: bool = True) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def write_file(self, path: Path, content_type: str, include_body: bool = True) -> bool:
        if not path.is_file():
            return False
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)
        return True

    def serve_download(self, include_body: bool = True) -> None:
        parsed_path = unquote(urlparse(self.path).path)
        if parsed_path == "/healthz":
            self.write_json(200, {"ok": True}, include_body=include_body)
            return
        if parsed_path == "/install.sh":
            if self.write_file(Path(self.server.config.install_script_path), "text/x-shellscript; charset=utf-8", include_body):
                return
        if parsed_path.startswith("/packages/"):
            name = Path(parsed_path).name
            if re.fullmatch(r"[A-Za-z0-9_.-]+\.tar\.gz", name):
                if self.write_file(Path(self.server.config.download_dir) / name, "application/gzip", include_body):
                    return
        self.write_json(404, {"ok": False, "error": "not found"}, include_body=include_body)

    def do_GET(self) -> None:
        self.serve_download(include_body=True)

    def do_HEAD(self) -> None:
        self.serve_download(include_body=False)

    def read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > self.server.config.max_body_bytes:
            raise BadRequest("invalid request size")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise BadRequest("body must be a JSON object")
        return payload

    def client_ip(self) -> str:
        for header in ("CF-Connecting-IP", "X-Real-IP"):
            value = self.headers.get(header)
            if value:
                return value.split(",")[0].strip()
        return self.client_address[0]

    def do_POST(self) -> None:
        try:
            if self.path == "/v1/installs/bootstrap":
                self.write_json(200, bootstrap_install(self.server.config, self.read_payload(), self.client_ip()))
                return
            if self.path not in {"/v1/transcripts/ingest", "/v1/advice/query"}:
                self.write_json(404, {"ok": False, "error": "not found"})
                return
            payload = self.read_payload()
            if not authorized(self.server.config, self.headers.get("Authorization"), payload):
                self.write_json(401, {"ok": False, "error": "unauthorized"})
                return
            if self.path == "/v1/transcripts/ingest":
                self.write_json(200, ingest_transcript(self.server.config, payload))
            else:
                self.write_json(200, query_advice(self.server.config, payload))
        except TooManyRequests as exc:
            self.write_json(429, {"ok": False, "error": str(exc)})
        except (BadRequest, json.JSONDecodeError, ValueError) as exc:
            self.write_json(400, {"ok": False, "error": str(exc)})
        except sqlite3.OperationalError:
            self.write_json(503, {"ok": False, "error": "database temporarily busy"})


def config_from_env(args: argparse.Namespace) -> ServerConfig:
    team_tokens = parse_team_tokens(os.environ.get("CONCORD_SERVER_TEAM_TOKENS", ""))
    loose_tokens = parse_loose_tokens(os.environ.get("CONCORD_SERVER_TOKENS", ""))
    return ServerConfig(
        db_path=args.db,
        team_tokens=team_tokens,
        loose_tokens=loose_tokens,
        public_api_url=args.public_api_url.rstrip("/"),
        bootstrap_limit_per_hour=args.bootstrap_limit_per_hour,
        max_body_bytes=args.max_body_bytes,
        llm_base_url=os.environ.get("CONCORD_LLM_BASE_URL", "").strip(),
        llm_api_key=os.environ.get("CONCORD_LLM_API_KEY", "").strip(),
        llm_model=os.environ.get("CONCORD_LLM_MODEL", "").strip(),
        llm_timeout_seconds=float(os.environ.get("CONCORD_LLM_TIMEOUT_SECONDS", "8")),
        install_script_path=os.environ.get("CONCORD_INSTALL_SCRIPT_PATH", "install.sh"),
        download_dir=os.environ.get("CONCORD_DOWNLOAD_DIR", "dist"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the hosted Concord API server.")
    parser.add_argument("--host", default=os.environ.get("CONCORD_SERVER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CONCORD_SERVER_PORT", "8500")))
    parser.add_argument("--db", default=os.environ.get("CONCORD_SERVER_DB", "concord_server.sqlite3"))
    parser.add_argument("--public-api-url", default=os.environ.get("CONCORD_PUBLIC_API_URL", ""))
    parser.add_argument("--bootstrap-limit-per-hour", type=int, default=int(os.environ.get("CONCORD_BOOTSTRAP_LIMIT_PER_HOUR", "30")))
    parser.add_argument("--max-body-bytes", type=int, default=int(os.environ.get("CONCORD_SERVER_MAX_BODY_BYTES", "5000000")))
    parser.add_argument("--mine-procedures", action="store_true", help="mine procedures from existing transcript deltas and exit")
    parser.add_argument("--mine-team-id", default=os.environ.get("CONCORD_MINE_TEAM_ID", ""))
    parser.add_argument("--mine-limit", type=int, default=int(os.environ.get("CONCORD_MINE_LIMIT", "0")))
    parser.add_argument("--no-background-miner", action="store_true", default=os.environ.get("CONCORD_NO_BACKGROUND_MINER", "") == "1")
    parser.add_argument("--miner-batch-size", type=int, default=int(os.environ.get("CONCORD_MINER_BATCH_SIZE", "10")))
    parser.add_argument("--miner-poll-seconds", type=float, default=float(os.environ.get("CONCORD_MINER_POLL_SECONDS", "1")))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = config_from_env(args)
    setup_db(config.db_path)
    if args.mine_procedures:
        print(json.dumps(mine_existing_procedures(config, args.mine_team_id or None, args.mine_limit), sort_keys=True))
        return
    server = ConcordHTTPServer((args.host, args.port), config)
    miner = None
    if not args.no_background_miner:
        miner = ProcedureMiner(config, batch_size=args.miner_batch_size, poll_seconds=args.miner_poll_seconds)
        miner.start()
    print(f"Concord server listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    finally:
        if miner:
            miner.stop()


if __name__ == "__main__":
    main()
