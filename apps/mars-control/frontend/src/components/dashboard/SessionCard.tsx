/**
 * SessionCard — one entry in the dashboard session list.
 *
 * Shows name, description, status badge, and an "Open chat" link
 * that navigates to /chat/[sessionId]. Status coloring mirrors the
 * epic-5 reconcile model: running (green), exited_clean (zinc),
 * killed/kill_timeout (amber), exited_error (red), needs_restart
 * (amber with a Resume call-out — TBD Story 5.4 UI).
 */

import type { RuntimeSession } from "@/lib/api";

const STATUS_CLASSES: Record<string, string> = {
  running: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-200",
  exited_clean: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  killed: "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200",
  kill_timeout:
    "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200",
  exited_error: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-200",
};

function statusClass(status: string): string {
  return (
    STATUS_CLASSES[status] ??
    "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
  );
}

interface Props {
  session: RuntimeSession;
}

export function SessionCard({ session }: Props) {
  return (
    <article className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5 shadow-sm flex flex-col gap-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base font-semibold truncate">{session.name}</h2>
          <p className="text-sm text-zinc-600 dark:text-zinc-400 line-clamp-2">
            {session.description}
          </p>
        </div>
        <span
          className={`text-xs font-medium px-2 py-1 rounded-full ${statusClass(session.status)}`}
        >
          {session.status}
        </span>
      </div>
      <div className="flex items-center justify-between text-xs text-zinc-500">
        <span>
          pid <code className="font-mono">{session.pid}</code>
        </span>
        <time>{new Date(session.started_at).toLocaleString()}</time>
      </div>
      <a
        href={`/chat/${encodeURIComponent(session.session_id)}`}
        className="mt-1 inline-flex items-center justify-center rounded-lg bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900 text-sm font-medium px-3 py-2 hover:opacity-90"
      >
        Open chat →
      </a>
    </article>
  );
}
