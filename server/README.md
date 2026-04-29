# Concord Hosted Server

This folder contains the managed API that the Concord client calls.

It implements:

- `POST /v1/installs/bootstrap`
- `POST /v1/transcripts/ingest`
- `POST /v1/advice/query`
- `GET /healthz`

The server stores transcript deltas in SQLite and returns fast extractive advice
from recent team transcripts. The extractive advice path is the minimal hosted
implementation; a stronger consolidation or model worker can be added behind the
same HTTP contract.

Run `PYTHONPATH=. python3 evals/concord_quality_eval.py` after changing
retrieval or advice formatting. The eval fails if advice is irrelevant, repeats
already-known prompt context, or leaks raw JSON transcript structure.
Fixture cases in `evals/fixtures/` are intentionally short slices derived from
real Codex and Claude logs, with source paths recorded for local spot checks.
The `procedural_memory` cases are the most important product gate: they require
Concord to surface workflow hints from prior sessions before the user explicitly
asks the CLI to read memory, markdown, artifacts, sibling repos, or live runtime
state.

## Run

```sh
export CONCORD_SERVER_DB=concord_server.sqlite3
python3 -m server.main --host 127.0.0.1 --port 8500
```

For a local client pointed at this server:

```sh
export CONCORD_API_URL='http://127.0.0.1:8500'
export CONCORD_API_TOKEN='dev-token'
export CONCORD_TEAM_ID='team-a'
```

`CONCORD_SERVER_TEAM_TOKENS` is a comma-separated list of `team_id:token`
pairs for operator-provisioned clients. Customer installs normally use
`/v1/installs/bootstrap`, which returns a per-install token and stores only the
token hash server-side. Requests are accepted only when the bearer token belongs
to the request `team_id`.

## Storage

SQLite tables:

- `teams`: anonymous or operator-created team/workspace records.
- `install_tokens`: hashed per-install bearer tokens.
- `bootstrap_events`: lightweight bootstrap rate-limit records.
- `transcript_deltas`: append-only transcript deltas uploaded by CLI hooks.
- `advice_queries`: prompts, transcript tails, and returned advice.

The server does not log request bodies.
