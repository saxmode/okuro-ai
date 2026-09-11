import * as React from "react";
import { cn } from "@/lib/utils";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { MarkdownContent } from "@/components/ui/markdown-content";
import { useVisualAdaptations } from "@/hooks/use-ui-profile";

interface DetailModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: React.ReactNode;
  /** Short line under the title — e.g. provider, project, date. */
  subtitle?: React.ReactNode;
  /** Eyebrow label above the title — "SESSION", "MEMORY", etc. */
  eyebrow?: string;
  children: React.ReactNode;
  /** Override the default (900px) max width. */
  maxWidth?: string;
  /**
   * Where the dialog portal mounts. Defaults to `document.body`. Exists so
   * the design-system showcase can render this inside the specimen iframe.
   */
  container?: HTMLElement | null;
}

/**
 * Reader-scale detail modal for any list row — sessions, memory, thoughts,
 * progress. Radix provides Esc + click-outside + focus trap; we layer on
 * the profile-driven reading scale so every detail surface respects the
 * user's font_scale / whitespace preferences.
 */
export function DetailModal({
  open,
  onOpenChange,
  title,
  subtitle,
  eyebrow,
  children,
  maxWidth = "900px",
  container,
}: DetailModalProps) {
  const visual = useVisualAdaptations();

  // Match the artifact viewer's whitespace cadence so detail surfaces
  // share a single rhythm.
  const paddingClass =
    visual.whitespace === "spacious"
      ? "px-10 py-9 md:px-14 md:py-10"
      : visual.whitespace === "compact"
        ? "px-6 py-5 md:px-8 md:py-6"
        : "px-8 py-7 md:px-10 md:py-8";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        container={container}
        className={cn(
          "grid max-h-[90vh] grid-rows-[auto_minmax(0,1fr)] gap-0 overflow-hidden p-0",
        )}
        style={
          {
            width: `min(92vw, ${maxWidth})`,
            maxWidth: `min(92vw, ${maxWidth})`,
            "--md-scale": visual.font_scale,
          } as React.CSSProperties
        }
        showCloseButton={true}
      >
        <DialogHeader
          className="min-w-0 shrink-0 border-b border-border bg-surface-elevated/95 px-6 py-4 pr-12 backdrop-blur"
        >
          {eyebrow && (
            <div className="truncate text-xs uppercase tracking-wider text-tertiary">
              {eyebrow}
            </div>
          )}
          <DialogTitle className="line-clamp-2 break-words text-lg font-semibold text-fg">
            {title}
          </DialogTitle>
          {subtitle && (
            <DialogDescription className="truncate text-sm text-fg-muted">
              {subtitle}
            </DialogDescription>
          )}
        </DialogHeader>

        <div className={cn("min-w-0 overflow-y-auto", paddingClass)}>
          <div className="mx-auto w-full min-w-0 max-w-[72ch] space-y-6 [overflow-wrap:anywhere]">
            {children}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Definition-list grid used for the metadata band at the top of a detail
 * modal. One concept per row, label on the left, value on the right.
 */
export function DetailFields({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  // AN ENGINE NAME, NOT A BARE REM. `0.95rem` was authored against a 16px root
  // and the app's root is 50 %, so it rendered 7.6px -- the owner's "modal has
  // still 8px text inside on mode XL" -- and it was rung-deaf besides, because a
  // literal is not a read. `markdown-content.tsx` already spends the correct
  // idiom at 14 sites; this file kept the literal. The fallback is the same
  // number in the compensated unit.
  return (
    <dl
      className={cn(
        "grid grid-cols-[max-content_1fr] gap-x-6 gap-y-2",
        className,
      )}
      style={{ fontSize: "calc(var(--font-size-sm, 1.75rem) * var(--md-scale, 1))" }}
    >
      {children}
    </dl>
  );
}

export function DetailField({
  label,
  children,
  mono,
}: {
  label: string;
  children: React.ReactNode;
  /** Render value in monospace (ids, hashes, paths). */
  mono?: boolean;
}) {
  if (children === null || children === undefined || children === "") return null;
  return (
    <>
      <dt className="whitespace-nowrap text-xs font-semibold uppercase tracking-wider text-tertiary pt-0.5">
        {label}
      </dt>
      <dd
        className={cn(
          "min-w-0 break-words text-fg",
          mono && "font-mono",
        )}
      >
        {children}
      </dd>
    </>
  );
}

/**
 * Free-text body for a detail modal — rendered with the viewer-grade
 * markdown styling so multi-paragraph content is actually readable.
 */
export function DetailProse({
  children,
  markdown = true,
}: {
  children: string;
  markdown?: boolean;
}) {
  if (!children) return null;
  if (markdown) {
    return <MarkdownContent variant="viewer">{children}</MarkdownContent>;
  }
  // Same class as the `<dl>` above: `1.0625rem` rendered 8.5px on the 50 % root
  // and could not follow the rung. `base` is the body cell (N5).
  return (
    <pre
      className="whitespace-pre-wrap break-words font-sans leading-relaxed text-fg"
      style={{
        fontSize: "calc(var(--font-size-base, 2rem) * var(--md-scale, 1))",
        lineHeight: 1.7,
      }}
    >
      {children}
    </pre>
  );
}

/**
 * Section divider — lets modals split "metadata" from "content" cleanly.
 */
export function DetailSection({
  title,
  children,
  className,
}: {
  title?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("space-y-3", className)}>
      {title && (
        <h3 className="text-xs font-semibold uppercase tracking-wider text-tertiary">
          {title}
        </h3>
      )}
      {children}
    </section>
  );
}
