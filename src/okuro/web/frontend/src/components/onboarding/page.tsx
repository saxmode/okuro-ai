import { ChevronDown, ChevronUp, HelpCircle, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useOnboardingPage } from "./shell";

/**
 * Shared layout for a single onboarding page: question, optional help icon
 * with modal, input control slot, Next / Back buttons, optional Skip.
 *
 * Kept deliberately thin — page-specific components pass their input into
 * `children` and only supply copy + handlers.
 *
 * `onAdvance` runs BEFORE the shell moves to the next page. Use it for
 * persisting the page's data (patchProfile etc.) — the pattern where every
 * step saves its own slice before the user scrolls on, so interrupting the
 * wizard at any point leaves a consistent DB. Returning a Promise makes the
 * Next button show a spinner until the save resolves.
 */
export function OnboardingPage({
  question,
  subtitle,
  moreInfo,
  canAdvance = true,
  onAdvance,
  onSkip,
  children,
}: {
  question: string;
  subtitle?: string;
  /** Long-form context shown in a modal behind a `?` icon next to the question. */
  moreInfo?: React.ReactNode;
  /** When false, Next is disabled — e.g. until the required field is filled. */
  canAdvance?: boolean;
  /** Called before the shell advances. Await-able — spinner while in flight.
   *  If this throws, the advance is aborted and the error surfaces in-page. */
  onAdvance?: () => Promise<void> | void;
  onSkip?: () => void;
  children: React.ReactNode;
}) {
  const { next, prev, index, total, registerAdvanceHandler } = useOnboardingPage();
  const [modalOpen, setModalOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Register the page's save function against the shell. The shell awaits it
  // before scrolling forward (and before re-firing the Enter / ↓ shortcuts),
  // so Next-button, keyboard, and programmatic advance all go through the
  // same save path. Deps include onAdvance so closure captures the latest.
  useEffect(() => {
    if (!onAdvance) {
      registerAdvanceHandler(index, null);
      return () => registerAdvanceHandler(index, null);
    }
    const wrapped = async () => {
      if (!canAdvance) throw new Error("Cannot advance yet");
      setSaveError(null);
      setSaving(true);
      try {
        await onAdvance();
      } catch (e) {
        const msg = e instanceof Error ? e.message : "Save failed";
        setSaveError(msg);
        throw e; // shell aborts advance, user stays on page with the error visible
      } finally {
        setSaving(false);
      }
    };
    registerAdvanceHandler(index, wrapped);
    return () => registerAdvanceHandler(index, null);
  }, [index, onAdvance, canAdvance, registerAdvanceHandler]);

  const handleNext = () => {
    if (!canAdvance || saving) return;
    // The shell's next() runs our registered advance handler, then scrolls.
    void next();
  };

  return (
    <div className="flex flex-col gap-8">
      <div>
        <div className="flex items-start gap-2">
          <h1 className="text-xl md:text-2xl font-bold text-fg-primary leading-tight">
            {question}
          </h1>
          {moreInfo && (
            <button
              type="button"
              onClick={() => setModalOpen(true)}
              aria-label="More info"
              className="mt-1 text-fg-tertiary hover:text-fg-primary transition-colors"
            >
              <HelpCircle size={18} />
            </button>
          )}
        </div>
        {subtitle && (
          <p className="text-sm text-fg-tertiary mt-2">{subtitle}</p>
        )}
      </div>

      <div>{children}</div>

      {saveError && (
        <div className="rounded border border-error/40 bg-surface-elevated text-error text-xs px-3 py-2">
          {saveError}
        </div>
      )}

      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={prev}
          disabled={index === 0}
          className="flex items-center gap-1 text-sm text-fg-tertiary hover:text-fg-primary disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
        >
          <ChevronUp size={16} />
          Back
        </button>

        <div className="flex items-center gap-4">
          {onSkip && (
            <button
              type="button"
              onClick={onSkip}
              className="text-sm text-fg-tertiary hover:text-fg-primary transition-colors"
            >
              Skip
            </button>
          )}
          <button
            type="button"
            onClick={handleNext}
            disabled={!canAdvance || saving}
            className="flex items-center gap-2 px-6 py-2.5 rounded bg-accent text-fg-inverse font-medium text-sm hover:bg-accent-hover disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            {saving ? <Loader2 size={14} className="animate-spin" /> : null}
            {index === total - 1 ? "Done" : "Next"}
            {!saving && <ChevronDown size={16} />}
          </button>
        </div>
      </div>

      {moreInfo && modalOpen && (
        <div
          role="dialog"
          aria-modal="true"
          onClick={() => setModalOpen(false)}
          className="fixed inset-0 z-50 bg-scrim backdrop-blur-sm flex items-center justify-center p-8"
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="bg-surface-elevated border border-border rounded-lg max-w-xl w-full p-6 text-sm text-fg-secondary leading-relaxed max-h-[70vh] overflow-y-auto"
          >
            <h2 className="text-base font-bold text-fg-primary mb-3">
              {question}
            </h2>
            {moreInfo}
            <button
              type="button"
              onClick={() => setModalOpen(false)}
              className="mt-4 text-sm text-accent hover:underline"
            >
              Close
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
