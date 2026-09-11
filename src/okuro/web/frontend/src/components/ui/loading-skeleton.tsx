import { cn } from "@/lib/utils";

interface LoadingSkeletonProps extends React.HTMLAttributes<HTMLDivElement> {
  lines?: number;
  lineClassName?: string;
}

/**
 * Neutral skeleton placeholder. Honors `prefers-reduced-motion` via the
 * `motion-safe` variant — the shimmer stays still when reduced motion is on.
 */
export function LoadingSkeleton({
  lines = 1,
  lineClassName,
  className,
  ...rest
}: LoadingSkeletonProps) {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-label="Loading"
      className={cn("space-y-2", className)}
      {...rest}
    >
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={i}
          className={cn(
            "h-3 w-full rounded bg-surface-elevated motion-safe:animate-pulse",
            lineClassName,
          )}
        />
      ))}
    </div>
  );
}
