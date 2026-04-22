from __future__ import annotations

import json
import os
import re
import warnings
from dataclasses import dataclass
from datetime import date, timedelta
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
    intent_type: str | None = None
    required_slots: list[str] | None = None
    resolved_slots: dict[str, Any] | None = None
    clarification_priority: str | None = None
    needs_clarification: bool = False
    clarification_question: str | None = None
    clarification_options: list[str] | None = None
    clarification_kind: str | None = None
    missing_slots: list[str] | None = None
    time_scope: str | None = Field(
        default=None,
        description="explicit_window|month_to_date|full_history_to_date|unsure",
    )


@dataclass(frozen=True)
class ResolvedCostContext:
    rewritten_question: str
    hint: str
    window_start: date | None
    window_end: date | None
    env: str | None
    service: str | None
    billing_project_id: str | None
    billing_region: str | None
    wants_total: bool
    wants_top: bool
    needs_clarification: bool
    clarification_question: str | None
    clarification_options: list[str]
    clarification_kind: str | None
    missing_slots: list[str]
    intent_type: str | None
    required_slots: list[str]
    resolved_slots: dict[str, Any]
    clarification_priority: str | None


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
        "intent_type": {"type": "string"},
        "required_slots": {"type": "array", "items": {"type": "string"}},
        "resolved_slots": {"type": "object"},
        "clarification_priority": {"type": "string"},
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": "string"},
        "clarification_options": {"type": "array", "items": {"type": "string"}},
        "clarification_kind": {"type": "string"},
        "missing_slots": {"type": "array", "items": {"type": "string"}},
        "time_scope": {"type": "string"},
    },
    "required": [
        "rewritten_question",
        "hint",
        "time_confident",
        "wants_total",
        "wants_top",
        "needs_clarification",
    ],
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
        "LLM-first slot contract:\n"
        "- Set intent_type to one of: cost_total, top_n_ranking, compare, schema, discovery, other.\n"
        "- Set required_slots to unresolved requirements among: time_window, top_n, compare_scope, compare_entities, column_name.\n"
        "- Set resolved_slots as an object with any known slot values (example keys: time_window, top_n, compare_scope, service_a, service_b, column_name).\n"
        "- Set clarification_priority to exactly one unresolved slot when clarification is needed.\n\n"
        "Clarification rules:\n"
        "- If required slots are missing, set needs_clarification=true.\n"
        "- Fill clarification_question with one concise question.\n"
        "- Fill clarification_options with 2-5 concrete options when useful.\n"
        "- Fill clarification_kind with one of: time_window, compare_scope, compare_entities, top_n, schema_column, other.\n"
        "- Fill missing_slots with required unresolved fields.\n"
        "- If question is executable, set needs_clarification=false and leave clarification_* empty.\n\n"
        "Priority rules:\n"
        "- For ranking asks like 'most expensive services', ask for top_n first if missing.\n"
        "- For compare asks, ask compare_scope -> compare_entities -> time_window (one at a time).\n"
        "- For cost_total asks without an explicit time window, ask for time_window instead of guessing.\n\n"
        "Time rules:\n"
        "- For 'this month till now' => first day of this month to today.\n"
        "- For 'March and April combined, until now' (or similar multi-month-to-date phrasing), "
        "set window_start to first day of the first named month and window_end=today.\n"
        "- For a named full month like 'March 2026', return full calendar bounds (e.g. 2026-03-01..2026-03-31).\n"
        "- For 'last N days' => inclusive range ending today.\n"
        "- For 'this year' => January 1st of current year through today.\n"
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


def _slot_list(value: list[str] | None) -> list[str]:
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def _normalized_intent(payload: BillingRoutePayload, message: str) -> str:
    intent = (payload.intent_type or "").strip().lower()
    if intent:
        return intent
    text = (payload.rewritten_question or message).lower()
    if payload.wants_top:
        return "top_n_ranking"
    if "compare" in text or " vs " in text:
        return "compare"
    if payload.wants_total:
        return "cost_total"
    return "other"


def _missing_required_slots(
    required_slots: list[str],
    resolved_slots: dict[str, Any],
    *,
    window_resolved: bool,
) -> list[str]:
    missing: list[str] = []
    for slot in required_slots:
        if slot == "time_window":
            if not window_resolved and not str(resolved_slots.get("time_window") or "").strip():
                missing.append(slot)
            continue
        if slot == "compare_entities":
            service_a = str(resolved_slots.get("service_a") or "").strip()
            service_b = str(resolved_slots.get("service_b") or "").strip()
            if not service_a or not service_b:
                missing.append(slot)
            continue
        value = resolved_slots.get(slot)
        if isinstance(value, str):
            if not value.strip():
                missing.append(slot)
        elif value is None:
            missing.append(slot)
    return missing


def _sanitized_str(s: str | None) -> str | None:
    t = (s or "").strip()
    if not t or t.lower() in ("null", "none", "undefined"):
        return None
    return t


def _apply_deterministic_slot_overrides(
    message: str,
    today: date,
    scope: str,
    *,
    window_from_payload: bool,
    ws: date | None,
    we: date | None,
    resolved_slots: dict[str, Any],
    required_slots: list[str],
    missing_slots: list[str],
) -> tuple[str, bool, date | None, date | None, dict[str, Any], list[str], list[str]]:
    """Remove false-positive missing slots (explicit top-N, compare pair, time window)."""
    msg = message
    m_lower = " ".join(msg.lower().split())
    rslots: dict[str, Any] = dict(resolved_slots)
    rreq = list(required_slots)
    miss = list(missing_slots)
    scope_out = scope
    wf, w0, w1 = window_from_payload, ws, we

    m_top = re.search(r"\btop\s*(\d+)\b", msg, re.IGNORECASE)
    if m_top:
        rslots["top_n"] = m_top.group(1)
        miss = [s for s in miss if s != "top_n"]
        rreq = [s for s in rreq if s != "top_n"]

    m_days = re.search(r"\blast\s+(\d+)\s+days?\b", m_lower)
    if m_days:
        n = int(m_days.group(1))
        w1 = today
        w0 = today - timedelta(days=n - 1)
        wf = True
        rslots["time_window"] = f"last {n} days"
        miss = [s for s in miss if s != "time_window"]
        rreq = [s for s in rreq if s != "time_window"]
        if scope_out in ("", "unsure"):
            scope_out = "explicit_window"

    if re.search(r"\bthis month\b", m_lower) and not wf:
        w0 = date(today.year, today.month, 1)
        w1 = today
        wf = True
        rslots["time_window"] = "this month (month-to-date)"
        miss = [s for s in miss if s != "time_window"]
        rreq = [s for s in rreq if s != "time_window"]
        if scope_out in ("", "unsure"):
            scope_out = "month_to_date"

    gcp_a = re.search(r"cloud\s*sql", m_lower) is not None
    gcp_b = re.search(r"vertex(?:\s*ai)?", m_lower) is not None
    if gcp_a and gcp_b:
        rslots.setdefault("service_a", "Cloud SQL")
        rslots.setdefault("service_b", "Vertex AI")
        for sname in ("compare_scope", "compare_entities"):
            miss = [s for s in miss if s != sname]
            rreq = [s for s in rreq if s != sname]

    if rslots.get("service_a") and rslots.get("service_b"):
        for sname in ("compare_scope", "compare_entities"):
            miss = [s for s in miss if s != sname]
            rreq = [s for s in rreq if s != sname]

    return (
        scope_out,
        wf,
        w0,
        w1,
        rslots,
        rreq,
        miss,
    )


def _clarification_for_slot(slot: str) -> tuple[str, list[str], str]:
    if slot == "top_n":
        return (
            "How many results should I return for 'most expensive'?",
            ["Top 3", "Top 5", "Top 10"],
            "top_n",
        )
    if slot == "compare_scope":
        return (
            "What two scopes should I compare?",
            ["prod vs dev", "project A vs project B", "service A vs service B"],
            "compare_scope",
        )
    if slot == "compare_entities":
        return (
            "Which two services should I compare?",
            ["Cloud SQL vs Vertex AI", "BigQuery vs Cloud Storage", "Cloud Run vs Compute Engine"],
            "compare_entities",
        )
    if slot == "column_name":
        return (
            "Which column should I use?",
            [],
            "schema_column",
        )
    return (
        "What time window should I use for this cost query?",
        ["Last 7 days", "This month (month-to-date)", "Full history to date"],
        "time_window",
    )


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
    window_from_payload = False
    if payload.window_start and payload.window_end:
        try:
            ws = date.fromisoformat(payload.window_start.strip())
            we = date.fromisoformat(payload.window_end.strip())
            window_from_payload = ws is not None and we is not None
        except ValueError:
            ws = None
            we = None
    intent = _normalized_intent(payload, message)
    required_slots = _slot_list(payload.required_slots)
    resolved_slots = dict(payload.resolved_slots if isinstance(payload.resolved_slots, dict) else {})
    if not required_slots:
        if intent == "top_n_ranking":
            required_slots = ["top_n", "time_window"]
        elif intent == "compare":
            required_slots = ["compare_scope", "compare_entities", "time_window"]
        elif intent == "cost_total":
            required_slots = ["time_window"]
    has_time_scope = scope in {"month_to_date", "mtd", "full_history_to_date"}
    resolved_time_window = str(resolved_slots.get("time_window") or "").strip()
    window_resolved = window_from_payload or has_time_scope or bool(resolved_time_window)
    missing_slots = _slot_list(payload.missing_slots)
    if not missing_slots:
        missing_slots = _missing_required_slots(required_slots, resolved_slots, window_resolved=window_resolved)
    (
        scope,
        window_from_payload,
        ws,
        we,
        resolved_slots,
        required_slots,
        missing_slots,
    ) = _apply_deterministic_slot_overrides(
        message,
        today,
        scope,
        window_from_payload=window_from_payload,
        ws=ws,
        we=we,
        resolved_slots=resolved_slots,
        required_slots=required_slots,
        missing_slots=missing_slots,
    )
    has_time_scope = scope in {"month_to_date", "mtd", "full_history_to_date"}
    resolved_time_window = str(resolved_slots.get("time_window") or "").strip()
    window_resolved = window_from_payload or has_time_scope or bool(resolved_time_window)
    missing_slots = _missing_required_slots(required_slots, resolved_slots, window_resolved=window_resolved)
    needs_clarification = bool(missing_slots)
    clarification_priority = (payload.clarification_priority or "").strip().lower() or None
    if clarification_priority in ("null", "none", "undefined", ""):
        clarification_priority = None
    if needs_clarification and not clarification_priority:
        clarification_priority = missing_slots[0] if missing_slots else None
    clarification_question = _sanitized_str((payload.clarification_question or "").strip() or None)
    clarification_options = [str(x).strip() for x in (payload.clarification_options or []) if str(x).strip()]
    kind_raw = _sanitized_str((payload.clarification_kind or "").strip() or None)
    clarification_kind = kind_raw
    if needs_clarification and clarification_priority and not clarification_question:
        clarification_question, clarification_options, clarification_kind = _clarification_for_slot(clarification_priority)
    if needs_clarification and clarification_priority and not clarification_kind:
        _, _, clarification_kind = _clarification_for_slot(clarification_priority)
    if not needs_clarification:
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
            if _default_till_now_scope() == "month_to_date":
                ws = date(today.year, today.month, 1)
                we = today
                hint = raw_hint + "; defaulted to this month (month-to-date)"
            else:
                ws = _full_history_start(today)
                we = today
                hint = raw_hint + f"; defaulted to full history-to-date ({ws} to {we})"
    else:
        hint = raw_hint

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
        wants_top=bool(payload.wants_top) or bool(str(resolved_slots.get("top_n") or "").strip()),
        needs_clarification=needs_clarification,
        clarification_question=clarification_question,
        clarification_options=clarification_options,
        clarification_kind=clarification_kind,
        missing_slots=missing_slots,
        intent_type=intent,
        required_slots=required_slots,
        resolved_slots=resolved_slots,
        clarification_priority=clarification_priority,
    )
