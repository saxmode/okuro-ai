"use client"

import * as React from "react"
import { ScrollArea as ScrollAreaPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

function ScrollArea({
  className,
  children,
  ...props
}: React.ComponentProps<typeof ScrollAreaPrimitive.Root>) {
  return (
    <ScrollAreaPrimitive.Root
      data-slot="scroll-area"
      className={cn("relative", className)}
      {...props}
    >
      <ScrollAreaPrimitive.Viewport
        data-slot="scroll-area-viewport"
        // `outline-none` WAS HERE AND IT SUPPRESSED THE ONLY RING THIS ELEMENT
        // CAN GET. Measured 2026-09-03 in the live COMPONENTS scene: the
        // viewport carries NO tabindex attribute, yet Chrome makes a scrollable
        // region keyboard-focusable on its own. The register's generic ring is
        // emitted as `[tabindex]:not([tabindex="-1"]):focus-visible`, which is
        // an ATTRIBUTE selector — so it cannot match an element the browser
        // focuses implicitly, and this class then removed the user agent's
        // fallback too. Tab-reachable with nothing drawn.
        //
        // Dropping the class is the honest local repair: the element gets a
        // ring again. It is NOT the system's ring, and that gap is a register
        // question rather than a component one — see the note in the session's
        // report. Do not "fix" it by hard-coding a ring here; that is the
        // build-time literal this whole phase has been removing.
        className="size-full rounded-[inherit] transition-[color,box-shadow]"
      >
        {children}
      </ScrollAreaPrimitive.Viewport>
      <ScrollBar />
      <ScrollAreaPrimitive.Corner />
    </ScrollAreaPrimitive.Root>
  )
}

function ScrollBar({
  className,
  orientation = "vertical",
  ...props
}: React.ComponentProps<typeof ScrollAreaPrimitive.ScrollAreaScrollbar>) {
  return (
    <ScrollAreaPrimitive.ScrollAreaScrollbar
      data-slot="scroll-area-scrollbar"
      orientation={orientation}
      className={cn(
        "flex touch-none p-px transition-colors select-none",
        orientation === "vertical" &&
          "h-full w-2.5 border-l border-l-transparent",
        orientation === "horizontal" &&
          "h-2.5 flex-col border-t border-t-transparent",
        className
      )}
      {...props}
    >
      <ScrollAreaPrimitive.ScrollAreaThumb
        data-slot="scroll-area-thumb"
        className="relative flex-1 rounded-full bg-border"
      />
    </ScrollAreaPrimitive.ScrollAreaScrollbar>
  )
}

export { ScrollArea, ScrollBar }
