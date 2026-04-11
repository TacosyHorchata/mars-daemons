/**
 * Dashboard — session list + templates tabs.
 *
 * Story 4.4 landed the Sessions tab. Story 8.2 adds a Templates tab
 * next to it, powered by GET /templates on the control plane.
 *
 * Client component that:
 * 1. Hits GET /me on mount to gate access (redirect to /signup on 401)
 * 2. Renders a two-tab view (Sessions / Templates)
 * 3. Sessions tab: fetches GET /sessions and renders one SessionCard per
 * 4. Templates tab: fetches GET /templates and renders one TemplateCard per
 *
 * v1 does NOT offer in-browser session creation — the user spawns
 * sessions either via ``mars deploy`` (developer track) or via the
 * Templates tab's onboarding wizard (operator track, Story 8.3+).
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
  TemplateSummary,
} from "@/lib/api";
import { SessionCard } from "@/components/dashboard/SessionCard";
import { OnboardingWizard } from "@/components/templates/OnboardingWizard";
import { TemplateList } from "@/components/templates/TemplateList";

type PageState =
  | { kind: "loading" }
  | { kind: "unauthorized" }
  | { kind: "error"; message: string }
  | { kind: "ready"; user: SessionUser; sessions: RuntimeSession[] };

type Tab = "sessions" | "templates";

export default function DashboardPage() {
  const router = useRouter();
  const [state, setState] = useState<PageState>({ kind: "loading" });
  const [tab, setTab] = useState<Tab>("sessions");
  const [wizardTemplate, setWizardTemplate] = useState<TemplateSummary | null>(
    null,
  );

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

  function onStartTemplate(template: TemplateSummary) {
    // Story 8.3 wires this to the OnboardingWizard modal.
    setWizardTemplate(template);
  }

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
            <h1 className="text-2xl font-semibold tracking-tight">Mars</h1>
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

        <div className="flex gap-6 border-b border-zinc-200 dark:border-zinc-800 mb-6">
          <button
            type="button"
            onClick={() => setTab("sessions")}
            className={`py-2 -mb-px border-b-2 text-sm font-medium transition-colors ${
              tab === "sessions"
                ? "border-zinc-900 dark:border-zinc-100 text-zinc-900 dark:text-zinc-100"
                : "border-transparent text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300"
            }`}
          >
            Daemons
          </button>
          <button
            type="button"
            onClick={() => setTab("templates")}
            className={`py-2 -mb-px border-b-2 text-sm font-medium transition-colors ${
              tab === "templates"
                ? "border-zinc-900 dark:border-zinc-100 text-zinc-900 dark:text-zinc-100"
                : "border-transparent text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300"
            }`}
          >
            Plantillas
          </button>
        </div>

        {tab === "sessions" &&
          (sessions.length === 0 ? (
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
          ))}

        {tab === "templates" && <TemplateList onStart={onStartTemplate} />}
      </div>

      <OnboardingWizard
        template={wizardTemplate}
        onClose={() => setWizardTemplate(null)}
      />
    </div>
  );
}
