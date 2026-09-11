import { useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { onboardingApi } from "@/lib/api";
import type { QuestionnaireItem } from "@/types/api";
import { OnboardingPage } from "../page";

/**
 * Communication step — questionnaire FIRST, then show derived values with
 * per-field override.
 *
 * Rationale: users don't know which format is best for their brain until
 * they answer preference questions. The 10-item IPIP forced-choice
 * questionnaire is the single source of truth. Backend `_map_answers`
 * derives every field `build_behavioral_section` reads:
 *   - communication.patterns
 *   - communication.response_length
 *   - communication.directness
 *   - communication.format_preferences.preferred
 *   - communication.pacing
 *   - decision_style.framing + decision_style.speed_vs_quality
 *   - error_handling
 *   - cognitive_style.abstraction
 *
 * After derivation, the user can override any of {format, verbosity, tone}
 * before advancing. Overrides patch on top of the saved derivation.
 */

const FORMAT_OPTIONS = [
  { value: "bullet lists", label: "Bullet lists" },
  { value: "tables", label: "Tables" },
  { value: "long paragraphs", label: "Long paragraphs" },
  { value: "code blocks", label: "Code blocks" },
  { value: "structured options", label: "Structured options" },
];

const RESPONSE_LENGTH_OPTIONS = [
  { value: "concise",  label: "Concise",  hint: "Short, direct" },
  { value: "balanced", label: "Balanced", hint: "Medium length" },
  { value: "detailed", label: "Detailed", hint: "Thorough reasoning" },
];

const DIRECTNESS_OPTIONS = [
  { value: "diplomatic", label: "Diplomatic" },
  { value: "balanced",   label: "Balanced"   },
  { value: "blunt",      label: "Blunt"      },
];

type OverrideState = {
  formats: string[];
  response_length: string;
  directness: string;
};

function _readExistingFormats(comm: Record<string, unknown>): string[] {
  const fp = comm.format_preferences;
  if (Array.isArray(fp)) return fp as string[];
  if (fp && typeof fp === "object" && Array.isArray((fp as Record<string, unknown>).preferred)) {
    return (fp as Record<string, unknown>).preferred as string[];
  }
  return [];
}

export function QuestionnaireStep({
  profile,
  onAfterSave,
}: {
  profile: Record<string, unknown>;
  onAfterSave?: () => void;
}) {
  const existing = useMemo(
    () => (profile.communication as Record<string, unknown>) ?? {},
    [profile],
  );

  const [questions, setQuestions] = useState<QuestionnaireItem[]>([]);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [derived, setDerived] = useState<Record<string, unknown> | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  const [override, setOverride] = useState<OverrideState>({
    formats: _readExistingFormats(existing),
    response_length: (existing.response_length as string) ?? "",
    directness:
      (existing.directness as string) ??
      (existing.tone as string) ??
      "",
  });

  useEffect(() => {
    onboardingApi
      .questionnaire()
      .then(setQuestions)
      .catch((e) => setLoadErr(e instanceof Error ? e.message : "Load failed"));
  }, []);

  const allAnswered =
    questions.length > 0 && Object.keys(answers).length >= questions.length;

  // After all 10 answered → submit to backend, seed override UI from the
  // derived fields the backend just wrote. This replaces the three scalar
  // PreferencePages the preview flow used to own.
  useEffect(() => {
    if (!allAnswered || derived || submitting) return;
    setSubmitting(true);
    onboardingApi
      .submitQuestionnaire(answers)
      .then((merged) => {
        setDerived(merged);
        const comm = (merged.communication as Record<string, unknown>) ?? {};
        setOverride({
          formats: _readExistingFormats(comm),
          response_length: (comm.response_length as string) ?? "",
          directness:
            (comm.directness as string) ??
            (comm.tone as string) ??
            "",
        });
      })
      .catch((e) => setLoadErr(e instanceof Error ? e.message : "Save failed"))
      .finally(() => setSubmitting(false));
  }, [allAnswered, answers, derived, submitting]);

  const advance = async () => {
    // No-op if they somehow reach Next before submit resolved.
    if (!derived) throw new Error("Questionnaire not submitted yet");
    const commPatch: Record<string, unknown> = {};
    if (override.formats.length) {
      commPatch.format_preferences = { preferred: override.formats };
    }
    if (override.response_length) {
      commPatch.response_length = override.response_length;
    }
    if (override.directness) {
      commPatch.directness = override.directness;
    }
    if (Object.keys(commPatch).length) {
      await onboardingApi.patchProfile({ communication: commPatch });
    }
    onAfterSave?.();
  };

  const toggleFormat = (v: string) =>
    setOverride((prev) => ({
      ...prev,
      formats: prev.formats.includes(v)
        ? prev.formats.filter((f) => f !== v)
        : [...prev.formats, v],
    }));

  const canAdvance = !!derived && !submitting;
  const qAnswered = Object.keys(answers).length;

  return (
    <OnboardingPage
      question="How should agents talk to you?"
      subtitle="Answer 10 quick pairs — we'll derive your communication style from your answers. Adjust anything that doesn't feel right."
      canAdvance={canAdvance}
      onAdvance={advance}
    >
      {loadErr && (
        <div className="mb-3 rounded border border-error/40 bg-surface-elevated text-error text-xs px-3 py-2">
          {loadErr}
        </div>
      )}

      {/* Questionnaire */}
      <div className="flex flex-col gap-5">
        {questions.map((q, i) => (
          <div key={q.id} className="flex flex-col gap-2">
            <div className="text-xs text-fg-secondary">
              <span className="text-fg-disabled mr-1.5">{i + 1}.</span>
              {q.question}
            </div>
            <div className="grid grid-cols-2 gap-3">
              {(["a", "b"] as const).map((choice) => {
                const selected = answers[String(q.id)] === choice;
                return (
                  <button
                    key={choice}
                    type="button"
                    onClick={() =>
                      setAnswers((prev) => ({ ...prev, [String(q.id)]: choice }))
                    }
                    className={
                      "px-3 py-2 rounded border text-xs text-left transition-colors " +
                      (selected
                        ? "border-accent bg-accent-subtle text-accent"
                        : "border-border-subtle text-fg-tertiary hover:border-accent/40")
                    }
                  >
                    {choice === "a" ? q.option_a : q.option_b}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      <div className="mt-4 text-2xs text-fg-disabled">
        {qAnswered} / {questions.length || 10} answered
        {submitting && (
          <span className="ml-2 inline-flex items-center gap-1 text-fg-tertiary">
            <Loader2 size={10} className="animate-spin" /> deriving
          </span>
        )}
      </div>

      {/* Derived values — shown after submit resolves */}
      {derived && (
        <>
          <div className="mt-8 border-t border-border-subtle pt-6">
            <h2 className="text-base font-bold tracking-widest uppercase text-fg-primary">
              Your profile
            </h2>
            <p className="text-xs text-fg-tertiary mt-1">
              Derived from your answers. Override anything that doesn't match.
            </p>
          </div>

          <div className="flex flex-col gap-6 mt-6">
            <div>
              <div className="text-xs uppercase tracking-widest text-fg-tertiary mb-2">
                Preferred formats
              </div>
              <div className="flex flex-wrap gap-2">
                {FORMAT_OPTIONS.map((opt) => {
                  const selected = override.formats.includes(opt.value);
                  return (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => toggleFormat(opt.value)}
                      className={
                        "px-3 py-1.5 rounded border text-xs transition-colors " +
                        (selected
                          ? "border-accent bg-accent-subtle text-accent"
                          : "border-border-subtle text-fg-tertiary hover:border-accent/40")
                      }
                    >
                      {opt.label}
                    </button>
                  );
                })}
              </div>
            </div>

            <div>
              <div className="text-xs uppercase tracking-widest text-fg-tertiary mb-2">
                Response length
              </div>
              <div className="flex flex-col gap-2">
                {RESPONSE_LENGTH_OPTIONS.map((opt) => {
                  const selected = override.response_length === opt.value;
                  return (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() =>
                        setOverride((prev) => ({ ...prev, response_length: opt.value }))
                      }
                      className={
                        "w-full text-left px-3 py-2 rounded border text-xs transition-colors flex justify-between items-center " +
                        (selected
                          ? "border-accent bg-accent-subtle text-accent"
                          : "border-border-subtle text-fg-tertiary hover:border-accent/40")
                      }
                    >
                      <span className="font-medium">{opt.label}</span>
                      <span className="text-fg-disabled">{opt.hint}</span>
                    </button>
                  );
                })}
              </div>
            </div>

            <div>
              <div className="text-xs uppercase tracking-widest text-fg-tertiary mb-2">
                Directness
              </div>
              <div className="flex flex-wrap gap-2">
                {DIRECTNESS_OPTIONS.map((opt) => {
                  const selected = override.directness === opt.value;
                  return (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() =>
                        setOverride((prev) => ({ ...prev, directness: opt.value }))
                      }
                      className={
                        "px-3 py-1.5 rounded border text-xs transition-colors " +
                        (selected
                          ? "border-accent bg-accent-subtle text-accent"
                          : "border-border-subtle text-fg-tertiary hover:border-accent/40")
                      }
                    >
                      {opt.label}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        </>
      )}
    </OnboardingPage>
  );
}
