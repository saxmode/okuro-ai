// <!-- AGENT_HEADER
// role: code
// purpose: useIncremental — render a large list in pages, growing on scroll.
// index: useIncremental
// AGENT_HEADER_END -->
import { useEffect, useState } from "react";
import { useInView } from "./use-in-view";

/**
 * Incremental ("windowed-lite") rendering for large lists.
 *
 * Knowledge can return 800+ nodes; mounting every card/row at once costs
 * seconds of layout on the first paint of the tab. Instead we render the
 * first `page` items and grow by `page` each time a bottom sentinel scrolls
 * into view — capping mounted DOM to roughly one viewport-plus regardless of
 * dataset size. Dependency-free (reuses the IntersectionObserver hook).
 *
 * Returns the current visible `count` and a callback `sentinelRef` to attach
 * to a bottom marker element. Render `items.slice(0, count)` and only render
 * the sentinel while `count < total`.
 *
 * `total` changing (a new filter/search result) resets back to one page so
 * the user always starts at the top of the new set.
 */
export function useIncremental(
  total: number,
  page = 60,
  root: Element | null = null,
): { count: number; sentinelRef: (node: HTMLElement | null) => void } {
  const [count, setCount] = useState(page);
  const [sentinelRef, inView] = useInView<HTMLElement>({ rootMargin: "400px", root });

  // New dataset (filter/search changed) → restart at the first page.
  useEffect(() => {
    setCount(page);
  }, [total, page]);

  // Sentinel visible and more remain → reveal the next page. Keeps firing
  // until the sentinel is pushed out of the prefetch margin or we hit total.
  useEffect(() => {
    if (inView && count < total) {
      setCount((c) => Math.min(total, c + page));
    }
  }, [inView, count, total, page]);

  return { count: Math.min(count, total), sentinelRef };
}
