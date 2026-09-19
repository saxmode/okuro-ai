// <!-- AGENT_HEADER
// role: code
// purpose: useInView — IntersectionObserver hook for lazy rendering + infinite scroll.
// index: useInView
// AGENT_HEADER_END -->
import { useCallback, useEffect, useState } from "react";

/**
 * Observe an element's intersection with a scroll root.
 *
 * Returns a CALLBACK ref (not a RefObject) on purpose: the observed node often
 * mounts conditionally/after data loads (e.g. an infinite-scroll sentinel). A
 * useRef wouldn't re-run the effect when the node attaches, so the observer
 * would never be created. A callback ref drives the node into state, so the
 * observer is (re)created whenever the node OR the root changes.
 *
 * @param once  stop observing after the first time it becomes visible (lazy mount).
 * @param rootMargin  prefetch margin so content mounts slightly before it scrolls in.
 * @param root  the scrolling ancestor to observe against. Pass the inner
 *   `overflow:auto` element when that is the scroller; A ROOT THAT DOES NOT
 *   SCROLL IS IGNORED — see `usableRoot`.
 */
/**
 * A ROOT THAT DOES NOT SCROLL IS NOT A ROOT, and this is the whole defect.
 *
 * ===========================================================================
 * WHAT IT COST, MEASURED
 * ===========================================================================
 * `/deliver/assets` fetched its ENTIRE icon library on mount — 17 `browse`
 * pages and 1,562 tiles in the first seconds, climbing toward all 35,582 —
 * because its infinite-scroll sentinel was observed against the leaf's own
 * `flex-1 overflow-y-auto` box, and under the shell that box does not scroll.
 * Measured in Chromium: `scrollHeight === clientHeight === 109423`. An
 * IntersectionObserver rooted at a box that never clips sees EVERYTHING inside
 * it as intersecting, so the sentinel was permanently in view and each answer
 * triggered the next request.
 *
 * ===========================================================================
 * WHY THE FLEXBOX FIX IS NOT THE FIX — measured, not argued
 * ===========================================================================
 * The obvious reading is a missing `min-h-0` on the flex scroll container.
 * Measured live on that page, the same frame, four variants:
 *
 *   as shipped                       clientHeight 109423 · scrollHeight 109423
 *   min-h-0 on scroller + row + pane clientHeight 109423 · scrollHeight 109423
 *   pane  height:600px               clientHeight    600 · scrollHeight 109423
 *   row   height:600px               clientHeight    600 · scrollHeight 109423
 *
 * `min-h-0` changes NOTHING, because `min-height:auto` is not the constraint:
 * no ancestor has a DEFINITE height, so `h-full` and `flex-1` resolve against
 * an indefinite one and the box simply grows to its content. That is not a bug
 * in the leaf — it is the shell's architecture. R2 made `.content` the
 * scroller and `.c-panes` `flex:1 0 auto` with `overflow-y:visible`, so a pane
 * is CONTENT-SIZED on purpose and every page scrolls as a whole.
 *
 * ===========================================================================
 * SO THE FIX IS HERE, AND IT IS THE CLASS
 * ===========================================================================
 * Every caller that hands this hook its own inner box has the same latent
 * defect the moment that box stops being the scroller — which is what moving
 * into the shell did to all of them at once. Fixing it per leaf would mean
 * teaching each one which element scrolls, which is exactly the knowledge a
 * leaf should not have.
 *
 * `root: null` is the VIEWPORT, and it is correct in both architectures: an
 * IntersectionObserver against the viewport already accounts for clipping by
 * every ancestor, so an inner scroller still hides what it has scrolled away.
 * The supplied root only tunes where `rootMargin` is measured from. Dropping
 * one that cannot clip therefore loses precision, never correctness.
 */
function usableRoot(root: Element | null): Element | null {
  if (!root) return null;
  return root.scrollHeight > root.clientHeight + 1 ? root : null;
}

export function useInView<T extends HTMLElement>(
  { once = false, rootMargin = "200px", root = null }:
    { once?: boolean; rootMargin?: string; root?: Element | null } = {},
): [(node: T | null) => void, boolean] {
  const [node, setNode] = useState<T | null>(null);
  const [inView, setInView] = useState(false);
  const setRef = useCallback((n: T | null) => setNode(n), []);

  useEffect(() => {
    if (!node) return;
    if (typeof IntersectionObserver === "undefined") {
      setInView(true); // SSR / unsupported → render eagerly
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        const entry = entries[0];
        if (!entry) return;
        if (entry.isIntersecting) {
          setInView(true);
          if (once) io.disconnect();
        } else if (!once) {
          setInView(false);
        }
      },
      { root: usableRoot(root), rootMargin },
    );
    io.observe(node);
    return () => io.disconnect();
  }, [node, once, rootMargin, root]);

  return [setRef, inView];
}
