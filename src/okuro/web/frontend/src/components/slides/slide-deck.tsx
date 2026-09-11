import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { motion, useReducedMotion } from "motion/react";
import { tokenizeApiSrc } from "@/lib/slides-api";
import {
  type Arrangement,
  type Deck,
  type SlideElement,
  DEFAULT_LINE_HEIGHT,
  allElementIds,
  captionScrim,
  chartSurface,
  boxDecor,
  deckSize,
  hugs,
  imageAlt,
  isComposite,
  maxElementDepth,
  resolveElement,
  visibleAtDepth,
} from "./scene";
import { flattenElements } from "./flatten";
import { fitToCanvas } from "./fit";
import { elementHeight } from "./measure";
import { renderRich } from "./rich-text";
import { ElementInner } from "./element-body";

/**
 * SlideDeck — the deck renderer. Two transition engines share one element
 * renderer:
 *
 * - "morph" (default, smart-animate): renders the UNION of every element on one
 *   persistent layer and animates each to its resolved state for the CURRENT
 *   slide. Shared ids morph (position/size/style); dropped ids slide off + fade;
 *   arriving ids slide in from the other side.
 * - "push": renders each slide as a cohesive, opaque block laid out in a track
 *   along the arrangement axis, then translates the whole track so the current
 *   slide fills the frame. The old slide slides fully out and the next slides
 *   fully in — no morph, no cross-fade. This is the "screen leaves, next
 *   appears" effect.
 *
 * `arrangement` (horizontal → left/right, vertical → up/down) owns the axis for
 * BOTH engines. `mode` prop overrides `deck.transition.mode` for live toggling.
 */
export function SlideDeck({
  deck,
  index,
  arrangement,
  mode,
  maxDepth,
}: {
  deck: Deck;
  index: number;
  arrangement?: Arrangement;
  mode?: "morph" | "push";
  /** Progressive-disclosure reveal level: render only elements whose
   *  `depth <= maxDepth`. Omit to show everything (editor/thumbnail/export
   *  default) — present mode passes its live depth to drive the reveal. */
  maxDepth?: number;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1);
  const { w: canvasW, h: canvasH } = deckSize(deck);
  // Flatten frames → absolute boxes so the animation engine only sees flat,
  // positioned elements (children keep their ids → they still morph). Inject the
  // guaranteed size so every downstream reader (MorphLayer/PushTrack/
  // resolveElement) is safe even if the source deck had none.
  const base = arrangement ? { ...deck, arrangement } : deck;
  // Flatten (frames → measured absolute boxes), then fit each slide to the
  // canvas using those MEASURED heights so wrapped text never crops or spills.
  const effectiveDeck = {
    ...base,
    size: { w: canvasW, h: canvasH },
    slides: base.slides.map((s) => ({
      ...s,
      // Pass a re-measurer so the fit converges against real wrapped text height
      // (scaling changes the line count → h*scale is only a first guess).
      elements: fitToCanvas(
        flattenElements(s.elements, base.font),
        canvasW,
        canvasH,
        (el) => elementHeight(el, base.font),
      ),
    })),
  };

  // Scale the baseline canvas to fit the wrapper width (keeps authored px coords).
  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => setScale(el.clientWidth / canvasW);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [canvasW]);

  // Honor prefers-reduced-motion: collapse the transition to an instant cut so
  // slide changes don't animate for users who asked the OS to reduce motion.
  const reduce = useReducedMotion();
  const { duration: baseDuration, easing } = effectiveDeck.transition;
  const duration = reduce ? 0 : baseDuration;
  const effectiveMode = mode ?? effectiveDeck.transition.mode ?? "morph";
  // Default reveal level shows everything (max depth present) → identical to the
  // pre-depth renderer. Present mode overrides with its live stepper value.
  const effectiveMaxDepth = maxDepth ?? maxElementDepth(effectiveDeck);

  return (
    <div
      ref={wrapRef}
      className="relative w-full overflow-hidden rounded-xl border border-border"
      style={{ height: canvasH * scale, background: deck.background ?? "#0b0f0c" }}
    >
      <div
        className="absolute left-0 top-0 origin-top-left"
        style={{ width: canvasW, height: canvasH, transform: `scale(${scale})`, fontFamily: deck.font }}
      >
        {effectiveMode === "push" ? (
          <PushTrack deck={effectiveDeck} index={index} duration={duration} easing={easing} maxDepth={effectiveMaxDepth} />
        ) : (
          <MorphLayer deck={effectiveDeck} index={index} duration={duration} easing={easing} maxDepth={effectiveMaxDepth} />
        )}
      </div>
    </div>
  );
}

/** Smart-animate: one persistent layer, every element tweens to its resolved
 *  state for the current slide. */
function MorphLayer({ deck, index, duration, easing, maxDepth }: { deck: Deck; index: number; duration: number; easing: any; maxDepth: number }) {
  const ids = allElementIds(deck);
  // Ids present on the current or an adjacent slide. Only these mount heavy
  // (flow iframe / video) content — otherwise the union render would mount and
  // autoplay EVERY embed in the deck at once, off-screen (#14). The morphing box
  // is always present; only its heavy child is deferred until the slide is near.
  const activeIds = new Set<string>();
  for (const i of [index - 1, index, index + 1]) {
    for (const e of deck.slides[i]?.elements ?? []) activeIds.add(e.id);
  }
  // Elements on the current slide, in flattened absolute coords — the space a
  // caption's scrim overlap is tested against.
  const slideEls = deck.slides[index]?.elements ?? [];
  return (
    <>
      {ids.map((id) => {
        const r = resolveElement(deck, id, index, maxDepth);
        if (!r) return null;
        const { el, dx, dy, opacity } = r;
        // Legibility plate for a caption over an image (only meaningful while the
        // element is actually on this slide).
        const scrim = r.present ? captionScrim(el, slideEls) : null;
        return (
          <motion.div
            key={id}
            className="absolute flex flex-col"
            style={layoutStyle(el)}
            initial={false}
            animate={{
              x: el.x + dx,
              y: el.y + dy,
              width: el.w,
              height: el.h,
              opacity: opacity * (el.opacity ?? 1),
              backgroundColor: scrim ?? el.bg ?? "rgba(0,0,0,0)",
              color: el.color ?? "#e8ffe8",
              fontSize: el.fontSize ?? 28,
              borderRadius: el.radius ?? (scrim ? 6 : 0),
            }}
            transition={{ duration, ease: easing }}
          >
            {elementContent(el, activeIds.has(id), el.kind === "chart" ? chartSurface(el, slideEls, deck.background) : deck.background)}
          </motion.div>
        );
      })}
    </>
  );
}

/** Push: slides laid out edge-to-edge in a track along the arrangement axis;
 *  the whole track translates so the current slide fills the frame. */
function PushTrack({ deck, index, duration, easing, maxDepth }: { deck: Deck; index: number; duration: number; easing: any; maxDepth: number }) {
  const horizontal = deck.arrangement === "horizontal";
  const { w: sw, h: sh } = deckSize(deck); // guarded — never touch deck.size raw
  return (
    <motion.div
      className="absolute left-0 top-0"
      style={{ width: sw, height: sh }}
      initial={false}
      animate={{ x: horizontal ? -index * sw : 0, y: horizontal ? 0 : -index * sh }}
      transition={{ duration, ease: easing }}
    >
      {deck.slides.map((s, i) => {
        // Same lazy-mount rule as MorphLayer: only the current + adjacent slides
        // mount their flow/video content so the whole track doesn't autoplay
        // every embed at once (#14).
        const near = Math.abs(i - index) <= 1;
        return (
          <div
            key={s.id}
            className="absolute"
            style={{
              left: horizontal ? i * sw : 0,
              top: horizontal ? 0 : i * sh,
              width: sw,
              height: sh,
              overflow: "hidden",
              background: deck.background ?? "transparent",
            }}
          >
            {s.elements.map((el) => {
              const scrim = captionScrim(el, s.elements);
              // Below the reveal level → hidden. It parks one canvas ahead along
              // the arrangement axis and fades out, so raising the depth SLIDES it
              // in (the same enter vector morph mode uses) instead of only fading.
              const hidden = !visibleAtDepth(el, maxDepth);
              const { dx, dy } = pushDepthOffset(deck.arrangement, hidden, sw, sh);
              return (
                <motion.div
                  key={el.id}
                  className="absolute flex flex-col"
                  style={{
                    ...layoutStyle(el),
                    ...staticPaint(el),
                    ...(scrim ? { backgroundColor: scrim, borderRadius: el.radius ?? 6 } : {}),
                    pointerEvents: hidden ? "none" : undefined,
                  }}
                  initial={false}
                  animate={{ x: el.x + dx, y: el.y + dy, opacity: hidden ? 0 : el.opacity ?? 1 }}
                  transition={{ duration, ease: easing }}
                >
                  {elementContent(el, near, el.kind === "chart" ? chartSurface(el, s.elements, deck.background) : deck.background)}
                </motion.div>
              );
            })}
          </div>
        );
      })}
    </motion.div>
  );
}

/** Layout/typography style shared by both engines (geometry-independent). */
function layoutStyle(el: SlideElement): CSSProperties {
  return {
    left: 0,
    top: 0,
    zIndex: el.z ?? 0,
    // Hug kinds (text + text-bearing primitives) sit from the top + wrap so long
    // copy is never cropped; boxes/images clip to their frame.
    overflow: hugs(el.kind) ? "visible" : "hidden",
    alignItems: el.align === "center" ? "center" : el.align === "right" ? "flex-end" : "flex-start",
    justifyContent: hugs(el.kind) ? "flex-start" : "center",
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
    lineHeight: el.lineHeight ?? DEFAULT_LINE_HEIGHT,
    letterSpacing: el.letterSpacing ? `${el.letterSpacing}px` : undefined,
    fontWeight: el.fontWeight ?? 400,
    padding: el.pad ?? 0,
    textAlign: el.align ?? "left",
    ...boxDecor(el),
  };
}

/** Size + paint for a push-mode element. Position (x/y) and opacity are NOT here
 *  — PushTrack animates those via motion so a depth reveal can slide the element
 *  in (enter vector) rather than only fading it. */
function staticPaint(el: SlideElement): CSSProperties {
  return {
    width: el.w,
    height: el.h,
    backgroundColor: el.bg ?? "rgba(0,0,0,0)",
    color: el.color ?? "#e8ffe8",
    fontSize: el.fontSize ?? 28,
    borderRadius: el.radius ?? 0,
  };
}

/** The off-state offset for a push-mode element hidden by the reveal depth —
 *  parked one canvas AHEAD along the arrangement axis so raising the level
 *  slides it into place. Mirrors resolveElement's depth off-state so push and
 *  morph reveal the same way. A visible element has no offset. Pure/testable. */
export function pushDepthOffset(
  arrangement: Arrangement,
  hidden: boolean,
  sw: number,
  sh: number,
): { dx: number; dy: number } {
  if (!hidden) return { dx: 0, dy: 0 };
  return arrangement === "horizontal" ? { dx: sw, dy: 0 } : { dx: 0, dy: sh };
}

/** Inner content for an element (image / video / flow / rich text).
 *
 * `mountHeavy` gates the two heavy embeds (video, flow iframe): when false the
 * element still renders its morphing box but the embed is NOT mounted, so a deck
 * full of videos/iframes doesn't mount + autoplay every one at once (#14).
 * Images stay mounted (cheap, and the browser lazy-decodes off-screen ones). */
function elementContent(el: SlideElement, mountHeavy = true, surface?: string): ReactNode {
  if (el.kind === "image" && el.src) {
    return <img src={tokenizeApiSrc(el.src)} alt={imageAlt(el)} className="h-full w-full object-cover" />;
  }
  if (el.kind === "video" && el.src) {
    if (!mountHeavy) return null;
    return <video src={tokenizeApiSrc(el.src)} className="h-full w-full object-cover" autoPlay muted loop playsInline />;
  }
  if (el.kind === "flow" && el.flowId) {
    if (!mountHeavy) return null;
    return <iframe src={`/flow?id=${encodeURIComponent(el.flowId)}&embed=1`} title="okuro flow" className="h-full w-full border-0" />;
  }
  if (isComposite(el.kind)) return <ElementInner el={el} surface={surface} />;
  return renderRich(el.text);
}
