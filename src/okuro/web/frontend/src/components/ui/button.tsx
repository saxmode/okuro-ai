/* Focus is not painted here: the engine owns it (emit.py `_focus_ring_rule`). */
import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Slot } from "radix-ui"

import { cn } from "@/lib/utils"

/**
 * THE VARIANT TABLE, NAMED so the library view can be driven off it.
 *
 * It lived inline in the `cva()` call below, which made the SET of variants
 * readable by nothing but `cva` itself — so the showcase hand-listed the
 * combinations it rendered, and a hand-list goes stale silently. Measured
 * 2026-08-20: this file declares 6 variants x 8 sizes = 48 combinations and the
 * scene rendered 11 of them, with `link` and three of the four icon sizes
 * rendered nowhere, under a caption that read "five variants, four sizes".
 *
 * Naming it costs nothing at runtime and makes the two exports below DERIVED, so
 * a variant added here appears in the matrix without anyone remembering to.
 */
const BUTTON_VARIANT_TABLE = {
  variant: {
    default: "bg-primary text-primary-foreground hover:bg-primary/90",
    destructive:
      "bg-destructive text-destructive-foreground hover:bg-destructive/90",
    outline:
      "border bg-background shadow-xs text-foreground hover:bg-accent/10 hover:text-foreground hover:border-accent/60",
    // "Secondary button is always a border button." (the owner, 2026-08-19)
    // Implemented literally, and the border takes no colour utility on
    // purpose: bare `border` is `currentColor`, so the edge is the button's
    // OWN ink, which is the value the engine already resolved for this
    // ground. Naming a colour here would be a second answer to a question
    // the ground has already answered.
    secondary:
      "border bg-secondary text-secondary-foreground hover:bg-secondary/80",
    ghost:
      "text-foreground hover:bg-accent/10 hover:text-foreground",
    link: "text-primary underline-offset-4 hover:underline",
  },
  size: {
    default: "h-9 px-4 py-2 has-[>svg]:px-3",
    xs: "h-6 gap-1 rounded-md px-2 text-xs has-[>svg]:px-1.5 [&_svg:not([class*='size-'])]:size-3",
    sm: "h-8 gap-1.5 rounded-md px-3 has-[>svg]:px-2.5",
    lg: "h-10 rounded-md px-6 has-[>svg]:px-4",
    icon: "size-9",
    "icon-xs": "size-6 rounded-md [&_svg:not([class*='size-'])]:size-3",
    "icon-sm": "size-8",
    "icon-lg": "size-10",
  },
} as const

/** Every declared variant, in declaration order. Derived, never hand-listed. */
export const BUTTON_VARIANTS = Object.keys(
  BUTTON_VARIANT_TABLE.variant
) as (keyof typeof BUTTON_VARIANT_TABLE.variant)[]

/** Every declared size, in declaration order. Derived, never hand-listed. */
export const BUTTON_SIZES = Object.keys(
  BUTTON_VARIANT_TABLE.size
) as (keyof typeof BUTTON_VARIANT_TABLE.size)[]

const buttonVariants = cva(
  "inline-flex shrink-0 items-center justify-center gap-2 rounded-md text-sm font-medium whitespace-nowrap transition-all outline-none disabled:pointer-events-none disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-destructive/20 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: BUTTON_VARIANT_TABLE,
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean
  }) {
  const Comp = asChild ? Slot.Root : "button"

  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }
