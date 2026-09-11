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
 * Tone-coded label. Carries meaning for colorblind users via uppercase
 * tracking + optional dot — never color-only.
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
        "inline-flex items-center gap-1.5 text-3xs font-bold uppercase tracking-wider",
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
