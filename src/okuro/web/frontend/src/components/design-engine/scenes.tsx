/**
 * THE CANVAS — three scenes, one switch, and the provenance that replaced a
 * 48-node diagram.
 *
 * DOCUMENT is a real page wearing the emitted sheet. TOKENS is this file: the
 * flat legend list, with each value's PROVENANCE permanently visible in its own
 * column. GUESTS is two brands meeting on one ground.
 *
 * WHY A LIST AND NOT A GRAPH. The derivation graph was measured at 1114 × 1460
 * px inside a 280 × 711px window — 12.2 % visible, 7 of 48 nodes, ten labels
 * truncated, two nested scroll axes, and identical at 1440 / 1920 / 2560
 * because more screen bought no extra nodes. Four best-in-class design-system
 * surfaces were read; none renders a node-link derivation graph anywhere. What
 * they all do instead is the shape below: swatch, name, value, and what it
 * READS, on one 40px row, scannable, at any width.
 *
 * THE TEN STAGE CAPTIONS LAND HERE. They are the best writing in the product
 * and they used to be tooltips on twelve identical 10px chips that had already
 * finished animating. Grouped by `derivation.stage`, each group's heading
 * carries its caption — permanently, at 14px, describing the values underneath
 * it. That is the same move Geist makes when it documents a type scale by role
 * range in the H2 itself.
 */

import { useMemo } from "react";
import { LightnessStrip } from "./panels";
import { SizeTables, GroundTable } from "./tables";
import type { Derivation, ResolvedGround, ResolvedModel, Stage } from "./types";

export type Scene = "systems" | "document" | "components" | "tokens" | "guests";

export const SCENES: { value: Scene; label: string }[] = [
  /* SYSTEMS IS THE LIBRARY, and it is first because it is the only scene that
     is not about the kit currently open — it is about which kit you want to be
     looking at. The default scene stays DOCUMENT: a builder who arrives with a
     brand in mind should meet it rendered, not meet a list. */
  { value: "systems", label: "Systems" },
  { value: "document", label: "System" },
  /* COMPONENTS SITS BESIDE DOCUMENT, not inside it. They are the same canvas —
     same frame, same ground chips, same rung — asking two different questions:
     DOCUMENT asks whether the sheet alone paints a page, COMPONENTS asks whether
     okuro's own 33 primitives survive contact with this brand. Second in the row
     because a reader meets the page as a document first. */
  { value: "components", label: "Components" },
  { value: "tokens", label: "Mechanics" },
  { value: "guests", label: "Inheritance" },
];

/* --------------------------------------------------------- the blast radius */

/**
 * "What changes if I move this" — a sentence and a filter, not a picture.
 *
 * The graph's one genuinely unique job, answered from the same `graph.edges`
 * data it was drawn from. No diagram, no nested scroll axes, no truncation, and
 * it works at 320px.
 */
export function readersOf(ground: ResolvedGround | null, slot: string): string[] {
  if (!ground) return [];
  const direct = ground.graph.edges
    .filter(([from]) => from === slot)
    .map(([, to]) => to);
  const seen = new Set(direct);
  const queue = [...direct];
  while (queue.length) {
    const id = queue.shift() as string;
    for (const [from, to] of ground.graph.edges) {
      if (from === id && !seen.has(to)) {
        seen.add(to);
        queue.push(to);
      }
    }
  }
  return [...seen];
}

/* ------------------------------------------------------------ the token row */

/**
 * One value: swatch, name, resolved value, WHERE IT CAME FROM, and its state.
 *
 * Column 4 is the whole replacement for the graph. Provenance is read by
 * scanning a list, in the same place the value is read.
 */
function TokenRow({
  derivation,
  flagged,
  onPick,
}: {
  derivation: Derivation;
  flagged: string | null;
  onPick: (slot: string) => void;
}) {
  return (
    <tr
      data-severity={flagged ?? undefined}
    >
      <td>
        <button
          type="button"
          data-token={derivation.slot}
          onClick={() => onPick(derivation.slot)}
          className="ce-token-link"
        >
          <span
            aria-hidden
            className="ce-swatch"
            style={{ background: derivation.swatch ?? "transparent" }}
          />
          <span className="ce-value">{derivation.label}</span>
        </button>
      </td>
      <td className="ce-value" style={{ fontFamily: "var(--font-mono)" }}>
        {derivation.value}
      </td>
      <td className="ce-body ce-2" data-provenance>
        reads {derivation.reads.length ? derivation.reads.join(" · ") : "nothing — it is authored"}
        {" · rule "}
        {derivation.rule}
      </td>
      <td aria-label={flagged ?? "clear"} style={{ color: "var(--ce-severity, var(--ce-fg-2))" }}>
        {flagged === "must-fix" ? "▲" : flagged ? "◆" : "✓"}
      </td>
    </tr>
  );
}

/* ------------------------------------------------------------ TOKENS scene */

export function TokensScene({
  model,
  ground,
  stages,
  onPick,
  filter,
  onFilter,
}: {
  model: ResolvedModel;
  ground: ResolvedGround | null;
  stages: Stage[];
  onPick: (slot: string) => void;
  /* THE BLAST-RADIUS FILTER LIVES ON THE PAGE, because the button that sets it
     is in the inspector — which opens over the canvas from any scene. Holding
     it here would make "[Show them]" reach across two components to a state it
     does not own. */
  filter: string | null;
  onFilter: (slot: string | null) => void;
}) {
  const setFilter = onFilter;

  const captions = useMemo(() => {
    const map: Record<string, Stage> = {};
    for (const stage of stages) map[stage.key] = stage;
    return map;
  }, [stages]);

  const severityBySlot = useMemo(() => {
    const map: Record<string, string> = {};
    for (const flag of [...(ground?.palette.flags ?? []), ...model.flags]) {
      for (const render of flag.renders) {
        map[render] = flag.severity ?? "note";
      }
    }
    return map;
  }, [ground, model.flags]);

  const shown = useMemo(() => {
    /* ONE ROW PER TOKEN. A ground's derivation list can name the same slot
       twice — `solid` and `highlight_neutral_background` both arrive from two
       rules — and printing it twice is the duplication this page was rejected
       for. It also broke React's keys, which left orphaned rows behind after a
       filter: the header counted 14 and the DOM held 25. */
    const seen = new Set<string>();
    const all = (ground?.derivations ?? []).filter((d) => {
      if (seen.has(d.slot)) return false;
      seen.add(d.slot);
      return true;
    });
    if (!filter) return all;
    const reach = new Set(readersOf(ground, filter));
    return all.filter((d) => reach.has(d.slot));
  }, [ground, filter]);

  const grouped = useMemo(() => {
    const out: { stage: string; rows: Derivation[] }[] = [];
    for (const row of shown) {
      const stage = row.stage ?? "other";
      const last = out[out.length - 1];
      if (last && last.stage === stage) last.rows.push(row);
      else out.push({ stage, rows: [row] });
    }
    return out;
  }, [shown]);

  const strip = useMemo(
    () =>
      model.authored
        .filter((input) => input.swatch && input.lightness != null)
        .map((input) => ({
          label: input.label.replace("signal ", "").replace("brand ", ""),
          colour: input.swatch as string,
          lightness: input.lightness as number,
        })),
    [model.authored],
  );

  return (
    <div className="ce-scene" data-scene="tokens">
      <header className="ce-scene-intro">
        <div className="ce-stack-tight">
          <span className="ce-kicker">System mechanics / resolved live</span>
          <h2 className="ce-display">One input. One rule. Every consequence.</h2>
          <p className="ce-body ce-2" style={{ maxWidth: "var(--ce-measure)" }}>
            This is the executable specification: authored facts enter once,
            the engine resolves a ground, and the published vocabulary drives
            every component without a local colour branch.
          </p>
        </div>
      </header>

      <div className="ce-flow-grid ce-flow-grid-four" aria-label="system resolution pipeline">
        {[
          ["01 · Authored", "Identity", "Colours, type, borders, radius, effects, motion and default rungs."],
          ["02 · Calculated", "Ground", "The resolved background crosses the 0.675 polarity threshold."],
          ["03 · Inherited", "Vocabulary", "Foreground, ladders, borders, states and emphasis descend by context."],
          ["04 · Consumed", "Interface", "Components read semantic roles; they do not invent a second decision."],
        ].map(([step, title, body]) => (
          <article key={step} className="ce-flow-step">
            <span className="ce-kicker">{step}</span>
            <h3 className="ce-h2">{title}</h3>
            <p className="ce-body ce-2">{body}</p>
          </article>
        ))}
      </div>

      {filter ? (
        <div className="ce-row" data-filter={filter}>
          <span className="ce-h2">
            {shown.length} value{shown.length === 1 ? "" : "s"} that read {filter}
          </span>
          <button type="button" className="ce-chip" onClick={() => setFilter(null)}>
            Clear
          </button>
        </div>
      ) : (
        <div className="ce-stack-tight">
          <h2 className="ce-h2">
            Every value on {ground?.origin ?? "this ground"}, and what it read
          </h2>
          <p className="ce-body ce-2" style={{ maxWidth: "var(--ce-measure)" }}>
            Nothing in this list was authored except the colours the first group
            names. Each row says which values its own was computed from, so the
            chain is read by scanning rather than by tracing a diagram.
          </p>
          {/* THE LAST COLUMN, NAMED. It shipped as a bare glyph at the end of a
              1000px row with no header and no legend, which asks a reader to
              infer a vocabulary from three shapes. */}
          <p className="ce-micro ce-2" data-token-legend>
            Last column · ✓ nothing flagged · ◆ worth deciding · ▲ must fix
          </p>
        </div>
      )}

      {grouped.map((group, index) => (
        /* THE KEY CARRIES THE INDEX, and that is a correctness fix rather than
           a lint appeasement. Groups are runs of CONSECUTIVE rows sharing a
           stage, so one stage can open two groups — `descent` and `components`
           both do — and two siblings with the same key leave React unable to
           reconcile: applying the blast-radius filter printed "14 values" in
           the header while 25 orphaned rows stayed in the DOM underneath it. */
        <section
          key={`${group.stage}-${index}`}
          className="ce-stack-tight"
          data-token-group={group.stage}
        >
          <h3 className="ce-h2">{captions[group.stage]?.title ?? group.stage}</h3>
          {captions[group.stage] && (
            <p className="ce-body ce-2" style={{ maxWidth: "var(--ce-measure)" }}>
              {captions[group.stage]?.caption}
            </p>
          )}
          <div className="ce-table-wrap">
            <table className="ce-table ce-token-table">
              <thead>
                <tr>
                  <th>Role</th>
                  <th>Resolved value</th>
                  <th>Provenance</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {group.rows.map((row) => (
                  <TokenRow
                    key={`${group.stage}:${row.slot}`}
                    derivation={row}
                    flagged={severityBySlot[row.slot] ?? null}
                    onPick={(slot) => {
                      onPick(slot);
                      setFilter(null);
                    }}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}

      {!filter && (
        <>
          <hr className="ce-hairline" />
          <LightnessStrip
            points={strip}
            threshold={model.threshold}
            warnBand={model.warn_band}
          />
        </>
      )}

      {/* THE NUMBERS, as the utility foot of this scene rather than a peer of
          the picture. They matter for checking a number, not for understanding
          the system — and at full canvas width all 21 columns fit. */}
      <NumbersFoot model={model} ground={ground} />
    </div>
  );
}

/**
 * The utility foot: a 48px bar that expands to the full tables.
 *
 * They used to be a 21-column and a 16-column table clipped to 9 and 4 columns
 * at 10px inside a 280px rail — present, unreadable, and impossible to widen.
 */
function NumbersFoot({
  model,
  ground,
}: {
  model: ResolvedModel;
  ground: ResolvedGround | null;
}) {
  return (
    <section data-numbers-foot data-open="true" className="ce-stack">
      <button
        type="button"
        className="ce-row"
        aria-expanded="true"
        onClick={(event) => event.currentTarget.nextElementSibling?.scrollIntoView({ block: "start" })}
        style={{
          width: "100%",
          height: 48,
          gap: 8,
          padding: "0 16px",
          background: "var(--ce-surface-2)",
          border: "1px solid var(--ce-border)",
          borderRadius: "var(--ce-r-surface)",
          color: "inherit",
          cursor: "pointer",
        }}
      >
        <span aria-hidden className="ce-body ce-2" style={{ width: 12 }}>↓</span>
        <span className="ce-h2">Computed reference tables</span>
        <span className="ce-body ce-2" style={{ marginLeft: "auto" }}>
          {model.sizes.text_rungs.length} rungs × {model.sizes.text_styles.length} styles
        </span>
      </button>
      <div className="ce-stack">
        <SizeTables sizes={model.sizes} />
        {ground && <GroundTable ground={ground} />}
      </div>
    </section>
  );
}
