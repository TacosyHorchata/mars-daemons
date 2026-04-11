/**
 * /chat/[sessionId] — live SSE chat view for one Mars session.
 */
"use client";

import { use, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { ChatView } from "@/components/chat/ChatView";
import { fetchCurrentUser } from "@/lib/api";

interface PageProps {
  params: Promise<{ sessionId: string }>;
}

export default function ChatPage({ params }: PageProps) {
  const router = useRouter();
  const { sessionId } = use(params);
  const [authed, setAuthed] = useState<"pending" | "ok" | "no">("pending");

  useEffect(() => {
    fetchCurrentUser()
      .then(() => setAuthed("ok"))
      .catch(() => {
        setAuthed("no");
        router.replace("/signup");
      });
  }, [router]);

  if (authed === "pending") {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-zinc-500">Loading chat…</p>
      </div>
    );
  }
  if (authed === "no") {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-zinc-500">Redirecting…</p>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col">
      <div className="border-b border-zinc-200 dark:border-zinc-800 px-6 py-3">
        <a
          href="/dashboard"
          className="text-sm text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
        >
          ← Back to dashboard
        </a>
        <h1 className="text-lg font-semibold mt-1 truncate">
          Session <code className="font-mono text-sm">{sessionId}</code>
        </h1>
      </div>
      <ChatView sessionId={sessionId} />
    </div>
  );
}
