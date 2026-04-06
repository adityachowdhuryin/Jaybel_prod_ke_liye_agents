import { getOrchestratorAuthHeaders } from "@/lib/orchestrator-server-auth";

const UPSTREAM =
  process.env.ORCHESTRATOR_SERVER_URL ?? "http://127.0.0.1:8000";

export async function GET() {
  const auth = await getOrchestratorAuthHeaders();
  try {
    const r = await fetch(`${UPSTREAM}/health`, {
      method: "GET",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...auth,
      },
    });
    const body = await r.text();
    return new Response(body, {
      status: r.status,
      headers: {
        "Content-Type": r.headers.get("content-type") ?? "application/json",
      },
    });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return Response.json({ error: msg, status: "unreachable" }, { status: 502 });
  }
}
