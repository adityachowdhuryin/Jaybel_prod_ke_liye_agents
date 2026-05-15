"""Platform assistant orchestrator: routes cost questions to deployed Agent Engine."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any

from google.adk.agents.context import Context
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool, ToolContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
import vertexai
import vertexai.agent_engines as agent_engines

logger = logging.getLogger(__name__)
_SPECIALIST_SESSION_KEY = "cost_specialist_session_id"
# Keep in sync with vertex_agents/cost_metrics_agent/cost_payload_contract.py
_COST_PAYLOAD_PREFIX = "COST_PAYLOAD_JSON:\n"

_ORCHESTRATOR_INSTRUCTION = """You are a routing orchestrator for GCP cost intelligence.
- For any cost, billing, spend, service-cost, project-cost, region-cost, trend, or usage question: ALWAYS call query_cost_specialist first.
- Questions about the billing source schema or columns (for example schema, column names, whether a column exists, or unique values in a column) are also cost-specialist questions.
- Never invent numbers, services, trends, dates, or filters.
- If the specialist return starts with COST_PAYLOAD_JSON: (typed clarification or error), your entire final message must be exactly that string with no paraphrase and no other text (do not prefix with "Here is" or similar).
- If user intent is ambiguous (missing time window, scope, grouping, or top-N) and the specialist has not already returned COST_PAYLOAD_JSON, ask exactly one concise clarification instead of guessing.
- For non-cost greetings or generic platform guidance, answer directly and briefly.
- When the specialist returns normal cost/result data (JSON arrays/objects, not COST_PAYLOAD_JSON), summarize in natural language for the user: use short markdown bullets or a markdown table when listing services or rows; round currency to at most two decimal places; state units explicitly (INR for GCP billing export, USD for workflow/runtime view totals on cost_usd when applicable). Never paste raw JSON as the final answer. Never echo internal slot or field names (for example billing_project_id, top_n); use plain English such as "GCP project jaybel-prod".
- Never output internal tool traces or raw debugging events.
"""

_QUERY_URL = os.environ.get("COST_AGENT_QUERY_ENDPOINT", "").strip()
_RESOURCE_NAME = os.environ.get("COST_AGENT_ENGINE_RESOURCE", "").strip()
_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1").strip()


def _resource_from_endpoint(url: str) -> str:
    m = re.search(
        r"(projects/[^/]+/locations/[^/]+/reasoningEngines/[^/:]+)",
        url,
        re.I,
    )
    return m.group(1) if m else ""


def _resolve_resource_name() -> str:
    if _RESOURCE_NAME:
        return _RESOURCE_NAME
    if _QUERY_URL:
        return _resource_from_endpoint(_QUERY_URL)
    return ""


def _extract_text_from_part(p: dict) -> str:
    """Pull human-readable or tool output text from one Gemini content part."""
    if p.get("text"):
        return str(p["text"])
    fc = p.get("function_call")
    if isinstance(fc, dict):
        name = fc.get("name", "")
        args = fc.get("args") if "args" in fc else fc.get("arguments")
        return f"[tool call: {name}] {json.dumps(args, ensure_ascii=False) if args is not None else ''}".strip()

    for fr_key in ("function_response", "functionResponse", "tool_response", "ToolResponse"):
        fr = p.get(fr_key)
        if isinstance(fr, dict):
            inner = fr.get("response")
            if inner is not None:
                if isinstance(inner, (dict, list)):
                    return json.dumps(inner, ensure_ascii=False)
                s = str(inner)
                if s.strip().startswith("{") and "response_type" in s:
                    return s
                return s
            return json.dumps(fr, ensure_ascii=False)
        if isinstance(fr, str):
            return fr
    return ""


def _walk_collect_response_type(obj: Any, out: list[dict]) -> None:
    if isinstance(obj, dict):
        if isinstance(obj.get("response_type"), str) and obj.get("response_type").strip():
            out.append(obj)
        for v in obj.values():
            _walk_collect_response_type(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_collect_response_type(v, out)


def _extract_text(event: dict) -> str:
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
        chunk = _extract_text_from_part(p)
        if chunk:
            out.append(chunk)
    return "\n".join(out).strip()


def _harvest_typed_from_event(ev: dict) -> str:
    """Prefer COST_PAYLOAD_JSON or raw function response JSON with response_type."""
    typed: list[dict] = []
    _walk_collect_response_type(ev, typed)
    for obj in typed:
        if not isinstance(obj, dict):
            continue
        rt = str(obj.get("response_type") or "").lower()
        if rt in ("clarification", "error"):
            return f"{_COST_PAYLOAD_PREFIX}{json.dumps(obj, ensure_ascii=False)}"
    return ""


def _normalize_specialist_output(text: str) -> str:
    """Convert structured specialist control payloads into plain routing directives."""
    cleaned = text.strip()
    if not cleaned:
        return cleaned
    if cleaned.startswith("COST_PAYLOAD_JSON:") or _COST_PAYLOAD_PREFIX in cleaned:
        return cleaned
    try:
        obj = json.loads(cleaned)
    except Exception:
        return cleaned
    if isinstance(obj, dict) and obj.get("response_type") in ("clarification", "error"):
        return f"{_COST_PAYLOAD_PREFIX}{json.dumps(obj, ensure_ascii=False)}"
    if isinstance(obj, dict) and obj.get("needs_clarification"):
        q = str(obj.get("question") or "").strip()
        options = obj.get("options")
        if isinstance(options, list) and options:
            opts = "\n".join(f"- {str(x).strip()}" for x in options if str(x).strip())
            if opts:
                return f"CLARIFICATION_REQUIRED:\n{q}\nOptions:\n{opts}".strip()
        return f"CLARIFICATION_REQUIRED:\n{q}".strip()
    return cleaned


def _summarize_events_for_empty_response(events: list) -> str:
    """When no extractable text was found, explain what the stream contained."""
    if not events:
        return "Specialist stream returned no events (empty iterator)."
    lines: list[str] = ["Specialist returned no model text; raw stream summary:"]
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            lines.append(f"  [{i}] {type(ev).__name__}: {repr(ev)[:500]}")
            continue
        if ev.get("code"):
            lines.append(f"  [{i}] error: {ev.get('code')} — {ev.get('message')}")
            continue
        keys = sorted(ev.keys())
        raw = json.dumps(ev, ensure_ascii=False, default=str)
        if len(raw) > 1500:
            raw = raw[:1500] + "…"
        lines.append(f"  [{i}] keys={keys}: {raw}")
    return "\n".join(lines)


def _specialist_user_id_from_context(context: ToolContext | None) -> str:
    if context and getattr(context, "user_id", None):
        return str(context.user_id).strip()
    return f"pa-orchestrator-{uuid.uuid4().hex[:8]}"


def query_cost_specialist(question: str, tool_context: ToolContext | None = None) -> str:
    """Query deployed cost specialist Agent Engine and return its response text."""
    resource_name = _resolve_resource_name()
    if not resource_name:
        return (
            "Cost routing is disabled: set COST_AGENT_QUERY_ENDPOINT or "
            "COST_AGENT_ENGINE_RESOURCE."
        )
    if not _PROJECT:
        return "Cost routing is disabled: set GOOGLE_CLOUD_PROJECT."
    try:
        vertexai.init(project=_PROJECT, location=_LOCATION)
        engine = agent_engines.get(resource_name)
        # Keep specialist memory/user scope stable across turns for the same user.
        user_id = _specialist_user_id_from_context(tool_context)
        session_id = None
        if tool_context and isinstance(tool_context.state, dict):
            sid = tool_context.state.get(_SPECIALIST_SESSION_KEY)
            session_id = str(sid).strip() if sid else None
        if not session_id:
            session = engine.create_session(user_id=user_id)
            session_id = session.get("id") if isinstance(session, dict) else None
            if tool_context and isinstance(tool_context.state, dict) and session_id:
                tool_context.state[_SPECIALIST_SESSION_KEY] = session_id
        if not session_id:
            raise RuntimeError("Specialist create_session returned no session id")
        try:
            events = list(
                engine.stream_query(message=question, user_id=user_id, session_id=session_id)
            )
        except Exception:
            # Session can expire/reset; recreate once and retry.
            session = engine.create_session(user_id=user_id)
            session_id = session.get("id") if isinstance(session, dict) else None
            if tool_context and isinstance(tool_context.state, dict) and session_id:
                tool_context.state[_SPECIALIST_SESSION_KEY] = session_id
            events = list(
                engine.stream_query(message=question, user_id=user_id, session_id=session_id)
            )
        chunks: list[str] = []
        for ev in events:
            if isinstance(ev, dict) and ev.get("code"):
                return f"Specialist error {ev.get('code')}: {ev.get('message')}"
            if isinstance(ev, dict):
                first = _harvest_typed_from_event(ev)
                if first:
                    return _normalize_specialist_output(first)
                t = _extract_text(ev)
                if t:
                    chunks.append(t)
        joined = "\n".join(chunks).strip()
        if joined:
            return _normalize_specialist_output(joined)
        return _summarize_events_for_empty_response(events)
    except Exception as e:
        return f"Specialist query failed: {e}"


async def _persist_turn_memory(callback_context: Context) -> None:
    """
    Trigger incremental memory generation after each turn.
    Use recent events to avoid re-processing the whole session repeatedly.
    """
    try:
        events = getattr(callback_context.session, "events", None) or []
        if isinstance(events, list) and events:
            await callback_context.add_events_to_memory(events=events[-8:])
        else:
            await callback_context.add_session_to_memory()
    except Exception:
        logger.exception("Failed to persist orchestrator memory")
    return None


root_agent = LlmAgent(
    name="pa_orchestrator",
    model="gemini-2.5-flash",
    instruction=_ORCHESTRATOR_INSTRUCTION,
    tools=[PreloadMemoryTool(), FunctionTool(query_cost_specialist)],
    after_agent_callback=_persist_turn_memory,
)
