/**
 * ChatView — SSE consumer + user input form.
 *
 * Subscribes to ``GET /sessions/{sessionId}/stream`` via native
 * EventSource, parses each SSE ``data:`` line as a MarsEvent, and
 * renders the 4 supported bubble types. User input POSTs to the
 * supervisor's ``POST /sessions/{sessionId}/input`` which round-
 * trips back as assistant events.
 *
 * Reconnection is handled by EventSource's built-in auto-retry —
 * v1 does NOT honor ``Last-Event-ID`` (see
 * mars_control.sse.stream docs), so a gap during reconnect means
 * the browser may miss a few events. Story 2.3's note explains the
 * tradeoff.
 */
"use client";

import { useEffect, useRef, useState, FormEvent } from "react";

import {
  isAssistantText,
  isPermissionRequest,
  isSessionEnded,
  isSessionStarted,
  isToolCall,
  isToolResult,
  MarsEvent,
} from "@/lib/events";
import { sendChatInput, sessionStreamUrl } from "@/lib/api";

import { AssistantTextBubble } from "./AssistantTextBubble";
import { PermissionRequestBubble } from "./PermissionRequestBubble";
import { ToolCallBubble } from "./ToolCallBubble";
import { ToolResultBubble } from "./ToolResultBubble";

interface Props {
  sessionId: string;
}

interface ChatEvent {
  key: string;
  event: MarsEvent;
}

export function ChatView({ sessionId }: Props) {
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [connectionStatus, setConnectionStatus] = useState<
    "connecting" | "open" | "closed" | "error"
  >("connecting");
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Subscribe to SSE stream
  useEffect(() => {
    const url = sessionStreamUrl(sessionId);
    const es = new EventSource(url, { withCredentials: true });

    // Listen on every known event type
    const handled = [
      "session_started",
      "assistant_text",
      "assistant_chunk",
      "tool_call",
      "tool_result",
      "tool_started",
      "permission_request",
      "turn_completed",
      "session_ended",
      "message",
    ];

    const onOpen = () => setConnectionStatus("open");
    const onError = () => setConnectionStatus("error");

    es.addEventListener("open", onOpen);
    es.addEventListener("error", onError);

    const onMessage = (evt: MessageEvent) => {
      try {
        const parsed = JSON.parse(evt.data) as MarsEvent;
        setEvents((prev) => [
          ...prev,
          { key: `${prev.length}-${parsed.type}`, event: parsed },
        ]);
      } catch (err) {
        console.warn("failed to parse SSE event", err, evt.data);
      }
    };

    for (const name of handled) {
      es.addEventListener(name, onMessage);
    }

    return () => {
      for (const name of handled) {
        es.removeEventListener(name, onMessage);
      }
      es.removeEventListener("open", onOpen);
      es.removeEventListener("error", onError);
      es.close();
      setConnectionStatus("closed");
    };
  }, [sessionId]);

  // Auto-scroll to the bottom on new events
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events]);

  async function onSubmit(evt: FormEvent<HTMLFormElement>) {
    evt.preventDefault();
    if (!input.trim() || sending) return;
    setSending(true);
    try {
      await sendChatInput(sessionId, input);
      setInput("");
    } catch (err) {
      console.error("failed to send input", err);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="flex-1 flex flex-col">
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto p-6 space-y-6 max-w-5xl w-full mx-auto"
      >
        {events.length === 0 && (
          <div className="text-center text-zinc-500 py-12">
            <p>Waiting for the daemon to start streaming…</p>
          </div>
        )}
        {events.map(({ key, event }) => {
          if (isSessionStarted(event)) {
            return (
              <p
                key={key}
                className="text-center text-xs text-zinc-500 font-mono"
              >
                session started · {event.model} · {event.claude_code_version}
              </p>
            );
          }
          if (isAssistantText(event)) {
            return <AssistantTextBubble key={key} event={event} />;
          }
          if (isToolCall(event)) {
            return <ToolCallBubble key={key} event={event} />;
          }
          if (isToolResult(event)) {
            return <ToolResultBubble key={key} event={event} />;
          }
          if (isPermissionRequest(event)) {
            return <PermissionRequestBubble key={key} event={event} />;
          }
          if (isSessionEnded(event)) {
            return (
              <p
                key={key}
                className="text-center text-xs text-zinc-500 font-mono"
              >
                session ended · {event.stop_reason ?? "?"} ·{" "}
                {event.num_turns ?? "?"} turns ·{" "}
                {event.total_cost_usd !== null && event.total_cost_usd !== undefined
                  ? `$${event.total_cost_usd.toFixed(4)}`
                  : "?"}
              </p>
            );
          }
          return null;
        })}
      </div>

      <form
        onSubmit={onSubmit}
        className="border-t border-zinc-200 dark:border-zinc-800 p-4 bg-white dark:bg-zinc-900"
      >
        <div className="max-w-5xl mx-auto flex gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={connectionStatus !== "open" || sending}
            placeholder={
              connectionStatus === "open"
                ? "Type a message and press enter…"
                : connectionStatus === "connecting"
                  ? "Connecting…"
                  : "Reconnecting…"
            }
            className="flex-1 rounded-lg border border-zinc-300 dark:border-zinc-700 bg-zinc-50 dark:bg-zinc-950 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-900 dark:focus:ring-zinc-100 disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={
              connectionStatus !== "open" || sending || !input.trim()
            }
            className="rounded-lg bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900 px-4 py-2 text-sm font-medium hover:opacity-90 disabled:opacity-50"
          >
            {sending ? "…" : "Send"}
          </button>
        </div>
      </form>
    </div>
  );
}
