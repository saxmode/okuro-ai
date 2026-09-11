import * as React from "react"
import { XIcon } from "lucide-react"
import { Dialog as DialogPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"
import { useFrameStamp } from "@/lib/ground-portal"
import { Button } from "@/components/ui/button"

function Dialog({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Root>) {
  return <DialogPrimitive.Root data-slot="dialog" {...props} />
}

function DialogTrigger({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Trigger>) {
  return <DialogPrimitive.Trigger data-slot="dialog-trigger" {...props} />
}

function DialogPortal({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Portal>) {
  return <DialogPrimitive.Portal data-slot="dialog-portal" {...props} />
}

function DialogClose({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Close>) {
  return <DialogPrimitive.Close data-slot="dialog-close" {...props} />
}

/**
 * THE SCRIM CARRIES THE GROUND STAMP AND MUST STILL BE TRANSLUCENT.
 *
 * `DialogContent` spreads `frameProps` onto this overlay as well as onto the
 * panel, and it is right to: without it a dialog opened from inside a dark
 * emphasis panel would veil the page with the ROOT ground's scrim. But the
 * emitted portal rule is `[data-ground="X"] { background-color: <the ground> }`,
 * a (0,1,0) selector in a sheet that loads AFTER the utilities — so it beats
 * `bg-scrim` and paints the backdrop OPAQUE. Measured in Chromium 151 against a
 * control: an unstamped `bg-scrim` element computes `rgba(0, 0, 0, 0.6)`, this
 * one computed `rgb(255, 255, 255)`. Every modal in the app would lose its
 * backdrop the moment the engine sheet is linked.
 *
 * THE INLINE STYLE IS THE FIX AND NOT A HACK: it is the engine's own scrim
 * name, and inline is the only specificity that outranks a portal rule without
 * inventing a second portal attribute. The class of the defect is general —
 * anything whose job is to be translucent cannot take the ground's paint — and
 * `sheet.tsx` and `command-palette.tsx` carry the same two lines.
 */
function DialogOverlay({
  className,
  style,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      data-slot="dialog-overlay"
      className={cn(
        "fixed inset-0 z-50 bg-scrim backdrop-blur-sm data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0",
        className
      )}
      style={{ backgroundColor: "var(--color-background-scrim)", ...style }}
      {...props}
    />
  )
}

function DialogContent({
  className,
  children,
  showCloseButton = true,
  container,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & {
  showCloseButton?: boolean
  /**
   * Where the portal mounts. Defaults to `document.body`, which is right for
   * every app use.
   *
   * IT EXISTS FOR THE DESIGN-SYSTEM SHOWCASE. /design-construction renders the
   * real primitives inside an iframe that wears the kit being edited; a dialog
   * that portals to the EDITOR's body opens over the page in the app's own
   * theme, so it looks correct and measures nothing. Nine primitives shared
   * that assumption and were listed as unreachable because of it. One optional
   * prop, threaded to Radix, makes them reachable and changes nothing for any
   * existing caller.
   */
  container?: HTMLElement | null
}) {
  const { probe, frameProps } = useFrameStamp()
  return (
    <>
      {/* In-tree marker: the ground this dialog was DECLARED on. Both the
          overlay and the content are stamped with it -- the overlay's scrim is
          a per-ground value too, so an unstamped overlay would veil the page
          with the ROOT ground's alpha. */}
      <span {...probe} />
      <DialogPortal data-slot="dialog-portal" container={container ?? undefined}>
        <DialogOverlay {...frameProps} />
        <DialogPrimitive.Content
          {...frameProps}
          data-slot="dialog-content"
          className={cn(
            // Width: fluid up to viewport-2rem, default cap at 32rem.
            // Consumers can widen via `max-w-*` (e.g. max-w-2xl) — we use
            // max-width here (not width) so those overrides actually apply.
            // overflow-x-hidden clips any rogue horizontal overflow from
            // wide inner content; overflow-y-auto handles tall content.
            "fixed top-[50%] left-[50%] z-50 grid w-[calc(100vw-2rem)] max-w-lg max-h-[calc(100vh-4rem)] translate-x-[-50%] translate-y-[-50%] gap-4 overflow-x-hidden overflow-y-auto rounded-lg border border-border bg-surface-elevated p-6 shadow-2xl duration-200 outline-none data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95",
            className
          )}
          {...props}
        >
          {children}
          {showCloseButton && (
            <DialogPrimitive.Close
              data-slot="dialog-close"
              className="absolute top-4 right-4 rounded-xs opacity-70 transition-opacity hover:opacity-100 disabled:pointer-events-none data-[state=open]:bg-accent data-[state=open]:text-muted-foreground [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4"
            >
              <XIcon />
              <span className="sr-only">Close</span>
            </DialogPrimitive.Close>
          )}
        </DialogPrimitive.Content>
      </DialogPortal>
    </>
  )
}

function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-header"
      className={cn("flex flex-col gap-2 text-center sm:text-left", className)}
      {...props}
    />
  )
}

function DialogFooter({
  className,
  showCloseButton = false,
  children,
  ...props
}: React.ComponentProps<"div"> & {
  showCloseButton?: boolean
}) {
  return (
    <div
      data-slot="dialog-footer"
      className={cn(
        "flex flex-col-reverse gap-2 sm:flex-row sm:justify-end",
        className
      )}
      {...props}
    >
      {children}
      {showCloseButton && (
        <DialogPrimitive.Close asChild>
          <Button variant="outline">Close</Button>
        </DialogPrimitive.Close>
      )}
    </div>
  )
}

function DialogTitle({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      data-slot="dialog-title"
      className={cn("type-subtitle leading-none", className)}
      {...props}
    />
  )
}

function DialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      data-slot="dialog-description"
      className={cn("type-small text-muted-foreground", className)}
      {...props}
    />
  )
}

export {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
}
