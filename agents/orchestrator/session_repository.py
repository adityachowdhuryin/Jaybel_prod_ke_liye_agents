"""Postgres persistence for chat sessions, messages, and Agent Engine bindings."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

import asyncpg


def _parse_client_session_id(raw: str | None) -> uuid.UUID | None:
    if not raw or not str(raw).strip():
        return None
    try:
        return uuid.UUID(str(raw).strip())
    except ValueError:
        return None


async def ensure_chat_session(
    conn: asyncpg.Connection,
    tenant_id: str,
    owner_user_id: str,
    client_session_id: str | None,
) -> tuple[uuid.UUID, bool]:
    """
    Resolve or create a chat session row for (tenant, user).
    If client_session_id is missing, invalid UUID, or not owned by this user → create a new session.
    Returns (canonical_session_id, created_new).
    """
    sid = _parse_client_session_id(client_session_id)
    if sid is not None:
        row = await conn.fetchrow(
            """
            SELECT id FROM chat_sessions
            WHERE id = $1 AND tenant_id = $2 AND owner_user_id = $3
            """,
            sid,
            tenant_id,
            owner_user_id,
        )
        if row:
            return row["id"], False

    row = await conn.fetchrow(
        """
        INSERT INTO chat_sessions (tenant_id, owner_user_id)
        VALUES ($1, $2)
        RETURNING id
        """,
        tenant_id,
        owner_user_id,
    )
    assert row is not None
    return row["id"], True


async def append_message(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
    role: str,
    content: str,
    client_message_id: str | None = None,
) -> None:
    row = await conn.fetchrow(
        """
        INSERT INTO chat_messages (session_id, role, content, client_message_id)
        SELECT $1::uuid, $2, $3, $4
        FROM chat_sessions s
        WHERE s.id = $1::uuid AND s.tenant_id = $5 AND s.owner_user_id = $6
        RETURNING chat_messages.id
        """,
        session_id,
        role,
        content,
        client_message_id,
        tenant_id,
        owner_user_id,
    )
    if row is None:
        raise ValueError("session not found or access denied")
    await conn.execute(
        "UPDATE chat_sessions SET updated_at = now() WHERE id = $1::uuid",
        session_id,
    )


async def append_user_message_idempotent(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
    content: str,
    client_message_id: str | None,
) -> tuple[Literal["inserted"] | "duplicate", int | None]:
    """
    Insert user message. If client_message_id duplicates within session, skip insert (duplicate).
    Returns ("inserted", new_msg_id) or ("duplicate", existing_user_row_id).
    """
    if client_message_id:
        existing = await conn.fetchval(
            """
            SELECT m.id
            FROM chat_messages m
            INNER JOIN chat_sessions s ON s.id = m.session_id
            WHERE m.session_id = $1::uuid
              AND m.client_message_id = $2
              AND m.role = 'user'
              AND s.tenant_id = $3
              AND s.owner_user_id = $4
            """,
            session_id,
            client_message_id,
            tenant_id,
            owner_user_id,
        )
        if existing is not None:
            return "duplicate", int(existing)

    try:
        await append_message(
            conn,
            session_id,
            tenant_id,
            owner_user_id,
            "user",
            content,
            client_message_id,
        )
    except asyncpg.UniqueViolationError:
        rid = await conn.fetchval(
            """
            SELECT m.id
            FROM chat_messages m
            INNER JOIN chat_sessions s ON s.id = m.session_id
            WHERE m.session_id = $1::uuid
              AND m.client_message_id = $2
              AND m.role = 'user'
              AND s.tenant_id = $3
              AND s.owner_user_id = $4
            """,
            session_id,
            client_message_id,
            tenant_id,
            owner_user_id,
        )
        return "duplicate", int(rid) if rid is not None else None

    rid = await conn.fetchval(
        """
        SELECT id FROM chat_messages
        WHERE session_id = $1::uuid AND role = 'user'
        ORDER BY id DESC LIMIT 1
        """,
        session_id,
    )
    return "inserted", int(rid) if rid is not None else None


async def get_assistant_after_user_client_id(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
    client_message_id: str,
) -> str | None:
    row = await conn.fetchrow(
        """
        SELECT am.content
        FROM chat_messages um
        INNER JOIN chat_sessions s ON s.id = um.session_id
        INNER JOIN chat_messages am
            ON am.session_id = um.session_id
            AND am.role = 'assistant'
            AND am.id > um.id
        WHERE um.session_id = $1::uuid
          AND um.role = 'user'
          AND um.client_message_id = $2
          AND s.tenant_id = $3
          AND s.owner_user_id = $4
        ORDER BY am.id ASC
        LIMIT 1
        """,
        session_id,
        client_message_id,
        tenant_id,
        owner_user_id,
    )
    if not row:
        return None
    return str(row["content"])


async def load_message_history(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
) -> list[dict[str, str]]:
    rows = await _fetch_messages(conn, session_id, tenant_id, owner_user_id, after_id=None)
    return [{"role": r["role"], "content": r["content"]} for r in rows]


async def _fetch_messages(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
    after_id: int | None,
) -> list[asyncpg.Record]:
    if after_id is None:
        return await conn.fetch(
            """
            SELECT m.id, m.role::text AS role, m.content, m.created_at, m.client_message_id
            FROM chat_messages m
            INNER JOIN chat_sessions s ON s.id = m.session_id
            WHERE m.session_id = $1::uuid AND s.tenant_id = $2 AND s.owner_user_id = $3
            ORDER BY m.created_at ASC, m.id ASC
            """,
            session_id,
            tenant_id,
            owner_user_id,
        )
    return await conn.fetch(
        """
        SELECT m.id, m.role::text AS role, m.content, m.created_at, m.client_message_id
        FROM chat_messages m
        INNER JOIN chat_sessions s ON s.id = m.session_id
        WHERE m.session_id = $1::uuid AND s.tenant_id = $2 AND s.owner_user_id = $3
          AND m.id > $4
        ORDER BY m.created_at ASC, m.id ASC
        """,
        session_id,
        tenant_id,
        owner_user_id,
        after_id,
    )


async def load_effective_history_rows(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
) -> list[tuple[int | None, str, str]]:
    """
    Chronological (msg_id, role, content) for model + compression.
    When a persisted summary exists, older messages are folded into one synthetic row (id None).
    """
    summ = await get_summary(conn, session_id, tenant_id, owner_user_id)
    if summ is None:
        rows = await _fetch_messages(conn, session_id, tenant_id, owner_user_id, None)
        return [(int(r["id"]), str(r["role"]), str(r["content"])) for r in rows]

    summary_text, covers_mid, _ = summ
    tail = await _fetch_messages(
        conn, session_id, tenant_id, owner_user_id, covers_mid
    )
    out: list[tuple[int | None, str, str]] = [
        (
            None,
            "user",
            "[PERSISTED SESSION SUMMARY]\n" + summary_text,
        )
    ]
    for r in tail:
        out.append((int(r["id"]), str(r["role"]), str(r["content"])))
    return out


async def get_summary(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
) -> tuple[str, int, datetime] | None:
    row = await conn.fetchrow(
        """
        SELECT sm.summary_text, sm.covers_up_to_message_id, sm.updated_at
        FROM chat_session_summaries sm
        INNER JOIN chat_sessions s ON s.id = sm.session_id
        WHERE sm.session_id = $1::uuid AND s.tenant_id = $2 AND s.owner_user_id = $3
        """,
        session_id,
        tenant_id,
        owner_user_id,
    )
    if not row:
        return None
    return (
        str(row["summary_text"]),
        int(row["covers_up_to_message_id"]),
        row["updated_at"],
    )


async def upsert_summary(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
    summary_text: str,
    covers_up_to_message_id: int,
) -> None:
    ok = await conn.fetchval(
        """
        SELECT 1 FROM chat_sessions
        WHERE id = $1::uuid AND tenant_id = $2 AND owner_user_id = $3
        """,
        session_id,
        tenant_id,
        owner_user_id,
    )
    if not ok:
        raise ValueError("session not found or access denied")
    await conn.execute(
        """
        INSERT INTO chat_session_summaries (session_id, summary_text, covers_up_to_message_id, updated_at)
        VALUES ($1::uuid, $2, $3, now())
        ON CONFLICT (session_id) DO UPDATE SET
            summary_text = EXCLUDED.summary_text,
            covers_up_to_message_id = EXCLUDED.covers_up_to_message_id,
            updated_at = now()
        """,
        session_id,
        summary_text,
        covers_up_to_message_id,
    )


async def delete_summary(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
) -> None:
    await conn.execute(
        """
        DELETE FROM chat_session_summaries sm
        USING chat_sessions s
        WHERE sm.session_id = s.id AND sm.session_id = $1::uuid
          AND s.tenant_id = $2 AND s.owner_user_id = $3
        """,
        session_id,
        tenant_id,
        owner_user_id,
    )


async def clear_session_messages(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
) -> None:
    await delete_summary(conn, session_id, tenant_id, owner_user_id)
    await conn.execute(
        """
        DELETE FROM chat_messages m
        USING chat_sessions s
        WHERE m.session_id = s.id
          AND s.id = $1::uuid
          AND s.tenant_id = $2
          AND s.owner_user_id = $3
        """,
        session_id,
        tenant_id,
        owner_user_id,
    )
    await conn.execute(
        """
        UPDATE chat_sessions SET updated_at = now(), cleared_at = now()
        WHERE id = $1::uuid AND tenant_id = $2 AND owner_user_id = $3
        """,
        session_id,
        tenant_id,
        owner_user_id,
    )


async def delete_agent_engine_binding(
    conn: asyncpg.Connection,
    client_session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
) -> None:
    await conn.execute(
        """
        DELETE FROM agent_engine_session_bindings
        WHERE client_session_id = $1::uuid AND tenant_id = $2 AND owner_user_id = $3
        """,
        client_session_id,
        tenant_id,
        owner_user_id,
    )


async def get_agent_engine_binding(
    conn: asyncpg.Connection,
    client_session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
) -> tuple[str, str] | None:
    row = await conn.fetchrow(
        """
        SELECT engine_user_id, engine_session_id
        FROM agent_engine_session_bindings
        WHERE client_session_id = $1::uuid AND tenant_id = $2 AND owner_user_id = $3
        """,
        client_session_id,
        tenant_id,
        owner_user_id,
    )
    if not row:
        return None
    return str(row["engine_user_id"]), str(row["engine_session_id"])


async def upsert_agent_engine_binding(
    conn: asyncpg.Connection,
    client_session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
    engine_user_id: str,
    engine_session_id: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO agent_engine_session_bindings (
            client_session_id, tenant_id, owner_user_id,
            engine_user_id, engine_session_id
        )
        VALUES ($1::uuid, $2, $3, $4, $5)
        ON CONFLICT (tenant_id, owner_user_id, client_session_id)
        DO UPDATE SET
            engine_user_id = EXCLUDED.engine_user_id,
            engine_session_id = EXCLUDED.engine_session_id,
            updated_at = now()
        """,
        client_session_id,
        tenant_id,
        owner_user_id,
        engine_user_id,
        engine_session_id,
    )


async def list_sessions_keyset(
    conn: asyncpg.Connection,
    tenant_id: str,
    owner_user_id: str,
    limit: int,
    cursor_updated_at: datetime | None,
    cursor_id: uuid.UUID | None,
) -> list[asyncpg.Record]:
    limit = max(1, min(limit, 100))
    if cursor_updated_at is None or cursor_id is None:
        return await conn.fetch(
            """
            SELECT s.id, s.title, s.created_at, s.updated_at, s.cleared_at
            FROM chat_sessions s
            WHERE s.tenant_id = $1 AND s.owner_user_id = $2
            ORDER BY s.updated_at DESC, s.id DESC
            LIMIT $3
            """,
            tenant_id,
            owner_user_id,
            limit + 1,
        )
    return await conn.fetch(
        """
        SELECT s.id, s.title, s.created_at, s.updated_at, s.cleared_at
        FROM chat_sessions s
        WHERE s.tenant_id = $1 AND s.owner_user_id = $2
          AND (s.updated_at, s.id) < ($3::timestamptz, $4::uuid)
        ORDER BY s.updated_at DESC, s.id DESC
        LIMIT $5
        """,
        tenant_id,
        owner_user_id,
        cursor_updated_at,
        cursor_id,
        limit + 1,
    )


async def get_session_row(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT id, title, created_at, updated_at, cleared_at
        FROM chat_sessions
        WHERE id = $1::uuid AND tenant_id = $2 AND owner_user_id = $3
        """,
        session_id,
        tenant_id,
        owner_user_id,
    )


async def list_messages_detailed(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT m.id, m.role::text AS role, m.content, m.created_at, m.client_message_id
        FROM chat_messages m
        INNER JOIN chat_sessions s ON s.id = m.session_id
        WHERE m.session_id = $1::uuid AND s.tenant_id = $2 AND s.owner_user_id = $3
        ORDER BY m.created_at ASC, m.id ASC
        """,
        session_id,
        tenant_id,
        owner_user_id,
    )


async def delete_session_for_user(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
) -> bool:
    result = await conn.execute(
        """
        DELETE FROM chat_sessions
        WHERE id = $1::uuid AND tenant_id = $2 AND owner_user_id = $3
        """,
        session_id,
        tenant_id,
        owner_user_id,
    )
    return result != "DELETE 0"


async def delete_sessions_older_than(
    conn: asyncpg.Connection, days: int
) -> int:
    """Operator retention: DELETE sessions where updated_at is older than N days. Returns deleted count."""
    if days < 1:
        return 0
    rows = await conn.fetch(
        """
        DELETE FROM chat_sessions
        WHERE updated_at < (now() - $1::int * interval '1 day')
        RETURNING id
        """,
        days,
    )
    return len(rows)


async def export_session_bundle(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    tenant_id: str,
    owner_user_id: str,
) -> dict[str, Any] | None:
    sess = await get_session_row(conn, session_id, tenant_id, owner_user_id)
    if not sess:
        return None
    msgs = await list_messages_detailed(conn, session_id, tenant_id, owner_user_id)
    summ = await get_summary(conn, session_id, tenant_id, owner_user_id)
    return {
        "session": {
            "id": str(sess["id"]),
            "title": sess["title"],
            "created_at": sess["created_at"].isoformat() if sess["created_at"] else None,
            "updated_at": sess["updated_at"].isoformat() if sess["updated_at"] else None,
            "cleared_at": sess["cleared_at"].isoformat() if sess["cleared_at"] else None,
        },
        "summary": (
            {
                "text": summ[0],
                "covers_up_to_message_id": summ[1],
                "updated_at": summ[2].isoformat(),
            }
            if summ
            else None
        ),
        "messages": [
            {
                "id": int(m["id"]),
                "role": m["role"],
                "content": m["content"],
                "created_at": m["created_at"].isoformat() if m["created_at"] else None,
                "client_message_id": m["client_message_id"],
            }
            for m in msgs
        ],
    }
