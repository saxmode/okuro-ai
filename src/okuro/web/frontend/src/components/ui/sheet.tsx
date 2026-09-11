import * as React from "react"
import { XIcon } from "lucide-react"
import { Dialog as DialogPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"
import { useFrameStamp } from "@/lib/ground-portal"

/**
 * Sheet — right-anchored (default) slide-over panel built on radix
 * Dialog. Mirrors the shadcn ``Sheet`` API so we can swap in the
 * upstream primitive later without churning consumers.
 *
 * ``side`` defaults to ``right`` because that is the only orientation
 * we ship today. Width is controlled by ``className`` on
 * ``SheetContent`` (e.g. ``w-[min(820px,100vw)]``).
 */

function Sheet({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Root>) {
  return <DialogPrimitive.Root data-slot="sheet" {...props} />
}

function SheetTrigger({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Trigger>) {
  return <DialogPrimitive.Trigger data-slot="sheet-trigger" {...props} />
}

function SheetPortal({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Portal>) {
  return <DialogPrimitive.Portal data-slot="sheet-portal" {...props} />
}

function SheetClose({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Close>) {
  return <DialogPrimitive.Close data-slot="sheet-close" {...props} />
}

/** The stamped scrim stays translucent — the whole reason is on `DialogOverlay`
 *  in `dialog.tsx`, measured there. Same two lines, same class of defect. */
function SheetOverlay({
  className,
  style,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      data-slot="sheet-overlay"
      className={cn(
        "fixed inset-0 z-50 bg-scrim backdrop-blur-sm data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0",
        className,
      )}
      style={{ backgroundColor: "var(--color-background-scrim)", ...style }}
      {...props}
    />
  )
}

type SheetSide = "right" | "left" | "top" | "bottom"

const SIDE_CLASSES: Record<SheetSide, string> = {
  right:
    "inset-y-0 right-0 h-full w-[min(820px,100vw)] border-l data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right",
  left:
    "inset-y-0 left-0 h-full w-[min(820px,100vw)] border-r data-[state=closed]:slide-out-to-left data-[state=open]:slide-in-from-left",
  top:
    "inset-x-0 top-0 w-full max-h-[min(80vh,100%)] border-b data-[state=closed]:slide-out-to-top data-[state=open]:slide-in-from-top",
  bottom:
    "inset-x-0 bottom-0 w-full max-h-[min(80vh,100%)] border-t data-[state=closed]:slide-out-to-bottom data-[state=open]:slide-in-from-bottom",
}

interface SheetContentProps
  extends React.ComponentProps<typeof DialogPrimitive.Content> {
  side?: SheetSide
  showCloseButton?: boolean
  /**
   * Where the portal mounts. Defaults to `document.body`. Exists so the
   * design-system showcase can render this primitive inside the specimen
   * iframe — see the note on `DialogContent`.
   */
  container?: HTMLElement | null
}

function SheetContent({
  className,
  children,
  side = "right",
  showCloseButton = true,
  container,
  ...props
}: SheetContentProps) {
  const { probe, frameProps } = useFrameStamp()
  return (
    <>
      {/* In-tree marker: the ground this panel was DECLARED on. See dialog.tsx. */}
      <span {...probe} />
      <SheetPortal data-slot="sheet-portal" container={container ?? undefined}>
        <SheetOverlay {...frameProps} />
        <DialogPrimitive.Content
          {...frameProps}
          data-slot="sheet-content"
          className={cn(
            "fixed z-50 flex flex-col bg-surface text-fg shadow-2xl outline-none border-border",
            "duration-300 data-[state=closed]:animate-out data-[state=open]:animate-in",
            SIDE_CLASSES[side],
            className,
          )}
          {...props}
        >
          {children}
          {showCloseButton && (
            <DialogPrimitive.Close
              data-slot="sheet-close"
              aria-label="Close panel"
              className="absolute top-4 right-4 rounded-xs opacity-70 transition-opacity hover:opacity-100 disabled:pointer-events-none [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4"
            >
              <XIcon />
              <span className="sr-only">Close</span>
            </DialogPrimitive.Close>
          )}
        </DialogPrimitive.Content>
      </SheetPortal>
    </>
  )
}

function SheetHeader({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sheet-header"
      className={cn("flex flex-col gap-1.5 text-left", className)}
      {...props}
    />
  )
}

function SheetFooter({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sheet-footer"
      className={cn("mt-auto flex flex-col gap-2", className)}
      {...props}
    />
  )
}

function SheetTitle({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      data-slot="sheet-title"
      className={cn("type-subtitle leading-none text-fg", className)}
      {...props}
    />
  )
}

function SheetDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      data-slot="sheet-description"
      className={cn("text-xs text-tertiary", className)}
      {...props}
    />
  )
}

export {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetOverlay,
  SheetPortal,
  SheetTitle,
  SheetTrigger,
}
