/**
 * The canvas the system grows on.
 *
 * WHY AN IFRAME, and it is not squeamishness about style leaks. The emitted
 * sheet is a DOCUMENT: it sets `html { font-size: 50% }`, it paints `:root`,
 * and every size it writes is a rem against that root. A shadow root would not
 * get its own document root, so rem would resolve against okuro's own html and
 * every number in the preview would be wrong while looking plausible. A real
 * frame gives the sheet a real document, unmodified — which is also the only
 * way the preview is EVIDENCE rather than an illustration.
 *
 * WHY THE FRAME CARRIES NO SCRIPT, and this is the load-bearing constraint on
 * this file. okuro serves
 *
 *     script-src 'self' 'wasm-unsafe-eval' 'unsafe-eval'
 *
 * with no `'unsafe-inline'`, and a `srcdoc` document INHERITS the embedding
 * document's policy. So an inline `<script>` in the frame is blocked by the
 * browser on the deployed app — silently, as a console CSP violation and
 * nothing else. Every line the script would have run is therefore run HERE, in
 * the parent, against `frame.contentDocument`: the frame is same-origin, so the
 * parent can build its DOM, listen to its events and observe its box directly.
 * No script, no postMessage, no ready handshake, nothing to drop.
 *
 * WHAT MOVES AND WHAT DOES NOT. The tree's DOM changes only when the BUILDER
 * changes the tree. A colour edit changes the SHEET and nothing else — the
 * elements keep their attributes, `@container style(--ground)` re-answers, and
 * the growth stylesheet's transitions carry every value to its new one in
 * place. That is the requirement ("every edit re-deriving visibly in place")
 * falling out of the emitter's own mechanism rather than being animated by
 * hand.
 *
 * THE MARKUP CARRIES NO COLOURS. Every node is class names and request
 * attributes; the sheet paints all of it. Same discipline as the P2/P3
 * screenshot fixtures, and for the same reason: a preview that hard-coded a
 * value could show a colour the engine never produced.
 *
 * NEVER USE `instanceof` OR THE PARENT'S GLOBAL `document` ON A NODE FROM THE
 * FRAME. Frame nodes are instances of the FRAME window's constructors, so every
 * `instanceof` against them is false and every parent-realm lookup misses. This
 * file only ever reaches into the frame through the document it was handed.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { INTERACTION_MS } from "./chrome";
import type { ResolvedModel, TreeNode } from "./types";

export interface FrameMessage {
  type: "reveal" | "place" | "drop" | "flag";
  slot?: string;
  ground?: string;
  node?: string;
  request?: string;
  /** The flag code a mark was clicked for. See `markFlags`. */
  flag?: string;
  /** Where in the PARENT's viewport the gesture landed, so the inspector can
      open beside it. The frame's own coordinates are useless to a panel that
      lives outside the frame. */
  y?: number;
}

/**
 * The frame's whole document. Two empty style tags and an empty tree — no
 * script, by the CSP rule at the top of this file, and no markup either: what
 * the canvas holds is built by the parent so that the same code path serves the
 * first paint and every later edit.
 */
export const FRAME_SKELETON = String.raw`<!doctype html>
<html data-appearance="light">
<head>
<meta charset="utf-8">
<!-- okuro's OWN application stylesheets, cloned in by adoptAppStyles(). They
     come FIRST so the engine's sheet below overrides them: the app's globals
     declare the same consumer names the adapter emits, and the engine has to be
     the one that wins. Empty until the clone runs. -->
<style id="app-anchor"></style>
<!-- the engine's own sheet, swapped whole on every edit -->
<style id="engine"></style>
<!-- the PAGE's choreography: transitions and stage gating. Not derivation. -->
<style id="growth"></style>
</head>
<body>
<div id="tree"></div>
</body>
</html>`;

const STEPS = [100, 97, 95, 90, 80, 70, 60, 50, 40, 30, 20, 10, 5, 3, 1, 0.1, 0];

/**
 * The seven background styles the emitter produces.
 *
 * They are still all demonstrated — but as the FILLS OF REAL COMPONENTS rather
 * than as seven buttons labelled with their own CSS class names. The board was
 * measured at 45 % empty at 1440 and 77 % empty at 2560, under a caption
 * claiming "a real document wearing the emitted sheet". The caption was right
 * about the ambition and wrong about the artefact.
 */
export const STYLES = [
  "no-background",
  "solid",
  "solid-inverse",
  "solid-brand",
  "solid-brand-inverse",
  "transparent",
  "transparent-inverse",
];

const stepClass = (p: number) => String(p).replace(".", "_");

const esc = (s: unknown) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/**
 * A ROOT PLACEMENT, in the REQUEST vocabulary the sheet reads.
 *
 * The engine names the branded root `brand` (`_roots`, api.py) because that is
 * the ground's id; a REQUEST for that ground is `branded`, because that is the
 * attribute `emit` writes selectors against. Two names for one thing, and the
 * root effect below compared the ground id against the request name — so
 * `data-branded` was never set, `:root[data-branded]` never matched, and the
 * brand-full chip did nothing at all while every signal chip worked. The
 * signals only escaped because `signal:error` is spelled the same on both
 * sides of the seam.
 *
 * It is a FUNCTION rather than two corrected literals so that there is one
 * place where the ground vocabulary becomes the request vocabulary. A second
 * corrected literal would have been the same bug waiting for the next root.
 */
export function requestOfRoot(placement: string | null): string | null {
  if (!placement) return null;
  return placement === "brand" ? "branded" : placement;
}

/** A request becomes the attribute the SHEET reads. "no request" is deliberately
    no attribute at all — inheritance is the absence of a rule. */
function requestAttr(request: string | null): string {
  if (!request) return "";
  if (request === "emphasis") return ' data-emphasis=""';
  if (request === "branded") return ' data-branded=""';
  if (request.startsWith("signal:")) return ` data-signal="${esc(request.slice(7))}"`;
  return "";
}

/** Which palette slots an element PAINTS. A flag names the same slots in its
    own renders list, so marking the failing component is a set intersection
    rather than a guess — "here is where it's failing", from the engine's map. */
function renderKeys(style: string): string {
  const key = style.replace(/-/g, "_");
  const keys = [key, "shadow", "blur_and_shadow"];
  if (key.startsWith("solid_brand")) keys.push("border_branded");
  return keys.join(" ");
}

/* -------------------------------------------------------- the seven blocks */

/** The demo's own navigation. It is intentionally quieter than the story. */
function headerBlock(): string {
  const nav = ["System", "Components", "Logic"]
    .map(
      (item) =>
        `<a class="nav ds-n6" data-slot="foreground" data-render="foreground">${item}</a>`,
    )
    .join("");
  return (
    `<header class="doc-head ds-bordered" data-render="border_half">` +
    `<span class="mark ds-h5 ds-titles" data-slot="foreground" data-render="foreground">OKURO / ENGINE</span>` +
    `<nav class="nav-row">${nav}</nav>` +
    `<button type="button" class="btn ds-solid ds-bordered ds-radius-m ds-container-size ds-padding ds-n6 ds-leads"` +
    ` data-slot="solid" data-render="${renderKeys("solid")}">Open system</button>` +
    `<span class="avatar ds-highlight-branded ds-radius-max" data-slot="highlight_branded_background"` +
    ` data-render="highlight_branded_background">01</span>` +
    `</header>`
  );
}

/** A full editorial opening: premise on the left, live system proof on the right. */
function heroBlock(model: FrameModel): string {
  return (
    `<section class="hero" data-render="ground">` +
    `<div class="hero-copy">` +
    `<span class="eyebrow ds-n7 ds-leads" data-slot="foreground" data-render="foreground">A design engine, not a theme</span>` +
    `<h1 class="ds-h1 ds-headings" data-slot="foreground" data-render="foreground">` +
    `One authored voice.<br>Every surface follows.</h1>` +
    `<p class="lead ds-n4 ds-leads" data-slot="foreground" data-render="foreground">` +
    `Author the identity once. Grounds, contrast, ladders, type, spacing, radius, states and components resolve as one living system.</p>` +
    `<div class="btn-row">` +
    `<button type="button" class="btn ds-solid-brand ds-bordered ds-radius-m ds-container-size ds-padding ds-n6 ds-leads" data-slot="solid_brand" data-render="${renderKeys("solid-brand")}">Explore the system</button>` +
    `<button type="button" class="btn ds-transparent ds-bordered ds-radius-m ds-container-size ds-padding ds-n6 ds-leads" data-slot="transparent" data-render="${renderKeys("transparent")}">See the logic</button>` +
    `</div></div>` +
    `<div class="hero-proof ds-transparent ds-bordered ds-radius-l" data-slot="transparent" data-render="transparent shadow blur_and_shadow">` +
    `<div class="proof-head"><span class="ds-n7 ds-leads" data-slot="foreground" data-render="foreground">LIVE SYSTEM / ${esc(model.canonical)}</span><span class="proof-status ds-n8">resolved</span></div>` +
    `<div class="logic-field" data-slot="alternate" data-render="alternate">${ladderStrip("alt")}</div>` +
    `<div class="proof-equation">` +
    `<div><span class="proof-value ds-h3 ds-titles">8</span><span class="ds-n8">BASE</span></div>` +
    `<div><span class="proof-value ds-h3 ds-titles">${model.threshold}</span><span class="ds-n8">POLARITY</span></div>` +
    `<div><span class="proof-value ds-h3 ds-titles">${model.rootPercent}%</span><span class="ds-n8">ROOT</span></div>` +
    `</div>` +
    `</div>` +
    `</section>`
  );
}

/** Three system principles in one ruled composition, not three detached cards. */
function cardsBlock(): string {
  const cards: [string, string, string, string][] = [
    [
      "Ground decides",
      "Every surface publishes one resolved background. The same rule works at the root, inside a card, or ten levels deep.",
      "ds-surface",
      "no_background",
    ],
    [
      "Ink generates",
      "The chosen foreground becomes two seventeen-step ladders. Borders, separators and subtle fills inherit the same answer.",
      "ds-transparent",
      "transparent",
    ],
    [
      "Components consume",
      "Components never invent a local colour decision. They read the resolved vocabulary and remain coherent on every ground.",
      "ds-transparent-inverse",
      "transparent_inverse",
    ],
  ];
  return (
    `<section class="principles">` +
    `<div class="section-intro"><span class="eyebrow ds-n7 ds-leads">THE OPERATING MODEL</span>` +
    `<h2 class="ds-h2 ds-headings" data-slot="foreground" data-render="foreground">A chain you can read from left to right.</h2></div>` +
    `<div class="cards">` +
    cards
      .map(
        ([title, body, cls, render]) =>
          `<article class="card ${cls} ds-bordered ds-padding"` +
          ` data-slot="${render}" data-render="${render} shadow blur_and_shadow">` +
          `<h3 class="ds-h5 ds-headings" data-slot="foreground" data-render="foreground">${title}</h3>` +
          `<p class="ds-n5 ds-paragraphs" data-slot="foreground" data-render="foreground">${body}</p>` +
          `<div class="sepline ds-separator" data-slot="separator" data-render="separator"></div>` +
          `<a class="ds-n7" data-slot="foreground" data-render="foreground">Inspect the consequence &rarr;</a>` +
          `</article>`,
      )
      .join("") +
    `</div></section>`
  );
}

/** Authored inputs and their visible consequence share one editorial section. */
function formBlock(canonical: string): string {
  return (
    `<section class="form-story">` +
    `<div class="section-intro"><span class="eyebrow ds-n7 ds-leads">AUTHOR ONCE</span>` +
    `<h2 class="ds-h2 ds-headings" data-slot="foreground" data-render="foreground">Small input. System-wide consequence.</h2>` +
    `<p class="ds-n5 ds-paragraphs" data-slot="foreground" data-render="foreground">The brand owns identity. The engine owns repetition, contrast and consistency.</p></div>` +
    `<div class="form ds-surface ds-bordered ds-radius-l ds-padding-double" data-slot="no_background" data-render="no_background shadow blur_and_shadow">` +
    `<div class="form-title"><span class="ds-n8 ds-leads">AUTHORED INPUT</span><span class="ds-n8">4 decisions shown</span></div>` +
    `<label class="field">` +
    `<span class="ds-n7" data-slot="foreground" data-render="foreground">Name</span>` +
    `<span class="input ds-bordered ds-radius-s ds-container-size ds-padding ds-n6"` +
    ` data-slot="border_full" data-render="border_full">meridian</span>` +
    `</label>` +
    `<label class="field">` +
    `<span class="ds-n7" data-slot="foreground" data-render="foreground">Brand colour</span>` +
    `<span class="input focused ds-bordered-branded ds-radius-s ds-container-size ds-padding ds-n6"` +
    ` data-slot="focus" data-render="focus border_branded" data-reads="canonical">${esc(
      canonical,
    )}</span>` +
    `</label>` +
    `<label class="field">` +
    `<span class="ds-n7" data-slot="foreground" data-render="foreground">Contrast target</span>` +
    `<span class="input errored ds-signal-error ds-radius-s ds-container-size ds-padding ds-n6"` +
    ` data-slot="signal_error" data-render="signal_error">2.1 : 1</span>` +
    `<span class="ds-n8" data-slot="signal_error" data-render="signal_error">Body text needs 4.5 : 1.</span>` +
    `</label>` +
    `<div class="switch-row">` +
    `<span class="track ds-highlight-branded ds-radius-max" data-slot="highlight_branded_background"` +
    ` data-render="highlight_branded_background"><i class="knob ds-solid-inverse ds-radius-max"` +
    ` data-slot="solid_inverse" data-render="${renderKeys("solid-inverse")}"></i></span>` +
    `<span class="ds-n6" data-slot="foreground" data-render="foreground">Two independent colours</span>` +
    `</div>` +
    `<button type="button" class="btn ds-solid-brand ds-bordered ds-radius-m ds-container-size ds-padding ds-n6 ds-leads" data-slot="solid_brand" data-render="${renderKeys("solid-brand")}">Apply identity</button>` +
    `</div></section>`
  );
}

/** The complete topology: authored input to product output, with no hidden leap. */
function tableBlock(model: FrameModel): string {
  const rows: [string, string, string, string][] = [
    ["Authored", "Identity", "brand + signals · black/white · font faces + assignments · borders · radius · effects · motion · default rungs", "builder"],
    ["Calculated", "Ground model", `background lightness compared with ${model.threshold} · foreground · hover direction · brand eligibility`, "engine"],
    ["Generated", "Semantic vocabulary", "17-step alternate + inverse ladders · fills · borders · focus · states · shadows", "engine"],
    ["Inherited", "Frame context", "resolved ground · size rung · optional downscale · nested brand scope", "document"],
    ["Consumed", "Components", "semantic roles only; behavior remains local to each primitive", "product"],
  ];
  return (
    `<section class="table-block"><div class="section-intro">` +
    `<span class="eyebrow ds-n7 ds-leads">SYSTEM TOPOLOGY</span>` +
    `<h2 class="ds-h2 ds-headings" data-slot="foreground" data-render="foreground">Every layer, its authority, and its output.</h2>` +
    `<p class="ds-n5 ds-paragraphs" data-slot="foreground" data-render="foreground">Only the first row is authored. Every later row is an answer, a context, or a consumer.</p></div>` +
    `<div class="atlas-table-wrap"><table class="atlas-table"><thead><tr><th>Layer</th><th>Subject</th><th>Contract</th><th>Authority</th></tr></thead><tbody>` +
    rows
      .map(
        ([layer, subject, contract, authority]) =>
          `<tr><th>${layer}</th><td>${subject}</td><td>${contract}</td><td><span class="badge ds-radius-max ds-transparent ds-n8" data-slot="transparent" data-render="transparent">${authority}</span></td></tr>`,
      )
      .join("") +
    `</tbody></table></div>` +
    `</section>`
  );
}

/** The inheritance decision table: the same rule for every colour and depth. */
function decisionBlock(model: FrameModel): string {
  const rows: [string, string, string][] = [
    ["No request", "inherit parent ground", "Inheritance is the absence of a new rule."],
    ["Emphasis", "neutral opposite polarity", `Parent lightness is read against ${model.threshold}.`],
    ["Branded on neutral", "brand-full or fallback", "Brand-full only when the brand sits on the opposite threshold side."],
    ["Branded on chromatic", "neutral fallback + adapted CTA", "A chromatic host never receives a second full chromatic ground."],
    ["Signal", "signal colour", "The signal becomes a ground and uses the same polarity law."],
  ];
  return (
    `<section class="decision-block"><div class="section-intro">` +
    `<span class="eyebrow ds-n7 ds-leads">INHERITANCE DECISION</span>` +
    `<h2 class="ds-h2 ds-headings" data-slot="foreground" data-render="foreground">No kind branches. No depth counter.</h2>` +
    `<p class="ds-n5 ds-paragraphs" data-slot="foreground" data-render="foreground">One resolved background descends. A child either inherits it or makes one explicit request; the resulting ground becomes the next parent.</p></div>` +
    `<div class="atlas-table-wrap"><table class="atlas-table"><thead><tr><th>Child request</th><th>Outcome</th><th>Why</th></tr></thead><tbody>` +
    rows.map(([request, outcome, why]) => `<tr><th>${request}</th><td>${outcome}</td><td>${why}</td></tr>`).join("") +
    `</tbody></table></div></section>`
  );
}

/** Four signals form a full-width colour proof, never a detached swatch table. */
function alertsBlock(): string {
  const alerts: [string, string, string][] = [
    ["error", "Contrast", "Text on your warning colour reads under 4.5 : 1."],
    ["success", "Grown", "Ten stages fired in the engine's own order."],
    ["info", "Guests", "Two guest brands resolved against this host ground."],
    ["warning", "Ground", "This lightness sits inside the warn band."],
  ];
  return (
    `<section class="signal-story"><div class="section-intro">` +
    `<span class="eyebrow ds-n7 ds-leads">SIGNALS ARE GROUNDS</span>` +
    `<h2 class="ds-h2 ds-headings" data-slot="foreground" data-render="foreground">The same law holds when meaning gets loud.</h2></div>` +
    `<div class="alerts">` +
    alerts
      .map(
        ([role, title, body]) =>
          `<div class="alert ds-signal-${role} ds-radius-m ds-padding"` +
          ` data-slot="signal_${role}" data-render="signal_${role}">` +
          `<span class="signal-mark ds-h4 ds-titles">${role.slice(0, 1).toUpperCase()}</span>` +
          `<span class="ds-n6 ds-headings">${title}</span>` +
          `<span class="ds-n7">${body}</span>` +
          `</div>`,
      )
      .join("") +
    `</div></section>`
  );
}

/** Typography and engineering constants close as one proof, not two tables. */
function footerBlock(model: FrameModel): string {
  const col = (head: string, items: string[]) =>
    `<div class="fcol">` +
    `<span class="ds-n7 ds-headings" data-slot="foreground" data-render="foreground">${head}</span>` +
    items
      .map(
        (item) =>
          `<span class="ds-n8" data-slot="foreground" data-render="foreground">${item}</span>`,
      )
      .join("") +
    `</div>`;
  return (
    `<footer class="doc-foot ds-surface" data-emphasis="" data-slot="ground" data-render="ground">` +
    `<div class="type-stage">` +
    `<span class="eyebrow ds-n7 ds-leads">TYPE IS PART OF THE SYSTEM</span>` +
    `<p class="type-display ds-h1 ds-titles" data-slot="foreground" data-render="foreground">One rung.<br>Twenty-one roles.<br>Every viewport.</p>` +
    `<p class="ds-n4 ds-paragraphs" data-slot="foreground" data-render="foreground">A frame inherits its rung. Downscale moves text one step and components three, both floor-clamped. The mother tables remain locked; a brand authors only its desktop and mobile defaults.</p>` +
    `</div><div class="atlas-table-wrap"><table class="atlas-table type-contract"><thead><tr><th>System</th><th>Range</th><th>Rule</th><th>Authored choice</th></tr></thead><tbody>` +
    `<tr><th>Typography</th><td>${model.textRungs} rungs × ${model.textRoles} roles</td><td>base × authored role factor</td><td>${esc(model.desktopRung)} desktop · ${esc(model.mobileRung)} mobile</td></tr>` +
    `<tr><th>Components</th><td>${model.componentRungs} rungs × ${model.componentRoles} roles</td><td>base × rung factor × role ratio</td><td>inherits frame rung</td></tr>` +
    `<tr><th>Root</th><td>${model.rootPercent}% = ${model.rootPx}px</td><td>emitted rem equals factor</td><td>system constant</td></tr>` +
    `<tr><th>Typeface</th><td>${esc(model.fontFamily)}</td><td>four measured faces assigned to four bands</td><td>titles · headings · leads · paragraphs</td></tr>` +
    `</tbody></table></div><div class="fcols">` +
    col("Constants", [`Base ${model.base}`, `Root ${model.rootPercent} %`, `Polarity ${model.threshold}`, "WCAG is a flag, not a branch"]) +
    col("Derived", ["34 ladder values", `${model.textRungs} text rungs`, `${model.componentRungs} component rungs`, "Every state emitted"]) +
    col("Guarantee", ["One source of truth", "No local colour branches", "Nested without a depth limit"]) +
    `</div>` +
    `</footer>`
  );
}

/** The two 17-step ladders, as the strip they are. */
function ladderStrip(name: string): string {
  return STEPS.map(
    (step) =>
      /* NO `title`. A native tooltip carrying a raw slot name and a percentage
         is the affordance §7 deleted; the step is readable from the strip and
         the rule behind it is one click away in the inspector. `aria-label`
         gives a screen reader the same fact without a hover surface. */
      `<i class="ds-${name}-${stepClass(step)}" aria-label="${name} ${step}%" data-slot="${
        name === "alt" ? "alternate" : "alternate_inverse"
      }" data-render="${name === "alt" ? "alternate" : "alternate_inverse"}"></i>`,
  ).join("");
}

/**
 * The builder's own controls, on the node.
 *
 * HOVER-REVEALED, 24px, at the node's top-right — not seven always-on 11px
 * chips across the top of the document. `remove` is NOT RENDERED on the root
 * rather than rendered permanently disabled: a control that can never fire is
 * a control that teaches a reader to distrust the others.
 */
function placeBar(id: string): string {
  const acts: [string | null, string][] = [
    ["emphasis", "emphasis"],
    ["branded", "brand"],
    ["signal:error", "error"],
    ["signal:success", "success"],
    ["signal:info", "info"],
    ["signal:warning", "warning"],
    [null, "inherit"],
  ];
  const buttons = acts
    .map(
      ([request, label]) =>
        `<button type="button" class="place" data-place="${request ?? ""}"` +
        ` data-for="${esc(id)}">+ ${label}</button>`,
    )
    .join("");
  return id === "root"
    ? buttons
    : buttons + `<button type="button" class="place drop" data-drop="${esc(id)}">remove</button>`;
}

/** What the frame needs to know about a ground, and nothing more. */
/**
 * WHAT THE FRAME PUTS INSIDE ITS NODES.
 *
 * `document` is the seven hand-built blocks — the evidence that the SHEET alone
 * paints a page, with no React and no app CSS in the frame. `showcase` is the
 * empty node plus the mount, so the whole occupant is the portalled component
 * set — the evidence that okuro's OWN components wear the kit. Two claims, two
 * artefacts, one node: the ground, the pill, the flags and the place bar are
 * identical in both, because being in a resolved ground is the part that makes
 * either claim mean anything.
 */
export type FrameVariant = "document" | "showcase";

interface FrameGround {
  origin?: string;
  lightness?: number | null;
  polarity?: string;
  flags?: {
    code: string;
    message: string;
    renders: readonly string[];
    severity?: string;
  }[];
}

interface FrameModel {
  grounds: Record<string, FrameGround>;
  /**
   * The brand's canonical, as TEXT for the one field that prints a hex.
   *
   * The mock form has a field labelled "Brand colour" and it shipped with
   * `#f03541` written into it as a literal. On a page whose entire subject is
   * the brand colour, that is the preview stating the old colour back to a
   * reader who just changed it — "brand stays initial", in the most literal
   * way available. It is the only string in this frame that names a colour, and
   * it is a READOUT rather than paint: the sheet cannot reach text content, so
   * this one value has to travel.
   */
  canonical: string;
  threshold: number;
  rootPercent: number;
  rootPx: number;
  base: number;
  textRungs: number;
  textRoles: number;
  componentRungs: number;
  componentRoles: number;
  desktopRung: string;
  mobileRung: string;
  fontFamily: string;
  brandFlags: {
    code: string;
    message: string;
    renders: readonly string[];
    severity?: string;
    where?: string;
  }[];
}

/**
 * One node: the ground, and a real page wearing it.
 *
 * The document is a `max-width: 1100px` column centred in the node, and the
 * surround is painted with the resolved ground — which is CONTENT on this page,
 * not void: the whole subject is what a ground does to everything on it.
 */
function nodeHTML(node: TreeNode, model: FrameModel, variant: FrameVariant = "document"): string {
  const g = model.grounds[node.groundKey] ?? {};
  const kids = node.children.map((child) => nodeHTML(child, model, variant)).join("");

  /* THE SHOWCASE VARIANT IS THE SAME NODE WITH A DIFFERENT OCCUPANT.
     Everything that makes a node a node stays — the ground, the pill, the flag
     chip, the place bar, the reveal — because those are what put the specimens
     INSIDE a resolved ground rather than beside one. What is dropped is the
     seven-block demo document, which is the DOCUMENT scene's whole subject and
     would bury the component set under it. One `[data-okuro-mount]`, and the
     parent portals 33 live components into it. */
  if (variant === "showcase") {
    return (
      `<div class="node ds-surface" data-node="${esc(node.id)}"` +
      ` data-render="ground"${requestAttr(node.request)}>` +
      `<div class="doc">` +
      `<div class="head">` +
      `<span class="pill ds-n7" data-slot="ground" data-render="ground">` +
      `${esc(g.origin || node.request || "root")} &middot; L ${
        g.lightness != null ? g.lightness.toFixed(3) : "?"
      } &middot; ${esc(g.polarity || "")}` +
      `</span>` +
      `<span class="flagchip ds-n7" data-slot="flags"></span>` +
      `<span class="acts">${placeBar(node.id)}</span>` +
      `</div>` +
      `<div class="reveal" data-reveal="components">` +
      (node.id === "root" ? `<div data-okuro-mount></div>` : "") +
      `</div>` +
      (kids ? `<div class="reveal kids" data-reveal="descent">${kids}</div>` : "") +
      `</div>` +
      `</div>`
    );
  }

  return (
    `<div class="node ds-surface" data-node="${esc(node.id)}"` +
    ` data-render="ground"${requestAttr(node.request)}>` +
    `<div class="doc">` +
    `<div class="head">` +
    `<span class="pill ds-n7" data-slot="ground" data-render="ground">` +
    `${esc(g.origin || node.request || "root")} &middot; L ${
      g.lightness != null ? g.lightness.toFixed(3) : "?"
    } &middot; ${esc(g.polarity || "")}` +
    `</span>` +
    /* ALWAYS PRESENT, ALWAYS EMPTY AT BUILD TIME. `markFlags` owns its text,
       because the number that belongs here is the number of marks actually
       DRAWN on this node — which includes the brand-level findings addressed to
       this ground, and which the per-ground palette list alone never knew. */
    `<span class="flagchip ds-n7" data-slot="flags"></span>` +
    `<span class="acts">${placeBar(node.id)}</span>` +
    `</div>` +

    `<div class="reveal" data-reveal="foregrounds">` +
    headerBlock() +
    heroBlock(model) +
    `</div>` +

    `<div class="reveal" data-reveal="ladders">` +
    cardsBlock() +
    `<div class="strip" data-slot="alternate" data-render="alternate">${ladderStrip("alt")}</div>` +
    `<div class="strip" data-slot="alternate_inverse" data-render="alternate_inverse">${ladderStrip(
      "alt-inverse",
    )}</div>` +
    `</div>` +

    `<div class="reveal" data-reveal="components">` +
    formBlock(model.canonical) +
    tableBlock(model) +
    decisionBlock(model) +
    /* WHERE okuro'S REAL PRIMITIVES LAND, and only on the root node.
       It is empty here and stays empty: the parent portals React into it, so
       what renders is `Button`, `Input`, `Select`, `Card`, `Tabs` from the
       vendored `ui/` — the app's own components wearing the emitted kit, which
       is the P4-canvas idea the redesign lost.

       INSIDE THE DOCUMENT, not beside it. A mount under `<body>` would resolve
       against the ROOT ground and prove nothing about descent; here it inherits
       whatever ground this node published, so placing `emphasis` or a signal on
       the node repaints okuro's real components with it.

       ONLY ON THE ROOT (`node.id === "root"`): one showcase per canvas. A nested
       guest node showing a second copy would say the same thing twice and double
       the React tree for it. */
    (node.id === "root"
      ? `<section class="live-proof"><div class="section-intro"><span class="eyebrow ds-n7 ds-leads">THE ADAPTER, PROVEN LIVE</span><h2 class="ds-h2 ds-headings" data-slot="foreground" data-render="foreground">The same components that run okuro.</h2><p class="ds-n5 ds-paragraphs" data-slot="foreground" data-render="foreground">No illustration and no second stylesheet. These are the product primitives consuming this system in real time.</p></div><div data-okuro-mount></div></section>`
      : "") +
    alertsBlock() +
    `</div>` +

    `<div class="reveal" data-reveal="type">` +
    footerBlock(model) +
    `</div>` +

    (kids ? `<div class="reveal kids" data-reveal="descent">${kids}</div>` : "") +
    `</div>` +
    `</div>`
  );
}

/** The tree's SHAPE: ids and requests, and nothing about colour. Two trees with
    the same shape differ only in what the sheet paints them, so the DOM must
    not be rebuilt between them — rebuilding restarts every transition and the
    edit reads as a swap rather than a derivation. */
function shapeOf(node: TreeNode): string {
  return (
    `${node.id}:${node.request || "-"}(` +
    node.children.map(shapeOf).join("") +
    ")"
  );
}

/** Patch the parts of a node that are TEXT rather than paint. Every colour on
    the canvas comes from the sheet, so a colour edit needs no DOM change at
    all; what does change is the readout. */
function patch(root: Element, node: TreeNode, model: FrameModel): void {
  const el = root.querySelector(`[data-node="${node.id}"]`);
  if (el) {
    const g = model.grounds[node.groundKey] ?? {};
    const pill = el.querySelector(":scope > .doc > .head > .pill");
    if (pill) {
      pill.textContent =
        `${g.origin || node.request || "root"}` +
        ` · L ${g.lightness != null ? g.lightness.toFixed(3) : "?"}` +
        ` · ${g.polarity || ""}`;
    }
    /* The flag chip is NOT patched here — `markFlags` writes it, from the marks
       it actually drew. Two writers for one number is how "2 things" ended up
       above 56 badges. */

    /* THE HEX READOUT IS THE OTHER TEXT A COLOUR EDIT MOVES. A canonical edit
       changes the sheet and never the tree's shape, so `nodeHTML` does not run
       again and this field would keep printing the colour the brand had when
       the DOM was last built. */
    for (const reads of el.querySelectorAll('[data-reads="canonical"]')) {
      reads.textContent = model.canonical;
    }
  }
  node.children.forEach((child) => patch(root, child, model));
}

/**
 * THE VISIBILITY HALF of his flag ruling.
 *
 *     "both configuration (something is not good with this value) and
 *      visibility (here is where it's failing)"
 *
 * The rail carries the first half. This is the second: the element that PAINTS a
 * flagged slot gets a visible mark, and the mark OPENS THE INSPECTOR. It is
 * advice and never blocking — a mark changes no value, gates no interaction, and
 * the component underneath renders exactly what the engine resolved.
 *
 * TWO THINGS THIS FILE GOT WRONG, both measured:
 *
 * 1. THE REASON WAS A NATIVE `title`. 339 to 681 characters of engine prose —
 *    raw slot names, ASCII arrows, the whole message CONCATENATED when two flags
 *    landed on one element — delivered through the one affordance §7 deleted by
 *    name. A mark now carries a human `aria-label` and nothing else, and the
 *    reason is the same `<Note>` the rail renders, in the inspector, once.
 *
 * 2. ONE FINDING DREW MANY MARKS. `renders: ["foreground"]` matches every
 *    heading, paragraph, link and cell in a node, so a single WCAG failure drew
 *    43 identical badges and two `decide` findings drew eight — under a band
 *    saying "2 things". ONE MARK PER FINDING PER NODE now: the first element
 *    that paints it. The count on the node's own chip is that same number, so
 *    what the band counts and what the canvas draws are the same set.
 *
 * Which elements are marked is still the ENGINE's answer (a flag's renders
 * list), matched against what each element declares it paints (data-render).
 */
function markFlags(root: Element, node: TreeNode, model: FrameModel): void {
  const el = root.querySelector(`[data-node="${node.id}"]`);
  if (el) {
    const g = model.grounds[node.groundKey] ?? {};
    /* THE M5 FIX, and it needs no engine change. A brand-level
       `foreground.contrast-short` ships with `renders: []` — deliberately, and
       the engine says why: its `where` names ONE ground, and broadcasting would
       outline the foreground of every node on the canvas, "saying here is where
       it's failing about places where it is not".
       So the page reads the SAME address the engine already gave it. `where`
       is an authored colour slot; `ground.origin` is the ground that colour
       becomes. `groundOf` maps one to the other, and the mark lands on that
       ground and nowhere else. Before this, the flag had no canvas address at
       all — a WCAG failure was invisible on the surface that shows it. */
    const addressed = model.brandFlags.filter(
      (flag) => !flag.where || groundOf(flag.where) == null || groundOf(flag.where) === g.origin,
    );
    /* ONE FINDING PER CODE. The brand-level `foreground.contrast-short` names
       `signals.warning` and the per-ground one names `ground:signal:warning` —
       the SAME finding about the same ground, arriving twice, and marked twice.
       The engine reports it from both places on purpose; the canvas draws it
       once. */
    const seen = new Set<string>();
    const flags = [...(g.flags ?? []), ...addressed].filter((flag) => {
      if (seen.has(flag.code)) return false;
      seen.add(flag.code);
      return true;
    });

    const clear = (marked: Element) => {
      marked.removeAttribute("data-flagged");
      marked.removeAttribute("data-severity");
      marked.removeAttribute("title");
      marked.removeAttribute("role");
      marked.removeAttribute("tabindex");
      if (marked.getAttribute("data-flag-label") !== null) {
        marked.removeAttribute("aria-label");
        marked.removeAttribute("data-flag-label");
      }
    };
    el.querySelectorAll(":scope [data-flagged]").forEach(clear);
    clear(el);

    /* SEVERITY RANKS THE MARKS. Dark mode used to read as a failure report:
       every finding, including "this is the system working", drew the same
       dashed outline and the same `!` badge. A `note` is a hairline; a `decide`
       is a dashed outline and a `◆`; only a must-fix gets the solid dash and
       the `▲`. Ranked DESCENDING so the worst finding claims its element first
       and a lesser one moves to the next candidate rather than overwriting it. */
    const RANK: Record<string, number> = { note: 1, decide: 2, "must-fix": 3 };
    const ranked = flags
      .slice()
      .sort((a, b) => (RANK[b.severity ?? "note"] ?? 1) - (RANK[a.severity ?? "note"] ?? 1));

    for (const flag of ranked) {
      /* ONE MARK PER FINDING. Every element this node paints with one of the
         flag's rendered slots is a candidate; the first unclaimed one carries
         the mark. Marking all of them said the same thing 43 times. */
      const candidates: Element[] = [];
      for (const render of flag.renders) {
        el.querySelectorAll(`[data-render~="${render}"]`).forEach((hit) => {
          /* A node's own descendants only — a nested child resolves against a
             different ground and answers for its own flags. */
          if (hit.closest("[data-node]") !== el) return;
          candidates.push(hit);
        });
      }
      const hit = candidates.find((c) => !c.hasAttribute("data-flagged"));
      if (!hit) continue;
      hit.setAttribute("data-flagged", flag.code);
      hit.setAttribute("data-severity", flag.severity ?? "note");
      /* THE MARK IS A CONTROL, not a hover surface. It opens the inspector,
         where the reason is the same sentence the rail shows — once, in the
         page's own voice, with its one action. */
      hit.setAttribute("role", "button");
      hit.setAttribute("tabindex", "0");
      if (!hit.hasAttribute("aria-label")) {
        hit.setAttribute("data-flag-label", "");
        hit.setAttribute("aria-label", `${MARK_LABEL[flag.code] ?? "Flagged"} — open the explanation`);
      }
    }

    /* The node's own chip counts what is DRAWN on it, so the chip, the marks
       and the verdict band are three readings of one set rather than three
       different numbers. */
    const chip = el.querySelector(":scope > .doc > .head > .flagchip");
    if (chip) {
      const drawn = el.querySelectorAll(":scope [data-flagged]").length;
      chip.textContent = drawn ? `${drawn} flag${drawn === 1 ? "" : "s"}` : "";
    }
  }
  node.children.forEach((child) => markFlags(root, child, model));
}

/**
 * A mark's accessible name — the TOPIC, never the engine's paragraph.
 *
 * Mirrors `severity.ts::LABEL`. It is restated here because this string is
 * written into the frame's own document, which is a different realm and must
 * not import page components.
 */
const MARK_LABEL: Record<string, string> = {
  "foreground.contrast-short": "Contrast",
  "brand.figure-contrast-short": "Contrast",
  "branded.figure-contrast-short": "Contrast",
  "brand.adaptation-does-not-separate": "Adaptation",
  "brand.possibly-unadjusted": "Adaptation",
  "brand.polarity-ambiguous": "Ground",
  "ground.polarity-ambiguous": "Ground",
  "font.weight-unreadable": "Typeface",
  "font.weights-not-monotone": "Typeface",
  "shadow.became-border": "Shadow",
};

/**
 * An authored colour slot -> the ground ORIGIN that colour becomes.
 *
 * The engine's own correspondence, restated on the page because the engine
 * deliberately does not broadcast it: `signals.warning` is authored in the rail
 * and appears on the canvas as the ground whose origin is `signal:warning`.
 * A slot with no ground (a font, a radius) answers null and its flag is not
 * placed by ground at all.
 */
function groundOf(where: string): string | null {
  const head = where.split("/")[0] ?? where;
  if (head.startsWith("ground:")) return head.slice(7);
  if (head.startsWith("signals.")) return `signal:${head.slice(8)}`;
  if (head === "brand.canonical") return "brand-canonical";
  if (head === "neutrals.white") return "root:light";
  if (head === "neutrals.black") return "root:dark";
  return null;
}

/** The model, reduced to what the canvas reads. */
function frameModel(model: ResolvedModel): FrameModel {
  const grounds: Record<string, FrameGround> = {};
  for (const ground of model.grounds) {
    grounds[ground.key] = {
      origin: ground.origin,
      lightness: ground.lightness,
      polarity: ground.polarity,
      flags: ground.palette.flags,
    };
  }
  // BRAND-level flags travel too, and only the ones with a render address. An
  // unadjusted `on_dark` is a fact about the brand rather than about any one
  // ground, but it is PAINTED on every branded figure — so the canvas has to be
  // told, or the rail would be the only place it appeared.
  /* BRAND-level flags travel with their `where` intact, because `where` is what
     the M5 fix in `markFlags` reads to place a per-ground finding. */
  return {
    grounds,
    canonical: model.brand_colours.canonical,
    threshold: model.threshold,
    rootPercent: model.sizes.root_percent,
    rootPx: model.sizes.root_px,
    base: model.sizes.base,
    textRungs: model.sizes.text_rungs.length,
    textRoles: model.sizes.text_styles.length,
    componentRungs: model.sizes.component_rungs.length,
    componentRoles: model.sizes.component_roles.length,
    desktopRung: model.brand.defaults.rung.desktop,
    mobileRung: model.brand.defaults.rung.mobile,
    fontFamily: model.type.family,
    brandFlags: model.flags
      .filter((flag) => flag.renders.length > 0 || groundOf(flag.where) != null)
      .map((flag) => ({
        code: flag.code,
        message: flag.message,
        where: flag.where,
        severity: flag.severity,
        renders: flag.renders.length ? flag.renders : ["foreground"],
      })),
  };
}

/**
 * The choreography stylesheet: transitions and stage gating.
 *
 * Timed with the brand's OWN motion. A brand that authored slow, gentle motion
 * watches its system grow slowly and gently — which is the point of a curve
 * being a brand signature rather than a framework constant.
 */
export function growthCss(model: ResolvedModel): string {
  const speed = model.motion.speeds[model.motion.default_speed] ?? 650;
  const curveName = model.motion.default_curve;
  const curve =
    curveName === "signature" && model.motion.signature
      ? model.motion.signature
      : (model.motion.curves[curveName] ?? "cubic-bezier(0.4, 0, 0.2, 1)");

  return `
/* okuro design-engine — GROWTH CHOREOGRAPHY. This file is the PAGE's, not the
   engine's: it holds no derived value, only how long a value takes to become
   its next one. Timing is the brand's own authored motion. */
* { box-sizing: border-box; }

/* THE SURROUND IS CONTENT, NOT VOID. The whole subject of this page is what a
   ground does to everything on it, so the ground is painted edge to edge and
   the DOCUMENT is a measure-wide column centred in it. Measured before: the
   canvas was 45 % empty at 1440 and 77 % empty at 2560 — stretched, not
   reflowed, and mostly nothing. */
body { margin: 0; padding: 0; min-height: 100%; }

/* NOT A BRAND VALUE, and it is declared as a property so that exactly one
   place — the reduced-motion branch at the foot of this sheet — can retime
   every base interaction at once without fighting selector specificity. */
:root { --interaction-duration: ${INTERACTION_MS}ms; }

body, .node, .node * {
  transition:
    background-color ${speed}ms ${curve},
    color ${speed}ms ${curve},
    border-color ${speed}ms ${curve},
    outline-color ${speed}ms ${curve},
    box-shadow ${speed}ms ${curve};
}

/* BASE INTERACTIONS ARE NOT MOTION — and the rule above cannot tell them apart,
   because a ground change and a hover move the SAME five properties. The rule
   above is the ground repaint: a value becoming its next one across the whole
   document, which is real motion and is the brand's to time. A hover is not.
   The blanket selector reaches every interactive element in the canvas, so
   without this
   override every button, chip, tab and switch in the scene answered a pointer at
   the brand's default speed — 650ms on the shipped kits, which reads as lag.
   Same property list, same curve, one non-authored duration. */
.node :is(
  button, a, summary, label, input, textarea, select, option,
  [role="button"], [role="tab"], [role="switch"], [role="checkbox"],
  [role="radio"], [role="menuitem"], [role="menuitemcheckbox"],
  [role="menuitemradio"], [role="option"], [role="combobox"], [role="slider"],
  [data-slot="badge"], [data-slot="switch"], [data-slot="switch-thumb"],
  [data-slot="input"], [data-slot="textarea"], [data-slot="select-trigger"],
  [data-slot="select-item"], [data-slot="tabs-trigger"],
  [data-slot="toggle-group-item"], [data-slot="dropdown-menu-item"],
  [data-slot="context-menu-item"], [data-slot="command-item"],
  [data-slot="table-row"], [data-slot="row"],
  /* A scroll region is keyboard-focusable and carries focus-visible:ring-[3px].
     Measured: it was the ONE handle the first cut of this list missed, which is
     why the gate enumerates the DOM rather than trusting this list. */
  [data-slot="scroll-area-viewport"], [data-slot="scroll-area-thumb"],
  .place, .flagchip, .pill, .nav, .btn, .cta, .chip, .seg
),
.node :is(button, a, [role="button"], .place, .btn, .cta) * {
  transition-duration: var(--interaction-duration);
}

/* A FOCUSABLE CONTAINER IS BOTH THINGS AT ONCE, so it gets both timings. A
   tabpanel with tabindex="0" is a ground-painted surface AND a focus target:
   its background, ink and border repaint with the ground and belong to the
   brand, while its ring and outline answer a keystroke and do not. The property
   list above is the order these five durations line up against —
   background-color, color, border-color, outline-color, box-shadow — and the
   ring lives in the last two. Retiming the whole element instead would make one
   panel repaint 5x faster than the document it sits in. */
.node :is([tabindex], [role="tabpanel"]):not(
  button, a, summary, input, textarea, select, [role="button"], [role="tab"],
  [role="switch"], [role="checkbox"], [role="radio"], [role="menuitem"],
  [role="option"], [data-slot="scroll-area-viewport"]
) {
  transition-duration:
    ${speed}ms, ${speed}ms, ${speed}ms,
    var(--interaction-duration), var(--interaction-duration);
}
.node { padding: 0; position: relative; }
.doc { max-width: none; margin: 0 auto; overflow: hidden; }
.node .node { margin: 3rem; border-radius: 0.8rem; }

/* ---- the builder's own chrome, on the node ------------------------------ */
/* HOVER-REVEALED. Seven always-on chips at 11px across the top of a document
   is a debug bar, and it was the first thing a reader met. The pill stays —
   it names the ground, which is the one fact a node has to state. */
.head {
  display: flex; align-items: center; gap: 0.8rem; flex-wrap: wrap;
  min-height: 2.4rem; max-width: 140rem; margin: 0 auto;
  padding: 2rem 3rem 0;
}
.pill { letter-spacing: 0.04em; text-transform: uppercase; opacity: 0.72; cursor: pointer; }
.flagchip {
  padding: 0.2rem 0.6rem; cursor: pointer;
  border: 0.1rem solid currentColor; opacity: 0.85; border-radius: 0.4rem;
}
.acts { display: flex; gap: 0.4rem; margin-left: auto; flex-wrap: wrap; }
.place {
  font: inherit; font-size: 1.2rem; cursor: pointer;
  min-height: 2.4rem; padding: 0 0.8rem; border-radius: 0.4rem;
  background: transparent; color: inherit;
  border: 0.1rem solid color-mix(in srgb, currentColor 35%, transparent);
}
.place:hover { border-color: currentColor; }
.place.drop { opacity: 0.6; }

/* ---- 1 · the header bar ------------------------------------------------- */
.doc-head {
  display: flex; align-items: center; gap: 2.4rem;
  max-width: 140rem; margin: 0 auto; padding: 2.4rem 3rem;
  border-style: solid; border-width: 0 0 0.1rem 0;
}
.nav-row { display: flex; gap: 2rem; margin-left: 2.4rem; }
.nav { cursor: pointer; opacity: 0.82; }
.btn {
  font: inherit; cursor: pointer; border-style: solid;
  display: inline-flex; align-items: center; justify-content: center;
}
.doc-head .btn { margin-left: auto; }
.avatar {
  width: 3.2rem; height: 3.2rem; display: inline-flex;
  align-items: center; justify-content: center; font-size: 1.2rem;
}

/* ---- the editorial opening ---------------------------------------------- */
.hero {
  display: grid; grid-template-columns: minmax(0, 1.08fr) minmax(34rem, 0.92fr);
  gap: 8rem; align-items: center; min-height: 76rem;
  max-width: 140rem; margin: 0 auto; padding: 10rem 3rem 12rem;
}
.hero-copy { display: grid; gap: 2.4rem; align-content: center; }
.hero h1 { margin: 0; max-width: 13ch; letter-spacing: -0.055em; }
.eyebrow { letter-spacing: 0.1em; text-transform: uppercase; opacity: 0.62; }
.lead { margin: 0; max-width: 58rem; opacity: 0.78; }
.btn-row { display: flex; gap: 1.2rem; flex-wrap: wrap; }
.hero-proof { min-height: 52rem; padding: 3rem; display: flex; flex-direction: column; justify-content: space-between; border-style: solid; }
.proof-head { display: flex; justify-content: space-between; gap: 2rem; align-items: center; }
.proof-status { padding: 0.4rem 1rem; border: 0.1rem solid currentColor; border-radius: 999rem; opacity: 0.72; }
.logic-field { display: grid; grid-template-columns: repeat(17, 1fr); gap: 0.6rem; align-items: end; min-height: 24rem; }
.logic-field i { display: block; min-width: 0; aspect-ratio: 1; border-radius: 999rem; }
.logic-field i:nth-child(3n) { transform: translateY(-3rem); }
.logic-field i:nth-child(4n) { transform: translateY(2rem); }
.proof-equation { display: grid; grid-template-columns: repeat(3, 1fr); border-top: 0.1rem solid currentColor; }
.proof-equation > div { display: grid; gap: 0.6rem; padding: 2.4rem 1.6rem 0; border-left: 0.1rem solid color-mix(in srgb, currentColor 22%, transparent); }
.proof-equation > div:first-child { border-left: 0; padding-left: 0; }
.proof-value { display: block; }

/* ---- the operating model ------------------------------------------------- */
.principles, .form-story, .table-block, .decision-block, .live-proof, .signal-story {
  max-width: 140rem; margin: 0 auto; padding: 12rem 3rem;
}
.section-intro { display: grid; gap: 1.6rem; max-width: 88rem; margin-bottom: 6rem; }
.section-intro h2, .section-intro p { margin: 0; }
.cards {
  display: grid; gap: 0; grid-template-columns: repeat(3, 1fr);
}
.card { display: flex; flex-direction: column; gap: 2rem; min-height: 34rem; border-style: solid; border-width: 0.1rem 0 0.1rem 0.1rem; }
.card:last-child { border-right-width: 0.1rem; }
.card h3, .card p { margin: 0; }
.card a { cursor: pointer; margin-top: auto; }
.sepline { height: 0.1rem; }
.strip { display: flex; max-width: 134rem; margin: 0 auto 1rem; cursor: pointer; overflow: hidden; }
.strip i { flex: 1; height: 2.4rem; display: block; }

/* ---- authored input ------------------------------------------------------ */
.form-story { display: grid; grid-template-columns: minmax(26rem, 0.7fr) minmax(0, 1.3fr); gap: 8rem; align-items: start; }
.form-story > .section-intro { margin-bottom: 0; position: sticky; top: 3rem; }
.form {
  display: grid; gap: 2.4rem; border-style: solid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.form-title { display: flex; justify-content: space-between; gap: 2rem; grid-column: 1 / -1; padding-bottom: 2rem; border-bottom: 0.1rem solid currentColor; opacity: 0.7; }
.field { display: flex; flex-direction: column; gap: 0.6rem; }
.input {
  display: inline-flex; align-items: center; border-style: solid;
  border-width: 0.1rem; font-variant-numeric: tabular-nums;
}
.switch-row { display: flex; align-items: center; gap: 1.2rem; }
.track {
  width: 4.4rem; height: 2.4rem; display: inline-flex; align-items: center;
  padding: 0.2rem; justify-content: flex-end;
}
.knob { width: 2rem; height: 2rem; display: block; }
.form > .btn { justify-self: start; grid-column: 1 / -1; }

/* ---- system reference tables -------------------------------------------- */
.decision-block { padding-top: 2rem; }
.atlas-table-wrap { overflow-x: auto; border: 0.1rem solid color-mix(in srgb, currentColor 18%, transparent); }
.atlas-table { width: 100%; min-width: 100rem; border-collapse: collapse; text-align: left; }
.atlas-table :is(th, td) { padding: 2rem 2.4rem; border-bottom: 0.1rem solid color-mix(in srgb, currentColor 14%, transparent); vertical-align: top; }
.atlas-table thead th { opacity: 0.55; font-size: 1.5rem; letter-spacing: 0.08em; text-transform: uppercase; }
.atlas-table tbody th { width: 14rem; font-weight: 600; }
.atlas-table tbody tr:last-child :is(th, td) { border-bottom: 0; }
.atlas-table tbody td:nth-child(2) { font-weight: 500; }
.badge { display: inline-flex; padding: 0.2rem 0.8rem; white-space: nowrap; }

/* ---- live component proof ------------------------------------------------ */
.live-proof { border-top: 0.1rem solid color-mix(in srgb, currentColor 14%, transparent); }
.live-proof [data-kit-showcase] { padding: 0; }

/* ---- complete component gallery ----------------------------------------- */
[data-component-showcase] { max-width: 140rem; margin: 0 auto; padding-left: 3rem; padding-right: 3rem; }
[data-component-showcase] .component-contract { display: grid; gap: 4rem; padding: 8rem 0 10rem; border-top: 0.1rem solid color-mix(in srgb, currentColor 16%, transparent); }
[data-component-showcase] .component-contract-intro { display: grid; gap: 1.2rem; max-width: 88rem; }
[data-component-showcase] .component-contract-intro > * { margin: 0; }
[data-component-showcase] .component-contract-intro > span { opacity: 0.55; letter-spacing: 0.1em; }
[data-component-showcase] .component-contract-table { overflow-x: auto; border: 0.1rem solid color-mix(in srgb, currentColor 18%, transparent); }
[data-component-showcase] .component-contract-table table { width: 100%; min-width: 112rem; border-collapse: collapse; text-align: left; }
[data-component-showcase] .component-contract-table :is(th, td) { padding: 1.6rem 2rem; border-bottom: 0.1rem solid color-mix(in srgb, currentColor 14%, transparent); vertical-align: top; }
[data-component-showcase] .component-contract-table thead th { opacity: 0.56; font-size: 1.5rem; letter-spacing: 0.08em; text-transform: uppercase; }
[data-component-showcase] .component-contract-table tbody th { font-weight: 600; }
[data-component-showcase] .component-contract-table tbody tr:last-child :is(th, td) { border-bottom: 0; }
[data-component-showcase] .showcase-contract { display: grid; gap: 0; margin: 1.2rem 0 0; border-top: 0.1rem solid color-mix(in srgb, currentColor 16%, transparent); }
[data-component-showcase] .showcase-contract > div { display: grid; grid-template-columns: 8rem minmax(0, 1fr); gap: 1.2rem; padding: 1rem 0; border-bottom: 0.1rem solid color-mix(in srgb, currentColor 12%, transparent); }
[data-component-showcase] .showcase-contract :is(dt, dd) { margin: 0; font-size: 1.5rem; line-height: 2rem; }
[data-component-showcase] .showcase-contract dt { opacity: 0.52; }
[data-variant-matrix] { margin-bottom: 8rem; }
[data-specimen] { overflow: hidden; }
[data-specimen-stage] { min-width: 0; }

/* ---- signals ------------------------------------------------------------- */
.alerts {
  display: grid; gap: 0; grid-template-columns: repeat(4, 1fr);
}
.alert { display: flex; flex-direction: column; gap: 1.2rem; min-height: 30rem; justify-content: flex-end; }
.signal-mark { margin-bottom: auto; }

/* ---- typography + utility close ----------------------------------------- */
.doc-foot { margin-top: 4rem; padding: 12rem max(3rem, calc((100% - 134rem) / 2)); }
.type-stage { display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 8rem; align-items: end; padding-bottom: 10rem; }
.type-stage p { margin: 0; }
.type-display { letter-spacing: -0.055em; }
.type-contract { margin-bottom: 8rem; }
.fcols { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4rem; padding-top: 4rem; border-top: 0.1rem solid currentColor; }
.fcol { display: flex; flex-direction: column; gap: 0.6rem; opacity: 0.82; }
.kids { margin-top: 2.4rem; }

@media (max-width: 980px) {
  .hero, .form-story, .type-stage { grid-template-columns: 1fr; gap: 5rem; }
  .hero { min-height: 0; padding-top: 8rem; }
  .hero-proof { min-height: 42rem; }
  .cards, .alerts { grid-template-columns: 1fr; }
  .card { min-height: 24rem; border-width: 0.1rem 0.1rem 0 0.1rem; }
  .card:last-child { border-bottom-width: 0.1rem; }
  .form-story > .section-intro { position: static; }
  .fcols { grid-template-columns: 1fr; }
  .nav-row { display: none; }
  [data-showcase-hero], [data-showcase-group] { grid-template-columns: 1fr !important; gap: 4rem !important; }
  [data-showcase-specimens] { grid-template-columns: 1fr !important; }
  [data-showcase-hero] { min-height: 0 !important; }
}

/* ---- flag marks: "here is where it's failing" --------------------------- */
/* ADVICE, NEVER BLOCKING. The mark is drawn OUTSIDE the element's own box, in
   currentColor so it reads on any ground the system can produce, and it touches
   no property the sheet paints — the component underneath still renders exactly
   what the engine resolved.

   SEVERITY RANKS THE MARK, which is what stops dark mode reading as a failure
   report. A note — "this shadow became a border", which is the system working —
   is a hairline. Only a must-fix keeps the dashed outline and the badge. */
/* THE GLYPH IS THE SEVERITY, and the outline agrees with it. One exclamation
   mark on both a must-fix and a decide is a page that cannot tell a WCAG
   failure from a judgement call — measured: every default mark was a decide
   drawn as a red bang while the one must-fix was drawn nowhere. The vocabulary
   is the page's own: a triangle for must-fix, a diamond for decide, a bare
   hairline for a note. Colour is never the only channel, and inside the frame
   there is only currentColor — so weight and glyph carry it. */
[data-flagged] { position: relative; cursor: pointer; }
[data-flagged]:focus-visible { outline-color: currentColor; }
[data-flagged][data-severity="note"] {
  outline: 0.1rem solid color-mix(in srgb, currentColor 30%, transparent);
  outline-offset: 0.2rem;
}
[data-flagged][data-severity="decide"] {
  outline: 0.1rem dashed color-mix(in srgb, currentColor 60%, transparent);
  outline-offset: 0.3rem;
}
[data-flagged][data-severity="must-fix"] {
  outline: 0.3rem solid currentColor;
  outline-offset: 0.4rem;
}
[data-flagged][data-severity="must-fix"]::after,
[data-flagged][data-severity="decide"]::after {
  position: absolute;
  top: -1.1rem;
  right: -1.1rem;
  width: 1.8rem;
  height: 1.8rem;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.2rem;
  font-family: inherit;
  border: 0.1rem solid currentColor;
  border-radius: 0.9rem;
  background: inherit;
  pointer-events: none;
}
[data-flagged][data-severity="decide"]::after { content: "◆"; }
[data-flagged][data-severity="must-fix"]::after {
  content: "▲";
  border-width: 0.2rem;
  font-weight: 700;
}

/* ---- stage gating: nothing exists until its beat arrives ---------------- */
.reveal {
  opacity: 0;
  transform: translateY(0.8rem);
  transition: opacity ${speed}ms ${curve}, transform ${speed}ms ${curve};
  pointer-events: none;
}
${["foregrounds", "ladders", "components", "type", "descent"]
  .map(
    (stage) =>
      `body[data-ready~="${stage}"] .reveal[data-reveal="${stage}"] { opacity: 1; transform: none; pointer-events: auto; }`,
  )
  .join("\n")}
/* THE ACTS BAR IS A BUILDER AFFORDANCE, so it waits for the descent beat AND
   for the pointer. Seven always-on controls across the top of every node is a
   debug bar sitting on top of a document; revealed on hover or focus, the
   document is what a reader sees and the controls are where a builder reaches.
   Keyboard reaches them through :focus-within, so nothing is pointer-only. */
.acts { opacity: 0; transition: opacity ${speed}ms ${curve}; pointer-events: none; }
body[data-ready~="descent"] .acts { pointer-events: auto; }
body[data-ready~="descent"] .node:hover > .doc > .head > .acts,
body[data-ready~="descent"] .node:focus-within > .doc > .head > .acts,
body[data-ready~="descent"] .acts:hover,
body[data-ready~="descent"] .acts:focus-within { opacity: 1; }

@media (prefers-reduced-motion: reduce) {
  body, .node, .node *, .reveal, .acts { transition-duration: 1ms; }
  /* The interactive override above out-specifies the blanket rule, so retiming
     the property is what actually reaches it. */
  :root { --interaction-duration: 1ms; }
  .reveal { transform: none; }
}
`;
}

/**
 * Clone okuro's own stylesheets into the frame.
 *
 * WHY IT IS NEEDED AT ALL. The frame is a separate document, so a React portal
 * into it creates real elements that match NO rule from the parent's sheet —
 * okuro's primitives are Tailwind utility classes, and without the app's CSS
 * they render as unstyled HTML. The frame therefore has to carry the same
 * cascade the app does.
 *
 * WHY THAT IS NOT A LEAK. The app's `globals.css` reads the SAME consumer names
 * the adapter emits (`--color-*`, `--radius-*`, `--type-*` — 144 of them), so
 * once both sheets are present the primitives are driven BY THE ENGINE. That is
 * the whole point of the adapter, and this is the first surface where it can be
 * seen rather than asserted.
 *
 * ORDER IS LOAD-BEARING: everything is inserted BEFORE `#engine`, so the engine's
 * sheet is last and wins every collision — including `html { font-size: 50% }`,
 * which the app has no business overriding inside the canvas.
 *
 * A `<link>` is cloned by href rather than by content: same origin, already in
 * the browser's cache, and it keeps the frame honest about what the app loads.
 */
function adoptAppStyles(doc: Document): void {
  const anchor = doc.getElementById("engine");
  if (!anchor || doc.querySelector("[data-app-style]")) return;
  for (const node of document.querySelectorAll<HTMLElement>(
    'link[rel="stylesheet"], style',
  )) {
    // Never clone the frame's own three slots back into it, and never clone a
    // style tag this function already put there.
    if (node.id === "engine" || node.id === "growth" || node.id === "app-anchor") continue;
    const copy =
      node.tagName === "LINK"
        ? Object.assign(doc.createElement("link"), {
            rel: "stylesheet",
            href: (node as HTMLLinkElement).href,
          })
        : Object.assign(doc.createElement("style"), { textContent: node.textContent });
    copy.setAttribute("data-app-style", "");
    anchor.parentNode?.insertBefore(copy, anchor);
  }
}

export interface PreviewFrameProps {
  sheet: string;
  model: ResolvedModel | null;
  tree: TreeNode | null;
  ready: string;
  appearance: "light" | "dark";
  rootPlacement: string | null;
  /**
   * THE SIZE RUNG THE WHOLE SCENE RENDERS AT — his items 2 and 5.
   *
   * Stamped as `data-rung` on the tree's own root, which is SIZE-BY-FRAME
   * exactly as the register describes it: "as long as the children have no
   * sizing overrides, they consume the parents size configuration". The emitted
   * sheet already carries `[data-rung="RUNG"] .ds-*` for every rung of both
   * tables, so one attribute on one element re-sizes every descendant — type and
   * components together — with no second mechanism.
   *
   * `null` leaves the attribute off, which is the brand's own default rung.
   */
  rung?: string | null;
  /**
   * THE SCALES AXIS — one switch that downsizes the whole scene relative to
   * whatever rung it is showing.
   *
   * Stamped as `data-scale="down"` on the SAME host as the rung, because it is
   * the same inheritance: `_size_rules` emits both `[data-scale="down"]
   * [data-rung="R"] .ds-*` and `[data-scale="down"][data-rung="R"] .ds-*`, so
   * the switch works whether the rung sits on this element or on an ancestor.
   *
   * The two mappings differ on purpose and belong to the SHEET, not here — text
   * moves one rung, components three. Nothing in this file knows that, which is
   * what keeps the axis a property of the engine rather than of the page.
   *
   * `false` leaves the attribute off; there is no `up`, because the sheet emits
   * exactly one scaled state.
   */
  scaled?: boolean;
  /**
   * What the nodes are FILLED with — see `FrameVariant`.
   *
   * Defaults to `document`, so every existing call site keeps the seven blocks.
   */
  variant?: FrameVariant;
  /**
   * Rendered INTO the frame, through a portal, at `[data-okuro-mount]`.
   *
   * It is a function of the frame's document because the components that leave
   * the tree need it: a `Select` or a `Tooltip` opened inside the canvas has to
   * portal to the FRAME's body, not the app's, or the overlay would appear
   * outside the iframe it belongs to. `lib/ground-portal.ts` names that exact
   * escape hatch.
   */
  children?: (doc: Document) => React.ReactNode;
  onMessage: (msg: FrameMessage) => void;
  /** Fills its parent instead of measuring its own content. The canvas is the
      hero surface, so on the page it gets a pane and owns all of it; the
      auto-height form stays for the places that stack it in a column. */
  fill?: boolean;
  className?: string;
}

export function PreviewFrame({
  sheet,
  model,
  tree,
  ready,
  appearance,
  rootPlacement,
  rung = null,
  scaled = false,
  variant = "document",
  children,
  onMessage,
  fill = false,
  className,
}: PreviewFrameProps) {
  const frameRef = useRef<HTMLIFrameElement>(null);

  /**
   * The frame's own document, once it is parsed.
   *
   * Held in STATE rather than a ref on purpose: every effect below writes into
   * this document, so they must re-run when it appears. A ref would leave the
   * canvas empty until the next unrelated render — which is exactly the class of
   * silent-empty-canvas bug the postMessage handshake used to have.
   */
  const [doc, setDoc] = useState<Document | null>(null);

  /**
   * The node the primitives portal into, held in STATE for the same reason `doc`
   * is: it is destroyed and recreated whenever the tree's SHAPE changes, and a
   * portal aimed at a detached node renders into nothing. The rebuild effect
   * re-reads it, so the two can never disagree.
   */
  const [mount, setMount] = useState<Element | null>(null);

  const adopt = useCallback(() => {
    const frame = frameRef.current;
    const next = frame?.contentDocument ?? null;
    if (next?.getElementById("tree")) setDoc(next);
  }, []);

  // A srcDoc frame may already be parsed before React attaches `onLoad`, so the
  // mount path checks as well as the load event.
  useEffect(() => {
    adopt();
  }, [adopt]);

  /* -------------------------------------------------- events, from the parent

     Delegated on the frame's document. Listening from here is what removes the
     need for any script inside the frame: the frame is same-origin, so its
     events are ours to handle. `closest` is called on the frame's own element,
     never through a parent-realm constructor. */
  useEffect(() => {
    if (!doc) return;
    function onClick(ev: Event) {
      const target = ev.target as Element | null;
      if (!target?.closest) return;

      const place = target.closest("[data-place]");
      if (place) {
        ev.preventDefault();
        onMessage({
          type: "place",
          node: place.getAttribute("data-for") ?? undefined,
          request: place.getAttribute("data-place") || undefined,
        });
        return;
      }
      const drop = target.closest("[data-drop]");
      if (drop) {
        ev.preventDefault();
        onMessage({ type: "drop", node: drop.getAttribute("data-drop") ?? undefined });
        return;
      }
      /* The frame's box plus the element's offset inside it — the panel lives
         in the PARENT document, so a frame-local y would anchor it wherever the
         frame happens to start. */
      const frameTop = () => frameRef.current?.getBoundingClientRect().top ?? 0;

      /* A MARK OPENS THE INSPECTOR. It is checked BEFORE the plain slot reveal
         because a mark always sits on a slot, and the finding is the more
         specific question: the reader clicked the outline, not the value. */
      const mark = target.closest("[data-flagged]");
      if (mark) {
        const host = mark.closest("[data-node]");
        onMessage({
          type: "flag",
          flag: mark.getAttribute("data-flagged") ?? undefined,
          slot: mark.getAttribute("data-slot") ?? undefined,
          ground: host?.getAttribute("data-node") ?? undefined,
          y: mark.getBoundingClientRect().top + frameTop(),
        });
        return;
      }

      const slot = target.closest("[data-slot]");
      if (slot) {
        const host = slot.closest("[data-node]");
        onMessage({
          type: "reveal",
          slot: slot.getAttribute("data-slot") ?? undefined,
          ground: host?.getAttribute("data-node") ?? undefined,
          y: slot.getBoundingClientRect().top + frameTop(),
        });
      }
    }
    /* KEYBOARD REACHES THE MARKS TOO. They are `role=button tabindex=0`, so
       Enter and Space have to do what a click does — otherwise the only route
       to a finding's explanation is a pointer. */
    function onKey(ev: Event) {
      const key = (ev as KeyboardEvent).key;
      if (key !== "Enter" && key !== " ") return;
      const target = ev.target as Element | null;
      if (!target?.closest?.("[data-flagged]")) return;
      ev.preventDefault();
      onClick(ev);
    }
    doc.addEventListener("click", onClick);
    doc.addEventListener("keydown", onKey);
    return () => {
      doc.removeEventListener("click", onClick);
      doc.removeEventListener("keydown", onKey);
    };
  }, [doc, onMessage]);

  /* ESCAPE FROM INSIDE THE FRAME.
     The frame is a different document with its own focus, so a keydown a reader
     presses while the canvas holds focus never reaches the page's own window
     listener — measured: Escape closed the inspector from TOKENS and not from
     DOCUMENT. Re-dispatching it on the parent window makes one gesture behave
     the same way from every scene. */
  useEffect(() => {
    if (!doc) return;
    const relay = (ev: Event) => {
      if ((ev as KeyboardEvent).key !== "Escape") return;
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    };
    doc.addEventListener("keydown", relay);
    return () => doc.removeEventListener("keydown", relay);
  }, [doc]);

  /* ------------------------------------------------------------ the height

     Only in the auto-height form. When the canvas fills a pane its height is
     the pane's, and measuring the content would fight the layout. */
  useEffect(() => {
    if (!doc || fill) return;
    const body = doc.body;
    const size = () => {
      const frame = frameRef.current;
      if (frame) frame.style.height = `${Math.max(body.scrollHeight + 8, 320)}px`;
    };
    size();
    const ro = new ResizeObserver(size);
    ro.observe(body);
    return () => ro.disconnect();
  }, [doc, fill, sheet, tree, ready]);

  /* ------------------------------------------------------------- the state

     Each of these is one attribute or one style text. They are separate effects
     so that a colour edit touches ONLY the sheet — the DOM stays and every
     value transitions in place. */

  useEffect(() => {
    if (!doc || !sheet) return;
    const style = doc.getElementById("engine");
    if (style) style.textContent = sheet;
  }, [doc, sheet]);

  useEffect(() => {
    if (!doc || !model) return;
    const style = doc.getElementById("growth");
    if (style) style.textContent = growthCss(model);
  }, [doc, model]);

  // The tree's DOM. Rebuilt only when its SHAPE moves; otherwise patched, so
  // the elements — and their running transitions — survive an edit.
  const shape = useRef<string | null>(null);
  useEffect(() => {
    if (!doc || !model || !tree) return;
    const host = doc.getElementById("tree");
    if (!host) return;
    const reduced = frameModel(model);
    // THE VARIANT IS PART OF THE SHAPE. Two scenes can hand this frame the same
    // tree and want different DOM in it, and a shape that ignored the variant
    // would keep whichever occupant rendered first — the components scene
    // showing the demo document, or the document scene showing an empty node.
    const next = `${variant}|${shapeOf(tree)}`;
    if (next !== shape.current) {
      shape.current = next;
      host.innerHTML = nodeHTML(tree, reduced, variant);
      // The mount node was just replaced along with everything else, so the
      // portal has to be re-pointed in the SAME effect that replaced it.
      setMount(host.querySelector("[data-okuro-mount]"));
    } else {
      patch(host, tree, reduced);
    }
    // Marks are re-applied on EVERY model change, not only on a rebuild: an
    // edit changes the sheet without touching the DOM, and the whole point is
    // that the flag it creates appears in the same beat as the colour.
    markFlags(host, tree, reduced);
  }, [doc, model, tree, variant]);

  useEffect(() => {
    if (!doc) return;
    doc.body.setAttribute("data-ready", ready);
  }, [doc, ready]);

  /* okuro'S OWN STYLESHEETS, cloned in once per frame document. Without them a
     portalled `Button` is unstyled HTML — see `adoptAppStyles`. */
  useEffect(() => {
    if (!doc) return;
    adoptAppStyles(doc);
  }, [doc]);

  /* THE SCENE'S SIZE RUNG (items 2 + 5). One attribute on the tree's own root,
     which is a FRAME in the register's sense, so every descendant consumes it
     unless it overrode its own. Removing the attribute is how "the brand's
     default" is spelled — an absent rung is not a rung named `L`. */
  useEffect(() => {
    if (!doc) return;
    const host = doc.getElementById("tree");
    if (!host) return;
    if (rung) host.setAttribute("data-rung", rung);
    else host.removeAttribute("data-rung");
  }, [doc, rung, tree]);

  /* THE SCALES AXIS. Same host, same inheritance, and the same "absent is the
     unscaled state" spelling as the rung above. It is a SEPARATE effect rather
     than a second line in that one because the two axes change independently —
     a reader flips the scale without touching the rung, and re-running the rung
     write on that flip would fight the `tree` dependency for no reason. The
     mapping (text one rung, components three) lives entirely in the sheet. */
  useEffect(() => {
    if (!doc) return;
    const host = doc.getElementById("tree");
    if (!host) return;
    if (scaled) host.setAttribute("data-scale", "down");
    else host.removeAttribute("data-scale");
  }, [doc, scaled, tree]);

  useEffect(() => {
    if (!doc) return;
    doc.documentElement.setAttribute("data-appearance", appearance);
  }, [doc, appearance]);

  /* THE PLACEMENT LANDS ON `:root`, and it has to be the document root rather
     than the root NODE. Brand at the root ground is CANONICAL — there is no
     surround for it to adapt to — and `:root[data-branded]` is the block that
     states that, background and complete palette together. Everything under it
     resolves against it through `@container style(--ground)`, so painting the
     page ground is the whole of "brand-full": one code path per ground, no
     second rule for the root's own children. */
  useEffect(() => {
    if (!doc) return;
    const html = doc.documentElement;
    html.removeAttribute("data-branded");
    html.removeAttribute("data-signal");
    const request = requestOfRoot(rootPlacement);
    if (request === "branded") html.setAttribute("data-branded", "");
    else if (request?.startsWith("signal:"))
      html.setAttribute("data-signal", request.slice(7));
  }, [doc, rootPlacement]);

  return (
    <>
    {/* okuro'S REAL PRIMITIVES, RENDERED INSIDE THE CANVAS.
        React runs in the PARENT and creates the nodes in the FRAME's document —
        which is allowed because the frame is same-origin and deliberately not
        sandboxed (see the note on the iframe below). So the components are the
        app's own, with the app's own props and behaviour, and everything they
        wear comes from the emitted sheet. No script runs inside the frame, so
        the CSP rule at the top of this file is untouched. */}
    {doc && mount && children ? createPortal(children(doc), mount) : null}
    <div
      data-frame-shell
      style={{
        position: "relative",
        width: "100%",
        height: fill ? "100%" : 420,
        minHeight: fill ? 0 : 320,
        overflow: "hidden",
      }}
    >
      <iframe
        ref={frameRef}
        title="the system, growing"
        srcDoc={FRAME_SKELETON}
        data-testid="engine-canvas"
        className={className ?? (fill ? "h-full w-full" : "w-full border border-border")}
        style={fill ? { display: "block" } : { height: 420, display: "block" }}
        // Deliberately NOT sandboxed. `sandbox="allow-scripts"` would give the
        // frame an opaque origin, and an opaque origin breaks the mechanism this
        // whole file is built on: the parent could no longer reach
        // `contentDocument`, so nothing would render at all. It also costs
        // same-origin introspection, so neither the browser gate nor a human with
        // devtools could read what the sheet actually computed. A preview that
        // cannot be measured is not evidence.
        onLoad={adopt}
      />
      {!ready.split(/\s+/).includes("foregrounds") && (
        <div
          data-frame-loading
          role="status"
          aria-live="polite"
          style={{
            position: "absolute",
            inset: 0,
            display: "grid",
            alignContent: "center",
            justifyItems: "center",
            gap: 8,
            padding: 24,
            background: "var(--ce-surface)",
            color: "var(--ce-fg)",
            textAlign: "center",
            pointerEvents: "none",
          }}
        >
          <span className="ce-kicker">Live preview</span>
          <strong className="ce-body">Assembling the first surface…</strong>
          <span className="ce-body ce-2">Authored inputs are resolving into the system.</span>
        </div>
      )}
    </div>
    </>
  );
}
