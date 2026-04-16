/**
 * mars-daemons — TypeScript client for the mars-daemons HTTP API.
 *
 * Enterprise claude code. Self-hosted agent runtime.
 */

export interface MarsClientOptions {
  /** Base URL of the mars daemon, e.g. http://mars.internal:8080 */
  baseUrl: string;
  /** Static bearer token (MARS_AUTH_TOKEN_FILE contents on the daemon) */
  bearer: string;
  /** Optional fetch implementation (defaults to global fetch) */
  fetch?: typeof fetch;
}

export interface SessionHeaders {
  /** Opaque caller identity — the daemon persists this as owner_subject.
   *  Must be ASCII-safe (HTTP header values). Non-ASCII values throw. */
  ownerSubject: string;
  /** 'user' (default) or 'admin' — admin gets write access to shared/. */
  ownerRole?: "user" | "admin";
}

export interface RequestOpts {
  /** AbortSignal to cancel in-flight request / SSE stream. */
  signal?: AbortSignal;
}

export interface CreateSessionRequest {
  assistantId: string;
}

export interface SessionCreated {
  sessionId: string;
  createdAt: string;
  assistantId: string;
}

export interface SessionMetadata {
  sessionId: string;
  status: "idle" | "running";
  createdAt: string;
  updatedAt: string;
  turnCount: number;
  assistantId: string;
}

export interface SendMessageRequest {
  /** UUIDv4 — client-generated, used for idempotency and replay */
  turnId: string;
  text: string;
}

/**
 * Event emitted by the worker during a turn. Field names match the runtime:
 *   assistant_chunk.delta (string), turn_completed, turn_aborted.reason (string).
 */
export interface WorkerEvent {
  type: string;
  [key: string]: unknown;
}

export interface ReplayEvent extends WorkerEvent {
  sequence: number;
  timestamp: string;
  session_id: string;
  turn_id?: string;
}

export interface FileUploadResponse {
  path: string;
}

export interface TurnCancelResponse {
  turn_id: string;
  state: "accepted" | "running" | "completed" | "failed";
}

/** HTTP-level error from the daemon (non-2xx response). */
export class MarsError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: unknown,
    message?: string,
  ) {
    super(message ?? `MarsError ${status}: ${JSON.stringify(body)}`);
    this.name = "MarsError";
  }
}

/** Transport-level error (network down, DNS failure, TLS, abort). */
export class MarsTransportError extends Error {
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
    this.name = "MarsTransportError";
  }
}

/** Raised when a malformed SSE frame (invalid JSON) is received. */
export class MarsStreamError extends Error {
  constructor(message: string, public readonly frame?: string) {
    super(message);
    this.name = "MarsStreamError";
  }
}

// HTTP header values must be printable ASCII (RFC 7230 field-value).
// Reject non-ASCII explicitly so consumers get a clear error rather than
// whichever garbage the runtime's fetch implementation produces.
const ASCII_HEADER_RE = /^[\x20-\x7E]+$/;

function assertAsciiHeader(name: string, value: string): void {
  if (!ASCII_HEADER_RE.test(value)) {
    throw new Error(
      `${name} must be ASCII-safe (printable \\x20-\\x7E). Got: ${JSON.stringify(value)}`,
    );
  }
}

export class MarsClient {
  private readonly baseUrl: string;
  private readonly bearer: string;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: MarsClientOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/$/, "");
    this.bearer = opts.bearer;
    this.fetchImpl = opts.fetch ?? fetch;
  }

  private headers(h: SessionHeaders, extra: Record<string, string> = {}): Record<string, string> {
    assertAsciiHeader("ownerSubject", h.ownerSubject);
    const role = h.ownerRole ?? "user";
    // Runtime-validate role. Type system only protects TypeScript callers;
    // plain JS or untyped payloads can pass anything.
    if (role !== "user" && role !== "admin") {
      throw new Error(`ownerRole must be "user" or "admin", got ${JSON.stringify(role)}`);
    }
    return {
      Authorization: `Bearer ${this.bearer}`,
      "X-Owner-Subject": h.ownerSubject,
      "X-Owner-Role": role,
      ...extra,
    };
  }

  /** Verify a Response is actually an SSE stream before parsing it. */
  private assertSSEResponse(res: Response): void {
    const ct = res.headers.get("content-type") ?? "";
    if (!ct.toLowerCase().includes("text/event-stream")) {
      throw new MarsStreamError(
        `expected SSE response, got Content-Type: ${JSON.stringify(ct)}`,
      );
    }
  }

  private async doFetch(url: string, init: RequestInit): Promise<Response> {
    try {
      return await this.fetchImpl(url, init);
    } catch (e) {
      // fetch() rejects only on network / abort / CORS — not on HTTP status.
      if ((e as { name?: string })?.name === "AbortError") throw e;
      throw new MarsTransportError(
        `fetch to ${url} failed: ${(e as Error)?.message ?? String(e)}`,
        e,
      );
    }
  }

  private async throwOnError(res: Response): Promise<void> {
    if (res.ok) return;
    let body: unknown;
    const ct = res.headers.get("content-type") ?? "";
    try {
      body = ct.includes("application/json") ? await res.json() : await res.text();
    } catch {
      body = null;
    }
    throw new MarsError(res.status, body);
  }

  /** POST /v1/sessions — creates a session bound to an assistantId. */
  async createSession(
    headers: SessionHeaders,
    body: CreateSessionRequest,
    opts: RequestOpts = {},
  ): Promise<SessionCreated> {
    const res = await this.doFetch(`${this.baseUrl}/v1/sessions`, {
      method: "POST",
      headers: this.headers(headers, { "Content-Type": "application/json" }),
      body: JSON.stringify({ assistant_id: body.assistantId }),
      signal: opts.signal,
    });
    await this.throwOnError(res);
    const data = (await res.json()) as Record<string, unknown>;
    return {
      sessionId: String(data.session_id),
      createdAt: String(data.created_at),
      assistantId: String(data.assistant_id),
    };
  }

  /** GET /v1/sessions/{id} — lightweight metadata (no transcript). */
  async getSession(
    headers: SessionHeaders,
    sessionId: string,
    opts: RequestOpts = {},
  ): Promise<SessionMetadata> {
    const res = await this.doFetch(
      `${this.baseUrl}/v1/sessions/${encodeURIComponent(sessionId)}`,
      { headers: this.headers(headers), signal: opts.signal },
    );
    await this.throwOnError(res);
    const data = (await res.json()) as Record<string, unknown>;
    return {
      sessionId: String(data.session_id),
      status: data.status as "idle" | "running",
      createdAt: String(data.created_at),
      updatedAt: String(data.updated_at),
      turnCount: Number(data.turn_count),
      assistantId: String(data.assistant_id),
    };
  }

  /**
   * POST /v1/sessions/{id}/messages — submits a turn and streams SSE events.
   * Yields each event object. Terminates on turn_completed / turn_aborted / turn_truncated,
   * or when the server closes the stream. Pass `opts.signal` to cancel.
   */
  async *sendMessage(
    headers: SessionHeaders,
    sessionId: string,
    body: SendMessageRequest,
    opts: RequestOpts = {},
  ): AsyncGenerator<SSEFrame, void, void> {
    const res = await this.doFetch(
      `${this.baseUrl}/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
      {
        method: "POST",
        headers: this.headers(headers, {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
        }),
        body: JSON.stringify({ turn_id: body.turnId, text: body.text }),
        signal: opts.signal,
      },
    );
    await this.throwOnError(res);
    this.assertSSEResponse(res);
    yield* parseSSE(res);
  }

  /**
   * GET /v1/sessions/{id}/events?after=<seq> — replays missed events then live-tails.
   */
  async *getEvents(
    headers: SessionHeaders,
    sessionId: string,
    after = 0,
    opts: RequestOpts = {},
  ): AsyncGenerator<SSEFrame, void, void> {
    const url = `${this.baseUrl}/v1/sessions/${encodeURIComponent(sessionId)}/events?after=${after}`;
    const res = await this.doFetch(url, {
      headers: this.headers(headers, { Accept: "text/event-stream" }),
      signal: opts.signal,
    });
    await this.throwOnError(res);
    this.assertSSEResponse(res);
    yield* parseSSE(res);
  }

  /**
   * POST /v1/sessions/{id}/files — uploads a file to the user's workspace.
   * `file` can be a Blob or Uint8Array (Node Buffer is a Uint8Array subclass).
   */
  async uploadFile(
    headers: SessionHeaders,
    sessionId: string,
    file: Blob | Uint8Array,
    filename: string,
    opts: RequestOpts = {},
  ): Promise<FileUploadResponse> {
    const form = new FormData();
    // Uint8Array is a valid BlobPart at runtime; the cast is needed only because
    // modern TS DOM lib narrows BlobPart to Uint8Array<ArrayBuffer> while generic
    // Uint8Array is Uint8Array<ArrayBufferLike>.
    const blob = file instanceof Blob ? file : new Blob([file as BlobPart]);
    form.append("file", blob, filename);
    const res = await this.doFetch(
      `${this.baseUrl}/v1/sessions/${encodeURIComponent(sessionId)}/files`,
      {
        method: "POST",
        headers: this.headers(headers),
        body: form,
        signal: opts.signal,
      },
    );
    await this.throwOnError(res);
    const data = (await res.json()) as Record<string, unknown>;
    return { path: String(data.path) };
  }

  /** GET /v1/sessions/{id}/files/{path} — downloads a file from the user's workspace. */
  async downloadFile(
    headers: SessionHeaders,
    sessionId: string,
    path: string,
    opts: RequestOpts = {},
  ): Promise<Blob> {
    const encodedPath = path.split("/").map(encodeURIComponent).join("/");
    const res = await this.doFetch(
      `${this.baseUrl}/v1/sessions/${encodeURIComponent(sessionId)}/files/${encodedPath}`,
      { headers: this.headers(headers), signal: opts.signal },
    );
    await this.throwOnError(res);
    return await res.blob();
  }

  /** POST /v1/sessions/{id}/turns/{turn_id}/cancel — best-effort cancel. */
  async cancelTurn(
    headers: SessionHeaders,
    sessionId: string,
    turnId: string,
    opts: RequestOpts = {},
  ): Promise<TurnCancelResponse> {
    const res = await this.doFetch(
      `${this.baseUrl}/v1/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(
        turnId,
      )}/cancel`,
      { method: "POST", headers: this.headers(headers), signal: opts.signal },
    );
    await this.throwOnError(res);
    const data = (await res.json()) as Record<string, unknown>;
    return {
      turn_id: String(data.turn_id),
      state: data.state as TurnCancelResponse["state"],
    };
  }

  /** GET /healthz — no auth. */
  async health(opts: RequestOpts = {}): Promise<{ status: string }> {
    const res = await this.doFetch(`${this.baseUrl}/healthz`, { signal: opts.signal });
    await this.throwOnError(res);
    const data = (await res.json()) as Record<string, unknown>;
    return { status: String(data.status) };
  }

  /** GET /readyz — no auth. */
  async ready(opts: RequestOpts = {}): Promise<{ status: string }> {
    const res = await this.doFetch(`${this.baseUrl}/readyz`, { signal: opts.signal });
    await this.throwOnError(res);
    const data = (await res.json()) as Record<string, unknown>;
    return { status: String(data.status) };
  }
}

/**
 * SSE frame yielded by `sendMessage` / `getEvents`.
 *
 * - `sseEvent` is the SSE transport-level `event:` field (defaults to "message" per spec).
 *   It is namespaced under `sseEvent` (not `event`) so it cannot be shadowed by the
 *   JSON payload — mars worker events use a `type` field of their own.
 * - All other fields come from the JSON payload in `data:` (including the runtime's
 *   `type`, `delta`, `reason`, and, on replay, `sequence`, `timestamp`, `session_id`,
 *   `turn_id`).
 */
export interface SSEFrame extends Record<string, unknown> {
  /** SSE transport event type (not to be confused with the payload's `type`). */
  sseEvent: string;
}

/**
 * Parse a `text/event-stream` Response body into structured frames.
 *
 * Compliant with SSE spec:
 *   - Line endings: \r\n, \r, or \n
 *   - Frames separated by a blank line
 *   - Multiple `data:` lines in a frame are concatenated with `\n`
 *   - `event:` sets the frame type; `id:` and `retry:` are currently ignored
 *   - Lines starting with `:` are comments (ignored)
 *   - Malformed JSON in `data` throws `MarsStreamError` (no silent drop)
 *   - Trailing buffered data at stream end is flushed
 *   - Reader is cancelled on iterator abandon (not just released)
 */
async function* parseSSE(res: Response): AsyncGenerator<SSEFrame, void, void> {
  if (!res.body) throw new MarsStreamError("response has no body");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        // Flush decoder and process any trailing frame without a blank line.
        buf += decoder.decode();
        for (const frame of extractFrames(buf, /*final=*/ true)) {
          const parsed = parseFrame(frame);
          if (parsed) yield parsed;
        }
        return;
      }
      buf += decoder.decode(value, { stream: true });
      // extractFrames mutates buf-via-return: it returns completed frames and
      // we reassign buf to the leftover (incomplete) tail.
      const [frames, rest] = extractFramesStreaming(buf);
      buf = rest;
      for (const frame of frames) {
        const parsed = parseFrame(frame);
        if (parsed) yield parsed;
      }
    }
  } finally {
    // Cancel the underlying stream so the connection closes promptly when
    // the consumer abandons iteration (break, throw, early return).
    try {
      await reader.cancel();
    } catch {
      /* ignore */
    }
  }
}

/**
 * Split `buf` on any SSE frame boundary: `\r\n\r\n`, `\n\n`, or `\r\r`.
 * Returns `[completeFrames, leftoverTail]`.
 */
function extractFramesStreaming(buf: string): [string[], string] {
  const frames: string[] = [];
  let rest = buf;
  // Match any of the three CRLF/LF/CR frame separators.
  const sep = /(\r\n\r\n|\n\n|\r\r)/;
  while (true) {
    const m = sep.exec(rest);
    if (!m) break;
    frames.push(rest.slice(0, m.index));
    rest = rest.slice(m.index + m[0].length);
  }
  return [frames, rest];
}

/**
 * Used on stream end (`final=true`): treats the whole buffer as one trailing
 * frame if non-empty. In streaming mode, returns full frames via extractFramesStreaming.
 */
function extractFrames(buf: string, final: boolean): string[] {
  if (!final) {
    const [frames] = extractFramesStreaming(buf);
    return frames;
  }
  const [frames, rest] = extractFramesStreaming(buf);
  if (rest.trim().length > 0) frames.push(rest);
  return frames;
}

/**
 * Parse one SSE frame into an SSEFrame object, per the HTML5 SSE spec.
 * Returns `null` for empty frames (e.g. all comments). Throws `MarsStreamError`
 * if `data:` lines fail to parse as JSON.
 */
function parseFrame(frame: string): SSEFrame | null {
  let event = "message";
  const dataLines: string[] = [];
  // Split on any line ending.
  for (const line of frame.split(/\r\n|\r|\n/)) {
    if (line === "") continue;
    if (line.startsWith(":")) continue; // comment
    const colon = line.indexOf(":");
    let field: string;
    let value: string;
    if (colon === -1) {
      field = line;
      value = "";
    } else {
      field = line.slice(0, colon);
      value = line.slice(colon + 1);
      // Per spec: if value starts with a single space, trim it.
      if (value.startsWith(" ")) value = value.slice(1);
    }
    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
    // id / retry ignored for now
  }
  if (dataLines.length === 0) return null;
  const payload = dataLines.join("\n");
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch (e) {
    throw new MarsStreamError(
      `failed to parse SSE data as JSON: ${(e as Error).message}`,
      payload,
    );
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new MarsStreamError(
      `SSE data is not a JSON object (got ${Array.isArray(parsed) ? "array" : typeof parsed})`,
      payload,
    );
  }
  // Spread payload first, then set `sseEvent` last so payload cannot shadow it.
  return { ...(parsed as Record<string, unknown>), sseEvent: event };
}
