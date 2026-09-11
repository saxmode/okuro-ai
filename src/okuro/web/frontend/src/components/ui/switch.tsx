import * as React from "react"
import { Switch as SwitchPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

/*
 * THE TRACK HEIGHT IS PX (a spacing-scale rung), NOT REM, AND THAT IS WHY THE
 * THUMB CENTRES.
 *
 * It was `h-[2.3rem]` — shadcn's own 1.15rem doubled by hand to survive the
 * 50 % root. Under that root it renders 18.3906px, a FRACTIONAL height, while
 * the other axis (`w-8`) and the thumb (`size-4`) are on Tailwind's spacing
 * scale, which is bound to a 4px unit and is root-immune. So one axis moved with
 * the root and the other did not: a 16px thumb inside a 16.3906px content box,
 * 0.195px of slack per side, and the compositor rounds that to 0 on one side and
 * 1 on the other. Measured on the deployed page: top gap 1.19px, bottom 1.20px.
 *
 * Both rungs now centre exactly, because every number on both axes is a whole
 * pixel in the same unit — 2px above and below the thumb (1px border + 1px of
 * slack), and a travel that lands the thumb flush inside the content box:
 *
 *   default  track 20 x 32, thumb 16, travel calc(100% - 2px) = 14 = 30 - 16
 *   sm       track 16 x 24, thumb 12, travel calc(100% - 2px) = 10 = 22 - 12
 *
 * The vertical inset (2px) and the horizontal one (1px, the border alone) are
 * deliberately NOT the same number: `items-center` distributes the cross-axis
 * slack, and the travel above is sized to put the thumb flush against the
 * content edge at both ends. Equal insets on all four sides would mean a
 * shorter travel, which is a different switch, not a centred one.
 *
 * Do not put the height back in rem. A chrome dimension that must centre a
 * spacing-scale child has to be denominated the same way the child is.
 */

function Switch({
  className,
  size = "default",
  ...props
}: React.ComponentProps<typeof SwitchPrimitive.Root> & {
  size?: "sm" | "default"
}) {
  return (
    <SwitchPrimitive.Root
      data-slot="switch"
      data-size={size}
      className={cn(
        "peer group/switch inline-flex shrink-0 items-center rounded-full border border-transparent shadow-xs transition-all outline-none disabled:cursor-not-allowed disabled:opacity-50 data-[size=default]:h-5 data-[size=default]:w-8 data-[size=sm]:h-4 data-[size=sm]:w-6 data-[state=checked]:bg-primary data-[state=unchecked]:bg-input",
        className
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        data-slot="switch-thumb"
        className={cn(
          "pointer-events-none block rounded-full bg-background ring-0 transition-transform group-data-[size=default]/switch:size-4 group-data-[size=sm]/switch:size-3 data-[state=checked]:translate-x-[calc(100%-2px)] data-[state=unchecked]:translate-x-0"
        )}
      />
    </SwitchPrimitive.Root>
  )
}

export { Switch }
