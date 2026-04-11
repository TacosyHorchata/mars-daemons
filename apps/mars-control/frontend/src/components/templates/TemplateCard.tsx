"use client";

import type { TemplateSummary } from "@/lib/api";

interface Props {
  template: TemplateSummary;
  onStart: (template: TemplateSummary) => void;
}

/**
 * Template card — the one-button entry point for a non-technical
 * operator (Maat) to deploy a turnkey agent. The "Start" button
 * opens the onboarding wizard (Story 8.3+) which collects secrets
 * and triggers the deploy.
 *
 * v1 copy is Spanish-first because Maat reads Spanish. When we
 * add more templates in v1.1 and templates target non-Spanish
 * verticals the label source moves to the template summary itself.
 */
export function TemplateCard({ template, onStart }: Props) {
  return (
    <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5 flex flex-col gap-3">
      <div>
        <h3 className="font-semibold text-zinc-900 dark:text-zinc-100">
          {template.name}
        </h3>
        <p className="text-sm text-zinc-600 dark:text-zinc-400 mt-1">
          {template.description}
        </p>
      </div>
      {template.system_prompt_preview && (
        <p className="text-xs text-zinc-500 dark:text-zinc-500 italic border-l-2 border-zinc-200 dark:border-zinc-800 pl-3">
          {template.system_prompt_preview}
        </p>
      )}
      <div className="flex flex-wrap gap-1.5">
        {template.mcps.map((mcp) => (
          <span
            key={mcp}
            className="text-[10px] font-mono uppercase tracking-wider rounded px-1.5 py-0.5 bg-zinc-100 dark:bg-zinc-800 text-zinc-600 dark:text-zinc-400"
          >
            {mcp}
          </span>
        ))}
      </div>
      <button
        type="button"
        onClick={() => onStart(template)}
        className="mt-auto self-start rounded-lg bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900 px-4 py-2 text-sm font-medium hover:opacity-90"
      >
        Empezar
      </button>
    </div>
  );
}
