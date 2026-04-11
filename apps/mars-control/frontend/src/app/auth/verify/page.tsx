/**
 * Magic-link landing page.
 *
 * Reads the ``token`` query param, POSTs it to
 * ``/auth/magic-link/verify`` on the backend, and redirects to
 * /dashboard on success. On failure, surfaces the backend's
 * detail (expired, consumed, invalid).
 *
 * The backend sets the ``mars_session`` HttpOnly cookie on the
 * verify response — our job is just to kick the request.
 */
"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { verifyMagicLink, MarsApiError } from "@/lib/api";

function VerifyInner() {
  const router = useRouter();
  const search = useSearchParams();
  const [state, setState] = useState<"verifying" | "ok" | "error">("verifying");
  const [detail, setDetail] = useState<string | null>(null);

  useEffect(() => {
    const token = search.get("token");
    if (!token) {
      setState("error");
      setDetail("Missing token in URL.");
      return;
    }
    verifyMagicLink(token)
      .then(() => {
        setState("ok");
        router.replace("/dashboard");
      })
      .catch((err) => {
        setState("error");
        if (err instanceof MarsApiError) {
          setDetail(err.detail);
        } else {
          setDetail("Unexpected error verifying your link.");
        }
      });
  }, [search, router]);

  return (
    <div className="flex-1 flex items-center justify-center p-6">
      <div className="w-full max-w-md text-center space-y-4">
        {state === "verifying" && (
          <p className="text-zinc-600 dark:text-zinc-400">
            Verifying your sign-in link…
          </p>
        )}
        {state === "ok" && (
          <p className="text-green-700 dark:text-green-300">
            Signed in — redirecting to your dashboard.
          </p>
        )}
        {state === "error" && (
          <>
            <p className="text-red-700 dark:text-red-300">
              {detail ?? "Couldn't verify your link."}
            </p>
            <a
              href="/signup"
              className="inline-block rounded-lg border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm hover:bg-zinc-100 dark:hover:bg-zinc-900"
            >
              Request a new link
            </a>
          </>
        )}
      </div>
    </div>
  );
}

export default function VerifyPage() {
  // Next.js requires Suspense around components that call useSearchParams
  return (
    <Suspense
      fallback={
        <div className="flex-1 flex items-center justify-center">
          <p className="text-zinc-500">Loading…</p>
        </div>
      }
    >
      <VerifyInner />
    </Suspense>
  );
}
