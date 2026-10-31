#!/usr/bin/env python3
"""Run the same side-effect-free runtime preflight used by the HTTP API."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from operational_readiness import preflight_report  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--probe-network", action="store_true")
parser.add_argument("--policy", type=Path, help="校验指定策略配置；默认使用 BANK_AGENT_POLICY_PATH 或 config/policy.json")
args = parser.parse_args()
report = preflight_report(probe_network=args.probe_network, policy_path=args.policy)
print(json.dumps(report, ensure_ascii=True, indent=2))
sys.exit(0 if not report["blockers"] else 1)
