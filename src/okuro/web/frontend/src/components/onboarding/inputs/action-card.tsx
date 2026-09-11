import { Check, Loader2, Terminal } from "lucide-react";
import { clsx } from "clsx";

/**
 * Row-shaped card used by the AI CLI step. Title + description on the left,
 * status chip + primary action button on the right. Supports three status
 * states (not_installed / installed_needs_auth / authenticated) and a
 * pending overlay that shows which terminal was spawned.
 *
 * Purely presentational — parent owns state + handlers.
 */
export type ActionCardStatus = "not_installed" | "needs_auth" | "authenticated";

export function ActionCard({
  title,
  description,
  recommended = false,
  status,
  busy = false,
  primaryLabel,
  onPrimary,
  pendingMessage,
  pendingFallbackCommand,
}: {
  title: string;
  description?: string;
  recommended?: boolean;
  status: ActionCardStatus;
  busy?: boolean;
  primaryLabel: string;
  onPrimary?: () => void;
  /** When a terminal was spawned, show what's happening. */
  pendingMessage?: string;
  /** When no terminal could be spawned, show the copyable command. */
  pendingFallbackCommand?: string;
}) {
  const ready = status === "authenticated";
  return (
    <div
      className={clsx(
        "rounded border px-5 py-4 transition-all",
        ready
          ? "border-accent/40 bg-accent-subtle"
          : "border-border-subtle bg-surface-elevated",
      )}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Terminal size={14} className="text-fg-tertiary" />
            <span className="text-sm font-bold text-fg-primary">{title}</span>
            {recommended && !ready && (
              <span className="text-2xs text-accent border border-accent/40 rounded-sm px-1.5 py-0.5">
                recommended
              </span>
            )}
          </div>
          {description && (
            <p className="mt-1 text-xs text-fg-tertiary leading-snug">{description}</p>
          )}
        </div>

        <div className="flex items-center gap-3 shrink-0">
          {ready ? (
            <span className="flex items-center gap-1 text-2xs text-success">
              <Check size={12} />
              authenticated
            </span>
          ) : status === "needs_auth" ? (
            <span className="text-2xs text-warning">installed · needs auth</span>
          ) : (
            <span className="text-2xs text-fg-disabled">not installed</span>
          )}

          {!ready && (
            <button
              type="button"
              onClick={onPrimary}
              disabled={busy}
              className={clsx(
                "flex items-center gap-1.5 text-xs px-3 py-1.5 rounded border transition-colors",
                busy
                  ? "border-border-subtle text-fg-disabled cursor-wait"
                  : "border-accent/40 text-accent hover:bg-accent-subtle",
              )}
            >
              {busy ? <Loader2 size={12} className="animate-spin" /> : <Terminal size={12} />}
              {primaryLabel}
            </button>
          )}
        </div>
      </div>

      {pendingMessage && (
        <div className="mt-3 flex items-center gap-2 border-t border-border-subtle pt-2 text-2xs text-fg-tertiary">
          <Loader2 size={12} className="animate-spin" />
          {pendingMessage}
        </div>
      )}

      {pendingFallbackCommand && (
        <div className="mt-3 border-t border-border-subtle pt-2">
          <p className="text-2xs text-warning mb-1">
            No supported terminal emulator found — run this yourself:
          </p>
          <code className="block text-2xs bg-surface px-2 py-1 rounded select-all break-all text-fg-tertiary">
            {pendingFallbackCommand}
          </code>
        </div>
      )}
    </div>
  );
}
