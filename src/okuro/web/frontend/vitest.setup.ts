import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Vitest does not auto-cleanup React Testing Library renders the way
// Jest+RTL does, so we wire it manually. Without this every render
// stacks and queries find ghosts from prior tests.
afterEach(() => {
  cleanup();
});

// jsdom does not implement ResizeObserver. Components such as the
// connection-graph SVG inside PipelineView mount one to size their
// canvas. The C10 tests render PipelineView in jsdom — provide a
// no-op polyfill so the constructor doesn't throw.
if (typeof globalThis.ResizeObserver === "undefined") {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  (globalThis as unknown as { ResizeObserver: typeof ResizeObserverStub }).ResizeObserver =
    ResizeObserverStub;
}

// jsdom implements no scrolling at all, so Element.prototype.scrollIntoView
// does not exist. Two shipped components call it in an effect on mount: the
// nav bar pulls an opened accordion group to the track's left edge, and cmdk
// keeps the selected command-palette item in view. Both throw in jsdom, which
// made rendering either of them in a test impossible for a reason that has
// nothing to do with what was being tested. Same class of gap as the two
// observers above, same remedy.
if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoViewStub() {};
}

// jsdom does not implement IntersectionObserver either. The codex page's
// section nav constructs one as soon as the resolved model puts its sections in
// the DOM — which, before the fix of 2026-09-11, it never got far enough to do.
// Without this stub the constructor throws inside an effect, React retries the
// commit, and every effect in that tree runs twice: the sheet-swap gate saw two
// announcements for one swap and read as a product defect.
if (typeof globalThis.IntersectionObserver === "undefined") {
  class IntersectionObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() { return []; }
    readonly root = null;
    readonly rootMargin = "";
    readonly thresholds: number[] = [];
  }
  (globalThis as unknown as { IntersectionObserver: typeof IntersectionObserverStub })
    .IntersectionObserver = IntersectionObserverStub;
}
