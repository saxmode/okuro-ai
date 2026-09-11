import type { ReactNode } from "react";
import { ArrowDown, ArrowUp, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

/**
 * Shared form primitives used by /settings, /people, /reminders, etc.
 *
 * The same three helpers (FieldLabel, SubsectionHeading, StringListEditor)
 * were copy-pasted into every new settings-style page. Subagent #11 extracted
 * them here so /people and /reminders can import them directly.
 *
 * Existing pages (e.g. settings.tsx) still define local copies for now —
 * leave those intact until a later agent does a cross-file cleanup pass.
 */

export function FieldLabel({ children }: { children: ReactNode }) {
  return (
    <label className="mb-1.5 block text-xs font-medium text-fg-muted">
      {children}
    </label>
  );
}

export function SubsectionHeading({
  title,
  hint,
}: {
  title: string;
  hint?: string;
}) {
  return (
    <div>
      <h3 className="type-subtitle text-fg">{title}</h3>
      {hint ? <p className="mt-1.5 text-xs text-fg-muted">{hint}</p> : null}
    </div>
  );
}

/**
 * Minimal array-of-strings editor. Add/remove/reorder rows.
 * Empty rows can be stripped on save via {@link stripEmpty}.
 */
export function StringListEditor({
  value,
  onChange,
  placeholder,
  multiline = false,
}: {
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  multiline?: boolean;
}) {
  const update = (idx: number, next: string) => {
    const out = value.slice();
    out[idx] = next;
    onChange(out);
  };
  const remove = (idx: number) => {
    onChange(value.filter((_, i) => i !== idx));
  };
  const move = (idx: number, delta: number) => {
    const target = idx + delta;
    if (target < 0 || target >= value.length) return;
    const out = value.slice();
    const row = out[idx] ?? "";
    out.splice(idx, 1);
    out.splice(target, 0, row);
    onChange(out);
  };
  const add = () => {
    onChange([...value, ""]);
  };

  return (
    <div className="space-y-1.5">
      {value.length === 0 && (
        <p className="text-2xs text-tertiary italic">No items yet.</p>
      )}
      {value.map((row, idx) => (
        <div key={idx} className="flex items-start gap-1.5">
          {multiline ? (
            <Textarea
              value={row}
              onChange={(e) => update(idx, e.target.value)}
              placeholder={placeholder}
              className="min-h-9"
            />
          ) : (
            <Input
              value={row}
              onChange={(e) => update(idx, e.target.value)}
              placeholder={placeholder}
            />
          )}
          {/* THE PRIMITIVE, not three raw `<button>`s. Those three took focus
              and matched `:focus-visible`, and carried no focus class at all —
              so they fell back to Chromium's `outline: auto 1px` while the
              `<Input>` beside them in the same row wore the system's ring. Two
              treatments, one row, and the fix is not a third: routing them
              through `Button` is what makes them inherit whatever the design
              system decides a focus ring is. */}
          <div className="flex shrink-0 items-center">
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              onClick={() => move(idx, -1)}
              disabled={idx === 0}
              className="text-tertiary hover:text-fg"
              aria-label="Move up"
            >
              <ArrowUp className="h-3.5 w-3.5" />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              onClick={() => move(idx, 1)}
              disabled={idx === value.length - 1}
              className="text-tertiary hover:text-fg"
              aria-label="Move down"
            >
              <ArrowDown className="h-3.5 w-3.5" />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              onClick={() => remove(idx)}
              className="text-tertiary hover:text-error"
              aria-label="Remove"
            >
              <X className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      ))}
      <Button
        size="sm"
        variant="outline"
        onClick={add}
        type="button"
        className="mt-1"
      >
        <Plus className="h-3.5 w-3.5" />
        Add row
      </Button>
    </div>
  );
}

export function stripEmpty(list: string[]): string[] {
  return list.map((s) => s.trim()).filter((s) => s.length > 0);
}
