import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FieldLabel } from "@/components/ui/form-primitives";

export interface CronPreset {
  label: string;
  value: string;
  help?: string;
}

// Cron preset pills shown in the schedule editor. Each preset fills the
// cron input when clicked — value must be a valid 5-field expression.
export const CRON_PRESETS: CronPreset[] = [
  { label: "Every 5 min", value: "*/5 * * * *" },
  { label: "Hourly", value: "0 * * * *" },
  { label: "Daily 5am", value: "0 5 * * *" },
  { label: "Weekdays 9am", value: "0 9 * * 1-5" },
  { label: "Weekly Mon 5am", value: "0 5 * * 1" },
  { label: "Monthly 1st 5am", value: "0 5 1 * *" },
];

/**
 * Validate a cron expression shape (5 fields). Returns an error message
 * string or ``null`` when valid. Intentionally permissive about field
 * contents — semantic validation is delegated to croniter on the server.
 */
export function validateCron(expr: string): string | null {
  const trimmed = expr.trim();
  if (!trimmed) return "Cron expression is required";
  const parts = trimmed.split(/\s+/);
  if (parts.length !== 5) {
    return `Expected 5 fields, got ${parts.length}`;
  }
  // Each field may only contain the chars we know (digits, *, /, -, ,)
  const ok = /^[0-9*/,\-]+$/;
  for (let i = 0; i < parts.length; i++) {
    const field = parts[i] ?? "";
    if (!ok.test(field)) {
      return `Field ${i + 1} has invalid characters: '${field}'`;
    }
  }
  return null;
}

/**
 * Presentational inline cron editor. Owns the input, validation and preset
 * UI; the parent owns persistence via ``onSave(cron)``. Save is disabled
 * while the value is invalid, unchanged, or ``saving`` is true.
 *
 * Shared by the Tasks page (recurring defs) and the Health → Schedules tab
 * (daemon tasks) so both edit cron the same way — single source of truth.
 */
export function ScheduleEditor({
  value: initial,
  onSave,
  onClose,
  saving = false,
  presets = CRON_PRESETS,
  hint = "Next run will recompute after save.",
}: {
  value: string;
  onSave: (cron: string) => void;
  onClose: () => void;
  saving?: boolean;
  presets?: CronPreset[];
  hint?: string;
}) {
  const [value, setValue] = useState(initial);

  const error = useMemo(() => validateCron(value), [value]);
  const unchanged = value.trim() === initial.trim();

  return (
    <div className="rounded border border-border bg-surface-elevated p-2.5 space-y-2">
      <FieldLabel>Cron expression</FieldLabel>
      <Input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="0 5 * * *"
        className="font-mono text-sm"
      />
      {error ? (
        <p className="text-2xs text-error">{error}</p>
      ) : (
        <p className="text-2xs text-tertiary">{hint}</p>
      )}

      <div>
        <FieldLabel>Presets</FieldLabel>
        <div className="flex flex-wrap gap-1.5">
          {presets.map((p) => (
            <Button
              key={p.value}
              type="button"
              size="sm"
              variant="outline"
              className="h-6 px-2 text-2xs"
              onClick={() => setValue(p.value)}
              title={p.help ? `${p.value} — ${p.help}` : p.value}
            >
              {p.label}
            </Button>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-2 pt-1">
        <Button
          size="sm"
          onClick={() => onSave(value.trim())}
          disabled={!!error || unchanged || saving}
          className="h-7 text-2xs"
        >
          {saving ? "Saving…" : "Save"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={onClose}
          className="h-7 text-2xs"
          type="button"
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}
