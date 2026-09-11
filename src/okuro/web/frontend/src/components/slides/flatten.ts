import type { SlideElement } from "./scene";
import { hugs } from "./scene";
import { elementHeight } from "./measure";

/**
 * Frames are an AUTHORING construct (a container + optional auto-layout). For
 * rendering and smart-animate we flatten them into absolute boxes so the
 * renderer + animation engine only ever deal with flat, positioned elements —
 * and each child keeps its stable id, so it still morphs across slides.
 *
 * Text elements HUG their wrapped content: their effective height is measured
 * (the authored `h` assumes a single line), and that measured height drives
 * stacking + the frame's hug size so containers size to real, broken text.
 */

/** Position a frame's children in the frame's LOCAL coords (0,0 = inner origin)
 *  and return the hugged content size for auto-layout flows. Heights are the
 *  measured (wrapped) heights so the frame truly hugs. */
export function layoutChildren(
  frame: SlideElement,
  deckFont?: string,
): { children: SlideElement[]; w: number; h: number } {
  const kids = frame.children ?? [];
  const lay = frame.layout;
  // A child's stacking height must be its RENDERED height: text hugs its wrapped
  // lines (measure.ts), and a nested FRAME hugs its own content (recurse). Using
  // the declared `h` for a sub-frame would stack it short, then flattenElements
  // re-hugs it taller → siblings overlap. Recursing here fixes overlap at every
  // nesting level (children form a tree, so no cycles).
  const eh = (k: SlideElement): number =>
    k.kind === "frame" ? layoutChildren(k, deckFont).h : elementHeight(k, deckFont);
  if (!lay || lay.flow === "none" || kids.length === 0) {
    // free layout: children keep relative x/y but text still hugs its height
    return { children: kids.map((k) => ({ ...k, h: eh(k) })), w: frame.w, h: frame.h };
  }
  const gap = lay.gap ?? 16;
  const padX = lay.padX ?? 16;
  const padY = lay.padY ?? 16;
  const cross = lay.flow === "col" ? Math.max(0, ...kids.map((k) => k.w)) : Math.max(0, ...kids.map(eh));
  const placed: SlideElement[] = [];
  let cursor = 0;
  for (const k of kids) {
    const kh = eh(k);
    if (lay.flow === "col") {
      const kx = lay.align === "center" ? (cross - k.w) / 2 : lay.align === "end" ? cross - k.w : 0;
      placed.push({ ...k, x: Math.round(padX + kx), y: Math.round(padY + cursor), h: kh });
      cursor += kh + gap;
    } else {
      const ky = lay.align === "center" ? (cross - kh) / 2 : lay.align === "end" ? cross - kh : 0;
      placed.push({ ...k, x: Math.round(padX + cursor), y: Math.round(padY + ky), h: kh });
      cursor += k.w + gap;
    }
  }
  const content = cursor - gap;
  const w = lay.flow === "col" ? cross + padX * 2 : content + padX * 2;
  const h = lay.flow === "col" ? content + padY * 2 : cross + padY * 2;
  return { children: placed, w: Math.round(w), h: Math.round(h) };
}

/** Expand all frames into a flat absolute element list for rendering/animation. */
export function flattenElements(els: SlideElement[], deckFont?: string): SlideElement[] {
  const out: SlideElement[] = [];
  for (const el of els) {
    if (el.kind === "frame") {
      const { children, w, h } = layoutChildren(el, deckFont);
      out.push({ ...el, kind: "box", w, h, children: undefined, layout: undefined });
      for (const c of children) {
        // A child (possibly itself a frame) flattens to a whole SUBTREE positioned
        // in the parent's local coords; translate every element of that subtree by
        // the frame's absolute origin. Taking only the first element would drop a
        // nested frame's grandchildren entirely.
        for (const f of flattenElements([c], deckFont)) {
          out.push({ ...f, x: el.x + f.x, y: el.y + f.y, z: (el.z ?? 0) + 1 + (f.z ?? 0) });
        }
      }
    } else if (hugs(el.kind)) {
      out.push({ ...el, h: elementHeight(el, deckFont) }); // hug wrapped content
    } else {
      out.push(el);
    }
  }
  return out;
}
