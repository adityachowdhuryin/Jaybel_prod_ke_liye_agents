"""
PA Orchestrator bridge for Vertex Agent Engine chat and session persistence.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator
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
    ENABLE_VERTEX_ROUTING,
    classify_intent_local,
    parse_sse_bytes_to_text,
    sse_stream_has_error,
    stream_synthetic_a2a,
)
from telemetry import setup_observability

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("orchestrator")

RETENTION_API_KEY = os.environ.get("RETENTION_API_KEY", "").strip()


async def verify_retention_api_key(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> None:
    if not RETENTION_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Retention disabled: set RETENTION_API_KEY",
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    try:
        logger.info("Orchestrator running in Agent Engine-only mode.")
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
            "mode": "agent_engine_only",
            "orchestratorEngineResource": agent_engine_chat.resolved_engine_resource(),
            "agentEngineChatEnabled": agent_engine_chat.is_agent_engine_chat_enabled(),
        }
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

    if not agent_engine_chat.is_agent_engine_chat_enabled():
        raise HTTPException(
            status_code=503,
            detail=(
                "Agent Engine chat is required in this build. "
                "Set ORCHESTRATOR_AGENT_ENGINE_RESOURCE and ensure ADC/IAM are configured."
            ),
        )

    stream = chat_via_agent_engine_persisted(body, canonical_id, pool, auth)

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


setup_observability(app, "pa-orchestrator")
