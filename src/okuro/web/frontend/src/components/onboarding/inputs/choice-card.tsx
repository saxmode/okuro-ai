import { clsx } from "clsx";
import { Check } from "lucide-react";

export interface Choice {
  value: string;
  label: string;
  hint?: string;
}

/**
 * Radio-style picker rendered as a vertical stack of full-width cards.
 * One click selects; keyboard is Space / Enter on the focused option.
 * Clear hit target, generous padding — "simple yet powerful" intent.
 */
export function ChoiceCard({
  choices,
  value,
  onChange,
}: {
  choices: Choice[];
  value: string | null;
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-col gap-2" role="radiogroup">
      {choices.map((c) => {
        const selected = c.value === value;
        return (
          <button
            key={c.value}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onChange(c.value)}
            className={clsx(
              "w-full flex items-center justify-between gap-4 px-5 py-4 rounded border text-left transition-all",
              "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
              selected
                ? "border-accent bg-accent-subtle"
                : "border-border-subtle bg-surface hover:border-border hover:bg-surface-elevated",
            )}
          >
            <div className="min-w-0">
              <div
                className={clsx(
                  "text-base font-medium",
                  selected ? "text-accent" : "text-fg-primary",
                )}
              >
                {c.label}
              </div>
              {c.hint && (
                <div className="text-xs text-fg-tertiary mt-0.5">{c.hint}</div>
              )}
            </div>
            {selected && <Check size={18} className="text-accent shrink-0" />}
          </button>
        );
      })}
    </div>
  );
}
