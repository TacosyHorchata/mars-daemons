/**
 * Dashboard — session list view.
 *
 * Story 4.4 frontend half. Client component that:
 * 1. Hits GET /me on mount to gate access (redirect to /signup on 401)
 * 2. Hits supervisor GET /sessions (via SUPERVISOR_URL)
 * 3. Renders one <SessionCard /> per session
 *
 * v1 does NOT offer in-browser session creation — the user spawns
 * sessions via ``mars deploy`` and this page is read-only for
 * discovery. Creation via UI is a v2 feature.
 */
"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import {
  fetchCurrentUser,
  listSessions,
  logout,
  MarsApiError,
  RuntimeSession,
  SessionUser,
} from "@/lib/api";
import { SessionCard } from "@/components/dashboard/SessionCard";

type PageState =
  | { kind: "loading" }
  | { kind: "unauthorized" }
  | { kind: "error"; message: string }
  | { kind: "ready"; user: SessionUser; sessions: RuntimeSession[] };

export default function DashboardPage() {
  const router = useRouter();
  const [state, setState] = useState<PageState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const user = await fetchCurrentUser();
        if (cancelled) return;
        try {
          const sessions = await listSessions();
          if (cancelled) return;
          setState({ kind: "ready", user, sessions });
        } catch (err) {
          if (cancelled) return;
          const msg =
            err instanceof MarsApiError
              ? `Supervisor unreachable: ${err.detail}`
              : "Supervisor unreachable.";
          setState({ kind: "ready", user, sessions: [] });
          // Non-fatal: show a banner via console; the UI renders
          // "no sessions" gracefully.
          console.warn(msg);
        }
      } catch {
        if (cancelled) return;
        setState({ kind: "unauthorized" });
        router.replace("/signup");
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [router]);

  if (state.kind === "loading") {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-zinc-500">Loading your daemons…</p>
      </div>
    );
  }
  if (state.kind === "unauthorized") {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-zinc-500">Redirecting…</p>
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-red-600">{state.message}</p>
      </div>
    );
  }

  const { user, sessions } = state;

  return (
    <div className="flex-1 p-6">
      <div className="mx-auto max-w-5xl">
        <div className="flex items-baseline justify-between mb-6">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Daemons</h1>
            <p className="text-sm text-zinc-500">
              Signed in as <strong>{user.email}</strong>
            </p>
          </div>
          <button
            onClick={() => {
              logout().finally(() => router.replace("/signup"));
            }}
            className="text-sm text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
          >
            Sign out
          </button>
        </div>

        {sessions.length === 0 ? (
          <div className="rounded-xl border border-dashed border-zinc-300 dark:border-zinc-700 p-10 text-center">
            <p className="text-zinc-600 dark:text-zinc-400 mb-4">
              No daemons running yet.
            </p>
            <p className="text-xs text-zinc-500 font-mono">
              $ mars deploy examples/pr-reviewer-agent.yaml
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {sessions.map((s) => (
              <SessionCard key={s.session_id} session={s} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
