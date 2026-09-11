// <!-- AGENT_HEADER
// role: code
// purpose: /prism-gallery — a living showcase of every prism MODULE (rendered
//   live through the real BlockView) and every LAYOUT archetype (with status),
//   so we can see at a glance what exists and what's still missing. Grouped by
//   module class; each card shows the type name + when-to-use + a live render.
// AGENT_HEADER_END -->
import { useState } from "react";
import { useNavigate } from "react-router";

import { BlockView } from "@/components/prism/blocks";
import type { Block } from "@/lib/prism-api";

const IMG = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='120' height='80'><rect width='120' height='80' fill='%230ea5e9' opacity='0.25'/><circle cx='60' cy='40' r='20' fill='%230ea5e9' opacity='0.5'/></svg>";
const ICON_SVG = "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' width='100%' height='100%'><path d='M12 2v20M2 12h20'/></svg>";

type Entry = { name: string; when: string; block: Block; wide?: boolean };
type Group = { title: string; note: string; entries: Entry[] };

// Every module type, grouped by class, with representative sample data — rendered
// live through BlockView (the same path a real deck uses).
const GROUPS: Group[] = [
  {
    title: "Typographic",
    note: "text-only slides — variety instead of stacked paragraphs",
    entries: [
      { name: "statement", when: "one decisive claim, huge type", block: { type: "statement", text: "Speed first. Full-fledged later.", kicker: "principle" } },
      { name: "quote", when: "a stakeholder's voice", block: { type: "quote", quote: "No memory at all today — Meridian just clones from the kernel.", attribution: "Zora Zebrabrand", role: "CTO" } },
      { name: "heading", when: "sub-section header", block: { type: "heading", eyebrow: "board recommendation", title: "Expand to DACH", lede: "A CHF 4M bet, shown with its uncertainty." } },
      { name: "section-divider", when: "a chapter break, full-bleed", wide: true, block: { type: "heading", display: "standalone", eyebrow: "02", title: "Market", lede: "Where the CHF 220M lives — and how much we can take." } },
      { name: "callout", when: "a warning / decision / wow", block: { type: "callout", variant: "risk", text: "europe-west6 has no Confidential VMs — a hard residency constraint." } },
      { name: "text", when: "prose connective tissue", block: { type: "text", md: "Only reach for **prose** when the argument needs connective tissue between modules." } },
    ],
  },
  {
    title: "Data-viz",
    note: "quantitative — pick by the shape of the data",
    entries: [
      { name: "stat", when: "a few headline numbers", block: { type: "stat", items: [{ value: "78%", label: "gross margin", delta: "+4pt", trend: "up", state: "good" }, { value: "11mo", label: "CAC payback" }, { value: "4.1%", label: "churn", state: "good" }] } },
      { name: "chart", when: "a series / distribution", wide: true, block: { type: "chart", chart: "bar", series: [{ label: "Q1", value: 3.1 }, { label: "Q2", value: 3.6 }, { label: "Q3", value: 4.1 }], unit: "CHF M" } },
      { name: "frequency", when: "risk as N-of-M, not %", block: { type: "frequency", items: [{ label: "deals that close at this stage", count: 38, of: 100, state: "warn" }] } },
      { name: "hops", when: "an uncertain forecast", wide: true, block: { type: "hops", quantiles: { p10: 3.2, p50: 4.1, p90: 5.6 }, unit: "CHF M", label: "FY27 ARR", reference: 4.0, referenceLabel: "plan", decimals: 1 } },
      { name: "meter", when: "proportion / budget bars", block: { type: "meter", items: [{ label: "runtime", value: 40, note: "POC" }, { label: "memory", value: 5, state: "bad" }, { label: "telemetry", value: 15 }], unit: "%" } },
      { name: "matrix", when: "options × criteria", wide: true, block: { type: "matrix", columns: ["us", "A", "B"], rows: [{ label: "on-box", cells: [true, false, "partial"] }, { label: "CH-resident", cells: [true, false, false] }], highlightCol: 0 } },
      { name: "table", when: "comparison / attribute list", wide: true, block: { type: "table", headers: ["region", "confidential VM", "vector 2.0"], rows: [["ew6", "no", "no"], ["us-central1", "yes", "yes"]], rowStates: ["warn", "good"] } },
      { name: "smallmultiples", when: "same metric across segments", wide: true, block: { type: "smallmultiples", chart: "bar", panels: [{ label: "board", series: [{ label: "a", value: 3 }, { label: "b", value: 5 }] }, { label: "eng", series: [{ label: "a", value: 4 }, { label: "b", value: 2 }] }] } },
    ],
  },
  {
    title: "Decision & rigor",
    note: "the board-grade moat — auditable, calibrated, adversarial",
    entries: [
      { name: "options", when: "3 choices, pick one", wide: true, block: { type: "options", options: [{ title: "Full memory first", cons: ["blocks the team"] }, { title: "Bucket now", recommended: true, pros: ["usable this week"] }, { title: "Defer", cons: ["team stays blind"] }] } },
      { name: "decisionrecord", when: "an auditable ADR", wide: true, block: { type: "decisionrecord", question: "Ship a memory bucket now?", decision: "Yes — minimal store+search this week", options: [{ label: "bucket now", chosen: true }, { label: "full C-memory first", rejected_because: "blocks the team for weeks" }], reversal_trigger: "recall quality < 60% after 2 weeks", confidence: "medium" } },
      { name: "assumptionledger", when: "load-bearing beliefs", block: { type: "assumptionledger", assumptions: [{ text: "MCP recall interface is stable across all 3 rungs", confidence: "high", falsifier: "a rung needs a different call shape", load_bearing: true }] } },
      { name: "tripwires", when: "pre-committed reversals", block: { type: "tripwires", wires: [{ condition: "CAC crosses 300", metric: "CAC", status: "watch" }, { condition: "< 2 logos by Q2", status: "clear" }] } },
      { name: "evidence", when: "source-graded claims", block: { type: "evidence", claims: [{ text: "Churn held at 4.1%", source: "Stripe Q2", confidence: "high", as_of: "2026-06-30", grade: "primary" }, { text: "TAM ≈ CHF 220M", source: "internal model", confidence: "med", grade: "model" }] } },
      { name: "biascheck", when: "pre-empt the room's biases", block: { type: "biascheck", biases: [{ bias: "authority bias", risk: "the CEO championed it; the board may defer", counter: "options were blind-scored before the sponsor was named", severity: "high" }] } },
      { name: "messageproof", when: "prove it per audience", wide: true, block: { type: "messageproof", audiences: ["board", "investor", "regulator"], messages: [{ message: "The unit economics work", proofs: [{ text: "78% gross margin", audiences: ["board", "investor"], weight: "high" }, { text: "data stays in-CH", audiences: ["regulator"], weight: "high" }] }] } },
      { name: "redteam", when: "multi-model objections", wide: true, block: { type: "redteam", panel: ["claude", "gemini"], challenges: [{ objection: "Unit economics don't close at scale", severity: "high", rebuttal: "gross margin holds at 78%", who: "claude" }] } },
    ],
  },
  {
    title: "Structure & narrative",
    note: "sequences, comparisons, peer sets",
    entries: [
      { name: "steps", when: "a numbered pipeline", wide: true, block: { type: "steps", steps: [{ n: "1", title: "Gather", detail: "pull all source" }, { n: "2", title: "Classify", detail: "one statement per screen" }, { n: "3", title: "Rewrite", detail: "per audience" }, { n: "4", title: "Build" }] } },
      { name: "timeline", when: "roadmap / sequence", wide: true, block: { type: "timeline", events: [{ time: "wk 1", title: "Memory bucket" }, { time: "wk 2", title: "Telemetry" }, { time: "later", title: "Full C-memory" }] } },
      { name: "compare", when: "before → after", wide: true, block: { type: "compare", before: { label: "today", items: ["no memory", "PR-only contribution"] }, after: { label: "target", items: ["usable bucket", "classified webform"] } } },
      { name: "cards", when: "peer items", wide: true, block: { type: "cards", columns: 3, cards: [{ title: "Backend builder", tag: "role" }, { title: "Frontend builder", tag: "role" }, { title: "CLI expert", tag: "role" }] } },
      { name: "spec", when: "tier / model cards", wide: true, block: { type: "spec", cards: [{ title: "Pro", variant: "active", badge: "MOSS-TTSD", rows: [{ k: "audio", v: "best" }] }, { title: "Air", variant: "default", rows: [{ k: "audio", v: "works" }] }] } },
      { name: "checklist", when: "✓ coverage", block: { type: "checklist", columns: 2, items: [{ text: "memory bucket", done: false }, { text: "telemetry", done: false }, { text: "bootstrap packet", done: true }] } },
      { name: "lens", when: "same point, N reframings", block: { type: "lens", tabs: [{ label: "CTO", md: "Architecture & residency." }, { label: "CPO", md: "Prioritized, sequenced." }, { label: "CEO", md: "The bet." }] } },
      { name: "cta", when: "the board ask", block: { type: "cta", variant: "ask", title: "Greenlight the week-1 bucket", items: ["approve 400k staged", "name the FUTURE lead"] } },
      { name: "code", when: "a prompt / snippet", block: { type: "code", lang: "python", code: "db.migrate()  # apply pending" } },
    ],
  },
  {
    title: "Interactive & relational",
    note: "the reader can move it; nodes & edges",
    entries: [
      { name: "simulator", when: "reader stress-tests the case", wide: true, block: { type: "simulator", inputs: [{ key: "seats", label: "Seats", min: 10, max: 200, value: 60 }, { key: "price", label: "Price", min: 20, max: 200, value: 80, unit: "CHF" }], outputs: [{ label: "ARR", expr: "seats*price*12", unit: "CHF", decimals: 0 }] } },
      { name: "graph", when: "an analytical relationship map", wide: true, block: { type: "graph", nodes: [{ id: "a", label: "Runtime", spine: true }, { id: "b", label: "Memory" }, { id: "c", label: "Telemetry" }], edges: [{ from: "a", to: "b" }, { from: "a", to: "c" }] } },
      { name: "diagram", when: "a linear mermaid flow", wide: true, block: { type: "diagram", kind: "mermaid", spec: "graph LR; A[Gather]-->B[Classify]-->C[Rewrite]-->D[Build]" } },
    ],
  },
  {
    title: "Media & embed",
    note: "any okuro output, carried into a slide",
    entries: [
      { name: "image", when: "a picture / figure", block: { type: "image", src: IMG, caption: "a figure" } },
      { name: "image · caption-over", when: "text overlaid on the image", wide: true, block: { type: "image", src: IMG, overlay: { title: "Expand to DACH", text: "CHF 220M mid-market, reachable now.", align: "left" } } },
      { name: "gallery", when: "visual proof by volume", wide: true, block: { type: "gallery", columns: 3, images: [{ src: IMG, caption: "one" }, { src: IMG, caption: "two" }, { src: IMG, caption: "three" }] } },
      { name: "visualizer · icon", when: "an okuro-assets glyph", block: { type: "visualizer", source: "icon", svg: ICON_SVG, caption: "assets icon" } },
      { name: "visualizer · studio", when: "a generated image (baked)", block: { type: "visualizer", source: "studio", src: IMG, caption: "okuro Studio render" } },
      { name: "visualizer · podcast", when: "an audio brief", block: { type: "visualizer", source: "podcast", src: "data:audio/mpeg;base64,//uQx", transcript: "A short spoken brief…", duration: 42 } },
      { name: "audiobrief", when: "the deck that talks", block: { type: "audiobrief", src: "data:audio/mpeg;base64,//uQx", transcript: "Hey — here's the recap.", voice: "af_heart" } },
    ],
  },
];

// R1's layout archetype catalogue — status reflects the product today. "built"
// covers BOTH the layout.py packer (splits, grids, comparison, sidebar, hero-
// rail, z-pattern, band-lead) AND single-block full-bleed treatments carried by
// `display:"standalone"` on the block components (statement/quote/gallery/
// visualizer/image/heading). caption-over-image is a CONTAINED overlay inside
// the image block's own box (image.overlay), so it never fights the flowing doc.
// All 22 archetypes are now built.
const LAYOUTS: { name: string; wire: string; when: string; built: boolean }[] = [
  { name: "vertical stack", wire: "▤▤▤", when: "default fallback", built: true },
  { name: "split-pack (text|visual)", wire: "▨▧", when: "explain then show", built: true },
  { name: "mirrored split (visual|text)", wire: "▧▨", when: "break the rhythm", built: true },
  { name: "band-lead (KPI row)", wire: "▬▬▬", when: "board glance", built: true },
  { name: "N-column visual grid", wire: "▦", when: "chart/image gallery", built: true },
  { name: "full-bleed visual", wire: "█", when: "the visual is the point", built: true },
  { name: "big-number hero", wire: "❱", when: "one giant KPI", built: true },
  { name: "section-divider", wire: "—", when: "chapter break", built: true },
  { name: "typographic hero", wire: "❝", when: "one assertion", built: true },
  { name: "quote", wire: "❞", when: "stakeholder voice", built: true },
  { name: "title + bullets", wire: "≣", when: "safe default", built: true },
  { name: "N-column grid", wire: "▦", when: "peer cards", built: true },
  { name: "image gallery", wire: "⊞", when: "proof by volume", built: true },
  { name: "comparison side-by-side", wire: "◧◨", when: "before/after", built: true },
  { name: "timeline band", wire: "╌╌", when: "roadmap", built: true },
  { name: "caption-over-image", wire: "▩", when: "mood opener", built: true },
  { name: "module standalone full-page", wire: "◻", when: "simulator/graph", built: true },
  { name: "sidebar + main", wire: "▏▤", when: "long expert rung", built: true },
  { name: "z-pattern (4-quadrant)", wire: "⊕", when: "persuasion-dense", built: true },
  { name: "centered-focus", wire: "◉", when: "one decisive artifact", built: true },
  { name: "matrix full-bleed", wire: "▦", when: "us-vs-competitors", built: true },
  { name: "hero + detail rail", wire: "█▏", when: "one story, few caveats", built: true },
];

export function PrismGalleryPage() {
  const navigate = useNavigate();
  const [wideOnly, setWideOnly] = useState(false);
  const accent = "var(--color-accent)";
  const moduleCount = GROUPS.reduce((n, g) => n + g.entries.length, 0);

  return (
    <div className="h-full overflow-y-auto p-4">
      <div className="mx-auto max-w-6xl">
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <span className="text-lg font-semibold text-fg">okuro·prism — module & layout gallery</span>
          <span className="text-xs text-tertiary">{moduleCount} modules live · {LAYOUTS.filter((l) => l.built).length}/{LAYOUTS.length} layouts built</span>
          <div className="ml-auto flex items-center gap-2">
            <label className="flex items-center gap-1 text-xs text-tertiary">
              <input type="checkbox" checked={wideOnly} onChange={(e) => setWideOnly(e.target.checked)} /> wide only
            </label>
            <button onClick={() => navigate("/prism")} className="rounded-md border border-border px-2.5 py-1 text-sm text-fg hover:border-accent hover:text-accent">← Prism</button>
          </div>
        </div>

        {GROUPS.map((g) => (
          <section key={g.title} className="mb-8">
            <div className="mb-2 flex items-baseline gap-2 border-b border-border pb-1">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-fg">{g.title}</h2>
              <span className="text-xs text-tertiary">{g.note}</span>
            </div>
            <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))" }}>
              {g.entries.filter((e) => !wideOnly || e.wide).map((e) => (
                <div key={e.name} className={`flex flex-col rounded-lg border border-border p-3 ${e.wide ? "md:col-span-2" : ""}`}>
                  <div className="mb-2 flex items-baseline gap-2">
                    <code className="rounded bg-accent/10 px-1.5 py-0.5 text-xs font-semibold text-accent">{e.name}</code>
                    <span className="text-[11px] text-tertiary">{e.when}</span>
                  </div>
                  <div className="min-w-0 flex-1 text-fg">
                    <BlockView block={e.block} accent={accent} />
                  </div>
                </div>
              ))}
            </div>
          </section>
        ))}

        <section className="mb-10">
          <div className="mb-2 flex items-baseline gap-2 border-b border-border pb-1">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-fg">Layout archetypes</h2>
            <span className="text-xs text-tertiary">page arrangements — green = built, muted = backlog (R1 research)</span>
          </div>
          <div className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))" }}>
            {LAYOUTS.map((l) => (
              <div key={l.name} className={`flex items-center gap-3 rounded-lg border p-2.5 ${l.built ? "border-accent/40" : "border-border opacity-70"}`}>
                <span className="grid h-10 w-10 shrink-0 place-items-center rounded border border-current/15 text-lg" style={{ color: l.built ? "var(--color-accent)" : undefined }}>{l.wire}</span>
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium text-fg">{l.name}</div>
                  <div className="truncate text-[11px] text-tertiary">{l.when}</div>
                </div>
                <span className={`ml-auto shrink-0 rounded-full px-1.5 py-0.5 text-[9px] uppercase ${l.built ? "bg-accent/10 text-accent" : "border border-border text-tertiary"}`}>{l.built ? "built" : "backlog"}</span>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
