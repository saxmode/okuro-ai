/**
 * GUEST BRANDS INSIDE A HOST — the sharpest statement the system makes.
 *
 * His own story, and the reason the descent has to be one rule rather than a
 * table of cases:
 *
 *     "we have nested brands inside light / dark or even branded themes
 *      ([host] theme as the main theme and UBS, Valiant, Mobiliar etc, as
 *      nested themes) ... A UBS component (background black, main cta: red)
 *      works with contrast towards the [host] yellow, while maybe Valiant
 *      (purple) might not have enough contrast."
 *
 * So: an inbox. Unread letters are BRANDED — each one carries a guest brand's
 * whole system inside the host's ground. Read letters request nothing and
 * inherit, which is what "no request" means everywhere else in this engine.
 *
 * WHAT MAKES IT EVIDENCE RATHER THAN AN ILLUSTRATION. The frame wears the sheet
 * that `emit(guests=…)` produced, guest blocks and all, so a letter is painted
 * by the same CSS an application would ship. The placement rows beside it are
 * READ BACK from the same resolvers — they report the outcome, they do not
 * produce it. A section that drew each letter from its placement row would
 * agree with itself by construction and prove nothing.
 *
 * THE REVEAL. Every guest says which of rule 4's two clauses fired and why, in
 * the engine's own sentence. That is the point of the section: two guests on
 * ONE host ground going different ways, with no setting anywhere that made them
 * differ.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { INTERACTION_MS } from "./chrome";
import { engineApi } from "./engine-api";
import type { BrandJson, Placement } from "./types";

/* ------------------------------------------------------------------ frame */

/**
 * The inbox document: two empty style tags, an empty list, and NO SCRIPT.
 *
 * okuro's CSP has no `'unsafe-inline'` in `script-src`, and a srcdoc document
 * inherits it, so an inline bootstrap here is blocked on the deployed app and
 * the inbox renders empty with nothing but a console violation to say why. The
 * frame is same-origin, so the parent builds and drives it directly instead —
 * see the header of preview-frame.tsx, which states the rule for both frames.
 */
const FRAME_SKELETON = String.raw`<!doctype html>
<html data-appearance="light">
<head>
<meta charset="utf-8">
<style id="engine"></style>
<style id="chrome"></style>
</head>
<body>
<div id="inbox"></div>
</body>
</html>`;

const esc = (s: unknown) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/**
 * One letter, as markup that carries no colour at all.
 *
 * Same discipline as the growth canvas: class names and request attributes
 * only. A guest's entry element carries `data-brand` and `data-branded` at
 * once, which is exactly the shape `emit`'s scoped selectors are written for
 * (`[data-brand="x"][data-branded]`), so an unread letter enters the guest's
 * system and resolves its ground against whatever the host is.
 */
function letterHTML(id: string, label: string, subject: string, unread: boolean): string {
  /* UNREAD = branded, and it is a guest's whole system entering here.
     READ = no request at all, because inheritance is the ABSENCE of a rule. */
  const attrs = unread ? ` data-brand="${esc(id)}" data-branded=""` : "";
  return (
    `<div class="letter ds-surface ds-radius-m ds-bordered"${attrs}` +
    ` data-guest="${esc(id)}" data-state="${unread ? "unread" : "read"}">` +
    `<div class="row">` +
    `<span class="ds-n4">${esc(label)}</span>` +
    `<span class="ds-n7 state">${unread ? "unread" : "read"}</span>` +
    `</div>` +
    `<div class="ds-n5 subject">${esc(subject)}</div>` +
    `<button type="button" class="cta ds-solid-brand ds-bordered ds-radius-m ` +
    `ds-container-size ds-padding" data-cta="${esc(id)}">Open</button>` +
    `</div>`
  );
}

/** The section's own chrome. Layout only — it paints nothing the engine owns. */
function chromeCss(speed: number, curve: string): string {
  return `
* { box-sizing: border-box; }
body { margin: 0; padding: 2rem; }
:root { --interaction-duration: ${INTERACTION_MS}ms; }
body, .letter, .letter * {
  transition: background-color ${speed}ms ${curve}, color ${speed}ms ${curve},
              border-color ${speed}ms ${curve};
}
/* BASE INTERACTIONS ARE NOT MOTION. The rule above is the ground repaint, which
   the brand times; its blanket selector also reaches the letter's CTA, whose
   hover the brand does not. Same statement as growthCss in preview-frame.tsx. */
.letter :is(button, a, .cta) { transition-duration: var(--interaction-duration); }
#inbox { display: flex; flex-direction: column; gap: 1.2rem; }
.letter { padding: 1.4rem; border-style: solid; }
.row { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; }
.state { text-transform: uppercase; letter-spacing: 0.08em; opacity: 0.7; }
.subject { margin: 0.6rem 0 1.2rem; }
.cta {
  font: inherit; font-size: 1.2rem; cursor: pointer;
  border-style: solid; display: inline-flex; align-items: center;
}
@media (prefers-reduced-motion: reduce) {
  body, .letter, .letter * { transition-duration: 1ms; }
  /* Retiming the property is what reaches the interactive override above. */
  :root { --interaction-duration: 1ms; }
}
`;
}

/* ---------------------------------------------------------------- section */

const GROUNDS = [
  { value: "light", label: "light" },
  { value: "dark", label: "dark" },
  { value: "brand", label: "brand-full" },
];

export interface NestingSectionProps {
  host: BrandJson;
  /** Brands offered as guests — the saved kits, minus the host itself. */
  available: BrandJson[];
  /**
   * True when the only guest on offer is the page's own contrasting demo
   * rather than a kit on this machine. The scene says so and offers to keep it.
   */
  demo?: boolean;
  onKeepDemo?: () => void;
  /** Open the inspector on a value, on the ground this section is showing. */
  onReveal?: (slot: string, root: string) => void;
}

export function NestingSection({
  host,
  available,
  demo,
  onKeepDemo,
  onReveal,
}: NestingSectionProps) {
  const [ground, setGround] = useState("dark");
  const [picked, setPicked] = useState<string[]>([]);
  /* A guest's canonical is editable HERE because the flip is the lesson: move
     one guest's colour across the threshold and watch it change clause without
     anything else on the page moving. */
  const [tint, setTint] = useState<Record<string, string>>({});

  const byId = useMemo(() => {
    const map: Record<string, BrandJson> = {};
    for (const brand of available) map[brand.id] = brand;
    return map;
  }, [available]);

  /* THE DEFAULT IS DERIVED, not merely scheduled. A scene can mount while the
     kit query is still resolving; if that empty render wins, an effect-only
     default can leave the live proof with available guest chips and no picked
     guest. The effective selection therefore falls back at read time too. */
  const validPicked = picked.filter((id) => Boolean(byId[id]));
  const effectivePicked = validPicked.length
    ? validPicked
    : available.slice(0, 2).map((brand) => brand.id);

  // Default to the first two available guests, so the section says something
  // the moment it appears rather than waiting to be configured.
  useEffect(() => {
    setPicked((prev) => {
      const valid = prev.filter((id) => Boolean(byId[id]));
      return valid.length ? valid : available.slice(0, 2).map((brand) => brand.id);
    });
  }, [available, byId]);

  const guests = useMemo<BrandJson[]>(() => {
    const out: BrandJson[] = [];
    for (const id of effectivePicked) {
      const brand = byId[id];
      if (!brand) continue;
      const colour = tint[id];
      out.push(
        colour
          ? {
              ...brand,
              brand: {
                ...brand.brand,
                canonical: colour,
                // The adaptations are cleared so the ENGINE re-derives them —
                // that re-derivation is what makes the clause flip visible. A
                // shade would survive the move and quietly re-shade the new
                // colour, which is a different demonstration.
                on_light: null,
                on_dark: null,
                shade_light: null,
                shade_dark: null,
              },
            }
          : brand,
      );
    }
    return out;
  }, [effectivePicked, byId, tint]);

  const scene = useQuery({
    queryKey: ["design-engine", "nesting", host, guests, ground],
    queryFn: () => engineApi.nesting(host, guests, ground),
    enabled: guests.length > 0,
  });

  return (
    <div className="ce-scene" data-scene="guests">
      <header className="ce-scene-intro">
        <div className="ce-stack-tight">
          <span className="ce-kicker">Inheritance / live resolution</span>
          <h2 className="ce-display">One value descends. Context does the rest.</h2>
          <p className="ce-body ce-2" style={{ maxWidth: "var(--ce-measure)" }}>
            Unread letters carry a guest brand into <b style={{ color: "var(--ce-fg)" }}>{host.id}</b>'s
            ground. Read mail makes no request and inherits. Every outcome below
            comes from the engine—not from a page-level exception.
          </p>
        </div>
      </header>

      <section className="ce-stack-tight" aria-labelledby="inheritance-contract-title">
        <div className="ce-section-heading">
          <div>
            <span className="ce-kicker">Context contract</span>
            <h3 id="inheritance-contract-title" className="ce-title">What crosses a boundary</h3>
          </div>
          <span className="ce-body ce-2">The same contract applies at every depth.</span>
        </div>
        <div className="ce-table-wrap">
          <table className="ce-table">
            <thead><tr><th>Boundary behavior</th><th>Value</th><th>Meaning</th></tr></thead>
            <tbody>
              <tr><th>Descends</th><td>resolved background</td><td>The child receives exactly one contextual value.</td></tr>
              <tr><th>Recalculates</th><td>polarity · foreground · ladders · states</td><td>A new ground derives its complete vocabulary from that background.</td></tr>
              <tr><th>Remains authored</th><td>identity · type · border · radius · effects · motion</td><td>A nested brand brings its own authored system facts.</td></tr>
              <tr><th>Never branches</th><td>depth · component kind · colour kind</td><td>No special case is introduced for level, component, signal or brand.</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <div className="ce-row" style={{ gap: 32, flexWrap: "wrap", alignItems: "flex-start" }}>
        <div className="ce-stack-tight">
          <span className="ce-label">Host ground</span>
          <div className="ce-row" style={{ gap: 4 }}>
            {GROUNDS.map((option) => (
              <button
                key={option.value}
                type="button"
                className="ce-chip"
                aria-pressed={ground === option.value}
                onClick={() => setGround(option.value)}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>

        <div className="ce-stack-tight">
          <span className="ce-label">Guests placed as unread letters</span>
          <div className="ce-row" style={{ gap: 4, flexWrap: "wrap" }}>
            {available.map((brand) => (
              <button
                key={brand.id}
                type="button"
                className="ce-chip"
                aria-pressed={effectivePicked.includes(brand.id)}
                onClick={() =>
                  setPicked((prev) =>
                    (prev.length ? prev : effectivePicked).includes(brand.id)
                      ? (prev.length ? prev : effectivePicked).filter((id) => id !== brand.id)
                      : [...(prev.length ? prev : effectivePicked), brand.id],
                  )
                }
              >
                {brand.id}
              </button>
            ))}
          </div>
        </div>
      </div>

      {demo && (
        /* A CONTRASTING GUEST, PREFILLED. The sharpest demonstration in the
           system needed two brands and a fresh install ships one, so GUESTS was
           a permanent dead end with no inspectable value on it. This one is
           derived by the ENGINE from a colour the page supplies — the page
           computes nothing — and it is offered rather than saved. */
        <div className="ce-row" data-demo-guest style={{ gap: 8, flexWrap: "wrap" }}>
          <span className="ce-body ce-2" style={{ minWidth: 0 }}>
            There is one brand on this machine, so this guest is a demonstration
            — a contrasting blue, derived by the engine, saved nowhere.
          </span>
          {onKeepDemo && (
            <button type="button" className="ce-chip" onClick={onKeepDemo}>
              Keep it as a brand
            </button>
          )}
        </div>
      )}

      {guests.length === 0 && (
        <p className="ce-body ce-2">Pick at least one guest brand to place in the host.</p>
      )}

      {scene.data && (
        /* THE GRID THIS SECTION WAS ALWAYS AUTHORED FOR. It carried
           `lg:grid-cols-[minmax(0,1fr)_minmax(0,380px)] max-w-3xl` and was then
           rendered into a 304px rail, so the two columns never happened and the
           frame was 304px wide. No code change to the grid — the container
           finally matches it. */
        <div className="ce-nesting-stage">
          <NestingFrame
            css={scene.data.css}
            guests={guests}
            hostLabel={host.id}
            ground={ground}
          />

          <div className="ce-stack" aria-label="engine outcomes">
            <div className="ce-stack-tight">
              <span className="ce-kicker">One rule / every guest</span>
              <h3 className="ce-h2">{scene.data.rule.title}</h3>
              <pre
                className="ce-micro ce-scroller"
                style={{
                  margin: 0,
                  padding: 12,
                  border: "1px solid var(--ce-border)",
                  borderRadius: "var(--ce-r-control)",
                  background: "var(--ce-surface-2)",
                  whiteSpace: "pre-wrap",
                  fontFamily: "var(--font-mono)",
                  color: "var(--ce-fg-2)",
                }}
              >
                {scene.data.rule.formula}
              </pre>
            </div>
            {scene.data.placements.map((placement) => (
              <PlacementCard
                key={placement.guest}
                placement={placement}
                ground={ground}
                onReveal={onReveal}
                tint={
                  tint[placement.guest] ??
                  byId[placement.guest]?.brand.canonical ??
                  "#000000"
                }
                onTint={(next) =>
                  setTint((prev) => ({ ...prev, [placement.guest]: next }))
                }
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * What GUESTS says on a fresh install, when only one kit exists.
 *
 * Never a 7px sentence in a 300 × 750px void, which is what it was: a permanent
 * dead end, correct logic (a brand cannot be its own guest) with no way out.
 * The demonstration is explained and there is one primary action.
 */
export function GuestsEmpty({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="ce-scene" data-scene="guests">
      {/* FULL WIDTH, with the MEASURE on the prose rather than on the block.
          `max-width: measure` on the container made the empty state 880px in a
          1066px canvas — the same "rendered into a column narrower than the
          thing it explains" defect the section itself was rescued from. */}
      <div data-empty className="ce-stack" style={{ width: "100%" }}>
        <h2 className="ce-h2" style={{ maxWidth: "var(--ce-measure)" }}>
          The sharpest thing this system says needs two brands.
        </h2>
        <p className="ce-body ce-2" style={{ maxWidth: "var(--ce-measure)" }}>
          Place two guest brands on one host ground and they can go different
          ways — one keeps its own colour, the other falls back to a neutral —
          with no setting anywhere that made them differ. It falls out of a
          single rule read against each guest's own colours. There is one brand
          on this machine, so there is nothing yet to place.
        </p>
        <button
          type="button"
          className="ce-primary"
          style={{ alignSelf: "flex-start" }}
          onClick={onCreate}
        >
          Create a guest brand
        </button>
      </div>
    </div>
  );
}

/** One guest's outcome, the clause that produced it, and the reason. */
function PlacementCard({
  placement,
  tint,
  onTint,
  ground,
  onReveal,
}: {
  placement: Placement;
  tint: string;
  onTint: (next: string) => void;
  ground: string;
  onReveal?: (slot: string, root: string) => void;
}) {
  const full = placement.outcome === "brand-full";
  return (
    <div className="ce-surface ce-stack-tight" data-placement={placement.guest} style={{ padding: 16 }}>
      <div className="ce-row">
        <span className="ce-value" style={{ fontFamily: "var(--font-mono)" }}>
          {placement.guest}
        </span>
        <span
          data-outcome={placement.outcome}
          className="ce-chip"
          style={{ marginLeft: "auto", cursor: "default" }}
        >
          {full ? "brand-full" : "fallback"}
        </span>
      </div>
      <div className="ce-row">
        <input
          type="color"
          aria-label={`${placement.guest} canonical`}
          value={tint}
          onChange={(e) => onTint(e.target.value)}
          className="ce-control"
          style={{ width: 40, padding: 0, cursor: "pointer" }}
        />
        {/* THE VALUES ARE CLICKABLE HERE TOO. A scene where nothing opens the
            inspector is a scene where the page's central explanatory act is
            unreachable — which is exactly criterion 14, and GUESTS was the one
            scene that could not answer it from its own content. */}
        <button
          type="button"
          className="ce-body ce-2 ce-row"
          data-guest-value="ground"
          onClick={() => onReveal?.("ground", ground)}
          style={{
            gap: 4,
            minHeight: 24,
            background: "transparent",
            border: 0,
            padding: 0,
            color: "inherit",
            cursor: onReveal ? "pointer" : "default",
            fontFamily: "var(--font-mono)",
          }}
        >
          ground {placement.ground ?? "—"}
        </button>
        <button
          type="button"
          className="ce-body ce-2 ce-row"
          data-guest-value={placement.cta_slot}
          onClick={() => onReveal?.(placement.cta_slot, ground)}
          style={{
            gap: 4,
            minHeight: 24,
            background: "transparent",
            border: 0,
            padding: 0,
            color: "inherit",
            cursor: onReveal ? "pointer" : "default",
            fontFamily: "var(--font-mono)",
          }}
        >
          cta {placement.cta}
        </button>
      </div>
      <span className="ce-label">{placement.clause}</span>
      <p className="ce-body ce-2">{placement.because}</p>
    </div>
  );
}

/* The frame, driven from the parent. No script inside it and no handshake to
   miss: the document is same-origin, so every write below lands the moment the
   document exists and the inbox is never empty waiting for a message. */
function NestingFrame({
  css,
  guests,
  hostLabel,
  ground,
}: {
  css: string;
  guests: BrandJson[];
  hostLabel: string;
  ground: string;
}) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [doc, setDoc] = useState<Document | null>(null);

  const adopt = useCallback(() => {
    const next = frameRef.current?.contentDocument ?? null;
    if (next?.getElementById("inbox")) setDoc(next);
  }, []);

  useEffect(() => {
    adopt();
  }, [adopt]);

  useEffect(() => {
    if (!doc) return;
    const style = doc.getElementById("chrome");
    if (style) {
      style.textContent = chromeCss(420, "cubic-bezier(0.4, 0, 0.2, 1)");
    }
  }, [doc]);

  // The SHEET on its own — an edit to the host or to a guest changes only this,
  // so every letter transitions to its new resolution without the inbox moving.
  useEffect(() => {
    if (!doc || !css) return;
    const style = doc.getElementById("engine");
    if (style) style.textContent = css;
  }, [doc, css]);

  useEffect(() => {
    if (!doc) return;
    const host = doc.getElementById("inbox");
    if (!host) return;
    /* One read letter at the end, so the contrast between "carries a brand" and
       "inherits the host" is visible in the same frame rather than being
       asserted in prose. */
    host.innerHTML =
      guests
        .map((brand) => letterHTML(brand.id, brand.id, `a letter from ${brand.id}`, true))
        .join("") +
      letterHTML("host", hostLabel, "read mail inherits the host", false);
  }, [doc, guests, hostLabel]);

  useEffect(() => {
    if (!doc) return;
    const html = doc.documentElement;
    html.setAttribute("data-appearance", ground === "dark" ? "dark" : "light");
    html.removeAttribute("data-branded");
    html.removeAttribute("data-signal");
    if (ground === "brand") html.setAttribute("data-branded", "");
  }, [doc, ground]);

  // The frame owns its height: a fixed one would either clip a long inbox or
  // leave a slab of empty ground under a short one.
  useEffect(() => {
    if (!doc) return;
    const body = doc.body;
    const size = () => {
      const frame = frameRef.current;
      if (frame) frame.style.height = `${Math.max(body.scrollHeight + 8, 260)}px`;
    };
    size();
    const ro = new ResizeObserver(size);
    ro.observe(body);
    return () => ro.disconnect();
  }, [doc, css, guests]);

  return (
    <iframe
      ref={frameRef}
      title="guest brands nested in the host"
      srcDoc={FRAME_SKELETON}
      data-testid="nesting-canvas"
      className="ce-surface"
      style={{ width: "100%", height: 320 }}
      onLoad={adopt}
    />
  );
}
