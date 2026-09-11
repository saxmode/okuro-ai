/**
 * Review surfaces — declaring WHAT the user is reviewing.
 *
 * IDENTITY vs CONTEXT is the whole design. A `surfaceId` is a DECLARED name
 * ("task.flow-feedback-card"), never something derived from where the user
 * happened to be. Every derivable candidate rots: this app has already renamed
 * /tasks -> /work, /roles -> /agents, /dashboard -> /agents, /system ->
 * /health, and a component path or CSS selector dies on the next refactor. The
 * route travels along as evidence so a review can be replayed — it is never
 * the thing being identified.
 *
 * UNIVERSAL ON DAY ONE. A route-level default surface is registered
 * automatically, so every page is reviewable with zero per-page work.
 * Components opt into finer granularity only where they want it, by rendering
 * a <ReviewSurface> or calling useReviewTarget().
 *
 * The registry is a module-level Map rather than React state on purpose: the
 * floating button reads it at click time, and re-rendering the whole app every
 * time a component mounts a surface would be pure churn.
 */

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  type ReactNode,
} from "react";
import type { ReviewSurfaceEntry, ReviewTargetType } from "@/types/api";

export interface ReviewTarget {
  surfaceId: string;
  label?: string;
  targetType?: ReviewTargetType;
  targetId?: string;
}

/** Everything this build can declare — POSTed once on boot to tombstone the rest. */
const catalogue = new Map<string, ReviewSurfaceEntry>();

export function declareSurface(entry: ReviewSurfaceEntry): void {
  const prev = catalogue.get(entry.surface_id);
  catalogue.set(entry.surface_id, {
    surface_id: entry.surface_id,
    label: entry.label ?? prev?.label ?? null,
    route: entry.route ?? prev?.route ?? null,
  });
}

export function surfaceCatalogue(): ReviewSurfaceEntry[] {
  return Array.from(catalogue.values());
}

/**
 * The innermost declared surface. `null` means "no component declared one", and
 * the caller falls back to the route-level default — which is why every page is
 * reviewable without any component doing anything.
 */
const ReviewTargetContext = createContext<ReviewTarget | null>(null);

export function useReviewTarget(): ReviewTarget | null {
  return useContext(ReviewTargetContext);
}

/**
 * Declare a surface for everything rendered inside.
 *
 * Nesting is intentional and the inner one wins: a page declares a coarse
 * surface, a card inside it declares a finer one, and a review lands on
 * whichever the user was actually pointing at.
 */
export function ReviewSurface({
  surfaceId,
  label,
  targetType,
  targetId,
  children,
}: ReviewTarget & { children: ReactNode }) {
  useEffect(() => {
    declareSurface({ surface_id: surfaceId, label: label ?? null });
  }, [surfaceId, label]);

  const value = useMemo(
    () => ({ surfaceId, label, targetType, targetId }),
    [surfaceId, label, targetType, targetId],
  );

  // The DOM attributes are what make this work at all. The feedback button is
  // mounted in AppShell — ABOVE every page — and React context only flows
  // downward, so a shell-level button can never read a page-level provider.
  // Stamping the identity onto the DOM lets the button resolve it by walking
  // up from whatever the user is pointing at, which is also the more honest
  // answer to "what did he mean": the thing under the cursor.
  //
  // `display: contents` so this wrapper generates no box and cannot disturb
  // the surrounding grid/flex layout.
  return (
    <ReviewTargetContext.Provider value={value}>
      <div
        style={{ display: "contents" }}
        data-review-surface={surfaceId}
        data-review-label={label ?? undefined}
        data-review-target-type={targetType ?? undefined}
        data-review-target-id={targetId ?? undefined}
      >
        {children}
      </div>
    </ReviewTargetContext.Provider>
  );
}


/**
 * The surface the pointer was last over — how the shell-level button learns
 * what the user is looking at.
 *
 * A module-level ref, not React state: this updates on every pointer move over
 * a declared region, and re-rendering the app for that would be pure churn.
 */
let lastPointed: Element | null = null;

export function trackPointer(): () => void {
  const onOver = (e: Event) => {
    const el = e.target as Element | null;
    if (!el || el.nodeType !== 1) return;
    // Ignore the feedback widget itself. Reaching for the button necessarily
    // moves the pointer onto it, and without this the widget would erase the
    // very context the user is about to complain about — every review would
    // land on the route default.
    if (el.closest?.("[data-review-ignore]")) return;
    lastPointed = el;
  };
  document.addEventListener("pointerover", onOver, true);
  return () => document.removeEventListener("pointerover", onOver, true);
}

/** Identity + entity for whatever the pointer last touched, if declared. */
export function pointedTarget(): ReviewTarget | null {
  let node: Element | null = lastPointed;
  while (node) {
    const sid = node.getAttribute?.("data-review-surface");
    if (sid) {
      return {
        surfaceId: sid,
        label: node.getAttribute("data-review-label") ?? undefined,
        targetType:
          (node.getAttribute("data-review-target-type") as ReviewTargetType) ??
          undefined,
        targetId: node.getAttribute("data-review-target-id") ?? undefined,
      };
    }
    node = node.parentElement;
  }
  return null;
}

/** Test seam — clears the pointer memory between cases. */
export function __resetPointer(): void {
  lastPointed = null;
}

/**
 * A stable-ish description of the clicked element, for Option D.
 *
 * Recorded as a HINT only. It is deliberately NOT identity: nth-child indices
 * and class names die on the next markup change, so identity always resolves to
 * the nearest DECLARED ancestor. This exists so a reviewer can say "that
 * button" and the reader can later see roughly which one.
 */
export function domHint(el: Element | null, maxDepth = 4): string {
  if (!el) return "";
  const parts: string[] = [];
  let node: Element | null = el;
  for (let i = 0; i < maxDepth && node && node.tagName !== "BODY"; i++) {
    let part = node.tagName.toLowerCase();
    if (node.id) {
      parts.unshift(`${part}#${node.id}`);
      break; // an id is as specific as this gets
    }
    const cls = (node.getAttribute("class") || "")
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .join(".");
    if (cls) part += `.${cls}`;
    parts.unshift(part);
    node = node.parentElement;
  }
  return parts.join(" > ").slice(0, 300);
}

/** Nearest ancestor carrying a declared surface, for the overlay's click. */
export function surfaceFromElement(el: Element | null): string | null {
  let node: Element | null = el;
  while (node) {
    const sid = node.getAttribute?.("data-review-surface");
    if (sid) return sid;
    node = node.parentElement;
  }
  return null;
}
