# Concord

Concord is collaborative memory for coding agents. It watches team agent
transcripts, sends them to Concord's managed backend, and injects relevant team
advice before the next agent acts. Customers install the local CLI client; they
do not host the Concord server.

The product wedge is not transcript storage by itself. Concord is the active
layer on top of stored transcripts:

- Post-turn hooks upload transcript deltas from Codex and Claude Code.
- Concord's hosted worker consolidates decisions, procedures, incidents, and
  repo norms for the customer's team.
- Pre-turn hooks ask Concord's hosted worker for advice before the CLI agent
  starts.
- If there is no useful advice, the hook stays silent.

## Install

From a checkout or packaged tarball:

```sh
./install.sh
```

The installer creates:

```text
~/.concord/
├── .venv/
├── config.json
├── state.json
└── logs/
```

It registers hooks for both Codex and Claude Code:

- `UserPromptSubmit`: pre-turn advice lookup.
- `Stop`: post-turn transcript upload.
- `SessionEnd`: Claude Code final transcript upload.

It also starts a background backfill from existing local JSONL transcripts under
`~/.codex/sessions` and `~/.claude/projects`. The install command returns after
hooks are installed; backfill progress and resume cursors are kept under
`~/.concord/`.

No local daemon, local SQLite database, or local LLM is installed on the default
customer path. Transcript storage, indexing, model calls, and latency-sensitive
advice generation are handled by Concord's managed service.

## How It Works

When a turn finishes, Concord receives the CLI hook payload. If the payload
contains `transcript_path`, Concord reads only the new bytes since the last
uploaded offset and sends them to:

```text
POST /v1/transcripts/ingest
```

Before the next user prompt is processed, Concord sends the current prompt,
workspace metadata, and recent transcript tail to:

```text
POST /v1/advice/query
```

The managed backend can return no advice, or a short model-visible block like:

```json
{
  "advice": "Before editing install.sh, preserve existing hook config instead of overwriting it.",
  "severity": "info",
  "sources": [
    {"kind": "decision", "title": "Hook config merge policy"}
  ]
}
```

The CLI then receives that advice as hook-injected context.

Concord stores raw transcripts separately from derived procedures. The hosted
worker distills durable procedures into structured fields: applicability,
non-applicability, steps, evidence, scope, and confidence. Advice lookup ranks
those procedures by relevance and novelty against the current prompt and recent
thread tail, preferring silence when the match is weak.

## CLI

Show local config:

```sh
concord status
```

Run the hook command manually:

```sh
concord hook --event auto
```

Check background backfill progress:

```sh
concord status
tail -f ~/.concord/logs/backfill.log
```

Install hooks again after changing config:

```sh
concord install
```

## Configuration

Environment variables used by `install.sh`:

```sh
CONCORD_HOME=~/.concord
CONCORD_PACKAGE_URL=https://downloads.example/concord.tar.gz
CONCORD_BACKFILL_ROOTS=~/.codex/sessions:~/.claude/projects
CONCORD_BACKFILL_MAX_FILES=0
```

Optional operator-provisioned installs may also set `CONCORD_API_TOKEN` and
`CONCORD_TEAM_ID`; otherwise the installer bootstraps its own token.

`CONCORD_API_URL` defaults to Concord's managed API and should only be set by
operators for staging or development.

Hosted operators can enable OpenAI-compatible model calls for procedure
consolidation and final reranking:

```sh
CONCORD_LLM_BASE_URL=https://litellm.example/v1
CONCORD_LLM_API_KEY=...
CONCORD_LLM_MODEL=slate-1
CONCORD_LLM_TIMEOUT_SECONDS=8
```

If those variables are absent, the server uses its deterministic fallback so
tests and local development remain offline.

Runtime config is stored in `~/.concord/config.json` with `0600`
permissions. Transcript contents are not stored locally beyond the native CLI
transcript files; Concord keeps only upload cursors in `state.json` and
background progress in `backfill_status.json`. `CONCORD_BACKFILL_MAX_FILES=0`
means all matching transcripts and is the default.

If `CONCORD_API_TOKEN` and `CONCORD_TEAM_ID` are not set, the installer calls:

```text
POST /v1/installs/bootstrap
```

The managed API returns a new anonymous team id and per-install token. The raw
token is written once to local config; the server stores only its hash. Operators
may still set `CONCORD_API_TOKEN` and `CONCORD_TEAM_ID` for pre-provisioned
teams or test installs.

## Managed Backend Contract

The hosted implementation lives in `server/`. It provides the API that the
local client calls and is intended to be run by Concord operators.

```sh
python3 -m server.main --host 127.0.0.1 --port 8500
```

For local end-to-end testing with a pre-provisioned token, point a client at
that server:

```sh
export CONCORD_SERVER_TEAM_TOKENS='team-a:dev-token'
export CONCORD_API_URL='http://127.0.0.1:8500'
export CONCORD_API_TOKEN='dev-token'
export CONCORD_TEAM_ID='team-a'
```

Transcript ingestion request:

```json
{
  "team_id": "team",
  "install_id": "device-install-id",
  "hostname": "workstation",
  "cli": "codex",
  "session_id": "session",
  "hook_event_name": "Stop",
  "cwd": "/repo",
  "transcript_path": "/native/transcript.jsonl",
  "transcript": {
    "path_hash": "sha256-of-path",
    "previous_offset": 0,
    "next_offset": 1200,
    "sha256": "sha256-of-delta",
    "content": "{\"jsonl\":\"delta\"}\n"
  }
}
```

Advice query request:

```json
{
  "team_id": "team",
  "install_id": "device-install-id",
  "hostname": "workstation",
  "cli": "claude-code",
  "session_id": "session",
  "hook_event_name": "UserPromptSubmit",
  "cwd": "/repo",
  "prompt": "Refactor the deployment flow",
  "transcript_tail": "recent native JSONL transcript tail"
}
```

Bootstrap request:

```json
{
  "install_id": "device-install-id",
  "hostname": "workstation",
  "cli": "installer"
}
```

Bootstrap response:

```json
{
  "team_id": "team_generated",
  "api_token": "concord_generated",
  "install_id": "device-install-id"
}
```

## Development

Run focused tests:

```sh
PYTHONPATH=src python3 tests/concord_hooks_test.py
PYTHONPATH=. python3 tests/server_test.py
PYTHONPATH=. python3 evals/concord_quality_eval.py
```

The quality eval is a deterministic first-pass gate for usefulness. It asserts
that Concord surfaces relevant prior decisions, stays silent for unrelated
prompts, suppresses advice that merely repeats the prompt, and avoids raw JSON
transcript noise in injected advice. Real Codex and Claude-derived cases live in
`evals/fixtures/`; add a small fixture there when a new customer transcript
reveals a retrieval miss or noisy injection pattern. The procedural-memory cases
specifically test that Concord injects source-of-truth workflow hints, such as
which README, memory file, sibling repo, live VM, service command, or deployment
flow to consult, even when the current prompt does not ask for those sources.
