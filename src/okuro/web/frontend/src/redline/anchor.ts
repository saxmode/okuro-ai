/**
 * Anchor v2 in the browser — the DOM half of `okuro.sense.redline_anchor`.
 *
 * This file is a MIRROR, not a reimplementation. The server builds and
 * resolves the same JSON from the served bytes (`src/okuro/sense/
 * redline_anchor.py`), and `redline_list` reports `anchor_state` from ITS
 * verdict. If the two disagree the panel says one thing and the page shows
 * another, so every constant, every tier and the agreement rule below are
 * transcribed from that module rather than invented here:
 *
 *   - the 8-character text floor, applied at EVERY chain rung
 *   - `quote` emitted only when `chain[0].text` is non-null
 *   - the five tiers in the order attr, css, quote, attrquote, ancestor
 *   - `state = "exact"` iff two tiers each return exactly one element and it
 *     is the same element; otherwise `orphan`. There is no third state.
 *   - `box` is never a tier — it draws a ghost, it does not vote
 *
 * One deliberate transcription of a server-side DEVIATION: tier T5
 * (`ancestor`) also matches the picked element's own tag, and its text when
 * that text is non-null. Design v1.1 §5.1 describes T5 by its ancestor rungs
 * alone; taken literally the tier returns every sibling under the matched
 * parent and can almost never name one element. Wave 1 added the element's own
 * (tag, text) and recorded it as deviation D6. This file does the same thing,
 * because a DOM resolver that skipped it would disagree with the Python one on
 * exactly the elements the module exists for.
 */

export const ANCHOR_VERSION = 2;

/** Recorded on every rung, in this order, when present. */
export const STABLE_ATTRS = [
  "id",
  "data-redline-anchor",
  "data-testid",
  "name",
  "title",
  "for",
  "href",
  "aria-label",
] as const;

/** T1 uses only the four that assert identity rather than describe. */
export const IDENTITY_ATTRS = ["id", "data-redline-anchor", "data-testid", "name"] as const;

/** T4, in preference order. This is the tier that saves a text-free element. */
export const ATTRQUOTE_ATTRS = [
  "title",
  "aria-label",
  "for",
  "href",
  "data-testid",
  "name",
] as const;

export const TEXT_FLOOR = 8;
export const TEXT_CAP = 120;
export const ATTR_VALUE_CAP = 120;
export const MAX_ATTRS_PER_RUNG = 8;
export const OUTER_HTML_CAP = 2000;
export const QUOTE_CONTEXT = 32;
export const CHAIN_CAP = 24;

export interface AnchorRung {
  tag: string;
  id: string | null;
  classes: string[];
  nth: number;
  attrs: Record<string, string>;
  text: string | null;
}

export interface AnchorQuote {
  exact: string;
  prefix: string;
  suffix: string;
}

export interface AnchorAttrQuote {
  n: string;
  v: string;
}

export interface Anchor {
  v: number;
  captured_at: string;
  point: [number, number];
  hosts: string[];
  chain: AnchorRung[];
  quote: AnchorQuote | null;
  attrquote: AnchorAttrQuote | null;
  box: [number, number, number, number] | null;
  doc: [number, number] | null;
  src: { line: number; col: number } | null;
  outer_html: string;
}

export type TierName = "attr" | "css" | "quote" | "attrquote" | "ancestor";

export interface Resolution {
  state: "exact" | "orphan";
  agreeing_tiers: TierName[];
  tiers: Record<TierName, number | null>;
  identified_by: string;
  element: Element | null;
}

// ── text ─────────────────────────────────────────────────────────────

export function collapse(text: string): string {
  return text.split(/\s+/).filter(Boolean).join(" ");
}

/**
 * The text rule, applied identically at every rung.
 *
 * `null` below the floor so the quote tier is never emitted empty, and so a
 * two-character number like `95` cannot become a selector that matched three
 * elements unchanged and six after a mutation.
 */
export function fingerprint(text: string): string | null {
  const collapsed = collapse(text);
  if (collapsed.length < TEXT_FLOOR) return null;
  return collapsed.slice(0, TEXT_CAP);
}

// ── ignore trap ──────────────────────────────────────────────────────

/**
 * True when `el` is inside a `[data-redline-ignore]` subtree — INCLUDING one
 * that lives in a shadow root, which is where the overlay's own bubbles are.
 *
 * Direct descendant of the trap `review-surface.tsx` already solves for the
 * reviews widget: reaching for the control necessarily moves the pointer onto
 * it, and without this the overlay would anchor comments to itself.
 */
export function isIgnored(node: Node | null): boolean {
  let current: Node | null = node;
  while (current) {
    if (current instanceof Element && current.hasAttribute("data-redline-ignore")) {
      return true;
    }
    const parent: Node | null = (current as Element).parentElement ?? null;
    if (parent) {
      current = parent;
      continue;
    }
    const root = current.getRootNode();
    current = root instanceof ShadowRoot ? root.host : null;
  }
  return false;
}

// ── DOM helpers that mirror the parser's tree ────────────────────────

/** Every element from the document root down — mirrors `Document.elements()`. */
export function allElements(root: Document | ShadowRoot): Element[] {
  const top = root instanceof Document ? root.documentElement : null;
  const out: Element[] = top ? [top] : [];
  const scope: ParentNode = top ?? root;
  for (const el of Array.from(scope.querySelectorAll("*"))) out.push(el);
  return out;
}

/**
 * Elements that hold no content and can never be an anchor rung. A shadow root
 * commonly opens with one — the prism deck host appends its kit stylesheet
 * before the mount node — so `firstElementChild` there is a `<style>`.
 */
const NON_CONTENT = new Set(["STYLE", "LINK", "SCRIPT", "TEMPLATE", "META", "TITLE"]);

/**
 * Where the css walk starts — `Document.resolution_root()`'s counterpart.
 *
 * For a document that is the BODY. For a shadow root it is the first element
 * child that could actually contain content, which is NOT always the first
 * element child: measured against a live prism deck, the shadow root's
 * children are `[STYLE, DIV.deck-mount]`, and starting the walk at the STYLE
 * made the head rung mismatch, the css tier return nothing, and a perfectly
 * identified element resolve to an orphan on one surviving tier.
 *
 * The Python resolver has no counterpart to change: it parses HTML, and a
 * shadow root only ever exists in a browser.
 */
export function resolutionRoot(root: Document | ShadowRoot): Element | null {
  if (root instanceof Document) return root.body ?? root.documentElement ?? null;
  for (const child of Array.from(root.children)) {
    if (!NON_CONTENT.has(child.tagName)) return child;
  }
  return root.firstElementChild;
}

/** nth-of-type among siblings, 1-based — mirrors `_number_siblings`. */
export function nthOfType(el: Element): number {
  const parent = el.parentElement ?? (el.parentNode as ParentNode | null);
  if (!parent) return 1;
  let n = 0;
  for (const sibling of Array.from(parent.children)) {
    if (sibling.tagName === el.tagName) {
      n += 1;
      if (sibling === el) return n;
    }
  }
  return n || 1;
}

function ancestorsOf(el: Element): Element[] {
  const out: Element[] = [];
  let cursor = el.parentElement;
  while (cursor) {
    out.push(cursor);
    cursor = cursor.parentElement;
  }
  return out;
}

// ── building ─────────────────────────────────────────────────────────

function rungOf(el: Element): AnchorRung {
  const attrs: Record<string, string> = {};
  for (const name of STABLE_ATTRS) {
    if (el.hasAttribute(name) && Object.keys(attrs).length < MAX_ATTRS_PER_RUNG) {
      attrs[name] = (el.getAttribute(name) ?? "").slice(0, ATTR_VALUE_CAP);
    }
  }
  return {
    tag: el.tagName.toUpperCase(),
    id: el.getAttribute("id") || null,
    classes: (el.getAttribute("class") || "").split(/\s+/).filter(Boolean),
    nth: nthOfType(el),
    attrs,
    text: fingerprint(el.textContent ?? ""),
  };
}

/**
 * Collapsed text of the whole resolution root, plus the owning element of
 * every surviving character — the input both to the quote tier and to the
 * prefix/suffix context recorded at capture.
 *
 * Mirrors `_document_text`: one DFS in document order, whitespace runs folded
 * to a single space ACROSS text nodes, trailing spaces dropped. A subtree
 * marked `[data-redline-ignore]` is skipped, because the overlay's own chrome
 * must not enter the text the page is identified by.
 */
export function documentText(root: Document | ShadowRoot): {
  text: string;
  owners: Element[];
} {
  const chars: string[] = [];
  const owners: Element[] = [];
  let prevSpace = true;

  const scope: Node = root instanceof Document ? root.documentElement : root;
  const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT, {
    acceptNode(node: Node) {
      const owner = node.parentElement;
      if (!owner || isIgnored(owner)) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  let node = walker.nextNode();
  while (node) {
    const owner = node.parentElement;
    if (owner) {
      for (const ch of node.nodeValue ?? "") {
        if (/\s/.test(ch)) {
          if (prevSpace) continue;
          chars.push(" ");
          owners.push(owner);
          prevSpace = true;
        } else {
          chars.push(ch);
          owners.push(owner);
          prevSpace = false;
        }
      }
    }
    node = walker.nextNode();
  }
  while (chars.length && chars[chars.length - 1] === " ") {
    chars.pop();
    owners.pop();
  }
  return { text: chars.join(""), owners };
}

function quoteFor(
  root: Document | ShadowRoot,
  exact: string | null,
): AnchorQuote | null {
  if (!exact) return null;
  const { text } = documentText(root);
  const idx = text.indexOf(exact);
  if (idx < 0) return { exact, prefix: "", suffix: "" };
  return {
    exact,
    prefix: text.slice(Math.max(0, idx - QUOTE_CONTEXT), idx),
    suffix: text.slice(idx + exact.length, idx + exact.length + QUOTE_CONTEXT),
  };
}

function attrQuoteFor(el: Element): AnchorAttrQuote | null {
  for (const name of ATTRQUOTE_ATTRS) {
    const value = el.getAttribute(name);
    if (value) return { n: name, v: value.slice(0, ATTR_VALUE_CAP) };
  }
  return null;
}

export interface BuildOptions {
  /** Click position normalized inside the picked element's own box. */
  point?: [number, number];
  /** `[x, y, w, h]` normalized to the document. Advisory — never a tier. */
  box?: [number, number, number, number] | null;
  /** Document `[width, height]` at capture, to denormalize `box`. */
  docSize?: [number, number] | null;
  hosts?: string[];
  capturedAt?: string;
  root?: Document | ShadowRoot;
}

/**
 * Build anchor v2 for `el` from the live DOM.
 *
 * `src` is always `null` here: `{line, col}` is computed server-side from the
 * served bytes at capture (`redline_anchor.src_for_anchor`), because the DOM
 * has no memory of where a tag was written.
 */
export function buildAnchor(el: Element, opts: BuildOptions = {}): Anchor {
  const root = opts.root ?? document;
  const chain: AnchorRung[] = [];
  let cursor: Element | null = el;
  while (cursor && chain.length < CHAIN_CAP) {
    chain.push(rungOf(cursor));
    if (cursor.tagName === "BODY") break;
    cursor = cursor.parentElement;
  }
  const first = chain[0];
  return {
    v: ANCHOR_VERSION,
    captured_at: opts.capturedAt ?? new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    point: opts.point ?? [0.5, 0.5],
    hosts: opts.hosts ?? [],
    chain,
    quote: quoteFor(root, first ? first.text : null),
    attrquote: attrQuoteFor(el),
    box: opts.box ?? null,
    doc: opts.docSize ?? null,
    src: null,
    outer_html: el.outerHTML.slice(0, OUTER_HTML_CAP),
  };
}

// ── paths ────────────────────────────────────────────────────────────

/** T2's selector, for reading. Classes are excluded on purpose. */
export function cssPath(chain: AnchorRung[]): string {
  const rungs = [...chain].reverse();
  let start = 0;
  for (let i = rungs.length - 1; i >= 0; i--) {
    if (rungs[i]?.id) {
      start = i;
      break;
    }
  }
  const parts: string[] = [];
  for (let i = start; i < rungs.length; i++) {
    const rung = rungs[i];
    if (!rung) continue;
    if (i === start && rung.id) parts.push(`#${rung.id}`);
    else parts.push(`${rung.tag.toLowerCase()}:nth-of-type(${rung.nth ?? 1})`);
  }
  return parts.join(" > ");
}

/** `body › div.board › section.col › label.row › span.bub` — for reading. */
export function humanPath(chain: AnchorRung[]): string {
  const parts: string[] = [];
  for (const rung of [...chain].reverse()) {
    const tag = rung.tag.toLowerCase();
    if (rung.id) parts.push(`${tag}#${rung.id}`);
    else if (rung.classes.length) parts.push(`${tag}.${rung.classes[0]}`);
    else parts.push(tag);
  }
  return parts.join(" › ");
}

// ── the five tiers ───────────────────────────────────────────────────

function tierAttr(root: Document | ShadowRoot, anchor: Anchor): Element[] | null {
  const own = anchor.chain[0];
  if (!own) return null;
  const wanted = Object.entries(own.attrs ?? {}).filter(
    ([name, value]) => (IDENTITY_ATTRS as readonly string[]).includes(name) && value,
  );
  if (!wanted.length) return null;
  return allElements(root).filter((el) =>
    wanted.every(([name, value]) => el.getAttribute(name) === value),
  );
}

function tierCss(root: Document | ShadowRoot, anchor: Anchor): Element[] | null {
  const rungs = [...anchor.chain].reverse();
  let start = 0;
  for (let i = rungs.length - 1; i >= 0; i--) {
    if (rungs[i]?.id) {
      start = i;
      break;
    }
  }
  const head = rungs[start];
  if (!head) return [];

  let cursor: Element;
  if (head.id) {
    const found = allElements(root).filter((el) => el.getAttribute("id") === head.id);
    if (found.length !== 1 || !found[0]) return [];
    cursor = found[0];
  } else {
    const base = resolutionRoot(root);
    if (!base || base.tagName !== head.tag.toUpperCase()) return [];
    cursor = base;
  }

  for (let i = start + 1; i < rungs.length; i++) {
    const rung = rungs[i];
    if (!rung) return [];
    const nth = Number(rung.nth || 1);
    const siblings = Array.from(cursor.children).filter(
      (c) => c.tagName === rung.tag.toUpperCase(),
    );
    const next = siblings[nth - 1];
    if (siblings.length < nth || !next) return [];
    cursor = next;
  }
  return [cursor];
}

function lca(nodes: Element[]): Element | null {
  if (!nodes.length) return null;
  const chains = nodes.map((node) => {
    const chain = [node, ...ancestorsOf(node)];
    chain.reverse();
    return chain;
  });
  const shortest = Math.min(...chains.map((c) => c.length));
  let common: Element | null = null;
  for (let depth = 0; depth < shortest; depth++) {
    const first = chains[0]?.[depth];
    if (first && chains.every((c) => c[depth] === first)) common = first;
    else break;
  }
  return common;
}

function tierQuote(root: Document | ShadowRoot, anchor: Anchor): Element[] | null {
  const quote = anchor.quote;
  if (!quote || !quote.exact) return null;
  const { text, owners } = documentText(root);
  const exact = quote.exact;
  const prefix = quote.prefix ?? "";
  const suffix = quote.suffix ?? "";
  const hits: Element[] = [];
  let from = 0;
  for (;;) {
    const start = text.indexOf(exact, from);
    if (start < 0) break;
    from = start + 1;
    const end = start + exact.length;
    if (prefix && !text.slice(Math.max(0, start - prefix.length), start).endsWith(prefix)) {
      continue;
    }
    if (suffix && !text.slice(end, end + suffix.length).startsWith(suffix)) continue;
    const owner = lca(owners.slice(start, end).filter(Boolean));
    if (owner && !hits.includes(owner)) hits.push(owner);
  }
  return hits;
}

function tierAttrQuote(root: Document | ShadowRoot, anchor: Anchor): Element[] | null {
  const attrquote = anchor.attrquote;
  if (!attrquote || !attrquote.n) return null;
  return allElements(root).filter((el) => el.getAttribute(attrquote.n) === attrquote.v);
}

/**
 * T5 — the ancestor tier, including the picked element's own (tag, text).
 *
 * See the module docstring: the own-element match is Wave 1's deviation D6,
 * transcribed so the two resolvers cannot disagree.
 */
function tierAncestor(root: Document | ShadowRoot, anchor: Anchor): Element[] | null {
  const chain = anchor.chain;
  const upper = chain.slice(1);
  if (!upper.some((rung) => rung.text)) return null;
  const own = chain[0];
  if (!own) return null;

  const hits: Element[] = [];
  for (const el of allElements(root)) {
    if (el.tagName.toUpperCase() !== own.tag.toUpperCase()) continue;
    if (own.text !== null && fingerprint(el.textContent ?? "") !== own.text) continue;
    const ancestors = ancestorsOf(el);
    if (ancestors.length < upper.length) continue;
    let ok = true;
    for (let i = 0; i < upper.length; i++) {
      const rung = upper[i];
      const ancestor = ancestors[i];
      if (!rung || !ancestor) {
        ok = false;
        break;
      }
      if (ancestor.tagName.toUpperCase() !== rung.tag.toUpperCase()) {
        ok = false;
        break;
      }
      if (rung.text !== null && fingerprint(ancestor.textContent ?? "") !== rung.text) {
        ok = false;
        break;
      }
    }
    if (ok) hits.push(el);
  }
  return hits;
}

const TIERS: ReadonlyArray<
  [TierName, (root: Document | ShadowRoot, anchor: Anchor) => Element[] | null]
> = [
  ["attr", tierAttr],
  ["css", tierCss],
  ["quote", tierQuote],
  ["attrquote", tierAttrQuote],
  ["ancestor", tierAncestor],
];

function identifiedBy(agreeing: TierName[], anchor: Anchor): string {
  if (agreeing.includes("attr")) {
    const attrs = anchor.chain[0]?.attrs ?? {};
    for (const name of IDENTITY_ATTRS) {
      if (attrs[name]) return name;
    }
    return "id";
  }
  if (agreeing.includes("attrquote")) return anchor.attrquote?.n || "position";
  if (agreeing.includes("quote")) return "text";
  return "position";
}

/**
 * Place one anchor inside ONE version's DOM.
 *
 * `state` is `exact` if and only if at least two tiers each resolved to
 * exactly one element and it is the same element — otherwise `orphan`. Two
 * states, and there is no third for "it relocated": a nth-of-type path was
 * measured resolving to the row BELOW the anchored one after a single sibling
 * insert, and a confident wrong answer is worse than "gone".
 */
export function resolveAnchor(
  anchor: Anchor,
  root: Document | ShadowRoot = document,
): Resolution {
  const fired = new Map<TierName, Element[] | null>();
  for (const [name, fn] of TIERS) {
    try {
      fired.set(name, fn(root, anchor));
    } catch {
      fired.set(name, []); // a broken tier is a silent tier
    }
  }

  const singles: Array<[TierName, Element]> = [];
  for (const [name, hits] of fired) {
    if (hits && hits.length === 1 && hits[0]) singles.push([name, hits[0]]);
  }

  let winner: Element | null = null;
  let agreeing: TierName[] = [];
  for (const [, node] of singles) {
    const same = singles.filter(([, other]) => other === node).map(([n]) => n);
    if (same.length >= 2) {
      winner = node;
      agreeing = same;
      break;
    }
  }

  const tiers = {} as Record<TierName, number | null>;
  for (const [name, hits] of fired) tiers[name] = hits === null ? null : hits.length;

  return {
    state: winner ? "exact" : "orphan",
    agreeing_tiers: agreeing,
    tiers,
    identified_by: identifiedBy(agreeing, anchor),
    element: winner,
  };
}
