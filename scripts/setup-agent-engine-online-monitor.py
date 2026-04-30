#!/usr/bin/env python3
"""Create or update one Agent Engine online monitor for cost agent traffic.

This script configures a single online evaluator (online monitor) for the
cost_metrics_agent deployment and binds four rubric metrics:
  - HALLUCINATION
  - FINAL_RESPONSE_QUALITY
  - TOOL_USE_QUALITY
  - SAFETY

The script is idempotent:
- If a monitor with the same display name already exists under the location,
  it updates that monitor.
- Otherwise it creates a new one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any

import google.auth
from google.auth.transport.requests import AuthorizedSession

DEFAULT_METRICS = [
    "HALLUCINATION",
    "FINAL_RESPONSE_QUALITY",
    "TOOL_USE_QUALITY",
    "SAFETY",
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    p.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", ""))
    p.add_argument(
        "--resource",
        default=os.environ.get("COST_AGENT_ENGINE_RESOURCE", ""),
        help="projects/.../locations/.../reasoningEngines/...",
    )
    p.add_argument("--display-name", default=os.environ.get("ONLINE_MONITOR_DISPLAY_NAME", "cost-agent-online-monitor"))
    p.add_argument(
        "--sampling-rate",
        type=int,
        default=int(os.environ.get("ONLINE_MONITOR_SAMPLING_RATE", "50")),
        help="Sampling percentage integer [1..100].",
    )
    p.add_argument(
        "--metrics",
        nargs="+",
        default=DEFAULT_METRICS,
        help="Rubric metric names (default: HALLUCINATION FINAL_RESPONSE_QUALITY TOOL_USE_QUALITY SAFETY).",
    )
    p.add_argument(
        "--max-evaluated-samples-per-run",
        type=int,
        default=int(os.environ.get("ONLINE_MONITOR_MAX_SAMPLES_PER_RUN", "200")),
        help="Optional run cap to control costs.",
    )
    p.add_argument(
        "--allow-non-cost-resource",
        action="store_true",
        help="Disable guard that the resource must equal COST_AGENT_ENGINE_RESOURCE.",
    )
    return p.parse_args()


def _parse_resource(resource: str) -> tuple[str, str]:
    m = re.match(r"^projects/([^/]+)/locations/([^/]+)/reasoningEngines/[^/]+$", resource)
    if not m:
        raise SystemExit("--resource must be projects/<id-or-number>/locations/<region>/reasoningEngines/<id>.")
    return m.group(1), m.group(2)


def _validate_resource(resource: str, *, expected_cost_resource: str, allow_non_cost_resource: bool) -> None:
    if not resource or not resource.startswith("projects/") or "/reasoningEngines/" not in resource:
        raise SystemExit("--resource must be a full reasoning engine resource.")
    if not allow_non_cost_resource and expected_cost_resource and resource != expected_cost_resource:
        raise SystemExit(
            "Refusing to configure monitor on non-cost resource. "
            "Pass --allow-non-cost-resource to override."
        )


def _metric_sources(metric_names: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in metric_names:
        key = name.strip().upper()
        if key not in DEFAULT_METRICS:
            raise SystemExit(f"Unsupported metric '{name}'. Supported: {', '.join(DEFAULT_METRICS)}")
        # OnlineEvaluator accepts MetricSource.metric with predefinedMetricSpec.
        out.append({"metric": {"predefinedMetricSpec": {"metricSpecName": key}}})
    return out


def _auth_session() -> AuthorizedSession:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(creds)


def _list_online_evaluators(sess: AuthorizedSession, *, project: str, location: str) -> list[dict[str, Any]]:
    url = f"https://aiplatform.googleapis.com/v1beta1/projects/{project}/locations/{location}/onlineEvaluators"
    items: list[dict[str, Any]] = []
    page_token = ""
    while True:
        q = f"?pageToken={page_token}" if page_token else ""
        r = sess.get(url + q, timeout=60)
        r.raise_for_status()
        data = r.json()
        items.extend(data.get("onlineEvaluators", []))
        page_token = str(data.get("nextPageToken") or "")
        if not page_token:
            break
    return items


def _wait_op(sess: AuthorizedSession, op_name: str, *, timeout_s: int = 600) -> dict[str, Any]:
    import time

    url = f"https://aiplatform.googleapis.com/v1beta1/{op_name}"
    end = time.time() + timeout_s
    while True:
        r = sess.get(url, timeout=60)
        r.raise_for_status()
        data = r.json()
        if data.get("done"):
            if "error" in data:
                raise SystemExit(f"Operation failed: {json.dumps(data['error'], ensure_ascii=False)}")
            return data.get("response", {})
        if time.time() > end:
            raise SystemExit(f"Timed out waiting for operation: {op_name}")
        time.sleep(3)


def _create_or_patch_online_evaluator(
    sess: AuthorizedSession,
    *,
    project: str,
    location: str,
    existing_name: str | None,
    payload: dict[str, Any],
) -> str:
    if existing_name:
        # Use proto field paths for updateMask.
        url = (
            f"https://aiplatform.googleapis.com/v1beta1/{existing_name}"
            "?updateMask=displayName,agentResource,metricSources,config"
        )
        r = sess.patch(url, json=payload, timeout=120)
        r.raise_for_status()
        op = r.json()
        _wait_op(sess, op["name"])
        return existing_name

    parent = f"projects/{project}/locations/{location}"
    url = f"https://aiplatform.googleapis.com/v1beta1/{parent}/onlineEvaluators"
    r = sess.post(url, json=payload, timeout=120)
    r.raise_for_status()
    op = r.json()
    resp = _wait_op(sess, op["name"])
    return str(resp.get("name") or "")


def _activate_if_needed(sess: AuthorizedSession, name: str) -> None:
    if not name:
        return
    get_url = f"https://aiplatform.googleapis.com/v1beta1/{name}"
    r = sess.get(get_url, timeout=60)
    r.raise_for_status()
    state = str((r.json() or {}).get("state") or "").upper()
    if state == "ONLINE_EVALUATOR_STATE_ACTIVE":
        return
    act_url = f"https://aiplatform.googleapis.com/v1beta1/{name}:activate"
    ar = sess.post(act_url, json={}, timeout=120)
    ar.raise_for_status()
    op = ar.json()
    _wait_op(sess, op["name"])


def main() -> None:
    args = _parse_args()
    if not (1 <= args.sampling_rate <= 100):
        raise SystemExit("--sampling-rate must be between 1 and 100.")
    if args.max_evaluated_samples_per_run <= 0:
        raise SystemExit("--max-evaluated-samples-per-run must be > 0.")

    expected_cost_resource = os.environ.get("COST_AGENT_ENGINE_RESOURCE", "").strip()
    _validate_resource(
        args.resource.strip(),
        expected_cost_resource=expected_cost_resource,
        allow_non_cost_resource=bool(args.allow_non_cost_resource),
    )
    resource_project, resource_location = _parse_resource(args.resource.strip())
    project = args.project.strip() or resource_project
    location = args.location.strip() or resource_location

    metric_sources = _metric_sources(args.metrics)

    payload = {
        "displayName": args.display_name,
        "agentResource": args.resource.strip(),
        "metricSources": metric_sources,
        "config": {
            "randomSampling": {"percentage": int(args.sampling_rate)},
            "maxEvaluatedSamplesPerRun": str(int(args.max_evaluated_samples_per_run)),
        },
    }

    sess = _auth_session()
    existing = _list_online_evaluators(sess, project=project, location=location)
    existing_name = None
    for e in existing:
        if str(e.get("displayName") or "") == args.display_name:
            existing_name = str(e.get("name") or "")
            break

    name = _create_or_patch_online_evaluator(
        sess,
        project=project,
        location=location,
        existing_name=existing_name,
        payload=payload,
    )
    _activate_if_needed(sess, name)
    print("Online monitor configured:")
    print(f"  name: {name}")
    print(f"  agent_resource: {args.resource.strip()}")
    print(f"  project: {project}")
    print(f"  location: {location}")
    print(f"  sampling_rate_percent: {args.sampling_rate}")
    print(f"  metrics: {', '.join(args.metrics)}")


if __name__ == "__main__":
    main()
