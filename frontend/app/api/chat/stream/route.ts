import { getOrchestratorAuthHeaders } from "@/lib/orchestrator-server-auth";

const UPSTREAM =
  process.env.ORCHESTRATOR_SERVER_URL ?? "http://127.0.0.1:8000";

export async function POST(req: Request) {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const auth = await getOrchestratorAuthHeaders();
  const upstream = await fetch(`${UPSTREAM}/chat/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...auth,
    },
    body: JSON.stringify(body),
  });

  const sessionId = upstream.headers.get("x-session-id");

  if (!upstream.ok || !upstream.body) {
    const text = await upstream.text();
    return new Response(text, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("content-type") ?? "text/plain" },
    });
  }

  const headers = new Headers();
  headers.set("Content-Type", "text/event-stream; charset=utf-8");
  headers.set("Cache-Control", "no-cache");
  headers.set("Connection", "keep-alive");
  if (sessionId) {
    headers.set("X-Session-Id", sessionId);
  }

  return new Response(upstream.body, {
    status: upstream.status,
    headers,
  });
}
