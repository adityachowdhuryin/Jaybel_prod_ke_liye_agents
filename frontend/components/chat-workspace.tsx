"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ChatPanel, type ChatMessage } from "@/components/chat-panel";
import { ChatSidebar } from "@/components/chat-sidebar";
import { SESSION_STORAGE_KEY } from "@/lib/chat-session-key";

const ORCHESTRATOR_URL =
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ?? "http://localhost:8000";

const USE_CHAT_PROXY =
  process.env.NEXT_PUBLIC_USE_CHAT_PROXY === "1" ||
  process.env.NEXT_PUBLIC_USE_CHAT_PROXY === "true";

const ACCESS_TOKEN_STORAGE_KEY = "pa-orchestrator-access-token";

function messagesUrl(sessionId: string): string {
  const enc = encodeURIComponent(sessionId);
  if (USE_CHAT_PROXY) return `/api/chat/session-messages?sessionId=${enc}`;
  return `${ORCHESTRATOR_URL}/chat/sessions/${enc}/messages`;
}

function orchestratorAuthHeaders(): Record<string, string> {
  if (typeof window === "undefined") return {};
  try {
    const t = sessionStorage.getItem(ACCESS_TOKEN_STORAGE_KEY);
    if (t) return { Authorization: `Bearer ${t}` };
  } catch {
    /* ignore */
  }
  return {};
}

type MessageRow = {
  id: number;
  role: string;
  content: string;
  client_message_id: string | null;
};

function mapRowsToMessages(rows: MessageRow[]): ChatMessage[] {
  return rows.map((row) => ({
    // Keep stable ids from DB while preserving optimistic user ids when available.
    id:
      String(row.role || "")
        .trim()
        .toLowerCase() === "user" && row.client_message_id
        ? row.client_message_id
        : `db-${row.id}`,
    role:
      String(row.role || "")
        .trim()
        .toLowerCase() === "user"
        ? "user"
        : "assistant",
    content: row.content,
  }));
}

export function ChatWorkspace() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [listRefreshKey, setListRefreshKey] = useState(0);
  const [streamBusy, setStreamBusy] = useState(false);
  const streamBusyRef = useRef(false);
  const loadTokenRef = useRef(0);

  useEffect(() => {
    streamBusyRef.current = streamBusy;
  }, [streamBusy]);

  const loadMessagesForSession = useCallback(async (id: string) => {
    const token = ++loadTokenRef.current;
    try {
      const r = await fetch(messagesUrl(id), {
        method: "GET",
        cache: "no-store",
        headers: {
          Accept: "application/json",
          ...(USE_CHAT_PROXY ? {} : orchestratorAuthHeaders()),
        },
      });
      if (!r.ok) {
        if (token === loadTokenRef.current && !streamBusyRef.current) {
          setMessages([]);
        }
        return;
      }
      const rows = (await r.json()) as MessageRow[];
      if (token !== loadTokenRef.current || streamBusyRef.current) return;
      setMessages(mapRowsToMessages(rows));
    } catch {
      if (token === loadTokenRef.current && !streamBusyRef.current) {
        setMessages([]);
      }
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      let sid: string | null = null;
      try {
        sid = sessionStorage.getItem(SESSION_STORAGE_KEY);
      } catch {
        /* ignore */
      }
      if (!sid || cancelled) return;
      setSessionId(sid);
      await loadMessagesForSession(sid);
    })();
    return () => {
      cancelled = true;
    };
  }, [loadMessagesForSession]);

  const handleSessionResolved = useCallback((id: string) => {
    setSessionId(id);
    try {
      sessionStorage.setItem(SESSION_STORAGE_KEY, id);
    } catch {
      /* ignore */
    }
    setListRefreshKey((k) => k + 1);
  }, []);

  const handleNewChat = useCallback(() => {
    setSessionId(null);
    setMessages([]);
    try {
      sessionStorage.removeItem(SESSION_STORAGE_KEY);
    } catch {
      /* ignore */
    }
    setListRefreshKey((k) => k + 1);
  }, []);

  const handleSelectSession = useCallback(
    async (id: string) => {
      if (streamBusy) return;
      try {
        sessionStorage.setItem(SESSION_STORAGE_KEY, id);
      } catch {
        /* ignore */
      }
      setSessionId(id);
      await loadMessagesForSession(id);
    },
    [streamBusy, loadMessagesForSession]
  );

  const handleSessionDeleted = useCallback((id: string) => {
    if (id === sessionId) {
      setSessionId(null);
      setMessages([]);
      try {
        sessionStorage.removeItem(SESSION_STORAGE_KEY);
      } catch {
        /* ignore */
      }
    }
    setListRefreshKey((k) => k + 1);
  }, [sessionId]);

  return (
    <div className="flex flex-col gap-6 md:flex-row md:items-stretch md:gap-4">
      <ChatSidebar
        activeSessionId={sessionId}
        onSelectSession={handleSelectSession}
        onNewChat={handleNewChat}
        listRefreshKey={listRefreshKey}
        streamBusy={streamBusy}
        onSessionDeleted={handleSessionDeleted}
      />
      <div className="min-w-0 flex-1">
        <ChatPanel
          sessionId={sessionId}
          onSessionResolved={handleSessionResolved}
          messages={messages}
          setMessages={setMessages}
          onStreamBusyChange={setStreamBusy}
          onTurnComplete={() => setListRefreshKey((k) => k + 1)}
        />
      </div>
    </div>
  );
}
