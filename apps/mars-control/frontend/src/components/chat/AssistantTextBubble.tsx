import type { AssistantTextEvent } from "@/lib/events";

interface Props {
  event: AssistantTextEvent;
}

export function AssistantTextBubble({ event }: Props) {
  return (
    <div className="flex gap-3 items-start">
      <div className="flex-shrink-0 h-8 w-8 rounded-full bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900 flex items-center justify-center text-xs font-semibold">
        ◆
      </div>
      <div className="flex-1 max-w-[80ch]">
        <p className="whitespace-pre-wrap leading-relaxed text-zinc-900 dark:text-zinc-100">
          {event.text}
        </p>
      </div>
    </div>
  );
}
