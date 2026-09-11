/**
 * Page context + UI-action execution for the pulse chat.
 *
 * The chat is a page-aware copilot: it snapshots the current route + the
 * section headings visible in the right-hand content, hands them to the
 * agent, and executes the actions the agent emits (scroll / highlight) so
 * okuro can drive the UI for the user ("take me to the Design section").
 *
 * Generic by design — sections are auto-derived from ``#main-content``
 * headings, so every page works without per-page wiring. Page-specific
 * capabilities (e.g. flow draw) layer on top later.
 */
const MAIN = "#main-content";

export interface PageContext {
  path: string;
  title: string;
  sections: string[];
  capabilities: Array<{ name: string; describe: string }>;
}

export interface ChatAction {
  name: string;
  target: string;
}

// --- Page-action registry ------------------------------------------------
// Pages register extra capabilities the chat can invoke beyond the generic
// scroll/highlight — e.g. okuro·flow registers `draw` to render a diagram on
// the open canvas. The handler runs async and may stream activity lines back
// into the chat. Registration is keyed by name; a page unregisters on unmount.
export type PageActionHandler = (
  arg: string,
  onActivity?: (msg: string) => void,
  params?: Record<string, unknown>,
) => Promise<boolean> | boolean;

// --- Dynamic option forms ------------------------------------------------
// A function/action can declare params; when the chat agent emits a ⟦FORM⟧
// marker the chat renders standardized widgets from this spec (segmented
// control / toggle / picker), collects the values, then dispatches the action
// with them. Fed dynamically by each tool — the source of truth for its UI.
export type ParamKind = "segmented" | "toggle" | "select" | "text" | "number" | "person" | "brand";

export interface ActionParam {
  key: string;
  label: string;
  kind: ParamKind;
  choices?: { value: string; label: string }[];
  default?: string | number | boolean;
  placeholder?: string;
  required?: boolean;
}

export interface ActionSpec {
  /** freeform context (e.g. the topic) binds to this text param key. */
  contextKey?: string;
  title?: string;
  submitLabel?: string;
  params: ActionParam[];
}

interface RegisteredAction {
  describe: string;
  handler: PageActionHandler;
  spec?: ActionSpec;
}

const REGISTRY = new Map<string, RegisteredAction>();

export function registerPageAction(
  name: string,
  describe: string,
  handler: PageActionHandler,
  spec?: ActionSpec,
): () => void {
  REGISTRY.set(name, { describe, handler, spec });
  return () => {
    if (REGISTRY.get(name)?.handler === handler) REGISTRY.delete(name);
  };
}

export function getActionSpec(name: string): ActionSpec | undefined {
  return REGISTRY.get(name)?.spec;
}

function registeredCapabilities(): Array<{ name: string; describe: string }> {
  return [...REGISTRY.entries()].map(([name, { describe }]) => ({ name, describe }));
}

function registeredHandler(name: string): PageActionHandler | undefined {
  return REGISTRY.get(name)?.handler;
}

// Navigable targets: section headings AND tab/section controls. Settings,
// for example, exposes its sections as `role=tab` buttons (Identity, Design,
// Keyring…), not headings — those are activated by a click, not a scroll.
const TARGET_SEL = "h1, h2, h3, [role=tab]";

function targetEls(): HTMLElement[] {
  const root = document.querySelector(MAIN);
  if (!root) return [];
  return Array.from(root.querySelectorAll<HTMLElement>(TARGET_SEL)).filter(
    (h) => (h.innerText || "").trim().length > 0,
  );
}

/** Snapshot the current page for the agent: route, title, section labels. */
export function snapshotPageContext(): PageContext {
  const path = window.location.pathname;
  const h1 = document.querySelector<HTMLElement>(`${MAIN} h1`);
  const title = (h1?.innerText || document.title || path).trim();
  const seen = new Set<string>();
  const sections: string[] = [];
  for (const h of targetEls()) {
    const t = h.innerText.trim();
    if (t && !seen.has(t.toLowerCase())) {
      seen.add(t.toLowerCase());
      sections.push(t);
    }
  }
  return { path, title, sections, capabilities: registeredCapabilities() };
}

const norm = (s: string) => s.trim().toLowerCase().replace(/\s+/g, " ");

/** Find the target element best matching a section label. */
function resolveTarget(target: string): HTMLElement | null {
  const want = norm(target);
  const els = targetEls();
  return (
    els.find((h) => norm(h.innerText) === want) ??
    els.find((h) => norm(h.innerText).includes(want)) ??
    els.find((h) => want.includes(norm(h.innerText))) ??
    null
  );
}

// A tab/button is *activated* to reveal its section; a heading is scrolled
// into view.
function isActivatable(el: HTMLElement): boolean {
  return el.getAttribute("role") === "tab" || el.tagName === "BUTTON";
}

// Radix Tabs (automatic mode) activate on FOCUS, not on a synthetic
// ``.click()``; plain buttons need a real pointer sequence. Do both so one
// activator covers tabs, accordions, and ordinary buttons.
function activate(el: HTMLElement) {
  el.focus();
  const seq: Array<[string, typeof PointerEvent | typeof MouseEvent]> = [
    ["pointerdown", typeof PointerEvent !== "undefined" ? PointerEvent : MouseEvent],
    ["mousedown", MouseEvent],
    ["pointerup", typeof PointerEvent !== "undefined" ? PointerEvent : MouseEvent],
    ["mouseup", MouseEvent],
    ["click", MouseEvent],
  ];
  for (const [type, Ev] of seq) {
    el.dispatchEvent(new Ev(type, { bubbles: true, cancelable: true, button: 0 }));
  }
}

function flashHighlight(el: HTMLElement) {
  // Highlight the heading's enclosing card/section if there is one, else the
  // heading itself. Self-contained inline styling so no global CSS is needed.
  const target =
    el.closest<HTMLElement>("section, article, [data-card], .card") ?? el;
  const prevTransition = target.style.transition;
  const prevShadow = target.style.boxShadow;
  const prevRadius = target.style.borderRadius;
  target.style.transition = "box-shadow 240ms ease";
  target.style.borderRadius = target.style.borderRadius || "8px";
  target.style.boxShadow = "0 0 0 2px var(--color-accent, #fff)";
  window.setTimeout(() => {
    target.style.boxShadow = prevShadow;
    window.setTimeout(() => {
      target.style.transition = prevTransition;
      target.style.borderRadius = prevRadius;
    }, 260);
  }, 1600);
}

/** Execute a chat-emitted action: built-in scroll/highlight, or a
 *  page-registered capability (e.g. flow `draw`). Async — registered
 *  handlers may stream activity back via `onActivity`. */
export async function dispatchChatAction(
  action: ChatAction,
  onActivity?: (msg: string) => void,
  params?: Record<string, unknown>,
): Promise<boolean> {
  if (action.name === "scrollTo" || action.name === "highlight") {
    return executeChatAction(action);
  }
  const handler = registeredHandler(action.name);
  if (!handler) return false;
  return await handler(action.target, onActivity, params);
}

/** Execute a built-in DOM action (scroll / highlight) against the page. */
export function executeChatAction(action: ChatAction): boolean {
  const el = resolveTarget(action.target);
  if (!el) return false;
  if (action.name !== "scrollTo" && action.name !== "highlight") return false;

  // Tab/button sections are revealed by activating them; headings by scroll.
  if (isActivatable(el)) {
    activate(el);
    el.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } else {
    el.scrollIntoView({
      behavior: "smooth",
      block: action.name === "highlight" ? "center" : "start",
    });
  }
  if (action.name === "highlight") flashHighlight(el);
  return true;
}
