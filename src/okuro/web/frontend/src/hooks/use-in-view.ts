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
 * @param root  the scrolling ancestor to observe against. MUST be the inner
 *   `overflow:auto` element when that (not the viewport) is the scroller.
 */
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
      { root, rootMargin },
    );
    io.observe(node);
    return () => io.disconnect();
  }, [node, once, rootMargin, root]);

  return [setRef, inView];
}
