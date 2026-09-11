import * as React from "react"

/**
 * The path out of the tree, for the design engine's FRAME — ground and rung.
 *
 *     "that's something that needs to be solved systematically"  (d8)
 *
 * TWO AXES, ONE MECHANISM. Ground came first and rung is the same defect: both
 * are properties an element inherits from its ancestors, and a portal has no
 * ancestors. Measured 2026-08-19 on the COMPONENTS scene: a `Dialog` opened
 * inside a frame at rung XL rendered its text at the app's default rung, because
 * `data-rung` sits on the frame's tree root and the dialog mounts under the
 * frame's `<body>`. Exactly the escape this file was written for, one axis later
 * — so it is fixed here rather than in nine components.
 *
 * THE PROBLEM. The engine resolves colour by descent: every boundary publishes
 * its resolved ground as `--ground`, and every ground-dependent rule is a style
 * container query on it. That works because a style query reads the nearest
 * ANCESTOR's published value -- so it works exactly as far as the DOM tree
 * reaches. A portal leaves the tree. A dropdown opened from inside a dark
 * emphasis panel mounts under `<body>`, inherits the ROOT's ground, and renders
 * light. Silently, and on every overlay in the app.
 *
 * THE MECHANISM, which is the engine's contract and not this file's invention:
 * the ground is a property of the ELEMENT, not of where it happens to be
 * rendered. An element that leaves the tree carries `data-ground="g-xxxxxx"`,
 * and the emitted sheet has a rule for every reachable ground key that
 * re-publishes it and repaints the element. So a portalled overlay is
 * indistinguishable from an in-tree one -- proven end to end in Chromium 151,
 * including a `[data-emphasis]` child INSIDE the portal continuing the
 * alternation, and including the unstamped control case falling to the root
 * ground, which is the documented fallback rather than a failure.
 *
 * WHY A PROBE AND NOT THE TRIGGER. The value has to be read from an element that
 * is still in the tree, and the component that renders a portal generally has no
 * handle on its own trigger -- Radix keeps that inside its own context. What it
 * does have is its own position in the tree: whatever it renders OUTSIDE the
 * portal lands where the consumer wrote the component. So `useFrameStamp`
 * hands back props for a zero-cost marker to render there, reads the ground off
 * it, and hands back the attribute for the portalled content.
 *
 * `display: none` for the marker, measured rather than assumed: custom
 * properties are computed on every element regardless of display, so a
 * display:none probe reports `--ground` correctly at every depth (verified
 * alongside `display: contents` and an absolutely-positioned zero box -- all
 * three agree). It is the only one of the three that cannot perturb layout at
 * all, since it generates no box and contributes nothing to a flex or grid
 * parent.
 *
 * WHAT THIS DELIBERATELY IS NOT:
 *
 *   * NOT a React `GroundProvider`. The recursion lives in CSS. A context would
 *     need a second resolver in TypeScript -- a second source of truth for the
 *     one thing the register calls uniform.
 *   * NOT a MutationObserver on `<body>`. It races the first paint, fights
 *     React's ownership of the node, and cannot be falsified by a test.
 *   * NOT a prop consumers have to remember. Radix's own `container` prop stays
 *     what it is -- the escape hatch that mounts a portal inside the
 *     design-system showcase's iframe -- and the two do not conflict.
 */

/** The attribute the emitted sheet keys its portal rules on. */
export const GROUND_ATTRIBUTE = "data-ground"

/** The attribute a frame carries its size rung on -- `[data-rung="XXL"]` etc. */
export const RUNG_ATTRIBUTE = "data-rung"

/** Read an in-tree element's resolved ground key, or nothing if it has none. */
export function groundOf(node: Element | null | undefined): string | undefined {
  if (!node || typeof getComputedStyle !== "function") return undefined
  const value = getComputedStyle(node).getPropertyValue("--ground").trim()
  return value || undefined
}

/**
 * Read the rung of the nearest frame at or above `node`, or nothing.
 *
 * `closest`, not a computed property, because a rung is an ATTRIBUTE and the
 * only thing that identifies the frame that owns it. Reading `--spacing-xs`
 * instead would give the resolved value and no way to re-publish it, since the
 * sheet keys its per-rung block on the attribute.
 */
export function rungOf(node: Element | null | undefined): string | undefined {
  const frame = node?.closest?.(`[${RUNG_ATTRIBUTE}]`)
  return frame?.getAttribute(RUNG_ATTRIBUTE) || undefined
}

const PROBE_STYLE: React.CSSProperties = { display: "none" }

export type FrameStamp = {
  /** Spread onto a marker rendered IN the tree, outside the portal. */
  probe: {
    ref: React.RefObject<HTMLSpanElement | null>
    "aria-hidden": true
    style: React.CSSProperties
  }
  /** Spread onto the portalled content. Either key is absent when the tree
   *  position had no value for it, which is the root fallback rather than a
   *  missing value. */
  frameProps: { [GROUND_ATTRIBUTE]?: string; [RUNG_ATTRIBUTE]?: string }
}

export function useFrameStamp(): FrameStamp {
  const ref = React.useRef<HTMLSpanElement | null>(null)
  const [ground, setGround] = React.useState<string | undefined>(undefined)
  const [rung, setRung] = React.useState<string | undefined>(undefined)

  // A layout effect, not an effect: React flushes these before the browser
  // paints, so the state change lands in the same frame and the overlay never
  // shows one frame of the root ground before correcting itself. No dependency
  // array on purpose -- the ground can change under a component that never
  // re-renders for its own reasons (a parent flips `data-emphasis`), and the
  // equality check makes a no-op run genuinely free rather than a render loop.
  React.useLayoutEffect(() => {
    const foundGround = groundOf(ref.current)
    setGround((previous) => (previous === foundGround ? previous : foundGround))
    const foundRung = rungOf(ref.current)
    setRung((previous) => (previous === foundRung ? previous : foundRung))
  })

  // AND AN OBSERVER FOR THE RUNG, which the render-driven read above cannot
  // replace. Measured 2026-08-19: switching the canvas rung with a dialog open
  // left the dialog one rung behind, permanently. The reason is effect ORDER --
  // React runs effects child-first, so the portal's read (child) fires before the
  // frame's own effect (parent) writes the new attribute, and nothing re-renders
  // afterwards to correct it.
  //
  // THIS IS NOT THE MutationObserver THE HEADER REJECTS. That one watched `<body>`
  // to stamp nodes REACT OWNS, and fought React for them. This watches ONE
  // attribute on the frame root, which React does not own at all: the preview
  // frame injects it as HTML into another document and sets `data-rung`
  // imperatively. An observer is the only mechanism that can see a change React
  // never made.
  // Scoped to the DOCUMENT rather than to the frame element, because a rung of
  // `null` is spelled as the ABSENCE of the attribute -- anchoring the observer on
  // `[data-rung]` would lose its own anchor the moment the canvas returned to the
  // brand's default rung and never see it come back.
  React.useEffect(() => {
    const doc = ref.current?.ownerDocument
    if (!doc || typeof MutationObserver !== "function") return
    const observer = new MutationObserver(() => {
      const found = rungOf(ref.current)
      setRung((previous) => (previous === found ? previous : found))
    })
    observer.observe(doc, {
      attributes: true,
      attributeFilter: [RUNG_ATTRIBUTE],
      subtree: true,
    })
    return () => observer.disconnect()
  }, [])

  return {
    probe: { ref, "aria-hidden": true, style: PROBE_STYLE },
    frameProps: {
      ...(ground ? { [GROUND_ATTRIBUTE]: ground } : {}),
      ...(rung ? { [RUNG_ATTRIBUTE]: rung } : {}),
    },
  }
}
