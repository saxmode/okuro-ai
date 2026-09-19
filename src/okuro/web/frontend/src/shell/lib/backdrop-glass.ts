// SPDX-License-Identifier: Apache-2.0
/**
 * REAL BLUR ON AN ENGINE THAT WILL NOT PAINT `backdrop-filter`.
 *
 * ===========================================================================
 * WHY THIS EXISTS, AND WHY NOTHING CHEAPER WOULD DO
 * ===========================================================================
 * The desktop app is pywebview on WebKitGTK. Measured 2026-09-17 in a WebKit2
 * 4.1 window against the built bundle:
 *
 *     CSS.supports('backdrop-filter','blur(10px)')   ->  true
 *     getComputedStyle(plate).backdropFilter         ->  "blur(16px)"
 *     what it paints                                 ->  nothing
 *
 * An independent verification pass enumerated 489 WebKitGTK feature flags and
 * found no BackdropFilter switch to turn on, refuted the GPU as a cause
 * (llvmpipe software GL reads identically to the NVIDIA path) and exonerated
 * pywebview (MiniBrowser with zero pywebview reproduces it exactly). 2.54.0,
 * released 2026-09-16, does not mention it. So the property is not coming.
 *
 * `filter: blur()` DOES paint there. The difference is what each one reads:
 * `backdrop-filter` blurs what is BEHIND an element, `filter` blurs the
 * element's OWN content. So the only way to get glass is to put a copy of what
 * is behind the plate INSIDE the plate and blur that. There is no third option:
 * a page cannot rasterise itself, `element()` is Firefox-only, and no
 * combination of masks reaches a neighbour's pixels.
 *
 * ===========================================================================
 * ONE CLONE, NOT ONE PER FRAME — WHERE THE COST ACTUALLY GOES
 * ===========================================================================
 * The naive version re-clones on scroll and dies. The three costs are separated:
 *
 *   SCROLLING   a `transform` on the existing clone. No clone, no layout.
 *   CONTENT     re-clone, but only when a MutationObserver says the source
 *               changed, and at most once per animation frame.
 *   RESIZE      reposition only, same as scrolling.
 *
 * NODE CAP. A pane carrying a graph or a long editor is not worth mirroring —
 * the clone would cost more than the decoration is worth, and at `blur(16px)`
 * nobody can read it anyway. Past the cap the glass removes itself and the
 * stylesheet's opaque veil takes over, which is the same thing the app did
 * before this file existed. Degrading is a state, not a failure.
 *
 * ===========================================================================
 * THE CLONE IS INERT, AND THAT IS THREE SEPARATE PRECAUTIONS
 * ===========================================================================
 *   `aria-hidden` + `inert`  it is not a second copy of the page for a screen
 *                            reader or for the tab order.
 *   ids stripped             a duplicated `id` breaks `getElementById`,
 *                            `aria-labelledby` and every `:target` on the page.
 *   `pointer-events:none`    set in the stylesheet on the host.
 *
 * ONLY WHERE IT IS NEEDED. This installs itself exclusively when `index.html`
 * has marked the engine `data-backdrop="none"`. In Chromium nothing here runs
 * and the real `backdrop-filter` does the work.
 */

/** Plates that want glass, in the order they are looked up. */
const PLATES = [".top-plate", ".genact"] as const;

/**
 * The element whose pixels the plates are blurring. It is the PANE HOLDER
 * rather than the scroller: `.content` also contains `.c-band`, and mirroring
 * the band into the plate would blur the title into itself.
 */
const SOURCE = ".bar[data-active] .c-panes";

/**
 * ===========================================================================
 * THE GUARD IS A TIME BUDGET, BECAUSE A NODE COUNT WAS A GUESS AND IT MISFIRED
 * ===========================================================================
 * This started as `MAX_NODES = 4000`, a number I invented and then wrote up as
 * if it had been measured. It cost a whole round: `/work/agents` is 4458 nodes
 * — an ordinary page, not a graph — so the mirror tore itself down and the app
 * showed no blur again, with the CSP failure below hiding it until that was
 * fixed.
 *
 * So the governor is what the guard was always trying to approximate: how long
 * the clone actually takes on THIS page. One frame at 60fps is 16.7ms; 8ms
 * leaves room for everything else the frame has to do. Two consecutive overruns
 * retire the mirror — one is a hiccup, two is the page.
 *
 * `HARD_NODES` stays only as a cheap pre-check so a genuinely enormous subtree
 * is never cloned even once to find out. It is deliberately far above anything
 * the shell's own pages produce.
 */
const CLONE_BUDGET_MS = 8;
const OVERRUNS_ALLOWED = 2;
const HARD_NODES = 40000;

interface Glass {
  plate: HTMLElement;
  host: HTMLElement;
  inner: HTMLElement;
}

function countNodes(el: Element): number {
  // `querySelectorAll('*')` is one flat walk in the engine rather than a
  // recursive one in JS, and it is only ever run on a re-clone.
  return el.querySelectorAll("*").length;
}

function ensureGlass(plate: HTMLElement): Glass {
  let host = plate.querySelector<HTMLElement>(":scope > .sh-glass");
  if (!host) {
    host = document.createElement("div");
    host.className = "sh-glass";
    host.setAttribute("aria-hidden", "true");
    const inner = document.createElement("div");
    inner.className = "sh-glass-inner";
    host.appendChild(inner);
    // FIRST CHILD, so it cannot come out above the plate's own content in
    // document order even if something later removes its z-index.
    plate.insertBefore(host, plate.firstChild);
  }
  return { plate, host, inner: host.firstElementChild as HTMLElement };
}

function stripIdentity(root: HTMLElement): void {
  root.removeAttribute("id");
  for (const el of root.querySelectorAll("[id]")) el.removeAttribute("id");
  // A cloned `<canvas>` is blank — its bitmap does not travel with the node.
  // Left in place deliberately: it keeps the LAYOUT honest, and a blank box
  // under 16px of blur reads as the flat ground it sits on anyway.
}

export function installBackdropGlass(): () => void {
  const html = document.documentElement;
  if (html.getAttribute("data-backdrop") !== "none") return () => {};

  let frame = 0;
  let cloneDirty = true;
  let overruns = 0;
  let retired = false;
  let observer: MutationObserver | null = null;
  let observed: Element | null = null;
  let disposed = false;

  const glasses = new Map<string, Glass>();

  function source(): HTMLElement | null {
    return document.querySelector<HTMLElement>(SOURCE);
  }

  function teardown() {
    for (const g of glasses.values()) g.host.remove();
    glasses.clear();
    html.removeAttribute("data-glass");
  }

  function paint() {
    frame = 0;
    if (disposed) return;
    const src = source();
    if (!src) return;

    if (retired) return;

    if (cloneDirty && countNodes(src) > HARD_NODES) {
      retire();
      return;
    }

    const srcRect = src.getBoundingClientRect();
    let spent = 0;

    for (const sel of PLATES) {
      const plate = document.querySelector<HTMLElement>(sel);
      if (!plate) {
        const stale = glasses.get(sel);
        if (stale) {
          stale.host.remove();
          glasses.delete(sel);
        }
        continue;
      }
      const g = glasses.get(sel) ?? ensureGlass(plate);
      glasses.set(sel, g);

      if (cloneDirty) {
        const t0 = performance.now();
        const copy = src.cloneNode(true) as HTMLElement;
        stripIdentity(copy);
        copy.setAttribute("inert", "");
        g.inner.replaceChildren(copy);
        spent = Math.max(spent, performance.now() - t0);
      }

      // The mirror has to sit where the original sits, in the PLATE's own
      // coordinates. Width is pinned from the source because the clone's
      // ancestors are the plate's, not the pane's, so it cannot derive its
      // own width from a `.content` it is no longer inside.
      const p = plate.getBoundingClientRect();
      g.inner.style.width = `${srcRect.width}px`;
      g.inner.style.height = `${srcRect.height}px`;
      g.inner.style.transform = `translate(${srcRect.left - p.left}px, ${srcRect.top - p.top}px)`;
    }

    if (cloneDirty) {
      overruns = spent > CLONE_BUDGET_MS ? overruns + 1 : 0;
      if (overruns >= OVERRUNS_ALLOWED) {
        retire();
        return;
      }
    }

    cloneDirty = false;
    html.setAttribute("data-glass", glasses.size ? "on" : "off");
  }

  /**
   * Give up on the mirror for the rest of the session and let the stylesheet's
   * opaque veil take over — the state the app had before this file existed.
   * Degrading is a state, not a failure, and it is one-way on purpose: a mirror
   * that flickers in and out as the page changes is worse than one that is
   * simply not there.
   */
  function retire() {
    retired = true;
    cloneDirty = false;
    teardown();
  }

  function schedule(dirty: boolean) {
    if (dirty) cloneDirty = true;
    if (frame) return;
    frame = requestAnimationFrame(paint);
  }

  const onScroll = () => schedule(false);
  const onResize = () => schedule(false);

  function rewire() {
    const src = source();
    if (src === observed) return;
    observer?.disconnect();
    observed = src;
    if (!src) {
      teardown();
      return;
    }
    observer = new MutationObserver(() => schedule(true));
    observer.observe(src, { childList: true, subtree: true, characterData: true, attributes: true });
    schedule(true);
  }

  // SCROLL IS LISTENED FOR ON THE WAY DOWN, not on one element: R2 made
  // `.content` the scroller and there are five of them, one per topic, with the
  // active one changing under us. A capturing listener on the document hears
  // whichever one actually scrolled without holding a reference to any.
  document.addEventListener("scroll", onScroll, { capture: true, passive: true });
  window.addEventListener("resize", onResize, { passive: true });

  // The active bar changes with navigation, and with it the source element.
  const barObserver = new MutationObserver(() => {
    rewire();
    schedule(false);
  });
  barObserver.observe(document.body, { subtree: true, attributes: true, attributeFilter: ["data-active", "data-topic"] });

  rewire();

  return () => {
    disposed = true;
    if (frame) cancelAnimationFrame(frame);
    observer?.disconnect();
    barObserver.disconnect();
    document.removeEventListener("scroll", onScroll, { capture: true } as EventListenerOptions);
    window.removeEventListener("resize", onResize);
    teardown();
  };
}
