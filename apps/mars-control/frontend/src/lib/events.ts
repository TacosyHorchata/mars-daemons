/**
 * TypeScript mirror of :mod:`events.types` (Python, mars-runtime).
 *
 * Kept as a hand-maintained shape match rather than a generated
 * OpenAPI client — the event schema is the *whole* contract between
 * the runtime and the frontend, small enough to mirror by hand, and
 * a generated client would add a build-time dep we don't need yet.
 *
 * Every change to ``apps/mars-runtime/src/events/types.py`` must
 * land here in the same PR.
 */

// ---------------------------------------------------------------------------
// Event type discriminators
// ---------------------------------------------------------------------------

export const EVENT_SESSION_STARTED = "session_started";
export const EVENT_SESSION_ENDED = "session_ended";
export const EVENT_ASSISTANT_TEXT = "assistant_text";
export const EVENT_ASSISTANT_CHUNK = "assistant_chunk";
export const EVENT_TOOL_STARTED = "tool_started";
export const EVENT_TOOL_CALL = "tool_call";
export const EVENT_TOOL_RESULT = "tool_result";
export const EVENT_PERMISSION_REQUEST = "permission_request";
export const EVENT_TURN_COMPLETED = "turn_completed";

export type MarsEventType =
  | typeof EVENT_SESSION_STARTED
  | typeof EVENT_SESSION_ENDED
  | typeof EVENT_ASSISTANT_TEXT
  | typeof EVENT_ASSISTANT_CHUNK
  | typeof EVENT_TOOL_STARTED
  | typeof EVENT_TOOL_CALL
  | typeof EVENT_TOOL_RESULT
  | typeof EVENT_PERMISSION_REQUEST
  | typeof EVENT_TURN_COMPLETED;

// ---------------------------------------------------------------------------
// Event shapes
// ---------------------------------------------------------------------------

interface BaseEvent {
  session_id: string;
  type: MarsEventType;
  sequence: number | null;
  timestamp: string; // ISO-8601
}

export interface SessionStartedEvent extends BaseEvent {
  type: typeof EVENT_SESSION_STARTED;
  model: string;
  cwd: string;
  claude_code_version: string;
  tools_available: string[];
}

export interface SessionEndedEvent extends BaseEvent {
  type: typeof EVENT_SESSION_ENDED;
  result: string | null;
  stop_reason: string | null;
  duration_ms: number | null;
  num_turns: number | null;
  total_cost_usd: number | null;
  permission_denials: Record<string, unknown>[];
}

export interface AssistantTextEvent extends BaseEvent {
  type: typeof EVENT_ASSISTANT_TEXT;
  text: string;
  message_id: string | null;
  block_index: number | null;
}

export interface AssistantChunkEvent extends BaseEvent {
  type: typeof EVENT_ASSISTANT_CHUNK;
  delta: string;
  message_id: string | null;
  block_index: number | null;
}

export interface ToolStartedEvent extends BaseEvent {
  type: typeof EVENT_TOOL_STARTED;
  tool_use_id: string;
  tool_name: string;
}

export interface ToolCallEvent extends BaseEvent {
  type: typeof EVENT_TOOL_CALL;
  tool_use_id: string;
  tool_name: string;
  input: Record<string, unknown>;
  message_id: string | null;
  block_index: number | null;
}

export interface ToolResultEvent extends BaseEvent {
  type: typeof EVENT_TOOL_RESULT;
  tool_use_id: string;
  content: string;
  is_error: boolean;
  message_id: string | null;
  block_index: number | null;
}

export interface PermissionRequestEvent extends BaseEvent {
  type: typeof EVENT_PERMISSION_REQUEST;
  tool_use_id: string;
  tool_name: string;
  input: Record<string, unknown>;
  reason: string | null;
}

export interface TurnCompletedEvent extends BaseEvent {
  type: typeof EVENT_TURN_COMPLETED;
  stop_reason: string | null;
  num_turns: number | null;
}

export type MarsEvent =
  | SessionStartedEvent
  | SessionEndedEvent
  | AssistantTextEvent
  | AssistantChunkEvent
  | ToolStartedEvent
  | ToolCallEvent
  | ToolResultEvent
  | PermissionRequestEvent
  | TurnCompletedEvent;

// ---------------------------------------------------------------------------
// Discriminator helpers
// ---------------------------------------------------------------------------

export function isAssistantText(
  e: MarsEvent,
): e is AssistantTextEvent {
  return e.type === EVENT_ASSISTANT_TEXT;
}

export function isToolCall(e: MarsEvent): e is ToolCallEvent {
  return e.type === EVENT_TOOL_CALL;
}

export function isToolResult(e: MarsEvent): e is ToolResultEvent {
  return e.type === EVENT_TOOL_RESULT;
}

export function isPermissionRequest(
  e: MarsEvent,
): e is PermissionRequestEvent {
  return e.type === EVENT_PERMISSION_REQUEST;
}

export function isSessionStarted(
  e: MarsEvent,
): e is SessionStartedEvent {
  return e.type === EVENT_SESSION_STARTED;
}

export function isSessionEnded(e: MarsEvent): e is SessionEndedEvent {
  return e.type === EVENT_SESSION_ENDED;
}
