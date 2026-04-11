import type { ToolResultEvent } from "@/lib/events";

interface Props {
  event: ToolResultEvent;
}

export function ToolResultBubble({ event }: Props) {
  const colorClass = event.is_error
    ? "border-red-200 dark:border-red-900 bg-red-50/50 dark:bg-red-950/20 text-red-900 dark:text-red-200"
    : "border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900/50 text-zinc-700 dark:text-zinc-300";

  return (
    <div className="flex gap-3 items-start">
      <div className="flex-shrink-0 h-8 w-8 rounded-full bg-zinc-100 dark:bg-zinc-800 text-zinc-700 dark:text-zinc-300 flex items-center justify-center text-xs font-semibold">
        ←
      </div>
      <div className={`flex-1 max-w-[80ch] rounded-lg border p-3 ${colorClass}`}>
        <p className="text-xs font-mono mb-2 opacity-70">
          tool_result{event.is_error ? " · error" : ""}
        </p>
        <pre className="text-xs font-mono whitespace-pre-wrap overflow-x-auto">
          {event.content.length > 2000
            ? `${event.content.slice(0, 2000)}\n… (${event.content.length - 2000} more chars truncated)`
            : event.content}
        </pre>
      </div>
    </div>
  );
}
