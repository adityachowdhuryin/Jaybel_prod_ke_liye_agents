"""
Intent routing + task refinement via Vertex Gemini structured outputs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Literal

logger = logging.getLogger(__name__)

# Approximate token budget: use char length / 4 as a cheap stand-in for tokens.
CONTEXT_TOKEN_APPROX_LIMIT = int(os.environ.get("SESSION_CONTEXT_TOKEN_APPROX", "4000"))

ENABLE_VERTEX_ROUTING = os.environ.get("ENABLE_VERTEX_ROUTING", "true").lower() in (
    "1",
    "true",
    "yes",
)

VERTEX_REGION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
VERTEX_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get(
    "GCP_PROJECT", ""
)
VERTEX_MODEL_ID = (os.environ.get("VERTEX_MODEL_ID") or "gemini-2.5-flash").strip()


def _approx_token_count(session_history: list[dict[str, str]]) -> int:
    total_chars = sum(len(m.get("content", "")) for m in session_history)
    return max(1, total_chars // 4)


def _call_gemini_sync(
    system: str,
    user_prompt: str,
    max_tokens: int = 1024,
    response_schema: dict | None = None,
    response_mime_type: str | None = None,
) -> str:
    import vertexai
    from vertexai.generative_models import GenerationConfig, GenerativeModel

    if not VERTEX_PROJECT:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT or GCP_PROJECT is required for Vertex")
    vertexai.init(project=VERTEX_PROJECT, location=VERTEX_REGION)
    model = GenerativeModel(VERTEX_MODEL_ID)
    prompt = f"{system}\n\n{user_prompt}".strip()
    cfg = GenerationConfig(
        temperature=0.1,
        max_output_tokens=max_tokens,
        response_mime_type=response_mime_type,
        response_schema=response_schema,
    )
    r = model.generate_content(prompt, generation_config=cfg)
    return (r.text or "").strip()


async def call_gemini(
    system: str,
    user_prompt: str,
    max_tokens: int = 1024,
    response_schema: dict | None = None,
    response_mime_type: str | None = None,
) -> str:
    return await asyncio.to_thread(
        _call_gemini_sync,
        system,
        user_prompt,
        max_tokens,
        response_schema,
        response_mime_type,
    )


@dataclass
class CompressionResult:
    """Compressed messages for routing + optional Postgres summary row."""

    messages: list[dict[str, str]]
    persist_summary: tuple[str, int] | None  # (summary_text, covers_up_to_message_id)


def _gemini_session_summary_configured() -> bool:
    return bool(
        (os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT", "")).strip()
    )


def _summarize_older_transcript_gemini_sync(older_blob: str) -> str:
    import vertexai
    from vertexai.generative_models import GenerationConfig, GenerativeModel

    project = (
        os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT", "")
    ).strip()
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT (or GCP_PROJECT) required for Gemini summary")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1").strip()
    model_id = (
        os.environ.get("SESSION_SUMMARY_VERTEX_MODEL")
        or os.environ.get("VERTEX_MODEL_ID")
        or "gemini-2.5-flash"
    ).strip()
    vertexai.init(project=project, location=location)
    model = GenerativeModel(model_id)
    prompt = (
        "Summarize the following chat turns into one dense bullet summary for a cost-metrics assistant. "
        "Preserve facts, numbers, product names, GCP project ids, dates, and cost-related constraints. "
        "Do not greet; output plain text only.\n\n"
        f"{older_blob}"
    )
    r = model.generate_content(
        prompt,
        generation_config=GenerationConfig(temperature=0.2, max_output_tokens=1024),
    )
    text = (r.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned empty session summary")
    return text


async def _summarize_older_transcript_gemini(older_blob: str) -> str:
    return await asyncio.to_thread(_summarize_older_transcript_gemini_sync, older_blob)


async def compress_session_context_with_ids(
    rows: list[tuple[int | None, str, str]],
) -> CompressionResult:
    """
    If history is large, summarize older turns with Vertex Gemini Flash (when ADC/project available),
    keep the 3 most recent turns. When Gemini succeeds, caller may persist persist_summary to Postgres.
    Otherwise truncate with no persistence.
    """
    if not rows:
        return CompressionResult([], None)

    session_history = [{"role": r, "content": c} for _, r, c in rows]
    approx_tok = _approx_token_count(session_history)
    over = approx_tok > CONTEXT_TOKEN_APPROX_LIMIT

    if not over or len(rows) <= 3:
        return CompressionResult(list(session_history), None)

    recent_rows = rows[-3:]
    older_rows = rows[:-3]
    older_blob = "\n".join(
        f"{r.upper()}: {c}" for _, r, c in older_rows
    )
    older_ids = [i for i, _, _ in older_rows if i is not None]
    covers = max(older_ids) if older_ids else None
    recent_dicts = [{"role": r, "content": c} for _, r, c in recent_rows]

    if _gemini_session_summary_configured():
        try:
            summary = await _summarize_older_transcript_gemini(older_blob)
            compressed_prefix: dict[str, str] = {
                "role": "user",
                "content": f"[COMPRESSED SESSION CONTEXT]\n{summary}",
            }
            persist = (summary, covers) if covers is not None else None
            return CompressionResult([compressed_prefix, *recent_dicts], persist)
        except Exception as e:
            logger.warning("Gemini session summary failed; truncating: %s", e)

    truncated_prefix = {
        "role": "user",
        "content": f"[Truncated prior turns — {len(older_rows)} messages omitted]\n",
    }
    return CompressionResult([truncated_prefix, *recent_dicts], None)


async def compress_session_context(session_history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Backward-compatible: no message ids → compression without DB summary persistence metadata."""
    rows = [(None, m.get("role", "user"), m.get("content", "")) for m in session_history]
    result = await compress_session_context_with_ids(rows)
    return result.messages


IntentLabel = Literal["chitchat", "clear", "metrics", "out_of_scope"]

DEFAULT_OUT_OF_SCOPE_REPLY = (
    "I only help with cloud cost and usage questions — for example spend by service, "
    "environment, trends, budgets, or billing data. I can’t assist with that request, "
    "but ask me anything about your cloud costs."
)


@dataclass
class IntentResult:
    intent: IntentLabel
    reply: str


def _parse_intent_json(text: str) -> IntentResult:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return IntentResult(intent="metrics", reply="")
    intent = str(data.get("intent", "metrics")).lower()
    reply = str(data.get("reply", "")).strip()
    if intent in ("greeting", "hello", "chitchat", "smalltalk"):
        intent_t: IntentLabel = "chitchat"
    elif intent in ("clear", "reset", "new_chat"):
        intent_t = "clear"
    elif intent in ("out_of_scope", "off_topic", "unrelated", "refuse"):
        intent_t = "out_of_scope"
    elif intent in ("metrics", "data", "cost", "complex", "query"):
        intent_t = "metrics"
    else:
        intent_t = "metrics"
    return IntentResult(intent=intent_t, reply=reply)


# --- Local routing (no Vertex): fast heuristics for localhost / tests ---

_CLEAR_LOCAL = re.compile(
    r"^\s*(clear|reset|start\s+over|new\s+chat|forget\s+(everything|history))\s*\.?!*\s*$",
    re.I,
)

_METRICS_LOCAL = re.compile(
    r"\b(cost|costs|spend|spending|billing|bill|bills|invoice|invoices|budget|budgets|usage|"
    r"charge|charges|charged|usd|\$|dollar|cloud\s+cost|cloud\s+spend|finops|"
    r"gcp|google\s+cloud|aws|azure|bigquery|big\s+query|storage|compute|kubernetes|gke|"
    r"cloud\s+run|sql|postgres|postgresql|database|environment|env\b|prod|production|"
    r"staging|dev\b|development|metric|metrics|trend|trends|spike|spikes|anomal|"
    r"yesterday|last\s+week|last\s+month|this\s+week|this\s+month|quarter|service|services|"
    r"resource|resources|skus?|line\s+item|export|table\s+cloud_costs|project)\b",
    re.I,
)

_OFF_TOPIC_LOCAL = re.compile(
    r"\b(weather|forecast|temperature|humidity|recipe|cook\b|baking|restaurant|"
    r"joke|jokes|funny|poem|haiku|write\s+a\s+story|translate\s+this|horoscope|"
    r"politics|election|president\b|sports?\b|football|cricket|basketball|"
    r"stock\s+price|bitcoin|crypto|ethereum|"
    r"debug\s+my\s+code|leetcode|homework|write\s+my\s+essay|"
    r"quantum\s+physics|philosophy|movie|who\s+won\s+the|capital\s+of\s+)\b",
    re.I,
)

_GREETING_LOCAL = re.compile(
    r"^\s*(hi|hello|hey|hiya|good\s+(morning|afternoon|evening)|"
    r"thanks|thank\s+you|thx|ty|ok\s*ok|okay|cheers)\s*[!?.]*\s*$",
    re.I,
)


def classify_intent_local(
    latest_user_message: str,
    orchestrator_context: list[dict[str, str]],
) -> IntentResult:
    """
    Rule-based router when ENABLE_VERTEX_ROUTING is off (typical local dev).
    Prefer routing cost questions to the specialist; refuse clearly off-topic requests.
    """
    text = latest_user_message.strip()
    if not text:
        return IntentResult(intent="out_of_scope", reply=DEFAULT_OUT_OF_SCOPE_REPLY)

    if _CLEAR_LOCAL.match(text):
        return IntentResult(
            intent="clear",
            reply="Chat cleared. Ask me about your cloud costs when you’re ready.",
        )

    if _METRICS_LOCAL.search(text):
        return IntentResult(intent="metrics", reply="")

    if _OFF_TOPIC_LOCAL.search(text):
        return IntentResult(intent="out_of_scope", reply=DEFAULT_OUT_OF_SCOPE_REPLY)

    if len(text) <= 120 and _GREETING_LOCAL.match(text):
        return IntentResult(
            intent="chitchat",
            reply=(
                "Hi — I’m your cost assistant. Ask about spend, services, environments, "
                "or trends in your cloud usage data."
            ),
        )

    # Short vague lines without cost signals: steer to cost topics instead of hitting the DB.
    if len(text.split()) <= 4 and not _METRICS_LOCAL.search(text):
        return IntentResult(
            intent="chitchat",
            reply=(
                "I can look up your cloud cost and usage metrics. Try asking for top services, "
                "costs by environment, or spend over a date range."
            ),
        )

    # Elliptical follow-up in an active cost thread (e.g. "And Artifact Registry?").
    if orchestrator_context and len(text) <= 160:
        prior = "\n".join(
            f"{m.get('role', 'user')}: {m.get('content', '')}"
            for m in orchestrator_context[:-1]
        )
        if prior and (
            _METRICS_LOCAL.search(prior)
            or "INR" in prior
            or "cost" in prior.lower()
            or "billing" in prior.lower()
            or "bigquery" in prior.lower()
        ):
            return IntentResult(intent="metrics", reply="")

    return IntentResult(intent="out_of_scope", reply=DEFAULT_OUT_OF_SCOPE_REPLY)


async def classify_intent_haiku(
    orchestrator_context: list[dict[str, str]],
    latest_user_message: str,
) -> IntentResult:
    """
    Gemini JSON router. Returns intent + optional direct reply.
    """
    ctx_lines = "\n".join(
        f"{m.get('role', 'user').upper()}: {m.get('content', '')}"
        for m in orchestrator_context
    )
    system = (
        "Classify the latest user message for a cloud cost assistant. "
        "Return JSON with keys intent and reply."
    )
    user_prompt = f"Conversation context:\n{ctx_lines}\n\nLatest user message:\n{latest_user_message}"
    schema = {
        "type": "object",
        "properties": {
            "intent": {"type": "string"},
            "reply": {"type": "string"},
        },
        "required": ["intent", "reply"],
    }
    text = await call_gemini(
        system,
        user_prompt,
        max_tokens=256,
        response_schema=schema,
        response_mime_type="application/json",
    )
    return _parse_intent_json(text)


async def refine_task_sonnet(
    orchestrator_context: list[dict[str, str]],
    latest_user_message: str,
) -> str:
    """
    Gemini prepares a single natural-language task for the Cost Metrics A2A agent.
    """
    ctx_lines = "\n".join(
        f"{m.get('role', 'user').upper()}: {m.get('content', '')}"
        for m in orchestrator_context
    )
    system = (
        "You are the orchestrator planner. Output ONE concise English instruction for a specialist "
        "that queries a PostgreSQL table of cloud_costs (date, service_name, environment, cost_usd). "
        "Include filters implied by the user. No SQL required — natural language only. "
        "Output plain text only, no JSON."
    )
    user_prompt = (
        f"Context:\n{ctx_lines}\n\nUser request:\n{latest_user_message}\n\nSpecialist task:"
    )
    task = await call_gemini(system, user_prompt, max_tokens=512)
    return task.strip() or latest_user_message


def sse_pack_a2a(task_id: str, state: str, text: str, completed: bool = False) -> str:
    if completed:
        body = {
            "id": task_id,
            "status": {"state": "completed"},
            "artifact": {"parts": [{"text": text}]},
        }
    else:
        body = {
            "id": task_id,
            "status": {
                "state": "working",
                "message": {"role": "agent", "parts": [{"text": text}]},
            },
        }
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


async def stream_synthetic_a2a(full_text: str) -> AsyncIterator[bytes]:
    """Emit A2A-shaped SSE so the existing frontend parser keeps working."""
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    chunk = 160
    for i in range(0, len(full_text), chunk):
        yield sse_pack_a2a(
            task_id, "working", full_text[i : i + chunk], completed=False
        ).encode()
        await asyncio.sleep(0.01)
    yield sse_pack_a2a(task_id, "completed", "", completed=True).encode()


def parse_sse_bytes_to_text(raw: bytes) -> str:
    """Recover assistant-visible text from an accumulated A2A SSE byte stream."""
    buffer = raw.decode("utf-8", errors="replace")
    collected: list[str] = []
    while "\n\n" in buffer:
        raw_event, buffer = buffer.split("\n\n", 1)
        line = next(
            (ln for ln in raw_event.split("\n") if ln.startswith("data:")),
            None,
        )
        if not line:
            continue
        data = line[5:].strip()
        if not data:
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        status = obj.get("status") if isinstance(obj.get("status"), dict) else {}
        message = status.get("message") if isinstance(status.get("message"), dict) else {}
        artifact = obj.get("artifact") if isinstance(obj.get("artifact"), dict) else {}
        parts = message.get("parts") or artifact.get("parts") or []
        if not isinstance(parts, list):
            continue
        for p in parts:
            if isinstance(p, dict) and p.get("text"):
                collected.append(str(p["text"]))
    return "".join(collected)


def sse_stream_has_error(raw: bytes) -> bool:
    """True if any SSE data frame is a JSON object with error: true."""
    buffer = raw.decode("utf-8", errors="replace")
    while "\n\n" in buffer:
        raw_event, buffer = buffer.split("\n\n", 1)
        line = next(
            (ln for ln in raw_event.split("\n") if ln.startswith("data:")),
            None,
        )
        if not line:
            continue
        data = line[5:].strip()
        if not data:
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if obj.get("error") is True:
            return True
    return False
