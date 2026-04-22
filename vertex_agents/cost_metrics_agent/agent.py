"""Cost metrics specialist for Vertex AI Agent Engine (Gemini + read-only SQL tools)."""

from __future__ import annotations

import json
import logging
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.tools import FunctionTool, ToolContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from . import db_logic
from .cost_payload_contract import COST_PAYLOAD_PREFIX

logger = logging.getLogger(__name__)
_PENDING_KEY = "pending_clarification"


def _to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _normalize_spaces(text: str) -> str:
    return " ".join((text or "").strip().split())


def _extract_vs_pair(text: str) -> tuple[str, str] | None:
    normalized = _normalize_spaces(text)
    lower = normalized.lower()
    for sep in (" vs. ", " vs "):
        idx = lower.find(sep)
        if idx < 0:
            continue
        left = normalized[:idx].strip(" .,:;")
        right = normalized[idx + len(sep) :].strip(" .,:;")
        if left and right:
            return left, right
    return None


def _detect_compare_scope(text: str) -> str | None:
    t = text.lower()
    if "prod vs dev" in t or (("prod" in t or "production" in t) and ("dev" in t or "development" in t)):
        return "env"
    if "project" in t and "vs" in t:
        return "project"
    if "service" in t and "vs" in t:
        return "service"
    if _extract_vs_pair(text):
        return "service"
    return None


def _extract_time_window(text: str) -> str | None:
    t = _normalize_spaces(text).lower()
    words = t.split()
    for i in range(len(words) - 2):
        if words[i] == "last" and words[i + 1].isdigit() and words[i + 2] in {"day", "days"}:
            return f"last {words[i + 1]} days"
    if "this month" in t or "month-to-date" in t:
        return "this month (month-to-date)"
    if "full history" in t or "all time" in t or "to date" in t or "till now" in t or "until now" in t:
        return "full history to date"
    if "last month" in t:
        return "last month"
    if "this week" in t:
        return "this week"
    if "last week" in t:
        return "last week"
    return None


def _default_missing_for_kind(kind: str) -> list[str]:
    if kind == "top_n":
        return ["top_n"]
    if kind in {"time_window", "compare_time_window"}:
        return ["time_window"]
    if kind == "compare_scope":
        return ["compare_scope"]
    if kind == "compare_entities":
        return ["service_a", "service_b"]
    if kind == "schema_column":
        return ["column_name"]
    return []


def _pending_clarification_payload(pending: dict[str, Any]) -> str:
    return _to_json(
        {
            "response_type": "clarification",
            "question": str(pending.get("question") or "Please clarify your request."),
            "options": pending.get("options") or [],
            "clarification_kind": pending.get("clarification_kind"),
            "missing_slots": pending.get("missing_slots") or [],
            "context": pending.get("context") or {},
        }
    )


def _build_compare_question(pending: dict[str, Any]) -> str:
    context = pending.get("context") if isinstance(pending.get("context"), dict) else {}
    scope = str(context.get("compare_scope") or "service")
    time_window = str(context.get("time_window") or "").strip()
    if scope == "env":
        base = "Compare spend for prod vs dev."
    elif scope == "project":
        a = str(context.get("project_a") or "project A").strip()
        b = str(context.get("project_b") or "project B").strip()
        base = f"Compare spend for project A ({a}) vs project B ({b})."
    else:
        a = str(context.get("service_a") or "service A").strip()
        b = str(context.get("service_b") or "service B").strip()
        base = f"Compare spend for service A ({a}) vs service B ({b})."
    if time_window:
        return f"{base} Time window: {time_window}."
    return base


def _resume_pending_clarification(question: str, pending: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    updated = dict(pending)
    context = dict(updated.get("context") or {})
    missing = list(updated.get("missing_slots") or _default_missing_for_kind(str(updated.get("clarification_kind") or "")))
    q = _normalize_spaces(question)
    q_lower = q.lower()

    if "compare_scope" in missing:
        scope = _detect_compare_scope(q)
        if scope:
            context["compare_scope"] = scope
            missing = [m for m in missing if m != "compare_scope"]
            if scope == "service":
                missing.extend([m for m in ["service_a", "service_b"] if m not in missing])

    pair = _extract_vs_pair(q)
    if pair:
        left, right = pair
        scope = str(context.get("compare_scope") or "")
        if scope == "project":
            context["project_a"] = left
            context["project_b"] = right
            missing = [m for m in missing if m not in {"project_a", "project_b"}]
        else:
            context["compare_scope"] = "service"
            context["service_a"] = left
            context["service_b"] = right
            missing = [m for m in missing if m not in {"service_a", "service_b", "compare_scope"}]

    time_window = _extract_time_window(q)
    if time_window:
        context["time_window"] = time_window
        missing = [m for m in missing if m != "time_window"]

    kind = str(updated.get("clarification_kind") or "")
    if kind in {"compare_scope", "compare_entities", "compare_time_window"}:
        scope = str(context.get("compare_scope") or "")
        if not scope:
            missing = list(dict.fromkeys([*missing, "compare_scope"]))
        if scope == "service" and (not context.get("service_a") or not context.get("service_b")):
            missing = list(dict.fromkeys([*missing, "service_a", "service_b"]))
        if not context.get("time_window"):
            missing = list(dict.fromkeys([*missing, "time_window"]))

    updated["context"] = context
    updated["missing_slots"] = missing
    if missing:
        if "compare_scope" in missing:
            updated["clarification_kind"] = "compare_scope"
            updated["question"] = "What two scopes should I compare?"
            updated["options"] = ["prod vs dev", "project A vs project B", "service A vs service B"]
        elif "service_a" in missing or "service_b" in missing:
            updated["clarification_kind"] = "compare_entities"
            updated["question"] = "Which two services should I compare?"
            updated["options"] = ["Cloud SQL vs Vertex AI", "BigQuery vs Cloud Storage", "Cloud Run vs Compute Engine"]
        elif "time_window" in missing:
            updated["clarification_kind"] = "compare_time_window"
            updated["question"] = "For what time window should I compare spend?"
            updated["options"] = ["Last 7 days", "Last 30 days", "This month (month-to-date)", "Full history to date"]
        return None, updated
    if str(updated.get("clarification_kind") or "").startswith("compare"):
        return _build_compare_question(updated), None
    return q, None


def _as_structured_tool_response(result: str) -> dict[str, Any]:
    try:
        payload = json.loads(result)
    except Exception:
        return {"response_type": "text", "text": result}
    if isinstance(payload, dict) and payload.get("response_type"):
        return payload
    if isinstance(payload, dict) and payload.get("needs_clarification"):
        return {
            "response_type": "clarification",
            "question": str(payload.get("question") or "").strip(),
            "options": payload.get("options", []),
            "clarification_kind": payload.get("clarification_kind"),
            "missing_slots": payload.get("missing_slots", []),
            "context": payload.get("context", {}),
        }
    if isinstance(payload, dict) and payload.get("error"):
        out = {
            "response_type": "error",
            "error": str(payload.get("error")),
            "detail": str(payload.get("detail") or "I cannot verify this from current data.").strip(),
        }
        hint = str(payload.get("hint") or "").strip()
        if hint:
            out["hint"] = hint
        return out
    if isinstance(payload, (dict, list)):
        return {"response_type": "result", "data": payload}
    return {"response_type": "text", "text": result}


def query_cloud_costs(question: str, tool_context: ToolContext | None = None) -> str:
    """Answer questions about cloud spend via BigQuery export or PostgreSQL.

    Pass the user's question in natural language; filters for environment, service,
    date, totals vs detail rows are inferred automatically.
    """
    pending = None
    if tool_context and isinstance(tool_context.state, dict):
        pending = tool_context.state.get(_PENDING_KEY)

    effective_question = question
    if isinstance(pending, dict):
        rewritten, new_pending = _resume_pending_clarification(question, pending)
        if new_pending is not None:
            if tool_context and isinstance(tool_context.state, dict):
                tool_context.state[_PENDING_KEY] = new_pending
            pld = _pending_clarification_payload(new_pending)
            return f"{COST_PAYLOAD_PREFIX}{pld}"
        if rewritten:
            effective_question = rewritten
            if tool_context and isinstance(tool_context.state, dict):
                tool_context.state.pop(_PENDING_KEY, None)

    result = db_logic.query_costs(effective_question)
    structured = _as_structured_tool_response(result)
    out_body = _to_json(structured)
    if structured.get("response_type") in ("clarification", "error"):
        out_body = f"{COST_PAYLOAD_PREFIX}{out_body}"

    if tool_context and isinstance(tool_context.state, dict):
        if structured.get("response_type") == "clarification":
            missing = structured.get("missing_slots")
            if not isinstance(missing, list) or not missing:
                missing = _default_missing_for_kind(str(structured.get("clarification_kind") or ""))
                structured["missing_slots"] = missing
            tool_context.state[_PENDING_KEY] = {
                "clarification_kind": structured.get("clarification_kind"),
                "question": structured.get("question"),
                "options": structured.get("options", []),
                "missing_slots": missing,
                "context": structured.get("context", {}),
                "original_question": effective_question,
            }
        else:
            tool_context.state.pop(_PENDING_KEY, None)
    return out_body


async def _persist_turn_memory(callback_context: Context) -> None:
    """
    Persist high-signal turn context into Memory Bank after each response.
    """
    try:
        events = getattr(callback_context.session, "events", None) or []
        if isinstance(events, list) and events:
            await callback_context.add_events_to_memory(events=events[-8:])
        else:
            await callback_context.add_session_to_memory()
    except Exception:
        logger.exception("Failed to persist cost agent memory")
    return None


root_agent = LlmAgent(
    name="cost_metrics_agent",
    model="gemini-2.5-flash",
    instruction=(
        "You are a cloud cost analyst. For cost answers, use query_cloud_costs as the source of truth. "
        "MANDATORY: call query_cloud_costs for every user turn that mentions or implies spend, cost, compare, services, time periods, or rankings (including one-word or vague asks such as 'Compare spend.' with no time window or entities). "
        "Do not answer from prior knowledge, templates, or generic cost advice before the tool has run. "
        "You may also answer schema questions about the configured billing BigQuery source, such as listing columns, checking whether a column exists, or listing distinct values for a valid column. "
        "Never invent values, services, currencies, date windows, rankings, or trends. "
        "Never confuse normalized output fields with the actual BigQuery view schema. "
        "STRICT: If the tool return starts with 'COST_PAYLOAD_JSON:' (clarification or error from query_cloud_costs), your entire final assistant message must be exactly that tool string — same characters, no paraphrase, no preamble, no extra lines. "
        "For any other tool result (numeric/table JSON without that prefix), summarize faithfully and mention the effective window/filters. "
        "If the request is ambiguous (missing time window, scope, grouping, compare entities, or top-N), ask one concise clarification after the tool provides it — but when the tool returns COST_PAYLOAD_JSON for clarification, output only that block. "
        "Never expose internal chain-of-thought or fabricate fallback results."
    ),
    tools=[PreloadMemoryTool(), FunctionTool(query_cloud_costs)],
    after_agent_callback=_persist_turn_memory,
)
