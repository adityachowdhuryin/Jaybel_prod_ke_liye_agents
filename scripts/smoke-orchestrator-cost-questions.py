#!/usr/bin/env python3
"""
POST a few cost questions to the local orchestrator /chat/stream and sanity-check replies.

Prerequisites:
  - Orchestrator on ORCHESTRATOR_URL (default http://127.0.0.1:8000)
  - ORCHESTRATOR_AGENT_ENGINE_RESOURCE set for the orchestrator process
  - ORCHESTRATOR_AUTH_DISABLED=1 for dev (no JWT), or pass Authorization Bearer
  - ADC / Vertex reachable if using Agent Engine chat

Usage (from repo root, with venv that has orchestrator deps):
  source config/gcp.env 2>/dev/null || true
  export ORCHESTRATOR_AUTH_DISABLED=1
  python scripts/smoke-orchestrator-cost-questions.py

Exit code 0 if all checks pass, 1 otherwise.

If the orchestrator returns 503 (e.g. Agent Engine not configured), or the TCP
connection fails (orchestrator not listening), set
  SMOKE_SKIP_IF_UNAVAILABLE=1
to exit 0 after printing a skip message (useful before local stack is up).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
ORCH = str(ROOT / "agents" / "orchestrator")
if ORCH not in sys.path:
    sys.path.insert(0, ORCH)

from intelligence import parse_sse_bytes_to_text  # noqa: E402


def _post_chat(base_url: str, message: str, session_id: str | None = None) -> bytes:
    url = f"{base_url.rstrip('/')}/chat/stream"
    body: dict = {"message": message}
    if session_id:
        body["session_id"] = session_id
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    token = os.environ.get("SMOKE_ORCHESTRATOR_BEARER", "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--base-url",
        default=os.environ.get("ORCHESTRATOR_URL", "http://127.0.0.1:8000"),
        help="Orchestrator base URL",
    )
    args = ap.parse_args()
    base = args.base_url

    cases: list[tuple[str, str, list[tuple[str, Callable[[str], bool]]]]] = [
        (
            "total_our_project",
            "What has been the total cost for our project till now?",
            [
                (
                    "no_billing_project_id_clarification_wording",
                    lambda t: "Which billing project ID" not in t
                    and "billing_project_id" not in t.lower(),
                ),
            ],
        ),
        (
            "service_breakdown_all",
            "Give me a service-wise cost breakdown for all services till now.",
            [
                (
                    "no_top_n_clarification",
                    lambda t: "How many results should I return" not in t
                    and '"clarification_kind": "top_n"' not in t,
                ),
            ],
        ),
        (
            "billing_month_by_service",
            "What is the total GCP spend by service for project jaybel-prod this month?",
            [
                ("non_empty", lambda t: len(t.strip()) > 40),
                (
                    "billing_or_clarification",
                    lambda t: any(
                        w in t.lower()
                        for w in ("inr", "service", "billing", "sku", "cost", "spend", "gcp")
                    )
                    or "COST_PAYLOAD_JSON" in t,
                ),
            ],
        ),
        (
            "ambiguous_usage",
            "How much did we spend on trace demo-trace-2 last week?",
            [
                ("non_empty", lambda t: len(t.strip()) > 20),
            ],
        ),
        (
            "workflow_token_intent",
            "What was the total input token usage in our workflow last 7 days?",
            [
                ("non_empty", lambda t: len(t.strip()) > 30),
                (
                    "no_raw_json_object_dump",
                    lambda t: not (t.strip().startswith("{") and '"trace_id"' in t[:500]),
                ),
            ],
        ),
    ]

    failed = 0
    sid = None
    for case_id, message, checks in cases:
        print(f"\n=== Case: {case_id} ===\nQ: {message!r}")
        try:
            raw = _post_chat(base, message, session_id=sid)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:800]
            print(f"HTTP {e.code}: {body}")
            if e.code in (502, 503, 504) and os.environ.get(
                "SMOKE_SKIP_IF_UNAVAILABLE", ""
            ).strip().lower() in ("1", "true", "yes"):
                print(
                    "SKIP: service unavailable (set ORCHESTRATOR_AGENT_ENGINE_RESOURCE, ADC, "
                    "and start orchestrator). SMOKE_SKIP_IF_UNAVAILABLE=1 so exit code is 0."
                )
                return 0
            failed += 1
            continue
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            print(f"Request failed: {e}")
            if os.environ.get("SMOKE_SKIP_IF_UNAVAILABLE", "").strip().lower() in (
                "1",
                "true",
                "yes",
            ):
                print(
                    "SKIP: orchestrator unreachable or network error. "
                    "Start orchestrator on ORCHESTRATOR_URL and set Agent Engine env; "
                    "SMOKE_SKIP_IF_UNAVAILABLE=1 so exit code is 0."
                )
                return 0
            failed += 1
            continue
        except Exception as e:
            print(f"Request failed: {e}")
            failed += 1
            continue
        text = parse_sse_bytes_to_text(raw)
        print(f"Assistant chars: {len(text)}")
        snippet = text.strip()[:400].replace("\n", " ")
        if snippet:
            print(f"Snippet: {snippet}…")

        for check_id, pred in checks:
            try:
                ok = bool(pred(text))
            except Exception:
                ok = False
            if not ok:
                print(f"  FAIL: {check_id}")
                failed += 1
            else:
                print(f"  ok: {check_id}")

        # keep session for follow-ups if backend returns X-Session-Id (not parsed here); optional

    if failed:
        print(f"\n{failed} check(s) failed.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
