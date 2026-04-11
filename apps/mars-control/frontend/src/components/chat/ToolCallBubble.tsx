import type { ToolCallEvent } from "@/lib/events";

interface Props {
  event: ToolCallEvent;
}

export function ToolCallBubble({ event }: Props) {
  return (
    <div className="flex gap-3 items-start">
      <div className="flex-shrink-0 h-8 w-8 rounded-full bg-blue-100 dark:bg-blue-900/30 text-blue-900 dark:text-blue-200 flex items-center justify-center text-xs font-semibold">
        →
      </div>
      <div className="flex-1 max-w-[80ch] rounded-lg border border-blue-200 dark:border-blue-900 bg-blue-50/50 dark:bg-blue-950/20 p-3">
        <p className="text-xs font-mono text-blue-900 dark:text-blue-200 mb-2">
          tool_call · {event.tool_name}
        </p>
        <pre className="text-xs font-mono text-zinc-700 dark:text-zinc-300 overflow-x-auto">
          {JSON.stringify(event.input, null, 2)}
        </pre>
      </div>
    </div>
  );
}
