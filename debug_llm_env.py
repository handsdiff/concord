#!/usr/bin/env python3
import json, os, sys, urllib.error, urllib.request
from pathlib import Path

env = Path(__file__).with_name("server.env")
if env.exists():
    for line in env.read_text().splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip("'\""))

base = os.environ.get("CONCORD_LLM_BASE_URL", "").rstrip("/")
key = os.environ.get("CONCORD_LLM_API_KEY", "")
model = os.environ.get("CONCORD_LLM_MODEL", "slate-1")
if not base or not key:
    sys.exit("missing CONCORD_LLM_BASE_URL or CONCORD_LLM_API_KEY")

body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 16}).encode()
req = urllib.request.Request(base + "/chat/completions", data=body, method="POST", headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "concord-llm/1.0"})
try:
    with urllib.request.urlopen(req, timeout=20) as res:
        print(res.status, res.read().decode("utf-8", "replace")[:1000])
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode("utf-8", "replace")[:1000])
