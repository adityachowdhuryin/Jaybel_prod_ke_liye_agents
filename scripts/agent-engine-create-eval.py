#!/usr/bin/env python3
"""Create Agent Engine eval baselines and optional Vertex evaluation runs."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import vertexai
import vertexai.agent_engines as agent_engines
from google.genai import types as genai_types
from vertexai import Client, types


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


def parse_labels(pairs: list[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"Invalid --label '{pair}'. Use key=value format.")
        key, value = pair.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise SystemExit(f"Invalid --label '{pair}'. Empty key/value is not allowed.")
        labels[key] = value
    return labels


def _safe_metric(name: str):
    metric = getattr(types.RubricMetric, name, None)
    if metric is None:
        raise SystemExit(f"SDK missing RubricMetric.{name}; upgrade google-cloud-aiplatform[evaluation].")
    return metric


def default_metrics() -> list:
    return [
        _safe_metric("FINAL_RESPONSE_QUALITY"),
        _safe_metric("TOOL_USE_QUALITY"),
        _safe_metric("HALLUCINATION"),
        _safe_metric("SAFETY"),
    ]


def build_eval_dataset(cases: list[dict]) -> pd.DataFrame:
    prompts: list[str] = []
    session_inputs: list = []
    for case in cases:
        prompt = str(case.get("prompt") or "").strip()
        if not prompt:
            continue
        prompts.append(prompt)
        session_inputs.append(types.evals.SessionInput(user_id=f"eval-ds-{uuid.uuid4().hex[:8]}", state={}))
    if not prompts:
        raise SystemExit("No non-empty prompts found in cases file.")
    return pd.DataFrame({"prompt": prompts, "session_inputs": session_inputs})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource", required=True, help="projects/.../reasoningEngines/ID")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
    parser.add_argument("--out", default="logs/agent-engine-eval-report.json")
    parser.add_argument(
        "--cases",
        default="scripts/evals/agent_engine_eval_cases.json",
        help="Path to JSON eval cases",
    )
    parser.add_argument(
        "--publish-to-vertex",
        action="store_true",
        help="Create a Vertex evaluation run (shows under Evaluation tab)",
    )
    parser.add_argument("--gcs-dest", help="gs://... destination for Vertex evaluation artifacts")
    parser.add_argument("--display-name", help="Display name for the Vertex evaluation run")
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Label for eval run as key=value (repeatable)",
    )
    args = parser.parse_args()

    if not args.project:
        raise SystemExit("Set --project or GOOGLE_CLOUD_PROJECT.")
    if args.publish_to_vertex and not args.gcs_dest:
        raise SystemExit("--gcs-dest is required when --publish-to-vertex is set.")

    cases = load_cases(args.cases)
    labels = parse_labels(args.label)

    vertexai.init(project=args.project, location=args.location)
    engine = agent_engines.get(args.resource)
    eval_client = Client(
        project=args.project,
        location=args.location,
        http_options=genai_types.HttpOptions(api_version="v1beta1"),
    )

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

    eval_run_name: str | None = None
    if args.publish_to_vertex:
        dataset = build_eval_dataset(cases)
        inferred_dataset = eval_client.evals.run_inference(agent=args.resource, src=dataset)
        eval_run = eval_client.evals.create_evaluation_run(
            dataset=inferred_dataset,
            agent=args.resource,
            metrics=default_metrics(),
            dest=args.gcs_dest,
            display_name=args.display_name,
            labels=labels if labels else None,
        )
        eval_run_name = eval_run.name

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": args.project,
        "location": args.location,
        "resource": args.resource,
        "vertex_eval_published": bool(args.publish_to_vertex),
        "vertex_eval_run_name": eval_run_name,
        "vertex_eval_display_name": args.display_name,
        "vertex_eval_labels": labels,
        "vertex_eval_gcs_dest": args.gcs_dest,
        "cases": rows,
        "note": "This harness records baseline responses and can publish a Vertex evaluation run.",
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote evaluation baseline report: {out}")
    if eval_run_name:
        print(f"Created Vertex evaluation run: {eval_run_name}")
        print("Check the Agent Engine Evaluation tab after the run is processed.")
    else:
        print("Baseline-only mode. Re-run with --publish-to-vertex and --gcs-dest to populate Evaluation tab.")


if __name__ == "__main__":
    main()
