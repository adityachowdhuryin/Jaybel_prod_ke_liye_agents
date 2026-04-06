-- Migration 002: session list index, summaries, idempotency, client_message_id.
-- Apply: docker compose exec -T postgres psql -U postgres -d postgres < database/schema_chat_memory_migration_002.sql

CREATE INDEX IF NOT EXISTS idx_chat_sessions_owner_updated
    ON chat_sessions (tenant_id, owner_user_id, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS chat_session_summaries (
    session_id UUID PRIMARY KEY REFERENCES chat_sessions (id) ON DELETE CASCADE,
    summary_text TEXT NOT NULL,
    covers_up_to_message_id BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS client_message_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_session_client_user
    ON chat_messages (session_id, client_message_id)
    WHERE client_message_id IS NOT NULL AND role = 'user';
