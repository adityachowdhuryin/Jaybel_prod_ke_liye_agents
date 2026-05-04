#!/usr/bin/env python3
"""Copy online-monitor evaluation scores from Cloud Trace into Firestore.

Online monitors attach rubric scores to trace spans (see Agent Platform docs).
This script lists traces matching your monitor, parses known metric labels from
all spans, and upserts one Firestore document per trace_id.

Prerequisites:
  - ADC with cloud-platform (e.g. gcloud auth application-default login)
  - roles/cloudtrace.user (or Editor) and Firestore write access on the project
  - Full online evaluator resource name (Console → copy), e.g.
      projects/PROJ/locations/us-central1/onlineEvaluators/4116571534393344000
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import google.auth
from google.api_core import exceptions as gcp_exceptions
from google.auth.transport.requests import AuthorizedSession
from google.cloud import firestore

DEFAULT_METRICS = (
    "HALLUCINATION",
    "FINAL_RESPONSE_QUALITY",
    "TOOL_USE_QUALITY",
    "SAFETY",
)

# Cloud Trace list filter: span label key used for online evaluator binding
# (matches Logs Explorer resource.labels.online_evaluator from Google troubleshoot docs).
_TRACE_EVALUATOR_LABEL = "online_evaluator"

_SYNC_COLLECTION = "online_eval_firestore_sync"
_SYNC_DOC_ID = "cost_agent_cursor"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""), help="GCP project id")
    p.add_argument(
        "--online-evaluator",
        default=os.environ.get("ONLINE_EVALUATOR_RESOURCE", "").strip(),
        help="Full onlineEvaluator resource name (or set ONLINE_EVALUATOR_RESOURCE).",
    )
    p.add_argument(
        "--trace-filter",
        default=os.environ.get("ONLINE_EVAL_TRACE_FILTER", "").strip(),
        help="Override Cloud Trace list filter (if set, --online-evaluator filter is not used).",
    )
    p.add_argument(
        "--agent-resource",
        default=os.environ.get("COST_AGENT_ENGINE_RESOURCE", "").strip(),
        help="Optional reasoning engine resource for metadata only.",
    )
    p.add_argument(
        "--collection",
        default=os.environ.get("ONLINE_EVAL_FIRESTORE_COLLECTION", "cost_agent_online_eval_traces"),
        help="Firestore collection for per-trace documents.",
    )
    p.add_argument(
        "--firestore-database",
        default=os.environ.get("FIRESTORE_DATABASE_ID", "").strip() or None,
        help="Firestore database id (omit for default database).",
    )
    p.add_argument("--lookback-minutes", type=int, default=180, help="First-run window if no cursor exists.")
    p.add_argument("--overlap-minutes", type=int, default=45, help="Re-query this much before last window end.")
    p.add_argument("--max-traces", type=int, default=200, help="Stop after persisting this many new traces.")
    p.add_argument("--page-size", type=int, default=50, help="Cloud Trace list page size (<=100 recommended).")
    p.add_argument("--dry-run", action="store_true", help="List and parse only; do not write Firestore.")
    p.add_argument(
        "--dump-labels-trace-id",
        metavar="TRACE_ID",
        help="Fetch one trace by id and print all span labels (debug filter / metric keys).",
    )
    p.add_argument(
        "--scan-without-list-filter",
        action="store_true",
        help=(
            "Omit Cloud Trace list `filter` and post-filter traces whose spans mention "
            "--online-evaluator (any label value contains the resource or /onlineEvaluators/ID). "
            "Use when +online_evaluator:\"...\" returns no rows but Console still shows traces."
        ),
    )
    p.add_argument(
        "--scan-max-list-traces",
        type=int,
        default=500,
        help="With --scan-without-list-filter, stop after examining this many traces (pagination).",
    )
    return p.parse_args()


def _auth_session() -> AuthorizedSession:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(creds)


def _default_trace_filter(online_evaluator_resource: str) -> str:
    # Exact label match; value is full resource name (requires quoting per Trace filter rules).
    escaped = online_evaluator_resource.replace("\\", "\\\\").replace('"', '\\"')
    return f'+{_TRACE_EVALUATOR_LABEL}:"{escaped}"'


def _list_traces(
    sess: AuthorizedSession,
    *,
    project_id: str,
    start_time: datetime,
    end_time: datetime,
    trace_filter: str | None,
    page_size: int,
    page_token: str | None,
) -> dict[str, Any]:
    url = f"https://cloudtrace.googleapis.com/v1/projects/{project_id}/traces"
    params: dict[str, Any] = {
        "startTime": start_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "endTime": end_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "pageSize": min(max(page_size, 1), 100),
        "orderBy": "start desc",
        "view": "COMPLETE",
    }
    if trace_filter:
        params["filter"] = trace_filter
    if page_token:
        params["pageToken"] = page_token
    r = sess.get(url, params=params, timeout=120)
    r.raise_for_status()
    return r.json()


def _get_trace(sess: AuthorizedSession, *, project_id: str, trace_id: str) -> dict[str, Any]:
    url = f"https://cloudtrace.googleapis.com/v1/projects/{project_id}/traces/{trace_id}"
    r = sess.get(url, timeout=120)
    r.raise_for_status()
    return r.json()


def _metric_names_from_env() -> tuple[str, ...]:
    raw = os.environ.get("ONLINE_EVAL_METRIC_NAMES", "").strip()
    if not raw:
        return DEFAULT_METRICS
    parts = tuple(m.strip().upper() for m in raw.replace(",", " ").split() if m.strip())
    return parts or DEFAULT_METRICS


def _try_parse_score(value: str) -> float | str:
    v = value.strip()
    if not v:
        return v
    try:
        return float(v)
    except ValueError:
        pass
    if v.startswith("{") or v.startswith("["):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def _label_suggests_metric(key: str, metric: str) -> bool:
    k = key.upper()
    m = metric.upper()
    if k == m:
        return True
    if m in k and ("METRIC" in k or "SCORE" in k or "EVAL" in k or "RUBRIC" in k or "GEN_AI" in k):
        return True
    if key.endswith(f"/{metric}") or key.endswith(f".{metric}"):
        return True
    return False


def _online_evaluator_needles(full_resource: str) -> tuple[str, ...]:
    """Return substrings to match in span label values (scan mode)."""
    full = full_resource.strip()
    out: list[str] = []
    if full:
        out.append(full)
    m = re.search(r"/onlineEvaluators/(\d+)$", full)
    if m:
        out.append(f"/onlineEvaluators/{m.group(1)}")
        out.append(m.group(1))
    return tuple(dict.fromkeys(out))  # dedupe preserve order


def _trace_matches_online_evaluator(trace: dict[str, Any], evaluator_resource: str) -> bool:
    needles = _online_evaluator_needles(evaluator_resource)
    if not needles:
        return False
    for sp in trace.get("spans") or []:
        labels = sp.get("labels") or {}
        if not isinstance(labels, dict):
            continue
        ev = labels.get(_TRACE_EVALUATOR_LABEL)
        if isinstance(ev, str) and ev.strip() == evaluator_resource.strip():
            return True
        for _k, raw_val in labels.items():
            if not isinstance(raw_val, str):
                continue
            for n in needles:
                if n and n in raw_val:
                    return True
    return False


def _extract_evaluation_fields(trace: dict[str, Any], metric_names: tuple[str, ...]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    rationales: dict[str, str] = {}
    matched_keys: list[str] = []
    evaluator_from_span: str | None = None
    root_start: str | None = None
    root_end: str | None = None

    spans = trace.get("spans") or []
    # Heuristic root: span with no parentSpanId
    for sp in spans:
        if not sp.get("parentSpanId"):
            root_start = str(sp.get("startTime") or "")
            root_end = str(sp.get("endTime") or "")
            break

    for sp in spans:
        labels = sp.get("labels") or {}
        if not isinstance(labels, dict):
            continue
        if _TRACE_EVALUATOR_LABEL in labels and labels[_TRACE_EVALUATOR_LABEL]:
            evaluator_from_span = str(labels[_TRACE_EVALUATOR_LABEL])

        for key, raw_val in labels.items():
            if not isinstance(raw_val, str):
                continue
            key_l = key.lower()
            if "rationale" in key_l or "explanation" in key_l or "_reason" in key_l:
                for m in metric_names:
                    if m in key.upper():
                        rationales[m] = raw_val[:16000]
                        matched_keys.append(key)
                        break
                continue

            for m in metric_names:
                if _label_suggests_metric(key, m):
                    if m not in metrics:
                        metrics[m] = _try_parse_score(raw_val)
                        matched_keys.append(key)
                    break

    return {
        "metrics": metrics,
        "rationales": rationales,
        "matched_label_keys": sorted(set(matched_keys)),
        "online_evaluator_from_trace": evaluator_from_span,
        "root_span_start_time": root_start,
        "root_span_end_time": root_end,
    }


def _firestore_client(project: str, database_id: str | None) -> firestore.Client:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if database_id:
        return firestore.Client(project=project, credentials=creds, database=database_id)
    return firestore.Client(project=project, credentials=creds)


def _read_cursor(db: firestore.Client | None) -> datetime | None:
    if db is None:
        return None
    doc = db.collection(_SYNC_COLLECTION).document(_SYNC_DOC_ID).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    s = str(data.get("last_window_end") or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _write_cursor(db: firestore.Client | None, end_time: datetime) -> None:
    if db is None:
        return
    payload = {
        "last_window_end": end_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    db.collection(_SYNC_COLLECTION).document(_SYNC_DOC_ID).set(payload, merge=True)


def _dump_trace_labels(sess: AuthorizedSession, *, project_id: str, trace_id: str) -> None:
    trace = _get_trace(sess, project_id=project_id, trace_id=trace_id)
    print(json.dumps(trace, indent=2, ensure_ascii=False)[:200000])
    spans = trace.get("spans") or []
    print("\n--- span labels (key -> value prefix) ---", file=sys.stderr)
    for i, sp in enumerate(spans):
        labels = sp.get("labels") or {}
        print(f"span[{i}] name={sp.get('name')!r} id={sp.get('spanId')}", file=sys.stderr)
        for k, v in sorted(labels.items()):
            vv = v if len(v) <= 200 else v[:200] + "…"
            print(f"  {k}: {vv!r}", file=sys.stderr)


def main() -> None:
    args = _parse_args()
    project = args.project.strip()
    if not project:
        raise SystemExit("Set --project or GOOGLE_CLOUD_PROJECT.")

    sess = _auth_session()

    if args.dump_labels_trace_id:
        _dump_trace_labels(sess, project_id=project, trace_id=args.dump_labels_trace_id.strip())
        return

    ev = args.online_evaluator.strip()
    trace_filter: str | None
    if args.scan_without_list_filter:
        if not ev:
            raise SystemExit("--scan-without-list-filter requires --online-evaluator (or ONLINE_EVALUATOR_RESOURCE).")
        trace_filter = None
        if args.trace_filter:
            raise SystemExit("Use either --scan-without-list-filter or --trace-filter, not both.")
    else:
        trace_filter = args.trace_filter
        if not trace_filter:
            if not ev:
                raise SystemExit(
                    "Set --online-evaluator or ONLINE_EVALUATOR_RESOURCE, "
                    "or pass an explicit --trace-filter / ONLINE_EVAL_TRACE_FILTER."
                )
            trace_filter = _default_trace_filter(ev)

    end_time = datetime.now(timezone.utc)
    db: firestore.Client | None = None if args.dry_run else _firestore_client(project, args.firestore_database)
    try:
        cursor = _read_cursor(db)
    except gcp_exceptions.NotFound as exc:
        raise SystemExit(
            "Firestore has no database in this project (or wrong FIRESTORE_DATABASE_ID). "
            "Create a Native mode database, e.g.: "
            "gcloud firestore databases create --database='(default)' --location=us-central1 "
            "--type=firestore-native --project=YOUR_PROJECT_ID\n"
            f"Underlying error: {exc}"
        ) from exc
    if cursor is not None:
        start_time = cursor - timedelta(minutes=max(args.overlap_minutes, 0))
    else:
        start_time = end_time - timedelta(minutes=max(args.lookback_minutes, 1))

    metric_names = _metric_names_from_env()

    total_written = 0
    page_token: str | None = None
    examined = 0
    print(f"Querying Cloud Trace project={project}", file=sys.stderr)
    print(f"  window: {start_time.isoformat()} .. {end_time.isoformat()}", file=sys.stderr)
    print(f"  filter: {trace_filter!s}", file=sys.stderr)
    if args.scan_without_list_filter:
        print("  mode: scan-without-list-filter (post-filter by evaluator resource)", file=sys.stderr)

    while total_written < args.max_traces:
        data = _list_traces(
            sess,
            project_id=project,
            start_time=start_time,
            end_time=end_time,
            trace_filter=trace_filter,
            page_size=args.page_size,
            page_token=page_token,
        )
        traces = data.get("traces") or []
        page_token = str(data.get("nextPageToken") or "") or None
        if not traces:
            break

        for tr in traces:
            if total_written >= args.max_traces:
                break
            if args.scan_without_list_filter:
                examined += 1
                if examined > args.scan_max_list_traces:
                    break
                if not _trace_matches_online_evaluator(tr, ev):
                    continue
            trace_id = str(tr.get("traceId") or "").strip()
            if not trace_id:
                continue

            extracted = _extract_evaluation_fields(tr, metric_names)
            if not extracted["metrics"] and not extracted["rationales"]:
                # Still persist stub so we know trace matched filter but parser needs tuning.
                pass

            doc = {
                "trace_id": trace_id,
                "project_id": project,
                "online_evaluator_resource": args.online_evaluator.strip()
                or extracted.get("online_evaluator_from_trace"),
                "agent_resource": args.agent_resource.strip() or None,
                "metrics": extracted["metrics"],
                "metric_rationales": extracted["rationales"] or None,
                "matched_trace_label_keys": extracted["matched_label_keys"],
                "root_span_start_time": extracted["root_span_start_time"],
                "root_span_end_time": extracted["root_span_end_time"],
                "source": "cloud_trace_online_monitor",
            }
            if db is not None:
                doc["ingested_at"] = firestore.SERVER_TIMESTAMP
            doc = {k: v for k, v in doc.items() if v is not None}

            if args.dry_run:
                print(json.dumps({"trace_id": trace_id, "metrics": extracted["metrics"]}, ensure_ascii=False))
            else:
                assert db is not None
                db.collection(args.collection).document(trace_id).set(doc, merge=True)
                total_written += 1

        if args.scan_without_list_filter and examined >= args.scan_max_list_traces:
            print(f"  scan cap reached (examined={examined})", file=sys.stderr)
            break
        if not page_token:
            break

    _write_cursor(db, end_time)

    print(f"Done. Upserted {total_written} trace document(s) into {args.collection!r}.", file=sys.stderr)


if __name__ == "__main__":
    main()
