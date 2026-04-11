"use client";

import { useEffect, useState } from "react";

import { listTemplates, MarsApiError, type TemplateSummary } from "@/lib/api";

import { TemplateCard } from "./TemplateCard";

interface Props {
  onStart: (template: TemplateSummary) => void;
}

/**
 * Templates tab content — fetches the list from mars-control,
 * renders a responsive card grid, handles loading/empty/error.
 */
export function TemplateList({ onStart }: Props) {
  const [templates, setTemplates] = useState<TemplateSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listTemplates()
      .then((data) => {
        if (!cancelled) setTemplates(data);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof MarsApiError) {
          setError(err.detail);
        } else {
          setError("No pudimos cargar las plantillas.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (templates === null && error === null) {
    return (
      <p className="text-sm text-zinc-500 dark:text-zinc-400">
        Cargando plantillas…
      </p>
    );
  }

  if (error !== null) {
    return (
      <div className="rounded-lg border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/20 p-4">
        <p className="text-sm text-red-800 dark:text-red-300">{error}</p>
      </div>
    );
  }

  if (templates!.length === 0) {
    return (
      <p className="text-sm text-zinc-500 dark:text-zinc-400">
        No hay plantillas disponibles todavía.
      </p>
    );
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {templates!.map((t) => (
        <TemplateCard key={t.name} template={t} onStart={onStart} />
      ))}
    </div>
  );
}
