"""
PA Orchestrator — discovers Cost Agent via Agent Card, proxies A2A SSE to the frontend.
Structured for future multi-agent ADK orchestration; HTTP layer is the integration seam for Phase 1.

Phase 3 — Proactive workflows: Cloud Scheduler POSTs to /proactive/morning-brief with header
X-API-Key (value from env PROACTIVE_API_KEY). Configure the scheduler OIDC or VPC as needed;
the API key is a basic guard against fully public unauthenticated triggers.

Phase 4 — Vertex Claude Haiku/Sonnet routing, session compression, then A2A to Cost Agent when needed.
Set ENABLE_VERTEX_ROUTING=true and GOOGLE_CLOUD_PROJECT; optional X-Session-Id / body.session_id for memory.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator

import httpx
import uuid
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

import agent_engine_chat
import db
import session_repository
from auth import AuthContext, get_auth_context

from intelligence import (
    DEFAULT_OUT_OF_SCOPE_REPLY,
    ENABLE_VERTEX_ROUTING,
    IntentResult,
    classify_intent_haiku,
    classify_intent_local,
    compress_session_context_with_ids,
    parse_sse_bytes_to_text,
    refine_task_sonnet,
    sse_stream_has_error,
    stream_synthetic_a2a,
)
from telemetry import setup_observability

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("orchestrator")

COST_AGENT_CARD_URL = os.environ.get(
    "COST_AGENT_CARD_URL",
    "http://127.0.0.1:8001/.well-known/agent.json",
)
COST_AGENT_TASKS_URL = os.environ.get(
    "COST_AGENT_TASKS_URL",
    "http://127.0.0.1:8001/tasks/send",
)

AGENT_CARD_RETRY_SECONDS = int(os.environ.get("AGENT_CARD_RETRY_SECONDS", "90"))
AGENT_CARD_RETRY_INTERVAL = float(os.environ.get("AGENT_CARD_RETRY_INTERVAL", "2"))

# Phase 3: required for /proactive/* (e.g. mount from Secret Manager on Cloud Run).
PROACTIVE_API_KEY = os.environ.get("PROACTIVE_API_KEY", "").strip()
RETENTION_API_KEY = os.environ.get("RETENTION_API_KEY", "").strip() or PROACTIVE_API_KEY

# Synthetic user utterance for scheduled morning brief (routed to Cost Agent via A2A).
MORNING_BRIEF_PROMPT = (
    "Summarize the cloud costs for the last 24 hours, highlighting any spikes."
)

SPECIALIST_CARD: dict[str, Any] | None = None


def send_push_notification(summary_text: str) -> None:
    """Placeholder for email / FCM / Slack; Phase 3 logs only."""
    logger.info(
        "[push_notification] (placeholder) delivered morning brief — %d characters\n%s",
        len(summary_text),
        summary_text,
    )


def _extract_a2a_text(payload: dict[str, Any]) -> str:
    """Pull assistant-visible text from an A2A-style SSE JSON object."""
    status = payload.get("status")
    if not isinstance(status, dict):
        status = {}
    message = status.get("message")
    if not isinstance(message, dict):
        message = {}
    artifact = payload.get("artifact")
    if not isinstance(artifact, dict):
        artifact = {}
    from_msg = message.get("parts")
    from_art = artifact.get("parts")
    parts = from_msg if isinstance(from_msg, list) and from_msg else from_art
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for p in parts:
        if isinstance(p, dict) and p.get("text"):
            out.append(str(p["text"]))
    return "".join(out)


async def verify_proactive_api_key(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> None:
    if not PROACTIVE_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Proactive routes disabled: set PROACTIVE_API_KEY",
        )
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    if len(x_api_key) != len(PROACTIVE_API_KEY) or not secrets.compare_digest(
        x_api_key, PROACTIVE_API_KEY
    ):
        raise HTTPException(status_code=401, detail="Invalid API key")


async def verify_retention_api_key(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> None:
    if not RETENTION_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Retention disabled: set RETENTION_API_KEY or PROACTIVE_API_KEY",
        )
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    if len(x_api_key) != len(RETENTION_API_KEY) or not secrets.compare_digest(
        x_api_key, RETENTION_API_KEY
    ):
        raise HTTPException(status_code=401, detail="Invalid API key")


def _encode_session_cursor(updated_at: datetime, session_id: uuid.UUID) -> str:
    payload = {"u": updated_at.isoformat(), "i": str(session_id)}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_session_cursor(cursor: str | None) -> tuple[datetime | None, uuid.UUID | None]:
    if not cursor or not str(cursor).strip():
        return None, None
    pad = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + pad)
        o = json.loads(raw.decode())
        return datetime.fromisoformat(o["u"]), uuid.UUID(o["i"])
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cursor") from None


async def run_cost_agent_task_to_completion(message: str) -> str:
    """
    Non-interactive A2A call: POST /tasks/send, consume SSE until stream ends, return concatenated text.
    """
    collected: list[str] = []
    buffer = ""
    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream(
            "POST",
            COST_AGENT_TASKS_URL,
            json={"message": message},
            headers={"Accept": "text/event-stream"},
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise HTTPException(
                    status_code=502,
                    detail=f"Cost agent error {response.status_code}: {body[:800]}",
                )
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                buffer += chunk.decode("utf-8", errors="replace")
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
                    if obj.get("error"):
                        raise HTTPException(
                            status_code=502,
                            detail=str(obj.get("detail", "cost agent stream error")),
                        )
                    text = _extract_a2a_text(obj)
                    if text:
                        collected.append(text)
    return "".join(collected)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global SPECIALIST_CARD
    SPECIALIST_CARD = None
    await db.init_db()
    try:
        deadline = time.monotonic() + AGENT_CARD_RETRY_SECONDS
        async with httpx.AsyncClient(timeout=30.0) as client:
            while time.monotonic() < deadline:
                try:
                    r = await client.get(COST_AGENT_CARD_URL)
                    r.raise_for_status()
                    SPECIALIST_CARD = r.json()
                    logger.info(
                        "Loaded specialist Agent Card: %s",
                        SPECIALIST_CARD.get("name", "(unknown)"),
                    )
                    break
                except Exception as e:
                    logger.warning(
                        "Agent Card fetch failed (%s), retrying in %.1fs...",
                        e,
                        AGENT_CARD_RETRY_INTERVAL,
                    )
                    await asyncio.sleep(AGENT_CARD_RETRY_INTERVAL)
            if SPECIALIST_CARD is None:
                logger.warning(
                    "Could not load Agent Card from %s after ~%ss; /health will show unloaded",
                    COST_AGENT_CARD_URL,
                    AGENT_CARD_RETRY_SECONDS,
                )
        yield
    finally:
        await db.close_db()


app = FastAPI(title="PA Orchestrator", version="1.0.0", lifespan=lifespan)

# CORS: comma-separated origins, or "*" for any origin (credentials disabled — OK for tokenless SSE/chat).
# Include 127.0.0.1 and localhost — browsers treat them as different origins; dev servers often use either.
_cors_raw = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000",
).strip()
if _cors_raw == "*":
    _cors_origins: list[str] = ["*"]
    _cors_credentials = False
else:
    _cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()]
    _cors_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Session-Id"],
)


class ChatMessage(BaseModel):
    message: str = Field(..., description="User question for the assistant")
    session_id: str | None = Field(
        default=None,
        description="Client session id for compressed multi-turn context",
    )
    client_message_id: str | None = Field(
        default=None,
        max_length=128,
        description="Idempotency key for this user turn (per session)",
    )


class ChatSessionItem(BaseModel):
    id: str
    title: str | None
    created_at: str
    updated_at: str
    cleared_at: str | None


class ChatSessionsPage(BaseModel):
    items: list[ChatSessionItem]
    next_cursor: str | None = None


class MessageRowDto(BaseModel):
    id: int
    role: str
    content: str
    created_at: str
    client_message_id: str | None = None


class RetentionBody(BaseModel):
    dry_run: bool = False


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "specialist_card_loaded": SPECIALIST_CARD is not None,
        "specialist_name": (SPECIALIST_CARD or {}).get("name"),
        "vertex_routing_enabled": ENABLE_VERTEX_ROUTING,
        "agent_engine_chat_enabled": agent_engine_chat.is_agent_engine_chat_enabled(),
        "orchestrator_engine_resource_set": bool(
            agent_engine_chat.resolved_engine_resource()
        ),
        "chat_persistence_ok": await db.check_db(),
    }


@app.get("/.well-known/orchestrator.json")
async def orchestrator_meta():
    """Lightweight discovery for debugging; not part of A2A spec."""
    return JSONResponse(
        {
            "name": "PA Orchestrator",
            "routesCostQueriesTo": COST_AGENT_TASKS_URL,
            "specialistCardUrl": COST_AGENT_CARD_URL,
            "cardCached": SPECIALIST_CARD,
        }
    )


def _pack_cost_agent_transcript(compressed: list[dict[str, str]]) -> str:
    """
    Local specialist path: send full (possibly compressed) transcript so follow-ups
    keep project / date / scope without Vertex Sonnet refinement.
    """
    if not compressed:
        return ""
    lines = "\n".join(
        f"{m.get('role', 'user').upper()}: {m.get('content', '')}"
        for m in compressed
    )
    return (
        "Multi-turn conversation. Answer the most recent USER message using the same "
        "project, date, and filters as earlier turns unless the user overrides them.\n\n"
        f"{lines}"
    )


async def proxy_cost_agent_sse(message: str) -> AsyncIterator[bytes]:
    payload = {"message": message}
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            async with client.stream(
                "POST",
                COST_AGENT_TASKS_URL,
                json=payload,
                headers={"Accept": "text/event-stream"},
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    err = body.decode("utf-8", errors="replace")
                    yield f"data: {json.dumps({'error': True, 'detail': err, 'status': response.status_code})}\n\n".encode()
                    return
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.RequestError as e:
            yield f"data: {json.dumps({'error': True, 'detail': str(e)})}\n\n".encode()


async def chat_orchestrated_loop(
    body: ChatMessage,
    session_id: uuid.UUID,
    pool: Any,
    auth: AuthContext,
) -> AsyncIterator[bytes]:
    """
    Phase 4 main loop (Postgres-backed):
    idempotent user row, effective history (summary + tail), Gemini compression + optional persist,
    intent routing, cost agent SSE, assistant row persisted.
    """
    user_text = body.message.strip()
    cmid = (body.client_message_id or "").strip() or None

    async with pool.acquire() as conn:
        ins_status, _uid = await session_repository.append_user_message_idempotent(
            conn,
            session_id,
            auth.tenant_id,
            auth.sub,
            user_text,
            cmid,
        )
        if ins_status == "duplicate" and cmid:
            replay = await session_repository.get_assistant_after_user_client_id(
                conn, session_id, auth.tenant_id, auth.sub, cmid
            )
            if replay:
                async for chunk in stream_synthetic_a2a(replay):
                    yield chunk
                return

        rows = await session_repository.load_effective_history_rows(
            conn, session_id, auth.tenant_id, auth.sub
        )

    comp = await compress_session_context_with_ids(rows)
    if comp.persist_summary:
        async with pool.acquire() as conn:
            stxt, covers = comp.persist_summary
            await session_repository.upsert_summary(
                conn,
                session_id,
                auth.tenant_id,
                auth.sub,
                stxt,
                covers,
            )
    compressed = comp.messages

    if not ENABLE_VERTEX_ROUTING:
        intent = classify_intent_local(user_text, compressed)
    else:
        try:
            intent = await classify_intent_haiku(compressed, user_text)
        except Exception as e:
            logger.warning("Haiku intent classification failed; routing to metrics: %s", e)
            intent = IntentResult(intent="metrics", reply="")

    if intent.intent == "clear":
        async with pool.acquire() as conn:
            await session_repository.clear_session_messages(
                conn, session_id, auth.tenant_id, auth.sub
            )
            await session_repository.delete_agent_engine_binding(
                conn, session_id, auth.tenant_id, auth.sub
            )
        reply = intent.reply or "Chat cleared. Ask me anything about your cloud costs."
        async for chunk in stream_synthetic_a2a(reply):
            yield chunk
        return

    if intent.intent == "chitchat":
        reply = intent.reply or (
            "Hi! I can help you explore cloud cost and usage metrics. What would you like to know?"
        )
        async for chunk in stream_synthetic_a2a(reply):
            yield chunk
        async with pool.acquire() as conn:
            await session_repository.append_message(
                conn, session_id, auth.tenant_id, auth.sub, "assistant", reply
            )
        return

    if intent.intent == "out_of_scope":
        reply = intent.reply or DEFAULT_OUT_OF_SCOPE_REPLY
        async for chunk in stream_synthetic_a2a(reply):
            yield chunk
        async with pool.acquire() as conn:
            await session_repository.append_message(
                conn, session_id, auth.tenant_id, auth.sub, "assistant", reply
            )
        return

    try:
        if ENABLE_VERTEX_ROUTING:
            specialist_task = await refine_task_sonnet(compressed, user_text)
        else:
            specialist_task = _pack_cost_agent_transcript(compressed)
    except Exception as e:
        logger.warning("Sonnet task refinement failed; using transcript fallback: %s", e)
        specialist_task = _pack_cost_agent_transcript(compressed)

    buf = bytearray()
    async for chunk in proxy_cost_agent_sse(specialist_task):
        buf.extend(chunk)
        yield chunk
    assistant_text = parse_sse_bytes_to_text(bytes(buf))
    async with pool.acquire() as conn:
        await session_repository.append_message(
            conn, session_id, auth.tenant_id, auth.sub, "assistant", assistant_text
        )


async def chat_via_agent_engine_persisted(
    body: ChatMessage,
    session_id: uuid.UUID,
    pool: Any,
    auth: AuthContext,
) -> AsyncIterator[bytes]:
    """
    Vertex Agent Engine: handle clear locally; idempotent user row; persist assistant after stream.
    """
    user_text = body.message.strip()
    cmid = (body.client_message_id or "").strip() or None
    intent = classify_intent_local(user_text, [])

    if intent.intent == "clear":
        async with pool.acquire() as conn:
            await session_repository.clear_session_messages(
                conn, session_id, auth.tenant_id, auth.sub
            )
            await session_repository.delete_agent_engine_binding(
                conn, session_id, auth.tenant_id, auth.sub
            )
        reply = intent.reply or "Chat cleared. Ask me anything about your cloud costs."
        async for chunk in stream_synthetic_a2a(reply):
            yield chunk
        return

    async with pool.acquire() as conn:
        ins_status, _ = await session_repository.append_user_message_idempotent(
            conn, session_id, auth.tenant_id, auth.sub, user_text, cmid
        )
        if ins_status == "duplicate" and cmid:
            replay = await session_repository.get_assistant_after_user_client_id(
                conn, session_id, auth.tenant_id, auth.sub, cmid
            )
            if replay:
                async for chunk in stream_synthetic_a2a(replay):
                    yield chunk
                return

    buf = bytearray()
    async for chunk in agent_engine_chat.stream_chat_via_agent_engine(
        user_text,
        str(session_id),
        pool,
        auth.tenant_id,
        auth.sub,
    ):
        buf.extend(chunk)
        yield chunk

    raw = bytes(buf)
    if not sse_stream_has_error(raw):
        assistant_text = parse_sse_bytes_to_text(raw)
        if assistant_text.strip():
            async with pool.acquire() as conn:
                await session_repository.append_message(
                    conn,
                    session_id,
                    auth.tenant_id,
                    auth.sub,
                    "assistant",
                    assistant_text,
                )


@app.post("/chat/stream")
async def chat_stream(
    body: ChatMessage,
    auth: AuthContext = Depends(get_auth_context),
):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message is required")

    pool = db.get_pool()
    async with pool.acquire() as conn:
        canonical_id, _ = await session_repository.ensure_chat_session(
            conn, auth.tenant_id, auth.sub, body.session_id
        )
    session_str = str(canonical_id)

    if agent_engine_chat.is_agent_engine_chat_enabled():
        stream = chat_via_agent_engine_persisted(body, canonical_id, pool, auth)
    else:
        stream = chat_orchestrated_loop(body, canonical_id, pool, auth)

    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-Id": session_str,
        },
    )


@app.get("/chat/sessions", response_model=ChatSessionsPage)
async def list_chat_sessions(
    auth: AuthContext = Depends(get_auth_context),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
):
    cu, cid = _decode_session_cursor(cursor)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await session_repository.list_sessions_keyset(
            conn, auth.tenant_id, auth.sub, limit, cu, cid
        )
    items: list[ChatSessionItem] = []
    next_cur: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cur = _encode_session_cursor(last["updated_at"], last["id"])
    for r in rows:
        items.append(
            ChatSessionItem(
                id=str(r["id"]),
                title=r["title"],
                created_at=r["created_at"].isoformat() if r["created_at"] else "",
                updated_at=r["updated_at"].isoformat() if r["updated_at"] else "",
                cleared_at=r["cleared_at"].isoformat() if r["cleared_at"] else None,
            )
        )
    return ChatSessionsPage(items=items, next_cursor=next_cur)


@app.get("/chat/sessions/{session_id}/messages", response_model=list[MessageRowDto])
async def list_chat_session_messages(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        sess = await session_repository.get_session_row(
            conn, session_id, auth.tenant_id, auth.sub
        )
        if not sess:
            raise HTTPException(status_code=404, detail="Session not found")
        msgs = await session_repository.list_messages_detailed(
            conn, session_id, auth.tenant_id, auth.sub
        )
    return [
        MessageRowDto(
            id=int(m["id"]),
            role=str(m["role"]),
            content=str(m["content"]),
            created_at=m["created_at"].isoformat() if m["created_at"] else "",
            client_message_id=m["client_message_id"],
        )
        for m in msgs
    ]


@app.get("/chat/sessions/{session_id}/export")
async def export_chat_session(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        bundle = await session_repository.export_session_bundle(
            conn, session_id, auth.tenant_id, auth.sub
        )
    if not bundle:
        raise HTTPException(status_code=404, detail="Session not found")
    body = json.dumps(bundle, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="chat-session-{session_id}.json"'
        },
    )


@app.delete("/chat/sessions/{session_id}", status_code=204)
async def delete_chat_session(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        ok = await session_repository.delete_session_for_user(
            conn, session_id, auth.tenant_id, auth.sub
        )
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return None


@app.post("/internal/chat/retention")
async def chat_retention(
    body: RetentionBody,
    _: None = Depends(verify_retention_api_key),
):
    days = int(os.environ.get("CHAT_RETENTION_DAYS", "90"))
    pool = db.get_pool()
    async with pool.acquire() as conn:
        if body.dry_run:
            n = await conn.fetchval(
                """
                SELECT count(*)::int FROM chat_sessions
                WHERE updated_at < (now() - $1::int * interval '1 day')
                """,
                days,
            )
            return {"dry_run": True, "would_delete": n or 0, "retention_days": days}
        deleted = await session_repository.delete_sessions_older_than(conn, days)
    return {"dry_run": False, "deleted": deleted, "retention_days": days}


@app.post("/proactive/morning-brief")
async def proactive_morning_brief(_: None = Depends(verify_proactive_api_key)):
    """
    Phase 3 — invoked by GCP Cloud Scheduler (HTTP POST + X-API-Key).
    Runs the morning prompt through the Cost Metrics specialist (A2A), then notifies (log placeholder).
    """
    summary_text = await run_cost_agent_task_to_completion(MORNING_BRIEF_PROMPT)
    send_push_notification(summary_text)
    return {
        "status": "ok",
        "workflow": "morning-brief",
        "summary_char_count": len(summary_text),
    }


setup_observability(app, "pa-orchestrator")
