import * as React from "react"
import { Progress as ProgressPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

function Progress({
  className,
  value,
  ...props
}: React.ComponentProps<typeof ProgressPrimitive.Root>) {
  return (
    <ProgressPrimitive.Root
      data-slot="progress"
      /* `value` REACHES THE ROOT, and that is a fix rather than tidying. It was
         destructured out and used only for the indicator's transform, so Radix
         never saw it: the root reported `data-state="indeterminate"` and emitted
         no `aria-valuenow` at all, on every progress bar in the app. A sighted
         reader saw 62 %; a screen reader was told the operation was of unknown
         length. Measured in Chromium 151 before the fix. */
      value={value}
      className={cn(
        "relative h-2 w-full overflow-hidden rounded-full bg-primary/20",
        className
      )}
      {...props}
    >
      <ProgressPrimitive.Indicator
        data-slot="progress-indicator"
        className="h-full w-full flex-1 bg-primary transition-all"
        style={{ transform: `translateX(-${100 - (value || 0)}%)` }}
      />
    </ProgressPrimitive.Root>
  )
}

export { Progress }
