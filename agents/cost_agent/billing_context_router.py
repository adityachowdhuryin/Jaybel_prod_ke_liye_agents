from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

try:
    import vertexai
    from vertexai.generative_models import GenerationConfig, GenerativeModel

    _VERTEX_OK = True
except Exception:  # pragma: no cover
    _VERTEX_OK = False

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        import google.generativeai as genai

    _GENAI_OK = True
except Exception:  # pragma: no cover
    _GENAI_OK = False


class BillingRoutePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rewritten_question: str = Field(..., min_length=1, max_length=2000)
    hint: str = Field(default="no explicit filters", max_length=2000)
    window_start: str | None = None
    window_end: str | None = None
    time_confident: bool = True
    env: str | None = Field(default=None, description="prod|dev|null")
    service: str | None = None
    billing_project_id: str | None = None
    billing_region: str | None = None
    wants_total: bool = False
    wants_top: bool = False
    time_scope: str | None = Field(
        default=None,
        description="explicit_window|month_to_date|full_history_to_date|unsure",
    )


@dataclass(frozen=True)
class ResolvedCostContext:
    rewritten_question: str
    hint: str
    window_start: date
    window_end: date
    env: str | None
    service: str | None
    billing_project_id: str | None
    billing_region: str | None
    wants_total: bool
    wants_top: bool


ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rewritten_question": {"type": "string"},
        "hint": {"type": "string"},
        "window_start": {"type": "string"},
        "window_end": {"type": "string"},
        "time_confident": {"type": "boolean"},
        "env": {"type": "string"},
        "service": {"type": "string"},
        "billing_project_id": {"type": "string"},
        "billing_region": {"type": "string"},
        "wants_total": {"type": "boolean"},
        "wants_top": {"type": "boolean"},
        "time_scope": {"type": "string"},
    },
    "required": ["rewritten_question", "hint", "time_confident", "wants_total", "wants_top"],
}


def _model_name() -> str:
    return (
        os.environ.get("BILLING_CONTEXT_ROUTER_MODEL")
        or os.environ.get("VERTEX_MODEL_ID")
        or "gemini-2.5-flash"
    ).strip()


def _provider() -> str:
    return os.environ.get("BILLING_LLM_PROVIDER", "auto").strip().lower()


def _google_ai_key() -> str | None:
    return (os.environ.get("GOOGLE_AI_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip() or None


def llm_context_router_usable() -> bool:
    return _VERTEX_OK or (_GENAI_OK and bool(_google_ai_key()))


def _router_prompt(message: str, today: date) -> str:
    return (
        "You are a billing query router. Understand the latest user ask in context and output only JSON.\n"
        "Conversation may include lines prefixed USER:/ASSISTANT:. Use conversation context for follow-ups\n"
        '(e.g. "same as above but for 4 days").\n'
        f"Today is {today.isoformat()}.\n\n"
        "Output fields:\n"
        "- rewritten_question: standalone query for billing SQL assistant\n"
        "- hint: short semicolon-separated summary for UI/source hint\n"
        "- window_start/window_end: YYYY-MM-DD if explicit or inferred safely\n"
        "- time_confident: true if window is confidently inferred from user intent\n"
        "- env: prod/dev/null\n"
        "- service, billing_project_id, billing_region (or null)\n"
        "- wants_total / wants_top booleans\n\n"
        "Time rules:\n"
        "- For 'this month till now' => first day of this month to today.\n"
        "- For 'March and April combined, until now' (or similar multi-month-to-date phrasing), "
        "set window_start to first day of the first named month and window_end=today.\n"
        "- For a named full month like 'March 2026', return full calendar bounds (e.g. 2026-03-01..2026-03-31).\n"
        "- For 'last N days' => inclusive range ending today.\n"
        "- For 'used till now' / 'until now' with no explicit month anchor, prefer full_history_to_date.\n"
        "- Set time_scope to one of: explicit_window, month_to_date, full_history_to_date, unsure.\n"
        "- If user is vague and cannot be resolved, set time_scope=unsure, time_confident=false and omit window fields.\n\n"
        f"Conversation:\n{message}"
    )


def _default_till_now_scope() -> str:
    v = os.environ.get("BILLING_DEFAULT_TILL_NOW_SCOPE", "full_history").strip().lower()
    if v in {"month_to_date", "mtd"}:
        return "month_to_date"
    return "full_history_to_date"


def _full_history_start(today: date) -> date:
    raw = os.environ.get("BILLING_FULL_HISTORY_START_DATE", "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    # Safe default: start of current year (still bounded by dry-run bytes cap).
    return date(today.year, 1, 1)


def _looks_discovery_query(text: str) -> bool:
    t = text.lower()
    return (
        ("unique" in t or "distinct" in t or "list" in t)
        and ("service" in t or "services" in t)
    )


def _mentions_till_now(text: str) -> bool:
    t = " ".join(text.lower().split())
    return bool(
        ("till now" in t)
        or ("until now" in t)
        or ("till date" in t)
        or ("to date" in t)
        or ("so far" in t)
    )


def _parse_json(raw: str) -> BillingRoutePayload:
    text = (raw or "").strip()
    if not text:
        raise RuntimeError("Router returned empty response")
    data = json.loads(text)
    return BillingRoutePayload.model_validate(data)


def _invoke_vertex(prompt: str) -> BillingRoutePayload:
    if not _VERTEX_OK:
        raise RuntimeError("vertex SDK unavailable")
    project = (os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("BQ_BILLING_PROJECT", "")).strip()
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT required for Vertex router")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1").strip()
    vertexai.init(project=project, location=location)
    model = GenerativeModel(_model_name())
    cfg = GenerationConfig(
        temperature=0.1,
        max_output_tokens=2048,
        response_mime_type="application/json",
        response_schema=ROUTER_SCHEMA,
    )
    r = model.generate_content(prompt, generation_config=cfg)
    return _parse_json(r.text or "")


def _invoke_google_ai(prompt: str) -> BillingRoutePayload:
    if not _GENAI_OK:
        raise RuntimeError("google-generativeai unavailable")
    key = _google_ai_key()
    if not key:
        raise RuntimeError("GOOGLE_AI_API_KEY or GEMINI_API_KEY required")
    genai.configure(api_key=key)
    model = genai.GenerativeModel(_model_name())
    cfg = genai.GenerationConfig(
        temperature=0.1,
        max_output_tokens=2048,
        response_mime_type="application/json",
        response_schema=ROUTER_SCHEMA,
    )
    r = model.generate_content(prompt, generation_config=cfg)
    text = (getattr(r, "text", None) or "").strip()
    if not text and r.candidates:
        parts = r.candidates[0].content.parts
        text = "".join(getattr(p, "text", "") for p in parts).strip()
    return _parse_json(text)


def _invoke_router(prompt: str) -> BillingRoutePayload:
    provider = _provider()
    key = _google_ai_key()
    if provider == "vertex":
        return _invoke_vertex(prompt)
    if provider == "google_ai":
        return _invoke_google_ai(prompt)
    v_err: BaseException | None = None
    if _VERTEX_OK:
        try:
            return _invoke_vertex(prompt)
        except Exception as e:  # noqa: BLE001
            v_err = e
            if not key:
                raise
    if key and _GENAI_OK:
        return _invoke_google_ai(prompt)
    if v_err:
        raise RuntimeError("Vertex context router failed and no Google AI fallback key configured") from v_err
    raise RuntimeError("No router backend available")


def resolve_cost_context(message: str, *, today: date) -> ResolvedCostContext:
    payload = _invoke_router(_router_prompt(message, today))
    scope = (payload.time_scope or "").strip().lower()
    raw_hint = payload.hint.strip() or "no explicit filters"
    text = payload.rewritten_question or message
    ws: date | None = None
    we: date | None = None
    if payload.window_start and payload.window_end:
        try:
            ws = date.fromisoformat(payload.window_start.strip())
            we = date.fromisoformat(payload.window_end.strip())
        except ValueError:
            ws = None
            we = None
    if ws is not None and we is not None:
        hint = raw_hint
    elif scope in {"month_to_date", "mtd"} or "this month" in text.lower():
        ws = date(today.year, today.month, 1)
        we = today
        hint = raw_hint + "; defaulted to this month (month-to-date)"
    elif scope == "full_history_to_date" or _mentions_till_now(text) or _looks_discovery_query(text):
        ws = _full_history_start(today)
        we = today
        hint = raw_hint + f"; defaulted to full history-to-date ({ws} to {we})"
    else:
        # Ambiguous fallback policy is configurable; default is full-history-to-date.
        if _default_till_now_scope() == "month_to_date":
            ws = date(today.year, today.month, 1)
            we = today
            hint = raw_hint + "; defaulted to this month (month-to-date)"
        else:
            ws = _full_history_start(today)
            we = today
            hint = raw_hint + f"; defaulted to full history-to-date ({ws} to {we})"
    env = payload.env.strip().lower() if payload.env else None
    if env not in {"prod", "dev"}:
        env = None
    return ResolvedCostContext(
        rewritten_question=payload.rewritten_question.strip(),
        hint=hint,
        window_start=ws,
        window_end=we,
        env=env,
        service=(payload.service or "").strip().lower() or None,
        billing_project_id=(payload.billing_project_id or "").strip().lower() or None,
        billing_region=(payload.billing_region or "").strip().lower() or None,
        wants_total=bool(payload.wants_total),
        wants_top=bool(payload.wants_top),
    )
