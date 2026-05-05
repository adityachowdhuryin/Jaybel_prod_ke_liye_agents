#!/usr/bin/env python3
"""Copy online-monitor evaluation scores from Cloud Trace into Firestore.

Online monitors attach rubric scores to trace spans in the **Agent Platform UI**, but
Cloud Trace HTTP `get`/`list` responses often **do not** include rubric keys on span
labels. Scores are also aggregated in Cloud Monitoring (`online_evaluator/scores`)
without a per-trace label there. For reliable `metrics` in Firestore, use
**`--metrics-overrides`** (JSON from Console Evaluation tab) or **`--apply-metrics-overrides-only`**.

This script lists traces, parses any metric-like labels it finds on spans, and upserts
one Firestore document per trace_id.

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
from pathlib import Path
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
    p.add_argument(
        "--start-time",
        default="",
        help="RFC3339 UTC window start (inclusive), e.g. 2026-04-30T00:00:00Z. Requires --end-time; ignores Firestore cursor.",
    )
    p.add_argument(
        "--end-time",
        default="",
        help="RFC3339 UTC window end (inclusive), e.g. 2026-05-01T00:00:00Z. Requires --start-time.",
    )
    p.add_argument(
        "--update-cursor-after-backfill",
        action="store_true",
        help="When using --start-time/--end-time, still write the sync cursor (default: skip, for one-shot backfills).",
    )
    p.add_argument(
        "--scan-gen-ai-agent-name",
        default=os.environ.get("ONLINE_EVAL_SCAN_GEN_AI_AGENT_NAME", "").strip(),
        help=(
            "With --scan-without-list-filter, also keep traces whose spans include "
            "label gen_ai.agent.name equal to this value (e.g. cost_metrics_agent). "
            "Use when online_evaluator labels are absent from exported spans but Agent Platform Traces UI still shows monitored runs."
        ),
    )
    p.add_argument(
        "--trace-ids",
        default="",
        help="Comma-separated Cloud Trace IDs to fetch directly (GET), write Firestore, then exit (no list crawl). Useful for pinpoint backfill.",
    )
    p.add_argument(
        "--metrics-overrides",
        default=os.environ.get("ONLINE_EVAL_METRICS_OVERRIDES_PATH", "").strip(),
        metavar="PATH",
        help="JSON file: trace_id -> { \"metrics\": {...}, \"provenance\": str, \"metrics_vertex_names\": {...} }. Merged into each document (Trace-exported metrics stay empty without this).",
    )
    p.add_argument(
        "--apply-metrics-overrides-only",
        action="store_true",
        help="Only merge --metrics-overrides into existing Firestore docs by trace_id (no Cloud Trace calls).",
    )
    p.add_argument(
        "--trace-ids-file",
        default="",
        metavar="PATH",
        help="Newline-separated trace IDs (optional # comments). Appended to --trace-ids for direct GET ingest.",
    )
    p.add_argument(
        "--evaluated-trace-allowlist-file",
        default=os.environ.get("ONLINE_EVAL_TRACE_ALLOWLIST_FILE", "").strip(),
        metavar="PATH",
        help=(
            "List crawl only (ignored with --trace-ids / --trace-ids-file): only upsert trace_ids "
            "present in this file (one hex id per line); copy from Agent Platform Traces with your "
            "online monitor filter active. Or set ONLINE_EVAL_TRACE_ALLOWLIST_FILE."
        ),
    )
    p.add_argument(
        "--include-non-evaluated-agent-traces",
        action="store_true",
        help=(
            "With --scan-without-list-filter + --scan-gen-ai-agent-name, also write traces that have no "
            "online_evaluator span label and no rubric labels (legacy behavior). Default is to skip those."
        ),
    )
    p.add_argument(
        "--prune-firestore-except-allowlist-file",
        default="",
        metavar="PATH",
        help=(
            "Delete Firestore documents in --collection whose document ID is not listed in this file "
            "(same newline format as evaluated-trace-allowlist). Use --dry-run to print IDs that would be removed."
        ),
    )
    return p.parse_args()


_RESERVED_METRICS_OVERRIDE_KEYS = frozenset(
    {"metrics", "provenance", "metrics_vertex_names", "metric_rationales", "metrics_note"}
)


def _load_trace_ids_file(path: str) -> list[str]:
    raw = Path(path.strip()).read_text(encoding="utf-8")
    out: list[str] = []
    seen: set[str] = set()
    for ln in raw.splitlines():
        t = ln.split("#", 1)[0].strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _load_trace_id_allowlist(path: str) -> set[str]:
    return set(_load_trace_ids_file(path))


def _should_persist_list_crawl_trace(
    trace_id: str,
    tr: dict[str, Any],
    evaluator_resource: str,
    extracted: dict[str, Any],
    overrides: dict[str, Any],
    *,
    include_non_evaluated_agent_traces: bool,
) -> bool:
    """Skip gen_ai-only traces unless evaluator/metrics/overrides match (see --include-non-evaluated-agent-traces)."""
    if include_non_evaluated_agent_traces:
        return True
    if trace_id in overrides:
        return True
    if evaluator_resource.strip() and _trace_matches_online_evaluator(tr, evaluator_resource):
        return True
    if extracted.get("metrics") or extracted.get("rationales"):
        return True
    return False


def _load_metrics_overrides(path: str) -> dict[str, Any]:
    p = path.strip()
    if not p:
        return {}
    raw = json.loads(Path(p).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("--metrics-overrides file must be a JSON object mapping trace_id -> overrides")
    return raw


def _merge_metrics_overrides_into_doc(trace_id: str, doc: dict[str, Any], overrides: dict[str, Any]) -> None:
    raw = overrides.get(trace_id)
    if not raw or not isinstance(raw, dict):
        return
    existing = doc.get("metrics")
    base: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    if "metrics" in raw and isinstance(raw["metrics"], dict):
        doc["metrics"] = {**base, **raw["metrics"]}
    else:
        from_flat = {k: v for k, v in raw.items() if k not in _RESERVED_METRICS_OVERRIDE_KEYS}
        if from_flat:
            doc["metrics"] = {**base, **from_flat}
        elif base:
            doc["metrics"] = base
    if raw.get("provenance"):
        doc["metrics_provenance"] = str(raw["provenance"])
    if isinstance(raw.get("metrics_vertex_names"), dict):
        doc["metrics_vertex_names"] = raw["metrics_vertex_names"]
    if raw.get("metrics_note"):
        doc["metrics_note"] = str(raw["metrics_note"])
    if isinstance(raw.get("metric_rationales"), dict):
        doc["metric_rationales"] = raw["metric_rationales"]


def _parse_rfc3339_utc(s: str) -> datetime:
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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


def _trace_has_gen_ai_agent_name(trace: dict[str, Any], agent_name: str) -> bool:
    want = agent_name.strip()
    if not want:
        return False
    for sp in trace.get("spans") or []:
        labels = sp.get("labels") or {}
        if not isinstance(labels, dict):
            continue
        v = labels.get("gen_ai.agent.name")
        if isinstance(v, str) and v.strip() == want:
            return True
    return False


def _trace_matches_scan_postfilter(
    trace: dict[str, Any],
    evaluator_resource: str,
    *,
    gen_ai_agent_name: str | None,
) -> bool:
    if evaluator_resource.strip() and _trace_matches_online_evaluator(trace, evaluator_resource):
        return True
    if gen_ai_agent_name and _trace_has_gen_ai_agent_name(trace, gen_ai_agent_name):
        return True
    return False


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

    overrides = _load_metrics_overrides(args.metrics_overrides) if args.metrics_overrides.strip() else {}

    if args.apply_metrics_overrides_only:
        if not overrides:
            raise SystemExit("--apply-metrics-overrides-only requires --metrics-overrides PATH with a JSON object.")
        db = _firestore_client(project, args.firestore_database)
        n = 0
        for trace_id in overrides:
            doc: dict[str, Any] = {
                "trace_id": trace_id,
                "project_id": project,
            }
            _merge_metrics_overrides_into_doc(trace_id, doc, overrides)
            if not args.dry_run:
                doc["metrics_last_patched_at"] = firestore.SERVER_TIMESTAMP
            doc = {k: v for k, v in doc.items() if v is not None}
            if args.dry_run:
                print(json.dumps(doc, ensure_ascii=False, default=str))
            else:
                db.collection(args.collection).document(trace_id).set(doc, merge=True)
                n += 1
        print(f"Patched metrics on {n} document(s) in {args.collection!r}.", file=sys.stderr)
        return

    prune_path = args.prune_firestore_except_allowlist_file.strip()
    if prune_path:
        keep = _load_trace_id_allowlist(prune_path)
        try:
            db_prune = _firestore_client(project, args.firestore_database)
        except gcp_exceptions.NotFound as exc:
            raise SystemExit(
                "Firestore has no database in this project (or wrong FIRESTORE_DATABASE_ID). "
                f"Underlying error: {exc}"
            ) from exc
        n_del = 0
        for doc in db_prune.collection(args.collection).stream():
            if doc.id in keep:
                continue
            if args.dry_run:
                print(f"prune (dry-run): would delete document id={doc.id!r}", file=sys.stderr)
            else:
                doc.reference.delete()
            n_del += 1
        print(
            f"Prune: {'would remove' if args.dry_run else 'removed'} {n_del} document(s) "
            f"not in {len(keep)} allowlisted id(s) from {prune_path!r}.",
            file=sys.stderr,
        )
        return

    ev = args.online_evaluator.strip()
    trace_filter: str | None
    trace_ids_direct = [x.strip() for x in str(args.trace_ids or "").split(",") if x.strip()]
    if args.trace_ids_file.strip():
        trace_ids_direct.extend(_load_trace_ids_file(args.trace_ids_file))
    _seen_ids: set[str] = set()
    trace_ids_direct = [x for x in trace_ids_direct if not (x in _seen_ids or _seen_ids.add(x))]

    eval_allowlist: set[str] | None = None
    if args.evaluated_trace_allowlist_file.strip():
        eval_allowlist = _load_trace_id_allowlist(args.evaluated_trace_allowlist_file)
        print(
            f"  evaluated-trace-allowlist: {len(eval_allowlist)} id(s) from {args.evaluated_trace_allowlist_file!r}",
            file=sys.stderr,
        )

    if args.scan_without_list_filter:
        if not ev and not args.scan_gen_ai_agent_name.strip():
            raise SystemExit(
                "--scan-without-list-filter requires --online-evaluator and/or --scan-gen-ai-agent-name "
                "(set metrics metadata and/or span match)."
            )
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

    st_raw = args.start_time.strip()
    et_raw = args.end_time.strip()
    explicit_range = bool(st_raw and et_raw)
    if bool(st_raw) != bool(et_raw):
        raise SystemExit("Provide both --start-time and --end-time, or neither.")

    db: firestore.Client | None = None if args.dry_run else _firestore_client(project, args.firestore_database)
    try:
        cursor = _read_cursor(db) if not explicit_range else None
    except gcp_exceptions.NotFound as exc:
        raise SystemExit(
            "Firestore has no database in this project (or wrong FIRESTORE_DATABASE_ID). "
            "Create a Native mode database, e.g.: "
            "gcloud firestore databases create --database='(default)' --location=us-central1 "
            "--type=firestore-native --project=YOUR_PROJECT_ID\n"
            f"Underlying error: {exc}"
        ) from exc

    end_time = datetime.now(timezone.utc)
    start_time: datetime
    if explicit_range:
        start_time = _parse_rfc3339_utc(st_raw)
        end_time = _parse_rfc3339_utc(et_raw)
        if start_time >= end_time:
            raise SystemExit("--start-time must be before --end-time.")
    else:
        end_time = datetime.now(timezone.utc)
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
    if explicit_range:
        print("  mode: explicit time range (Firestore cursor ignored for window)", file=sys.stderr)
    if args.scan_without_list_filter:
        print("  mode: scan-without-list-filter (post-filter by evaluator resource)", file=sys.stderr)
    if args.scan_gen_ai_agent_name.strip():
        print(f"  scan also match gen_ai.agent.name={args.scan_gen_ai_agent_name.strip()!r}", file=sys.stderr)

    # Direct trace id ingest (no list).
    if trace_ids_direct:
        for trace_id in trace_ids_direct:
            if total_written >= args.max_traces:
                break
            tr = _get_trace(sess, project_id=project, trace_id=trace_id)
            trace_id = str(tr.get("traceId") or trace_id).strip()
            extracted = _extract_evaluation_fields(tr, metric_names)
            doc = {
                "trace_id": trace_id,
                "project_id": project,
                "online_evaluator_resource": args.online_evaluator.strip() or extracted.get("online_evaluator_from_trace"),
                "agent_resource": args.agent_resource.strip() or None,
                "metrics": extracted["metrics"],
                "metric_rationales": extracted["rationales"] or None,
                "matched_trace_label_keys": extracted["matched_label_keys"],
                "root_span_start_time": extracted["root_span_start_time"],
                "root_span_end_time": extracted["root_span_end_time"],
                "source": "cloud_trace_online_monitor",
                "ingest_path": "trace_ids",
            }
            if db is not None:
                doc["ingested_at"] = firestore.SERVER_TIMESTAMP
            _merge_metrics_overrides_into_doc(trace_id, doc, overrides)
            doc = {k: v for k, v in doc.items() if v is not None}
            if args.dry_run:
                print(json.dumps({"trace_id": trace_id, "metrics": doc.get("metrics")}, ensure_ascii=False))
            else:
                assert db is not None
                db.collection(args.collection).document(trace_id).set(doc, merge=True)
                total_written += 1
        should_cursor = db is not None and not args.dry_run and (not explicit_range or args.update_cursor_after_backfill)
        if should_cursor:
            _write_cursor(db, datetime.now(timezone.utc) if explicit_range else end_time)
        print(f"Done. Upserted {total_written} trace document(s) into {args.collection!r}.", file=sys.stderr)
        return

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
                if not _trace_matches_scan_postfilter(
                    tr,
                    ev,
                    gen_ai_agent_name=args.scan_gen_ai_agent_name.strip() or None,
                ):
                    continue
            trace_id = str(tr.get("traceId") or "").strip()
            if not trace_id:
                continue

            extracted = _extract_evaluation_fields(tr, metric_names)
            if eval_allowlist is not None:
                if trace_id not in eval_allowlist:
                    continue
            elif not _should_persist_list_crawl_trace(
                trace_id,
                tr,
                ev,
                extracted,
                overrides,
                include_non_evaluated_agent_traces=args.include_non_evaluated_agent_traces,
            ):
                continue

            if not extracted["metrics"] and not extracted["rationales"]:
                # Still persist stub so we know trace matched filter but parser needs tuning.
                pass

            ingest_path = "list_crawl"
            if eval_allowlist is not None:
                ingest_path = "list_crawl_eval_allowlist"
            elif args.scan_without_list_filter:
                ingest_path = "list_crawl_scan"

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
                "ingest_path": ingest_path,
            }
            if db is not None:
                doc["ingested_at"] = firestore.SERVER_TIMESTAMP
            _merge_metrics_overrides_into_doc(trace_id, doc, overrides)
            doc = {k: v for k, v in doc.items() if v is not None}

            if args.dry_run:
                print(json.dumps({"trace_id": trace_id, "metrics": doc.get("metrics")}, ensure_ascii=False))
            else:
                assert db is not None
                db.collection(args.collection).document(trace_id).set(doc, merge=True)
                total_written += 1

        if args.scan_without_list_filter and examined >= args.scan_max_list_traces:
            print(f"  scan cap reached (examined={examined})", file=sys.stderr)
            break
        if not page_token:
            break

    should_cursor = db is not None and not args.dry_run and (not explicit_range or args.update_cursor_after_backfill)
    if should_cursor:
        _write_cursor(db, datetime.now(timezone.utc) if explicit_range else end_time)

    print(f"Done. Upserted {total_written} trace document(s) into {args.collection!r}.", file=sys.stderr)


if __name__ == "__main__":
    main()
