import { type ReactNode } from "react";
import { Sheet, SheetContent, SheetTitle } from "./sheet";
import { useIsMobile } from "@/hooks/use-is-mobile";
import { cn } from "@/lib/utils";

/**
 * SidePanel — one primitive, two layouts.
 *
 *  - Desktop (>=768px): renders an inline <aside> exactly where it sits in
 *    the page's flex row, using `desktopClassName` for its width/border. The
 *    existing desktop layout is unchanged.
 *  - Mobile (<768px): renders nothing inline (so the page's centre column
 *    takes the full width) and instead portals the same children into a
 *    slide-over Sheet, summoned via `open`/`onOpenChange`.
 *
 * Pages pair this with <MobilePanelTrigger> in a `md:hidden` toolbar to drive
 * the open state. Breakpoint matches Tailwind `md`, so the JS swap here and
 * the CSS-hidden trigger never disagree.
 */
interface SidePanelProps {
  side?: "left" | "right";
  /** Classes for the desktop inline <aside> (width, border, flex, etc.). */
  desktopClassName?: string;
  /** Mobile sheet open state (ignored on desktop). */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** Accessible title for the mobile sheet (sr-only). */
  title: string;
  /** Extra classes for the mobile <SheetContent> (e.g. width override). */
  sheetClassName?: string;
  /** Classes for the mobile scroll wrapper around children. Default p-4. */
  contentClassName?: string;
  /**
   * Where the mobile sheet portals. Defaults to the app's own `<body>`.
   *
   * SidePanel was the one Sheet consumer that could not forward this. `sheet.tsx`
   * has taken a `container` since the portal contract landed (register rule 14),
   * and every other overlay in the vendored set exposes it -- so a SidePanel
   * rendered inside the design-system showcase's frame would have slid its mobile
   * sheet out over the app instead, on a ground it never belonged to.
   */
  container?: HTMLElement | null;
  children: ReactNode;
}

export function SidePanel({
  side = "left",
  desktopClassName,
  open = false,
  onOpenChange,
  title,
  sheetClassName,
  contentClassName = "p-4",
  container,
  children,
}: SidePanelProps) {
  const isMobile = useIsMobile();

  if (!isMobile) {
    return <aside className={desktopClassName}>{children}</aside>;
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side={side}
        container={container}
        className={cn("w-[min(360px,88vw)] p-0", sheetClassName)}
      >
        <SheetTitle className="sr-only">{title}</SheetTitle>
        <div className={cn("flex-1 min-h-0 overflow-y-auto", contentClassName)}>
          {children}
        </div>
      </SheetContent>
    </Sheet>
  );
}

/**
 * Mobile-only button that toggles a SidePanel. Hidden at >=768px via CSS
 * (`md:hidden`), so desktop never shows it and no JS gate is needed.
 */
export function MobilePanelTrigger({
  icon,
  label,
  onClick,
  className,
}: {
  icon?: ReactNode;
  label: string;
  onClick: () => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      className={cn(
        "md:hidden inline-flex items-center gap-1.5 rounded border border-border px-2.5 py-1.5 text-xs text-tertiary transition-colors hover:bg-surface-elevated hover:text-fg",
        className,
      )}
    >
      {icon}
      {label}
    </button>
  );
}
