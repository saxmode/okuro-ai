import { clsx } from "clsx";
import { Check } from "lucide-react";

export interface PickItem {
  value: string;
  label: string;
  description?: string;
  hint?: string;
}

/**
 * 2-column card grid for selecting one or more options from a library.
 * Used for principles (multi-select, capped) and design profiles
 * (single-select). Cards are full-height, clickable anywhere, with a
 * subtle check badge when selected.
 */
export function PickGrid({
  items,
  values,
  onChange,
  mode = "multi",
  max,
}: {
  items: PickItem[];
  values: string[];
  onChange: (next: string[]) => void;
  mode?: "single" | "multi";
  /** For mode=multi, limit selected count. */
  max?: number;
}) {
  const toggle = (v: string) => {
    const set = new Set(values);
    if (mode === "single") {
      onChange([v]);
      return;
    }
    if (set.has(v)) {
      set.delete(v);
    } else {
      if (max && set.size >= max) return;
      set.add(v);
    }
    onChange(Array.from(set));
  };

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
      {items.map((it) => {
        const selected = values.includes(it.value);
        return (
          <button
            key={it.value}
            type="button"
            onClick={() => toggle(it.value)}
            className={clsx(
              "relative flex flex-col items-start gap-1 rounded border px-4 py-3 text-left transition-all",
              "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
              selected
                ? "border-accent bg-accent-subtle"
                : "border-border-subtle bg-surface hover:border-border hover:bg-surface-elevated",
            )}
          >
            <div className="flex items-center justify-between w-full">
              <span
                className={clsx(
                  "text-sm font-medium",
                  selected ? "text-accent" : "text-fg-primary",
                )}
              >
                {it.label}
              </span>
              {selected && <Check size={14} className="text-accent shrink-0" />}
            </div>
            {it.description && (
              <p className="text-xs text-fg-tertiary leading-snug">
                {it.description}
              </p>
            )}
            {it.hint && (
              <span className="text-2xs text-fg-disabled mt-1">{it.hint}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
