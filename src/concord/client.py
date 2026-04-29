from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from concord.config import ConcordConfig


MAX_PRE_TURN_REQUEST_SECONDS = 4.0


class ConcordClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConcordClient:
    config: ConcordConfig

    def post(self, path: str, payload: dict[str, Any], timeout: float, require_auth: bool = True) -> dict[str, Any]:
        if require_auth and not self.config.api_token:
            raise ConcordClientError("CONCORD_API_TOKEN is not configured")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "concord-client/0.1.0",
        }
        if self.config.api_token:
            headers["Authorization"] = f"Bearer {self.config.api_token}"
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.api_url}{path}",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.URLError as exc:
            raise ConcordClientError(str(exc)) from exc
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}

    def ingest_transcript_delta(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/v1/transcripts/ingest", payload, self.config.upload_timeout_seconds)

    def query_advice(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/v1/advice/query", payload, min(self.config.advice_timeout_seconds, MAX_PRE_TURN_REQUEST_SECONDS))

    def bootstrap_install(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/v1/installs/bootstrap", payload, self.config.upload_timeout_seconds, require_auth=False)
