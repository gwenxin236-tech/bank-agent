#!/usr/bin/env python3
"""Start a local demo server and exercise the public safety/health contract."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.getenv("BANK_AGENT_SMOKE_PORT", "18090"))
env = os.environ.copy()
env.update({"BANK_AGENT_HOST": "127.0.0.1", "BANK_AGENT_PORT": str(PORT), "APP_ENV": "demo", "ALLOW_DEMO_CORE": "true"})
proc = subprocess.Popen([sys.executable, str(ROOT / "app" / "banking_agent.py")], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def get(path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=3) as response:
        return response.status, json.loads(response.read().decode("utf-8"))

try:
    for _ in range(30):
        try:
            if get("/health/live")[0] == 200:
                break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    else:
        raise RuntimeError("local server did not become live")
    assert get("/health/live")[1]["status"] == "LIVE"
    assert get("/api/capabilities")[1]["capabilities"]["elder_mode"]["large_text"] is True
    assert get("/api/production/contracts")[1]["contracts"]["status"] == "DEMO_CONTRACTS_ONLY"
    status, preflight = get("/api/preflight")
    assert status == 200 and preflight["preflight"]["status"] == "READY_FOR_CONFIGURED_RUNTIME"
    print("smoke test passed")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
