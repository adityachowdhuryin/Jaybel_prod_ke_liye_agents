-- Orchestrator chat persistence + Vertex Agent Engine session bindings.
-- Safe to run multiple times (IF NOT EXISTS). Apply to existing DBs:
--   docker compose exec -T postgres psql -U postgres -d postgres < database/schema_chat_memory.sql

CREATE TABLE IF NOT EXISTS chat_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    title TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cleared_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES chat_sessions (id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session_created
    ON chat_messages (session_id, created_at);

CREATE TABLE IF NOT EXISTS agent_engine_session_bindings (
    client_session_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    engine_user_id TEXT NOT NULL,
    engine_session_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, owner_user_id, client_session_id)
);
