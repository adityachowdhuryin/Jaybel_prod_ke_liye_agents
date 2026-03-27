"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { Bot, Loader2, Send, Sparkles, User } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";

type Role = "user" | "assistant";

export type ChatMessage = {
  id: string;
  role: Role;
  content: string;
};

const ORCHESTRATOR_URL =
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ?? "http://localhost:8000";

const SESSION_STORAGE_KEY = "pa-orchestrator-session";

const SUGGESTIONS = [
  "What were our top cloud costs last week?",
  "Compare prod vs dev spend by service",
  "Show BigQuery-related costs for this month",
];

function extractA2AText(payload: Record<string, unknown>): string {
  const status = payload.status as Record<string, unknown> | undefined;
  const message = status?.message as Record<string, unknown> | undefined;
  const artifact = payload.artifact as Record<string, unknown> | undefined;
  const fromMsg = message?.parts as { text?: string }[] | undefined;
  const fromArt = artifact?.parts as { text?: string }[] | undefined;
  const parts = fromMsg?.length ? fromMsg : fromArt ?? [];
  return parts.map((p) => p.text ?? "").join("");
}

function parseSseBlocks(buffer: string): { events: string[]; rest: string } {
  const events: string[] = [];
  let rest = buffer;
  let idx: number;
  while ((idx = rest.indexOf("\n\n")) !== -1) {
    const raw = rest.slice(0, idx);
    rest = rest.slice(idx + 2);
    const line = raw.split("\n").find((l) => l.startsWith("data:"));
    if (line) {
      events.push(line.replace(/^data:\s?/, "").trim());
    }
  }
  return { events, rest };
}

type HealthState = "checking" | "ok" | "error";

export function ChatPanel() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthState>("checking");
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    try {
      const existing = sessionStorage.getItem(SESSION_STORAGE_KEY);
      if (existing) setSessionId(existing);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      setHealth("checking");
      try {
        const r = await fetch(`${ORCHESTRATOR_URL}/health`, {
          method: "GET",
          cache: "no-store",
        });
        if (!cancelled) setHealth(r.ok ? "ok" : "error");
      } catch {
        if (!cancelled) setHealth("error");
      }
    };
    void check();
    const id = setInterval(check, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const scrollToBottom = useCallback(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  const sendMessage = async (override?: string) => {
    const text = (override ?? input).trim();
    if (!text || loading) return;

    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: text,
    };
    const assistantId = crypto.randomUUID();
    setMessages((m) => [
      ...m,
      userMsg,
      { id: assistantId, role: "assistant", content: "" },
    ]);
    setInput("");
    setLoading(true);
    requestAnimationFrame(() => textareaRef.current?.focus());

    try {
      const res = await fetch(`${ORCHESTRATOR_URL}/chat/stream`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
        },
        body: JSON.stringify({
          message: text,
          ...(sessionId ? { session_id: sessionId } : {}),
        }),
      });

      const sid = res.headers.get("X-Session-Id");
      if (sid) {
        setSessionId(sid);
        try {
          sessionStorage.setItem(SESSION_STORAGE_KEY, sid);
        } catch {
          /* ignore */
        }
      }

      if (!res.ok || !res.body) {
        const errText = await res.text();
        setMessages((m) =>
          m.map((msg) =>
            msg.id === assistantId
              ? {
                  ...msg,
                  content: `Request failed (${res.status}): ${errText || res.statusText}`,
                }
              : msg
          )
        );
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let sseBuffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        sseBuffer += decoder.decode(value, { stream: true });
        const { events, rest } = parseSseBlocks(sseBuffer);
        sseBuffer = rest;

        for (const ev of events) {
          if (!ev) continue;
          let data: Record<string, unknown>;
          try {
            data = JSON.parse(ev) as Record<string, unknown>;
          } catch {
            continue;
          }
          if (data.error) {
            setMessages((m) =>
              m.map((msg) =>
                msg.id === assistantId
                  ? {
                      ...msg,
                      content: String(
                        (data.detail as string) || "Orchestrator error"
                      ),
                    }
                  : msg
              )
            );
            continue;
          }
          const delta = extractA2AText(data);
          if (!delta) continue;
          setMessages((m) =>
            m.map((msg) =>
              msg.id === assistantId
                ? { ...msg, content: msg.content + delta }
                : msg
            )
          );
          scrollToBottom();
        }
      }
    } catch (e) {
      setMessages((m) =>
        m.map((msg) =>
          msg.id === assistantId
            ? {
                ...msg,
                content: `Network error: ${e instanceof Error ? e.message : String(e)}`,
              }
            : msg
        )
      );
    } finally {
      setLoading(false);
      scrollToBottom();
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void sendMessage();
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Try asking
        </span>
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            type="button"
            disabled={loading}
            onClick={() => void sendMessage(s)}
            className={cn(
              "rounded-full border border-border/80 bg-background/80 px-3 py-1 text-left text-xs text-foreground shadow-sm",
              "transition hover:border-primary/40 hover:bg-muted/60",
              "disabled:pointer-events-none disabled:opacity-50"
            )}
          >
            {s}
          </button>
        ))}
      </div>

      <div className="relative flex min-h-[min(520px,70vh)] flex-col overflow-hidden rounded-2xl border border-border/60 bg-gradient-to-b from-card to-muted/20 shadow-lg shadow-black/5">
        <div className="flex items-center justify-between border-b border-border/60 bg-muted/30 px-4 py-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Sparkles className="h-4 w-4" />
            </span>
            <div>
              <p className="leading-tight">Orchestrator</p>
              <p className="text-xs font-normal text-muted-foreground">
                Routes cost questions to the specialist agent
              </p>
            </div>
          </div>
          <div
            className={cn(
              "flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
              health === "ok" && "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
              health === "error" && "bg-destructive/15 text-destructive",
              health === "checking" && "bg-muted text-muted-foreground",

            )}
            title="Orchestrator /health"
          >
            {health === "checking" && (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            )}
            {health === "ok" && (
              <span className="h-2 w-2 rounded-full bg-emerald-500" />
            )}
            {health === "error" && (
              <span className="h-2 w-2 rounded-full bg-destructive" />
            )}
            {health === "ok"
              ? "Connected"
              : health === "error"
                ? "Offline"
                : "…"}
          </div>
        </div>

        <ScrollArea className="flex-1 p-4">
          <div className="flex flex-col gap-4 pr-3">
            {messages.length === 0 && (
              <div className="mx-auto max-w-md rounded-xl border border-dashed border-border/80 bg-muted/20 px-5 py-8 text-center">
                <p className="text-sm font-medium text-foreground">
                  Cloud cost &amp; usage only
                </p>
                <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
                  The orchestrator answers greetings, then sends real cost
                  questions to the specialist via A2A. Unrelated requests get a
                  safe decline — try a suggestion above or type your own.
                </p>
              </div>
            )}
            {messages.map((m) => (
              <div
                key={m.id}
                className={cn(
                  "flex gap-3",
                  m.role === "user" ? "flex-row-reverse" : "flex-row"
                )}
              >
                <div
                  className={cn(
                    "mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full border shadow-sm",
                    m.role === "user"
                      ? "border-primary/30 bg-primary text-primary-foreground"
                      : "border-border bg-background text-muted-foreground"
                  )}
                >
                  {m.role === "user" ? (
                    <User className="h-4 w-4" />
                  ) : (
                    <Bot className="h-4 w-4" />
                  )}
                </div>
                <div
                  className={cn(
                    "max-w-[min(100%,42rem)] rounded-2xl px-4 py-3 text-sm leading-relaxed shadow-sm",
                    m.role === "user"
                      ? "bg-primary text-primary-foreground"
                      : "border border-border/80 bg-card text-foreground"
                  )}
                >
                  <p className="whitespace-pre-wrap break-words">
                    {m.content ||
                      (m.role === "assistant" && loading ? "" : m.content)}
                  </p>
                  {m.role === "assistant" && loading && !m.content && (
                    <span className="inline-flex gap-1 pt-0.5">
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground/70 [animation-delay:-0.3s]" />
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground/70 [animation-delay:-0.15s]" />
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground/70" />
                    </span>
                  )}
                </div>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
        </ScrollArea>

        <form
          className="border-t border-border/60 bg-card/90 p-3 backdrop-blur"
          onSubmit={(e) => {
            e.preventDefault();
            void sendMessage();
          }}
        >
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Ask about cloud spend, services, environments, trends…"
              disabled={loading}
              rows={2}
              autoComplete="off"
              className={cn(
                "min-h-[44px] flex-1 resize-y rounded-xl border border-input bg-background px-3 py-2.5 text-sm",
                "shadow-inner outline-none ring-offset-background placeholder:text-muted-foreground",
                "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
                "disabled:cursor-not-allowed disabled:opacity-50"
              )}
            />
            <Button
              type="submit"
              disabled={loading || !input.trim()}
              className="h-11 shrink-0 gap-2 rounded-xl px-5 sm:h-[44px]"
            >
              {loading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Send className="h-4 w-4" />
              )}
              Send
            </Button>
          </div>
          <p className="mt-2 text-center text-[11px] text-muted-foreground">
            Enter to send · Shift+Enter for newline · Orchestrator SSE → cost
            agent
          </p>
        </form>
      </div>
    </div>
  );
}
