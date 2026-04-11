/**
 * API client for the Mars control plane backend.
 *
 * All calls include ``credentials: "include"`` so the browser
 * forwards the ``mars_session`` cookie on cross-origin requests
 * during local dev (frontend on :3000, backend on :8000).
 *
 * The base URL is read from ``NEXT_PUBLIC_MARS_CONTROL_URL`` so
 * the same build can point at localhost, a Vercel preview, or
 * a production Fly deploy without rebuilding.
 */

export const MARS_CONTROL_URL =
  process.env.NEXT_PUBLIC_MARS_CONTROL_URL ?? "http://localhost:8000";

export class MarsApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(`mars-control ${status}: ${detail}`);
  }
}

async function handle<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      if (typeof body === "object" && body !== null && "detail" in body) {
        detail = String((body as { detail: unknown }).detail);
      }
    } catch {
      // ignore
    }
    throw new MarsApiError(resp.status, detail);
  }
  // 204 / empty body
  if (resp.status === 204) return {} as T;
  const text = await resp.text();
  return (text ? JSON.parse(text) : {}) as T;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const resp = await fetch(`${MARS_CONTROL_URL}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
  return handle<T>(resp);
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export interface SessionUser {
  email: string;
  issued_at: string;
  expires_at: string;
}

export interface MagicLinkResponse {
  status: string;
  email: string;
}

export async function requestMagicLink(email: string): Promise<MagicLinkResponse> {
  return request("/auth/magic-link", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export async function verifyMagicLink(token: string): Promise<{ email: string }> {
  return request("/auth/magic-link/verify", {
    method: "POST",
    body: JSON.stringify({ token }),
  });
}

export async function fetchCurrentUser(): Promise<SessionUser> {
  return request("/me");
}

export async function logout(): Promise<void> {
  await request("/auth/logout", { method: "POST" });
}

// ---------------------------------------------------------------------------
// Sessions (via control plane session locator)
// ---------------------------------------------------------------------------

export interface RuntimeSession {
  session_id: string;
  name: string;
  description: string;
  status: string;
  pid: number;
  is_alive: boolean;
  started_at: string;
  terminated_at: string | null;
}

/**
 * List sessions through the control plane.
 *
 * The browser only ever talks to mars-control — never directly to a
 * supervisor. Control plane's ``GET /sessions`` proxies to the
 * default supervisor (v1, MARS_DEFAULT_SUPERVISOR_URL) or fans out
 * to the persisted session registry (Epic 5). Either way, auth +
 * CORS + logging all happen in one place.
 */
export async function listSessions(): Promise<RuntimeSession[]> {
  const data = await request<{ sessions: RuntimeSession[] }>("/sessions");
  return data.sessions ?? [];
}

export async function sendChatInput(
  sessionId: string,
  text: string,
): Promise<void> {
  await request(`/sessions/${encodeURIComponent(sessionId)}/input`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}

export async function updateAgentPrompt(
  agentName: string,
  sessionId: string,
  content: string,
): Promise<void> {
  await request(`/agents/${encodeURIComponent(agentName)}/prompt`, {
    method: "PATCH",
    body: JSON.stringify({ session_id: sessionId, content }),
  });
}

/**
 * Build the SSE URL for a session. Caller uses native EventSource.
 */
export function sessionStreamUrl(sessionId: string): string {
  return `${MARS_CONTROL_URL}/sessions/${encodeURIComponent(sessionId)}/stream`;
}
