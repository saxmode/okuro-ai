import { cn } from "@/lib/utils";

interface FacetGroup<T> {
  label: string;
  field: keyof T;
  /** Optional formatter (e.g. capitalize, or render "claude-code" → "claude"). */
  format?: (raw: string) => string;
}

interface FacetBarProps<T> {
  items: T[];
  groups: FacetGroup<T>[];
  /** Currently-active facet values keyed by field (e.g. {project: "okuro"}). */
  active: Partial<Record<keyof T, string>>;
  onChange: (active: Partial<Record<keyof T, string>>) => void;
}

/**
 * Derived-from-data facet filter chips. Each group shows all distinct values
 * found in `items` for its `field`. Click to toggle; click the same chip to
 * clear.
 *
 * Kept minimal — doesn't do search, multi-select, or count pills; that's
 * deferred to Wave 3 when entity pages land.
 */
export function FacetBar<T extends Record<string, unknown>>({
  items,
  groups,
  active,
  onChange,
}: FacetBarProps<T>) {
  // Derive distinct values per group
  const values: Record<string, string[]> = {};
  for (const g of groups) {
    const set = new Set<string>();
    for (const item of items) {
      const v = item[g.field];
      if (v == null || v === "") continue;
      set.add(String(v));
    }
    values[String(g.field)] = Array.from(set).sort();
  }

  const toggle = (field: keyof T, value: string) => {
    const fieldKey = String(field) as keyof typeof active;
    const next = { ...active };
    if (next[fieldKey] === value) {
      delete next[fieldKey];
    } else {
      (next as Record<string, string>)[String(field)] = value;
    }
    onChange(next);
  };

  const anyActive = Object.keys(active).length > 0;

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border border-border-subtle bg-surface-subtle px-3.5 py-2.5">
      {groups.map((g) => {
        const key = String(g.field);
        const vals = values[key] ?? [];
        if (vals.length === 0) return null;
        const current = active[g.field];
        return (
          <div key={key} className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
            <span className="inline-flex items-center py-1 text-2xs uppercase leading-none tracking-wider text-tertiary">
              {g.label}
            </span>
            {vals.map((v) => {
              const selected = current === v;
              return (
                <button
                  key={v}
                  type="button"
                  onClick={() => toggle(g.field, v)}
                  className={cn(
                    "inline-flex items-center rounded-full border px-2.5 py-1 text-2xs leading-none transition-colors",
                    // A SELECTED CHIP IS A BRANDED FIGURE, so it carries the
                    // brand at FULL strength and the brand's own computed ink.
                    // It was `bg-accent-subtle text-accent` — a 10 % tint of the
                    // brand — and 10 % of any colour is nearly achromatic, so
                    // the chip's surface went grey the moment the on-light
                    // adaptation was shaded toward the brand's own black
                    // (measured: #feebec -> #e6e6e6 at 97 %) while its border
                    // and label stayed branded. Register rule 5 makes the
                    // ADJUSTMENT the contrast mechanism for a figure on a
                    // neutral ground; an alpha tint substitutes a second one.
                    // Same pair `segmented`'s pills variant already uses.
                    selected
                      ? "border-accent bg-primary text-primary-foreground"
                      : "border-border bg-transparent text-fg-muted hover:border-border-hover",
                  )}
                >
                  {g.format ? g.format(v) : v}
                </button>
              );
            })}
          </div>
        );
      })}
      {anyActive && (
        <button
          type="button"
          onClick={() => onChange({})}
          className="ml-auto inline-flex items-center py-1 text-2xs uppercase leading-none tracking-wider text-tertiary hover:text-fg-muted rounded-sm"
        >
          Clear
        </button>
      )}
    </div>
  );
}
