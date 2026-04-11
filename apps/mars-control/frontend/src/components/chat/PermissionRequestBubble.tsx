import type { PermissionRequestEvent } from "@/lib/events";

interface Props {
  event: PermissionRequestEvent;
}

/**
 * v1 permission requests are *advisory* — by the time the event
 * reaches the browser the decision is already made by the
 * PreToolUse hook in the runtime. The component surfaces the
 * denied call so the admin understands what the daemon tried to
 * do. A true round-trip approve/deny UI is v1.1 (see
 * spikes/03-permission-roundtrip.md).
 */
export function PermissionRequestBubble({ event }: Props) {
  return (
    <div className="flex gap-3 items-start">
      <div className="flex-shrink-0 h-8 w-8 rounded-full bg-amber-100 dark:bg-amber-900/30 text-amber-900 dark:text-amber-200 flex items-center justify-center text-xs font-semibold">
        !
      </div>
      <div className="flex-1 max-w-[80ch] rounded-lg border border-amber-200 dark:border-amber-900 bg-amber-50/50 dark:bg-amber-950/20 p-3">
        <p className="text-xs font-mono text-amber-900 dark:text-amber-200 mb-1">
          permission denied · {event.tool_name}
        </p>
        {event.reason && (
          <p className="text-xs text-amber-800 dark:text-amber-300 mb-2">
            {event.reason}
          </p>
        )}
        <details className="text-xs font-mono text-zinc-700 dark:text-zinc-300">
          <summary className="cursor-pointer opacity-70">Attempted input</summary>
          <pre className="mt-2 overflow-x-auto">
            {JSON.stringify(event.input, null, 2)}
          </pre>
        </details>
      </div>
    </div>
  );
}
