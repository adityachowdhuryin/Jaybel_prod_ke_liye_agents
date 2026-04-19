#!/usr/bin/env python3
"""Seed Agent Engine sessions/memories with reusable multi-turn scenarios."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import vertexai
import vertexai.agent_engines as agent_engines


def extract_text(event: dict) -> str:
    content = event.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("text"):
            out.append(str(p["text"]))
    return "\n".join(out).strip()


def _default_scenarios() -> list[dict]:
    return [
        {
            "name": "cost_preference_memory",
            "turns": [
                "Hello, remember I care about spend by project and service.",
                "What are my top services this month?",
                "Now focus only on invoice-like cost categories and summarize.",
            ],
        },
        {
            "name": "schema_and_followup_memory",
            "turns": [
                "List all columns available in the billing view.",
                "Now tell me if project_name exists.",
                "Great. Keep project_name in context for future follow-up questions.",
            ],
        },
    ]


def load_scenarios(path: str | None) -> list[dict]:
    if not path:
        return _default_scenarios()
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise SystemExit("--scenarios must point to a JSON array")
    return data


def _resource_short_name(resource: str) -> str:
    return resource.rstrip("/").split("/")[-1]


def _run_scenario(engine, resource: str, scenario: dict) -> dict:
    turns = scenario.get("turns")
    if not isinstance(turns, list) or not turns:
        raise SystemExit(f"Scenario '{scenario.get('name', 'unnamed')}' has no turns")
    user_id = f"memory-smoke-{uuid.uuid4().hex[:8]}"
    sess = engine.create_session(user_id=user_id)
    session_id = sess.get("id")
    if not session_id:
        raise SystemExit("Agent Engine did not return session id.")

    print(f"\nresource={resource}")
    print(f"scenario={scenario.get('name', 'unnamed')}")
    print(f"user_id={user_id}")
    print(f"session_id={session_id}")

    turn_rows: list[dict] = []
    for i, prompt in enumerate(turns, start=1):
        prompt_text = str(prompt).strip()
        if not prompt_text:
            continue
        chunks: list[str] = []
        for ev in engine.stream_query(message=prompt_text, user_id=user_id, session_id=session_id):
            text = extract_text(ev)
            if text:
                chunks.append(text)
        joined = "\n".join(chunks).strip()
        preview = joined[:500] + ("..." if len(joined) > 500 else "")
        print(f"\n[{i}] prompt: {prompt_text}\n[{i}] response preview:\n{preview}\n")
        turn_rows.append({"turn_index": i, "prompt": prompt_text, "response": joined})

    return {
        "resource": resource,
        "resource_short_name": _resource_short_name(resource),
        "scenario_name": scenario.get("name", "unnamed"),
        "user_id": user_id,
        "session_id": session_id,
        "turns": turn_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--resource",
        action="append",
        default=[],
        help="projects/.../reasoningEngines/ID (repeat flag for multiple engines)",
    )
    parser.add_argument("--resources-file", help="JSON file containing an array of engine resources")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
    parser.add_argument(
        "--scenarios",
        default="scripts/evals/memory_seed_cases.json",
        help="Path to JSON scenario list (default: scripts/evals/memory_seed_cases.json)",
    )
    parser.add_argument(
        "--out",
        default="logs/agent-engine-memory-seed-report.json",
        help="Where to write memory seeding report JSON",
    )
    args = parser.parse_args()

    if not args.project:
        raise SystemExit("Set --project or GOOGLE_CLOUD_PROJECT.")

    resources = list(args.resource)
    if args.resources_file:
        raw = Path(args.resources_file).read_text(encoding="utf-8")
        file_resources = json.loads(raw)
        if not isinstance(file_resources, list):
            raise SystemExit("--resources-file must contain a JSON array")
        resources.extend(str(x).strip() for x in file_resources if str(x).strip())
    if not resources:
        raise SystemExit("Provide at least one --resource (or --resources-file).")

    scenarios = load_scenarios(args.scenarios)
    vertexai.init(project=args.project, location=args.location)
    rows: list[dict] = []
    for resource in resources:
        engine = agent_engines.get(resource)
        for scenario in scenarios:
            rows.append(_run_scenario(engine=engine, resource=resource, scenario=scenario))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": args.project,
        "location": args.location,
        "resources": resources,
        "scenario_count": len(scenarios),
        "runs": rows,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Done. Wrote memory seeding report: {out}")
    print("Check Agent Engine Sessions/Traces/Memories tabs for each engine.")


if __name__ == "__main__":
    main()
