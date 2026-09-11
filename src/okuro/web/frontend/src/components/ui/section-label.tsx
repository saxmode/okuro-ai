import { cn } from "@/lib/utils";

interface SectionLabelProps extends React.HTMLAttributes<HTMLElement> {
  as?: "h1" | "h2" | "h3" | "h4" | "h5" | "h6" | "div";
  /**
   * Visual treatment.
   *  - `default` (12px sentence case, subtle color) — use for section
   *    headers and metadata labels. This is the new preferred look.
   *  - `micro` (10px uppercase) — reserved for status-chip captions and
   *    other true micro-labels. Avoid using for section headers.
   */
  variant?: "default" | "micro";
  /** @deprecated kept for backwards compat — prefer `variant`. */
  size?: "sm" | "md";
}

/**
 * Section heading primitive.
 *
 * After the UI-craft audit (2026-04-19) the default treatment is
 * 12px sentence-case in `fg-subtle`, matching Linear's section label
 * convention. 10px uppercase is still available via `variant="micro"`
 * for true badges but is no longer the default — the product had 232
 * usages of `text-2xs`/`text-3xs` which collapsed the type hierarchy.
 */
export function SectionLabel({
  as: Tag = "h2",
  variant = "default",
  size,
  className,
  children,
  ...rest
}: SectionLabelProps) {
  const legacyMicro = size === "sm";
  const isMicro = variant === "micro" || legacyMicro;
  return (
    <Tag
      className={cn(
        isMicro
          ? "text-2xs font-medium uppercase tracking-wider text-fg-subtle"
          : "type-label text-fg-subtle",
        className,
      )}
      {...rest}
    >
      {children}
    </Tag>
  );
}
