import type { ComponentPropsWithoutRef, ReactNode, MouseEventHandler } from "react";
import { cn } from "@/lib/utils";

interface RowProps extends Omit<ComponentPropsWithoutRef<"div">, "onClick"> {
  /** Render as a Link, button, div — caller passes the element. */
  as?: "div" | "button" | "li";
  density?: "dense" | "default" | "spacious";
  /** Show a bottom separator. Default true. */
  divider?: boolean;
  /** Hover background. Default true. */
  hover?: boolean;
  /** Cursor: pointer when clickable. */
  onClick?: MouseEventHandler<HTMLElement>;
  className?: string;
  children: ReactNode;
}

/**
 * List row primitive.
 *
 * Replaces the ad-hoc `flex items-center gap-3 py-1` patterns scattered
 * across list pages. Default geometry matches the Linear / Stripe table
 * norm (44px min-height via --row-default, py-3 px-4, hairline bottom
 * divider, subtle hover background).
 *
 * Density tiers map to --row-dense / --row-default / --row-spacious
 * tokens so row height is configurable per surface without touching
 * padding values inline.
 */
export function Row({
  as: Tag = "div",
  density = "default",
  divider = true,
  hover = true,
  onClick,
  className,
  children,
  ...rest
}: RowProps) {
  const Comp = Tag as "div";
  const densityClass =
    density === "dense"
      ? "h-row-dense px-3 py-2"
      : density === "spacious"
        ? "h-row-spacious px-5 py-4"
        : "h-row-default px-4 py-3";
  return (
    <Comp
      /* THE REST SPREAD IS A CORRECTNESS FIX, not convenience. This component
         named six props and silently dropped everything else, so a caller could
         not give a row an `id`, an `aria-label`, an `aria-current`, a `title` or
         a test hook -- on a primitive whose `as="button"` form is one of the
         app's commonest interactive elements. Found by the design-system
         showcase, which could not put a `data-*` attribute on its own specimen.

         `type="button"` FIRST, so it is a default a caller can override: a bare
         `<button>` defaults to `type="submit"`, which means every clickable Row
         inside a form has been submitting it. */
      {...(Tag === "button" ? { type: "button" as const } : {})}
      {...rest}
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-3 text-sm transition-fast",
        densityClass,
        divider && "border-b border-border-subtle last:border-b-0",
        hover && "hover:bg-surface-elevated",
        onClick && "cursor-pointer",
        className,
      )}
    >
      {children}
    </Comp>
  );
}
