import { useState } from "react";
import { Plus, X } from "lucide-react";
import { clsx } from "clsx";

/**
 * Tag-style multi-select. Existing chips show inline; typing a new value and
 * pressing Enter / comma (or clicking +) appends. Clicking the × on a chip
 * removes it. Caller owns the string[] — component is pure controlled.
 *
 * Used for boundaries (ok_autonomous, never_without_asking) and anywhere
 * else the onboarding wants a free-form list of short terms.
 */
export function ChipMulti({
  values,
  onChange,
  placeholder = "Add an item",
  suggestions = [],
  max,
}: {
  values: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  /** Optional seed suggestions rendered as greyed-out chips under the input.
   *  Clicking one adds it. Once added, it disappears from the suggestion row. */
  suggestions?: string[];
  max?: number;
}) {
  const [draft, setDraft] = useState("");

  const add = (raw: string) => {
    const v = raw.trim();
    if (!v) return;
    if (values.includes(v)) return;
    if (max && values.length >= max) return;
    onChange([...values, v]);
    setDraft("");
  };

  const remove = (v: string) => onChange(values.filter((x) => x !== v));

  const unchosenSuggestions = suggestions.filter((s) => !values.includes(s));

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2 min-h-[5rem]">
        {values.map((v) => (
          <span
            key={v}
            className="inline-flex items-center gap-1.5 rounded-full bg-accent-subtle text-accent pl-3 pr-1.5 py-1 text-sm"
          >
            {v}
            <button
              type="button"
              onClick={() => remove(v)}
              aria-label={`Remove ${v}`}
              className="rounded-full hover:bg-accent/20 p-0.5 transition-colors"
            >
              <X size={12} />
            </button>
          </span>
        ))}
      </div>

      <div className="flex items-center gap-2">
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              add(draft);
            } else if (e.key === "Backspace" && draft === "" && values.length) {
              e.preventDefault();
              remove(values[values.length - 1]!);
            }
          }}
          placeholder={placeholder}
          className="flex-1 bg-transparent border-0 border-b border-border-subtle focus:border-accent focus:outline-none text-base text-fg-primary placeholder:text-fg-disabled py-2"
        />
        <button
          type="button"
          onClick={() => add(draft)}
          disabled={!draft.trim() || (max ? values.length >= max : false)}
          aria-label="Add"
          className="rounded p-1.5 text-fg-tertiary hover:text-accent disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
        >
          <Plus size={16} />
        </button>
      </div>

      {unchosenSuggestions.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          <span className="text-2xs text-fg-tertiary uppercase tracking-widest mr-2 self-center">
            Common:
          </span>
          {unchosenSuggestions.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => add(s)}
              className={clsx(
                "rounded-full border border-border-subtle px-3 py-1 text-xs text-fg-tertiary",
                "hover:border-accent/50 hover:text-accent transition-colors",
              )}
            >
              {s}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
