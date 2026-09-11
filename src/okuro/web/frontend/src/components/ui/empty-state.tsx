import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

interface EmptyStateProps {
  title: string;
  description?: string;
  /**
   * 32–48px icon (e.g. Lucide `<Inbox className="h-10 w-10" />`). Primary
   * visual anchor for the empty state — raises the card out of "looks
   * like loading" territory.
   */
  icon?: ReactNode;
  action?: ReactNode;
  /** Render without card chrome (no border/background). Use when the
   * surrounding container already has its own framing. */
  bare?: boolean;
  className?: string;
}

/**
 * Empty state — title + optional description + optional icon + action.
 *
 * Bumped from the previous 14px/plain card to a 16px title with a 32+px
 * icon above it, per the UI audit (the old empty states read as
 * "loading" instead of "deliberate zero state"). Use the `bare` prop
 * when dropped inside a card that already has its own framing.
 */
export function EmptyState({
  title,
  description,
  icon,
  action,
  bare = false,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center px-6 py-10 text-center",
        !bare &&
          "rounded-md border border-border-subtle bg-surface-subtle",
        className,
      )}
    >
      {icon && (
        <div className="mb-4 text-fg-subtle" aria-hidden="true">
          {icon}
        </div>
      )}
      <p className="text-base font-medium text-fg">{title}</p>
      {description && (
        <p className="mt-2 max-w-sm text-sm text-fg-muted">{description}</p>
      )}
      {action && <div className="mt-6">{action}</div>}
    </div>
  );
}
