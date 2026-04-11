/**
 * Signup form — enter email, receive magic link.
 *
 * Story 4.3 frontend half. POSTs to ``POST /auth/magic-link`` on
 * mars-control. The backend responds 202 + sends a Resend email (or
 * in local dev, the InMemoryEmailSender records it and the
 * magic-link URL is printed server-side for manual verification).
 */
"use client";

import { useState, FormEvent } from "react";

import { requestMagicLink, MarsApiError } from "@/lib/api";

export default function SignupPage() {
  const [email, setEmail] = useState("");
  const [status, setStatus] = useState<"idle" | "sending" | "sent" | "error">(
    "idle",
  );
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  async function onSubmit(evt: FormEvent<HTMLFormElement>) {
    evt.preventDefault();
    setStatus("sending");
    setErrorMessage(null);
    try {
      await requestMagicLink(email);
      setStatus("sent");
    } catch (err) {
      setStatus("error");
      if (err instanceof MarsApiError) {
        setErrorMessage(err.detail);
      } else {
        setErrorMessage("Unexpected error — check the server logs.");
      }
    }
  }

  return (
    <div className="flex-1 flex items-center justify-center p-6">
      <div className="w-full max-w-md rounded-2xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 shadow-sm p-8">
        <h1 className="text-2xl font-semibold tracking-tight">Sign in to Mars</h1>
        <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">
          We&apos;ll send you a single-use link that expires in 15 minutes.
        </p>

        <form className="mt-6 space-y-4" onSubmit={onSubmit}>
          <label className="block">
            <span className="text-sm font-medium">Email</span>
            <input
              type="email"
              required
              autoFocus
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={status === "sending" || status === "sent"}
              className="mt-1 w-full rounded-lg border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-950 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-900 dark:focus:ring-zinc-100 disabled:opacity-50"
              placeholder="you@example.com"
            />
          </label>
          <button
            type="submit"
            disabled={status === "sending" || status === "sent"}
            className="w-full rounded-lg bg-zinc-900 dark:bg-zinc-100 px-4 py-2 text-sm font-medium text-white dark:text-zinc-900 hover:opacity-90 disabled:opacity-50"
          >
            {status === "sending"
              ? "Sending…"
              : status === "sent"
                ? "Sent — check your email"
                : "Send magic link"}
          </button>
        </form>

        {status === "sent" && (
          <p className="mt-4 rounded-lg bg-green-50 dark:bg-green-950/30 border border-green-200 dark:border-green-900 px-3 py-2 text-sm text-green-900 dark:text-green-200">
            Check <strong>{email}</strong> for the sign-in link.
          </p>
        )}
        {status === "error" && errorMessage && (
          <p className="mt-4 rounded-lg bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-900 px-3 py-2 text-sm text-red-900 dark:text-red-200">
            {errorMessage}
          </p>
        )}
      </div>
    </div>
  );
}
