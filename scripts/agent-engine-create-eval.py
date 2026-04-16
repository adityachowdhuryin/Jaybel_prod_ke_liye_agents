#!/usr/bin/env python3
"""Prepare and run a lightweight Agent Engine evaluation harness.

Note: Vertex console currently indicates evaluation creation is primarily via SDK/Colab.
This script runs a deterministic prompt suite against an engine and writes a JSON report
that can be used as a baseline and attached to eval workflows.
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import vertexai
import vertexai.agent_engines as agent_engines


def load_cases(cases_path: str | None) -> list[dict]:
    if not cases_path:
        return [
            {"prompt": "List all unique services used till now.", "expected_mode": "answer"},
            {"prompt": "What are the 3 most expensive services till date?", "expected_mode": "answer"},
            {"prompt": "What was total spend in march and april combined till now?", "expected_mode": "answer"},
        ]
    raw = Path(cases_path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise SystemExit("--cases must point to a JSON array")
    return data


def extract_text(event: dict) -> str:
    content = event.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for p in parts:
        if isinstance(p, dict) and p.get("text"):
            out.append(str(p["text"]))
    return "\n".join(out).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource", required=True, help="projects/.../reasoningEngines/ID")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
    parser.add_argument("--out", default="logs/agent-engine-eval-report.json")
    parser.add_argument(
        "--cases",
        default="scripts/evals/hallucination_guardrail_cases.json",
        help="Path to JSON eval cases",
    )
    args = parser.parse_args()

    if not args.project:
        raise SystemExit("Set --project or GOOGLE_CLOUD_PROJECT.")

    cases = load_cases(args.cases)

    vertexai.init(project=args.project, location=args.location)
    engine = agent_engines.get(args.resource)

    rows: list[dict] = []
    for case in cases:
        prompt = str(case.get("prompt") or "").strip()
        if not prompt:
            continue
        user_id = f"eval-{uuid.uuid4().hex[:8]}"
        sess = engine.create_session(user_id=user_id)
        session_id = sess.get("id")
        if not session_id:
            raise SystemExit("create_session failed for eval run")
        chunks: list[str] = []
        for ev in engine.stream_query(message=prompt, user_id=user_id, session_id=session_id):
            text = extract_text(ev)
            if text:
                chunks.append(text)
        rows.append(
            {
                "prompt": prompt,
                "expected_mode": case.get("expected_mode"),
                "must_contain_any": case.get("must_contain_any", []),
                "must_not_contain_any": case.get("must_not_contain_any", []),
                "user_id": user_id,
                "session_id": session_id,
                "response": "\n".join(chunks).strip(),
            }
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": args.project,
        "location": args.location,
        "resource": args.resource,
        "cases": rows,
        "note": (
            "This harness records baseline responses and sessions. "
            "Use these cases in your Colab/SDK evaluation workflow to create console evaluation runs."
        ),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote evaluation baseline report: {out}")
    print("You can now use these cases in your Vertex evaluation notebook/SDK flow.")


if __name__ == "__main__":
    main()
