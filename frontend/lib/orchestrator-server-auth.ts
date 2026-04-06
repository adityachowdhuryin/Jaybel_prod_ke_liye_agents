/**
 * Server-only: headers to authenticate the orchestrator when proxying /chat/stream.
 * Replace this implementation with your IdP (e.g. NextAuth getToken, Clerk auth()).
 *
 * Dev: set ORCHESTRATOR_SERVER_BEARER in .env.local (never NEXT_PUBLIC_*).
 * When unset and the orchestrator runs with ORCHESTRATOR_AUTH_DISABLED=1, an empty
 * object is fine.
 */
export async function getOrchestratorAuthHeaders(): Promise<
  Record<string, string>
> {
  const token = process.env.ORCHESTRATOR_SERVER_BEARER?.trim();
  if (token) {
    return { Authorization: `Bearer ${token}` };
  }
  return {};
}
