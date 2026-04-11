"use client";

import { useEffect, useState } from "react";

import type { TemplateSummary } from "@/lib/api";

interface Props {
  template: TemplateSummary | null;
  onClose: () => void;
}

/**
 * OnboardingWizard — the operator-facing deploy flow for Maat.
 *
 * v1 scope (this file):
 * - **Story 8.3 — steps 1-3**: welcome screen, Claude account
 *   redirect, Claude Max subscription redirect. Pure navigation,
 *   no backend calls yet.
 * - **Story 8.4 — steps 4-6** (deferred): Anthropic OAuth flow,
 *   secrets form, deploy progress. Step 4 placeholder lives here
 *   as a "Continuemos pronto" card so the wizard doesn't dead-end
 *   until 8.4 lands.
 *
 * Spanish is the default (and only) locale for v1. Maat reads
 * Spanish. An English toggle is a v1.1 polish.
 *
 * The wizard is controlled: `template` null → hidden; non-null →
 * mounted as a modal overlay. `onClose` is called on explicit
 * close (X button, Escape key) OR when the user finishes the
 * final step. The parent dashboard is responsible for state.
 */
export function OnboardingWizard({ template, onClose }: Props) {
  const [step, setStep] = useState(1);

  // Reset step when a new template opens.
  useEffect(() => {
    if (template !== null) {
      setStep(1);
    }
  }, [template]);

  // Escape-to-close for keyboard users. The modal is otherwise
  // pretty lightweight — no focus trap, no aria-dialog polish yet.
  // Story 8.5's mobile sanity pass adds the missing a11y bits.
  useEffect(() => {
    if (template === null) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [template, onClose]);

  if (template === null) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={(e) => {
        // Click outside the card closes the wizard — matches
        // standard modal UX.
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        className="w-full max-w-md rounded-2xl bg-white dark:bg-zinc-900 p-6 shadow-2xl"
      >
        <div className="flex items-start justify-between mb-4">
          <div>
            <p className="text-xs uppercase tracking-wider text-zinc-500">
              Paso {step} de 6
            </p>
            <h2 className="text-lg font-semibold mt-0.5">{template.name}</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Cerrar"
            className="text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-200 text-xl leading-none"
          >
            ×
          </button>
        </div>

        <div className="flex gap-1 mb-6">
          {[1, 2, 3, 4, 5, 6].map((i) => (
            <div
              key={i}
              className={`h-1 flex-1 rounded ${
                i <= step
                  ? "bg-zinc-900 dark:bg-zinc-100"
                  : "bg-zinc-200 dark:bg-zinc-800"
              }`}
            />
          ))}
        </div>

        {step === 1 && <Step1Welcome template={template} onNext={() => setStep(2)} />}
        {step === 2 && (
          <Step2ClaudeAccount onBack={() => setStep(1)} onNext={() => setStep(3)} />
        )}
        {step === 3 && (
          <Step3ClaudeMax onBack={() => setStep(2)} onNext={() => setStep(4)} />
        )}
        {step === 4 && <Step4Placeholder onBack={() => setStep(3)} />}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 1 — welcome
// ---------------------------------------------------------------------------

function Step1Welcome({
  template,
  onNext,
}: {
  template: TemplateSummary;
  onNext: () => void;
}) {
  return (
    <div className="space-y-4">
      <p className="text-sm text-zinc-700 dark:text-zinc-300">
        Bienvenido. Vas a conectar un asistente de IA que habla español
        y trabaja para ti — no un chatbot genérico, sino un agente que
        lee WhatsApp, consulta tu CRM y te ayuda a cerrar pendientes.
      </p>
      <p className="text-sm text-zinc-700 dark:text-zinc-300">
        <strong>{template.description}</strong>
      </p>
      <p className="text-xs text-zinc-500">
        Toma entre 5 y 10 minutos. Necesitarás una tarjeta para la
        suscripción de Claude Max ($200 USD/mes) — puedes cancelar
        cuando quieras.
      </p>
      <div className="flex justify-end pt-2">
        <PrimaryButton onClick={onNext}>Empezar</PrimaryButton>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 2 — Claude account
// ---------------------------------------------------------------------------

function Step2ClaudeAccount({
  onBack,
  onNext,
}: {
  onBack: () => void;
  onNext: () => void;
}) {
  return (
    <div className="space-y-4">
      <p className="text-sm text-zinc-700 dark:text-zinc-300">
        Necesitas una cuenta de Claude (Anthropic) para darle energía
        a tu asistente. Te vamos a abrir la página de registro en
        otra pestaña.
      </p>
      <SecondaryLinkButton
        href="https://claude.ai/signup"
        targetBlank
      >
        Abrir registro de Claude ↗
      </SecondaryLinkButton>
      <p className="text-xs text-zinc-500">
        Si ya tienes cuenta, solo da click en el botón de abajo y
        continuamos.
      </p>
      <WizardNav onBack={onBack} onNext={onNext} nextLabel="Ya tengo cuenta" />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 3 — Claude Max subscription
// ---------------------------------------------------------------------------

function Step3ClaudeMax({
  onBack,
  onNext,
}: {
  onBack: () => void;
  onNext: () => void;
}) {
  return (
    <div className="space-y-4">
      <p className="text-sm text-zinc-700 dark:text-zinc-300">
        Ahora suscríbete a <strong>Claude Max</strong>. Es el plan que
        le da capacidad suficiente a tu asistente para responder todo
        el día. Cuesta $200 USD/mes, se cobra directo con Anthropic,
        y puedes cancelar cuando quieras.
      </p>
      <SecondaryLinkButton
        href="https://claude.ai/upgrade"
        targetBlank
      >
        Abrir suscripción de Claude Max ↗
      </SecondaryLinkButton>
      <p className="text-xs text-zinc-500">
        Mars nunca toca tu tarjeta — el cobro lo hace Anthropic
        directamente. Cuando ya estés suscrito, regresa aquí y dale
        continuar.
      </p>
      <WizardNav
        onBack={onBack}
        onNext={onNext}
        nextLabel="Ya estoy suscrito"
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 4 placeholder — Story 8.4 replaces this with OAuth + secrets form
// + deploy progress. For v1 story 8.3 we stop here with a "pronto" note.
// ---------------------------------------------------------------------------

function Step4Placeholder({ onBack }: { onBack: () => void }) {
  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/20 p-4">
        <p className="text-sm text-amber-900 dark:text-amber-200 font-medium">
          Próximamente
        </p>
        <p className="text-xs text-amber-800 dark:text-amber-300 mt-1">
          Los pasos 4 (conectar Claude), 5 (secretos) y 6 (desplegar)
          llegan en la siguiente versión del asistente. Por ahora,
          esta pantalla existe para que veas el flujo sin romper nada.
        </p>
      </div>
      <div className="flex justify-between pt-2">
        <SecondaryButton onClick={onBack}>Atrás</SecondaryButton>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Reusable button primitives
// ---------------------------------------------------------------------------

function PrimaryButton({
  onClick,
  children,
}: {
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-lg bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900 px-4 py-2 text-sm font-medium hover:opacity-90"
    >
      {children}
    </button>
  );
}

function SecondaryButton({
  onClick,
  children,
}: {
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-lg border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm font-medium text-zinc-700 dark:text-zinc-300 hover:bg-zinc-50 dark:hover:bg-zinc-800"
    >
      {children}
    </button>
  );
}

function SecondaryLinkButton({
  href,
  targetBlank,
  children,
}: {
  href: string;
  targetBlank?: boolean;
  children: React.ReactNode;
}) {
  return (
    <a
      href={href}
      target={targetBlank ? "_blank" : undefined}
      rel={targetBlank ? "noopener noreferrer" : undefined}
      className="block w-full text-center rounded-lg border border-zinc-300 dark:border-zinc-700 px-4 py-2.5 text-sm font-medium text-zinc-800 dark:text-zinc-200 hover:bg-zinc-50 dark:hover:bg-zinc-800"
    >
      {children}
    </a>
  );
}

function WizardNav({
  onBack,
  onNext,
  nextLabel = "Continuar",
}: {
  onBack: () => void;
  onNext: () => void;
  nextLabel?: string;
}) {
  return (
    <div className="flex justify-between pt-2">
      <SecondaryButton onClick={onBack}>Atrás</SecondaryButton>
      <PrimaryButton onClick={onNext}>{nextLabel}</PrimaryButton>
    </div>
  );
}
