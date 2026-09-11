import { cn } from "@/lib/utils";

interface MetricCardProps {
  label: string;
  value: string | number;
  accent?: boolean;
  hint?: string;
  className?: string;
}

/**
 * Compact stat tile. Replaces inline `StatCard` definitions in home/dashboard.
 */
export function MetricCard({
  label,
  value,
  accent,
  hint,
  className,
}: MetricCardProps) {
  return (
    <div
      className={cn(
        "rounded-md border border-border bg-surface-elevated p-3",
        className,
      )}
    >
      <div className="text-2xs uppercase tracking-wider text-tertiary">
        {label}
      </div>
      <div
        className={cn(
          "mt-1 text-xl font-bold tabular-nums",
          accent ? "text-accent" : "text-fg",
        )}
      >
        {value}
      </div>
      {hint && (
        <div className="mt-1 text-3xs text-fg-subtle">{hint}</div>
      )}
    </div>
  );
}
