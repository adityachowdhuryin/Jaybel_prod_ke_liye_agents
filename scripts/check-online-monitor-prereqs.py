#!/usr/bin/env python3
"""Check local prerequisites for Agent Engine online monitoring signal flow."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def _check(name: str, ok: bool, detail: str) -> bool:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}: {detail}")
    return ok


def _health(url: str) -> dict:
    req = urllib.request.Request(url=url, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def main() -> None:
    all_ok = True
    orch_local_chat = (os.environ.get("ORCHESTRATOR_LOCAL_CHAT", "") or "").strip().lower()
    all_ok &= _check(
        "ORCHESTRATOR_LOCAL_CHAT",
        orch_local_chat not in {"1", "true", "yes"},
        f"value={orch_local_chat or '<unset>'}",
    )

    orch_resource = (os.environ.get("ORCHESTRATOR_AGENT_ENGINE_RESOURCE", "") or "").strip()
    all_ok &= _check(
        "ORCHESTRATOR_AGENT_ENGINE_RESOURCE",
        bool(orch_resource),
        orch_resource or "missing",
    )
    cost_resource = (os.environ.get("COST_AGENT_ENGINE_RESOURCE", "") or "").strip()
    all_ok &= _check(
        "COST_AGENT_ENGINE_RESOURCE",
        bool(cost_resource),
        cost_resource or "missing",
    )

    health_url = os.environ.get("ORCHESTRATOR_HEALTH_URL", "http://127.0.0.1:8000/health").strip()
    try:
        health = _health(health_url)
        all_ok &= _check("orchestrator health reachable", True, health_url)
        all_ok &= _check(
            "agent_engine_chat_enabled",
            bool(health.get("agent_engine_chat_enabled")),
            str(health.get("agent_engine_chat_enabled")),
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        all_ok &= _check("orchestrator health reachable", False, f"{health_url} ({e})")

    print("")
    if all_ok:
        print("All prerequisite checks passed.")
        print("Next: chat via local UI and inspect Agent Engine > Evaluation > Online Monitors.")
    else:
        print("One or more prerequisite checks failed.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
