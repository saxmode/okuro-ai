/**
 * The redline overlay's MECHANISM, independent of where it runs.
 *
 * There are two places it runs, and they differ in exactly four ways:
 *
 * | | served document (`overlay.ts`) | prism deck (`deck-stage.tsx`) |
 * |---|---|---|
 * | resolution root | `document` | the deck host's open `shadowRoot` |
 * | how it answers | `postMessage` to the parent | a local callback |
 * | who owns the keyboard | the frame (a sandboxed iframe swallows keys) | the SPA page |
 * | how a candidate is found | hit-test the point | `composedPath()` of the event |
 *
 * Everything else — the shadow-rooted chrome, the numbered bubbles, the orphan
 * ghosts, the hover outline, the breadcrumb, the ancestor walk on wheel and
 * arrows, and committing at the WALKED level rather than at `event.target` —
 * is one implementation here. Two copies of that would drift, and a drift
 * between them shows up as the panel saying one thing while the page shows
 * another; the anchor module beneath this one is already shared with the
 * server's resolver for the same reason.
 *
 * The chrome always mounts into the top-level `document.documentElement`, even
 * when the resolution root is a shadow root. Bubbles are `position: fixed`
 * against viewport-relative rects, and a `fixed` element inside the deck's
 * transformed zoom stage would be positioned against that transform instead of
 * against the viewport.
 */

import {
  buildAnchor,
  humanPath,
  isIgnored,
  resolveAnchor,
  type Anchor,
} from "./anchor";
import type { FrameToParent, RenderComment } from "./protocol";

const HOST_STYLE = `
:host { all: initial; }
* { box-sizing: border-box; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
#root {
  position: fixed; inset: 0; pointer-events: none; z-index: 2147483647;
}
#capture { position: fixed; inset: 0; pointer-events: none; cursor: crosshair; }
#root.add.grab #capture { pointer-events: auto; }
#root.add #capture { cursor: crosshair; }
.bubble {
  position: fixed; width: 20px; height: 20px; margin: -10px 0 0 -10px;
  border-radius: 50%; pointer-events: auto; cursor: pointer;
  background: #0a0a0a; color: #f5f5f5; border: 1px solid #f5f5f5;
  font-size: 11px; line-height: 18px; text-align: center; font-weight: 600;
}
.bubble.done { background: #f5f5f5; color: #0a0a0a; border-color: #0a0a0a; opacity: 0.7; }
.bubble.active { outline: 2px solid #f5f5f5; outline-offset: 2px; }
.ghost {
  position: fixed; pointer-events: none;
  border: 1px dashed #8a8a8a; background: rgba(138, 138, 138, 0.12);
}
#outline {
  position: fixed; pointer-events: none; display: none;
  border: 1px solid #f5f5f5; box-shadow: 0 0 0 1px #0a0a0a;
}
#pulse {
  position: fixed; pointer-events: none; display: none;
  border: 2px solid #f5f5f5; box-shadow: 0 0 0 2px #0a0a0a;
}
#pulse.on { animation: redline-pulse 600ms ease-out 2; }
@keyframes redline-pulse {
  0% { opacity: 1; }
  100% { opacity: 0.15; }
}
#crumb {
  position: fixed; pointer-events: none; display: none; max-width: 60vw;
  padding: 3px 6px; background: #0a0a0a; color: #cfcfcf;
  border: 1px solid #5a5a5a; font-size: 11px; line-height: 15px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
#crumb b { color: #ffffff; font-weight: 700; }
`;

const INTERACTIVE = "a,button,input,select,textarea,label,summary,option,details";
const BLOCKISH = new Set([
  "block",
  "flex",
  "grid",
  "list-item",
  "table",
  "flow-root",
  "inline-block",
]);

export interface OverlayOptions {
  /**
   * Where anchors are built and resolved. `document` for a served page; the
   * deck host's `shadowRoot` for a prism deck, because no document-level
   * lookup pierces a shadow boundary.
   */
  root?: Document | ShadowRoot;
  /** The shadow-host chain recorded in every anchor this overlay builds. */
  hosts?: string[];
  /** How the overlay answers: postMessage in a frame, a callback in the SPA. */
  emit: (message: FrameToParent) => void;
  /**
   * Bind `keydown` on window. True in a sandboxed frame, which swallows every
   * key once it has focus; false in the SPA, where the page owns the keyboard
   * and drives `setAddMode` directly.
   */
  bindKeys?: boolean;
  /**
   * Suppress the page's own interactions with a pointer-events layer while
   * picking. True for a served document. False inside a shadow root, where the
   * candidate is read from the event's own composed path and a layer over the
   * page would make every event target the layer.
   */
  captureLayer?: boolean;
}

export interface OverlayHandle {
  render(comments: RenderComment[]): void;
  setAddMode(next: boolean): void;
  scrollToComment(commentId: string): void;
  reposition(): void;
  destroy(): void;
  readonly host: HTMLElement;
  readonly shadow: ShadowRoot;
  /** Test + debug seam. */
  walk(deeper: boolean): void;
  commit(event: MouseEvent): void;
  chainFrom(el: Element): Element[];
  defaultLevel(chain: Element[]): number;
  setHover(chain: Element[]): void;
  state(): {
    addMode: boolean;
    levelIndex: number;
    hoverChain: Element[];
    placed: Placed[];
  };
}

export interface Placed {
  comment: RenderComment;
  element: Element | null;
  node: HTMLElement;
  ghost: HTMLElement | null;
}

export function createOverlay(opts: OverlayOptions): OverlayHandle {
  const root: Document | ShadowRoot = opts.root ?? document;
  const hosts = opts.hosts ?? [];
  const bindKeys = opts.bindKeys ?? true;
  const useCaptureLayer = opts.captureLayer ?? true;
  const emit = opts.emit;

  // ── chrome ─────────────────────────────────────────────────────────

  const host = document.createElement("div");
  host.setAttribute("data-redline-ignore", "");
  const shadow = host.attachShadow({ mode: "open" });
  const style = document.createElement("style");
  style.textContent = HOST_STYLE;
  const chrome = document.createElement("div");
  chrome.id = "root";
  chrome.innerHTML =
    '<div id="capture"></div><div id="ghosts"></div><div id="bubbles"></div>' +
    '<div id="outline"></div><div id="pulse"></div><div id="crumb"></div>';
  shadow.append(style, chrome);
  if (useCaptureLayer) chrome.classList.add("grab");
  document.documentElement.appendChild(host);

  const layerGhosts = shadow.getElementById("ghosts") as HTMLElement;
  const layerBubbles = shadow.getElementById("bubbles") as HTMLElement;
  const elOutline = shadow.getElementById("outline") as HTMLElement;
  const elPulse = shadow.getElementById("pulse") as HTMLElement;
  const elCrumb = shadow.getElementById("crumb") as HTMLElement;

  // ── state ──────────────────────────────────────────────────────────

  let placed: Placed[] = [];
  let addMode = false;
  let hoverChain: Element[] = [];
  let levelIndex = 0;
  let cursorX = 0;
  let cursorY = 0;

  // ── geometry ───────────────────────────────────────────────────────

  function documentSize(): [number, number] {
    const de = document.documentElement;
    const body = document.body;
    return [
      Math.max(de.scrollWidth, body?.scrollWidth ?? 0, de.clientWidth),
      Math.max(de.scrollHeight, body?.scrollHeight ?? 0, de.clientHeight),
    ];
  }

  function place(node: HTMLElement, element: Element, point: [number, number]): void {
    const rect = element.getBoundingClientRect();
    node.style.left = `${rect.left + point[0] * rect.width}px`;
    node.style.top = `${rect.top + point[1] * rect.height}px`;
  }

  function placeGhost(node: HTMLElement, anchor: Anchor): void {
    const box = anchor.box;
    if (!box) {
      node.style.display = "none";
      return;
    }
    const [docW, docH] = anchor.doc ?? documentSize();
    node.style.display = "block";
    node.style.left = `${box[0] * docW - window.scrollX}px`;
    node.style.top = `${box[1] * docH - window.scrollY}px`;
    node.style.width = `${box[2] * docW}px`;
    node.style.height = `${box[3] * docH}px`;
  }

  let repositionQueued = false;
  function reposition(): void {
    if (repositionQueued) return;
    repositionQueued = true;
    requestAnimationFrame(() => {
      repositionQueued = false;
      for (const item of placed) {
        if (item.element)
          place(item.node, item.element, item.comment.anchor?.point ?? [0.5, 0.5]);
        else if (item.ghost && item.comment.anchor)
          placeGhost(item.ghost, item.comment.anchor);
      }
      if (addMode) drawOutline();
    });
  }

  // ── rendering bubbles ──────────────────────────────────────────────

  function render(comments: RenderComment[]): void {
    layerBubbles.textContent = "";
    layerGhosts.textContent = "";
    placed = [];

    for (const comment of comments) {
      const anchor = comment.anchor;
      const resolution = anchor ? resolveAnchor(anchor, root) : null;
      const element = resolution?.state === "exact" ? resolution.element : null;

      const node = document.createElement("div");
      node.className = `bubble ${comment.status}`;
      node.dataset.commentId = comment.commentId;
      node.textContent = String(comment.seq);
      node.title = `#${comment.seq} · ${comment.status}${element ? "" : " · orphan"}`;
      node.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        emit({ type: "redline:bubbleClick", commentId: comment.commentId });
      });

      let ghost: HTMLElement | null = null;
      if (element) {
        layerBubbles.appendChild(node);
        place(node, element, anchor?.point ?? [0.5, 0.5]);
      } else if (anchor) {
        // No bubble for an orphan — a dimmed ghost from the advisory box, which
        // may be in the wrong place. The panel item is the truth.
        ghost = document.createElement("div");
        ghost.className = "ghost";
        layerGhosts.appendChild(ghost);
        placeGhost(ghost, anchor);
      }
      placed.push({ comment, element, node, ghost });
    }
  }

  function scrollToComment(commentId: string): void {
    const item = placed.find((p) => p.comment.commentId === commentId);
    if (!item?.element) return;
    item.element.scrollIntoView({ block: "center", behavior: "smooth" });
    window.setTimeout(() => {
      if (!item.element) return;
      const rect = item.element.getBoundingClientRect();
      elPulse.style.display = "block";
      elPulse.style.left = `${rect.left}px`;
      elPulse.style.top = `${rect.top}px`;
      elPulse.style.width = `${rect.width}px`;
      elPulse.style.height = `${rect.height}px`;
      elPulse.classList.remove("on");
      void elPulse.offsetWidth; // restart the animation
      elPulse.classList.add("on");
      window.setTimeout(() => {
        elPulse.classList.remove("on");
        elPulse.style.display = "none";
      }, 1300);
      reposition();
    }, 350);
  }

  // ── add mode: the multi-level pick ─────────────────────────────────

  function ownsText(el: Element): boolean {
    for (const child of Array.from(el.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE && (child.nodeValue ?? "").trim())
        return true;
    }
    return false;
  }

  /**
   * The default candidate: the deepest element that owns its own text, is a
   * leaf, or is interactive — otherwise the nearest block ancestor. Never
   * `event.target` by reflex, which on this class of page is usually an inline
   * wrapper nobody meant to point at.
   */
  function defaultLevel(chain: Element[]): number {
    const first = chain[0];
    if (!first) return 0;
    if (ownsText(first) || first.children.length === 0 || first.matches(INTERACTIVE))
      return 0;
    for (let i = 0; i < chain.length; i++) {
      const el = chain[i];
      if (el && BLOCKISH.has(getComputedStyle(el).display)) return i;
    }
    return 0;
  }

  /**
   * The ancestor chain, ending at `BODY` in a document and at the shadow
   * root's first element child in a shadow tree — `parentElement` is null at
   * that boundary, so the loop stops there on its own.
   */
  function chainFrom(el: Element): Element[] {
    const chain: Element[] = [];
    let cursor: Element | null = el;
    while (cursor) {
      chain.push(cursor);
      if (cursor.tagName === "BODY") break;
      cursor = cursor.parentElement;
    }
    return chain;
  }

  /**
   * The candidate under an event.
   *
   * In a document, hit-test the point and skip our own chrome. In a shadow
   * tree, read the event's own composed path: `document.elementFromPoint`
   * returns the HOST for any point over the deck, and that is the measured
   * reason a document-level hit-test cannot pick an element inside one.
   */
  function candidateFor(event: PointerEvent | MouseEvent): Element | null {
    if (root instanceof ShadowRoot) {
      for (const node of event.composedPath()) {
        if (!(node instanceof Element)) continue;
        if (node === host || isIgnored(node)) continue;
        if (!root.contains(node)) continue;
        return node;
      }
      return null;
    }
    for (const el of document.elementsFromPoint(event.clientX, event.clientY)) {
      if (el === host) continue;
      if (isIgnored(el)) continue;
      return el;
    }
    return null;
  }

  function describe(el: Element): string {
    const tag = el.tagName.toLowerCase();
    const cls = (el.getAttribute("class") || "").split(/\s+/).filter(Boolean)[0];
    const id = el.getAttribute("id");
    if (id) return `${tag}#${id}`;
    return cls ? `${tag}.${cls}` : tag;
  }

  function drawOutline(): void {
    const el = hoverChain[levelIndex];
    if (!el) {
      elOutline.style.display = "none";
      elCrumb.style.display = "none";
      return;
    }
    const rect = el.getBoundingClientRect();
    elOutline.style.display = "block";
    elOutline.style.left = `${rect.left}px`;
    elOutline.style.top = `${rect.top}px`;
    elOutline.style.width = `${rect.width}px`;
    elOutline.style.height = `${rect.height}px`;

    const shown = hoverChain.slice(0, Math.max(levelIndex + 1, 4)).slice(0, 5);
    const parts = shown
      .map((node, index) =>
        index === levelIndex ? `<b>${describe(node)}</b>` : describe(node),
      )
      .reverse();
    elCrumb.innerHTML = parts.join(" &rsaquo; ");
    elCrumb.style.display = "block";
    elCrumb.style.left = `${Math.min(cursorX + 12, window.innerWidth - 260)}px`;
    elCrumb.style.top = `${Math.max(cursorY - 24, 4)}px`;
  }

  function onPointerMove(event: PointerEvent): void {
    if (!addMode) return;
    cursorX = event.clientX;
    cursorY = event.clientY;
    const target = candidateFor(event);
    if (!target) {
      hoverChain = [];
      drawOutline();
      return;
    }
    if (hoverChain[0] !== target) {
      hoverChain = chainFrom(target);
      levelIndex = defaultLevel(hoverChain);
    }
    drawOutline();
  }

  function walk(deeper: boolean): void {
    if (!hoverChain.length) return;
    levelIndex = deeper
      ? Math.max(0, levelIndex - 1)
      : Math.min(hoverChain.length - 1, levelIndex + 1);
    drawOutline();
  }

  function onWheel(event: WheelEvent): void {
    if (!addMode) return;
    event.preventDefault();
    if (event.deltaY === 0) return;
    walk(event.deltaY > 0);
  }

  function commit(event: MouseEvent): void {
    const element = hoverChain[levelIndex];
    if (!element) return;
    const rect = element.getBoundingClientRect();
    const [docW, docH] = documentSize();
    const point: [number, number] = [
      rect.width ? (event.clientX - rect.left) / rect.width : 0.5,
      rect.height ? (event.clientY - rect.top) / rect.height : 0.5,
    ];
    const anchor = buildAnchor(element, {
      point,
      box: [
        (rect.left + window.scrollX) / docW,
        (rect.top + window.scrollY) / docH,
        rect.width / docW,
        rect.height / docH,
      ],
      docSize: [docW, docH],
      root,
      hosts,
    });
    emit({ type: "redline:anchor", anchor });
    setAddMode(false);
    emit({ type: "redline:mode", add: false });
  }

  function onClick(event: MouseEvent): void {
    if (!addMode) return;
    // Modal: the page's own interactions are suppressed while picking.
    event.preventDefault();
    event.stopPropagation();
    // Inside a shadow root there is no pointer-events layer to intercept the
    // gesture, so the candidate is read from THIS event rather than from the
    // last pointermove — a click without a preceding move would otherwise
    // commit at whatever was hovered before.
    if (root instanceof ShadowRoot) {
      const target = candidateFor(event);
      if (target && hoverChain[0] !== target) {
        cursorX = event.clientX;
        cursorY = event.clientY;
        hoverChain = chainFrom(target);
        levelIndex = defaultLevel(hoverChain);
      }
    }
    commit(event);
  }

  function onKeyDown(event: KeyboardEvent): void {
    if (addMode && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();
      walk(event.key === "ArrowDown");
      return;
    }
    if (event.key === "Escape") {
      if (addMode) {
        setAddMode(false);
        emit({ type: "redline:mode", add: false });
        return;
      }
      emit({ type: "redline:key", key: "Escape" });
      return;
    }
    if (event.key === "c" || event.key === "C") {
      if (!addMode) {
        setAddMode(true);
        emit({ type: "redline:mode", add: true });
      }
      return;
    }
    if (event.key === "n" || event.key === "N") {
      emit({ type: "redline:key", key: "n" });
    }
  }

  function setAddMode(next: boolean): void {
    addMode = next;
    chrome.classList.toggle("add", next);
    if (!next) {
      hoverChain = [];
      elOutline.style.display = "none";
      elCrumb.style.display = "none";
    }
  }

  // `capture: true` on scroll so a nested scroller counts too: a scroll event
  // does not bubble, but the capture phase still passes through window.
  window.addEventListener("scroll", reposition, { passive: true, capture: true });
  window.addEventListener("resize", reposition, { passive: true });
  window.addEventListener("pointermove", onPointerMove, true);
  window.addEventListener("wheel", onWheel, { passive: false, capture: true });
  window.addEventListener("click", onClick, true);
  if (bindKeys) window.addEventListener("keydown", onKeyDown, true);

  function destroy(): void {
    window.removeEventListener("scroll", reposition, true);
    window.removeEventListener("resize", reposition);
    window.removeEventListener("pointermove", onPointerMove, true);
    window.removeEventListener("wheel", onWheel, true);
    window.removeEventListener("click", onClick, true);
    if (bindKeys) window.removeEventListener("keydown", onKeyDown, true);
    host.remove();
    placed = [];
  }

  // A webfont landing after first paint moves every rect on the page. Bubbles
  // are positioned from live rects, so re-place them once fonts settle.
  if (document.fonts?.ready) void document.fonts.ready.then(() => reposition());

  return {
    render,
    setAddMode,
    scrollToComment,
    reposition,
    destroy,
    host,
    shadow,
    walk,
    commit,
    chainFrom,
    defaultLevel,
    setHover: (chain: Element[]) => {
      hoverChain = chain;
      levelIndex = defaultLevel(chain);
    },
    state: () => ({ addMode, levelIndex, hoverChain, placed }),
  };
}

export { humanPath };
