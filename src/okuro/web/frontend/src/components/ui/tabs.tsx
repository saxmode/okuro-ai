import * as TabsPrimitive from "@radix-ui/react-tabs";
import { cn } from "@/lib/utils";

/**
 * Radix Tabs — keyboard-navigable (arrow keys, home/end) out of the box.
 * Horizontal by default; pass `orientation="vertical"` to Tabs for a
 * left-rail layout (used by Settings, where 14+ tabs overflow the
 * horizontal bar into a 2-row wrap).
 */
export const Tabs = TabsPrimitive.Root;

export const TabsList = ({
  className,
  ...props
}: React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>) => (
  <TabsPrimitive.List
    className={cn(
      // Shape changes based on orientation data attribute that Radix
      // stamps on the list root.
      "inline-flex gap-1",
      // Horizontal: fill the row and scroll when triggers overflow the width
      // (scrollbar hidden — swipe/scroll still works) instead of wrapping.
      "data-[orientation=horizontal]:flex data-[orientation=horizontal]:w-full data-[orientation=horizontal]:max-w-full data-[orientation=horizontal]:flex-row data-[orientation=horizontal]:flex-nowrap data-[orientation=horizontal]:overflow-x-auto data-[orientation=horizontal]:border-b data-[orientation=horizontal]:border-border",
      "data-[orientation=horizontal]:[scrollbar-width:none] data-[orientation=horizontal]:[&::-webkit-scrollbar]:hidden",
      "data-[orientation=vertical]:w-48 data-[orientation=vertical]:flex-col data-[orientation=vertical]:gap-0.5 data-[orientation=vertical]:border-r data-[orientation=vertical]:border-border data-[orientation=vertical]:pr-2",
      className,
    )}
    {...props}
  />
);

export const TabsTrigger = ({
  className,
  ...props
}: React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>) => (
  <TabsPrimitive.Trigger
    className={cn(
      // inline-flex keeps leading icons (lucide SVGs are display:block
      // under Tailwind preflight) aligned on the same line as the label.
      "inline-flex items-center text-xs font-medium text-fg-subtle transition-fast",
      // Horizontal: underline on active
      "data-[orientation=horizontal]:shrink-0 data-[orientation=horizontal]:whitespace-nowrap data-[orientation=horizontal]:px-3 data-[orientation=horizontal]:py-1.5 data-[orientation=horizontal]:uppercase data-[orientation=horizontal]:tracking-wider",
      "data-[orientation=horizontal]:hover:text-fg-muted",
      "data-[orientation=horizontal]:data-[state=active]:text-fg data-[orientation=horizontal]:data-[state=active]:border-b-2 data-[orientation=horizontal]:data-[state=active]:border-accent data-[orientation=horizontal]:data-[state=active]:-mb-px",
      // Vertical: left-rail pill, accent strip on active
      "data-[orientation=vertical]:h-9 data-[orientation=vertical]:justify-start data-[orientation=vertical]:rounded-md data-[orientation=vertical]:px-3 data-[orientation=vertical]:text-left data-[orientation=vertical]:normal-case",
      "data-[orientation=vertical]:hover:bg-surface-elevated data-[orientation=vertical]:hover:text-fg",
      "data-[orientation=vertical]:data-[state=active]:bg-accent-subtle data-[orientation=vertical]:data-[state=active]:text-accent",
      className,
    )}
    {...props}
  />
);

export const TabsContent = ({
  className,
  ...props
}: React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content>) => (
  <TabsPrimitive.Content
    className={cn(
      // Horizontal: top margin. Vertical: inset left.
      "data-[orientation=horizontal]:mt-6",
      "data-[orientation=vertical]:pl-6",
      className,
    )}
    {...props}
  />
);
