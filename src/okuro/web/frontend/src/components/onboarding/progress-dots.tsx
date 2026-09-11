import { clsx } from "clsx";
import { Check } from "lucide-react";

/**
 * Vertical left-aligned progress rail — one item per onboarding page.
 *
 * Done items render green with a check, the current item a filled accent
 * marker, and upcoming items a muted dot. Labels appear next to each when
 * supplied so the rail doubles as a table of contents; click a done /
 * current label to jump back.
 *
 * Kept deliberately narrow (80px total including padding) so it lives in
 * the empty space to the left of the centered max-w-2xl content column
 * without overlapping page controls.
 */
export function ProgressDots({
  total,
  current,
  labels,
  onJump,
}: {
  total: number;
  current: number;
  /** Optional human-readable label per page — same length as `total` if
   *  provided. Unlabeled entries render as just a marker. */
  labels?: (string | null)[];
  onJump?: (index: number) => void;
}) {
  return (
    <ul
      className="flex flex-col gap-3"
      role="list"
      aria-label="Onboarding progress"
    >
      {Array.from({ length: total }, (_, i) => {
        const isCurrent = i === current;
        const isDone = i < current;
        const clickable = Boolean(onJump) && (isDone || isCurrent);
        const label = labels?.[i] ?? null;
        return (
          <li key={i}>
            <button
              type="button"
              onClick={clickable ? () => onJump?.(i) : undefined}
              aria-label={label ?? `Step ${i + 1} of ${total}`}
              aria-current={isCurrent ? "step" : undefined}
              disabled={!clickable}
              className={clsx(
                "flex items-center gap-2.5 text-left rounded",
                "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
                clickable ? "cursor-pointer" : "cursor-default",
              )}
            >
              <span
                className={clsx(
                  "inline-flex h-3 w-3 shrink-0 items-center justify-center rounded-full transition-colors",
                  isDone
                    ? "bg-success-subtle text-fg-inverse"
                    : isCurrent
                      ? "bg-accent"
                      : "bg-fg-disabled/30",
                )}
              >
                {isDone && <Check size={9} strokeWidth={3} />}
              </span>
              {label && (
                <span
                  className={clsx(
                    "text-xs leading-tight truncate",
                    isCurrent
                      ? "text-fg-primary font-medium"
                      : isDone
                        ? "text-fg-secondary"
                        : "text-fg-disabled",
                  )}
                >
                  {label}
                </span>
              )}
            </button>
          </li>
        );
      })}
    </ul>
  );
}
