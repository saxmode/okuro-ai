import { cn } from "@/lib/utils";

type Tone = "success" | "warning" | "error" | "info" | "neutral" | "accent";

const TONE_STYLES: Record<Tone, { dot: string; text: string }> = {
  success: { dot: "bg-success", text: "text-success" },
  warning: { dot: "bg-warning", text: "text-warning" },
  error: { dot: "bg-error", text: "text-error" },
  info: { dot: "bg-info", text: "text-info" },
  accent: { dot: "bg-accent", text: "text-accent" },
  neutral: { dot: "bg-fg-subtle", text: "text-tertiary" },
};

interface StatusBadgeProps {
  tone?: Tone;
  label: string;
  showDot?: boolean;
  className?: string;
}

/**
 * Tone-coded label. Carries meaning for colorblind users via case +
 * tracking + optional dot — never color-only.
 *
 * CASE COMES FROM THE KIT (`case-label`, globals.css), not from a literal
 * `uppercase` — ruled 0d37d05e. The fallback is `uppercase`, so under a kit
 * that publishes nothing this renders exactly as it did. Fixed in the
 * PRIMITIVE because every consumer inherits it in one edit; the p3 specs
 * counted this class of literal at 510 sites across 130 files.
 */
export function StatusBadge({
  tone = "neutral",
  label,
  showDot = false,
  className,
}: StatusBadgeProps) {
  const style = TONE_STYLES[tone];
  return (
    <span
      className={cn(
        "case-label inline-flex items-center gap-1.5 text-3xs font-bold tracking-wider",
        style.text,
        className,
      )}
    >
      {showDot && (
        <span
          aria-hidden="true"
          className={cn("h-1.5 w-1.5 shrink-0 rounded-full", style.dot)}
        />
      )}
      {label}
    </span>
  );
}
