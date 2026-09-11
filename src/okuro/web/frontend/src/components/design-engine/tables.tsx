/**
 * The numeric tables — the utility foot of the TOKENS scene.
 *
 * They matter for CHECKING a number, not for understanding the system, so they
 * sit behind one bar at the bottom of a scene rather than beside the thing they
 * are about. What changed is where that bar is: in a 280px rail the 21-column
 * text table was clipped to nine columns and the 16-column component table to
 * four, both at 10px, with no way to widen either. At full canvas width all 21
 * fit at 1920, and the rung column and the header row stay put while the rest
 * scrolls.
 *
 * Every cell here is computed off the grid: there is no stored value list
 * anywhere in the engine, which is why changing the grid base moves this whole
 * page at once.
 */

import type { ResolvedGround, Sizes } from "./types";

/**
 * A size, as a label.
 *
 * `base × rung_factor × ratio` is a product of binary floats, so `8 × 2.3 × 1`
 * is genuinely 18.400000000000002 and the engine is right to hand that over
 * unrounded — nothing in the system snaps any more, and a value the engine
 * rounded would no longer be exactly proportional in the base.
 *
 * But 18.400000000000002 and 18.4 are the same number, and printing the noise
 * makes the system look imprecise where it is in fact exact. So the rounding
 * happens HERE, on the way out, and never touches what is stored, computed or
 * emitted.
 */
function label(value: number): string {
  return `${Number(value.toFixed(4))}`;
}

const CELL: React.CSSProperties = {
  border: "1px solid var(--ce-border)",
  padding: "4px 8px",
  textAlign: "right",
  fontFamily: "var(--font-mono)",
  fontVariantNumeric: "tabular-nums",
  whiteSpace: "nowrap",
};

/** The header row stays at the top of its own scroller. */
const HEAD: React.CSSProperties = {
  ...CELL,
  fontWeight: 500,
  background: "var(--ce-surface-2)",
  position: "sticky",
  top: 0,
  zIndex: 2,
};

/** The rung column stays at the left of its own scroller. */
const RUNG: React.CSSProperties = {
  ...HEAD,
  left: 0,
  zIndex: 3,
  textAlign: "left",
};

function Cell({ children }: { children: number | undefined }) {
  return <td style={CELL}>{children === undefined ? "" : label(children)}</td>;
}

export function SizeTables({ sizes }: { sizes: Sizes }) {
  return (
    <div className="ce-stack">
      <section className="ce-stack-tight">
        <h3 className="ce-h2">
          Text · {sizes.text_rungs.length} rungs × {sizes.text_styles.length} styles
        </h3>
        <div className="ce-scroller ce-micro" style={{ maxHeight: 420, overflowY: "auto" }}>
          <table style={{ borderCollapse: "separate", borderSpacing: 0 }}>
            <thead>
              <tr>
                <th style={{ ...RUNG, zIndex: 4 }}>rung</th>
                {sizes.text_styles.map((style) => (
                  <th key={style} style={HEAD}>
                    {style}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sizes.text_rungs.map((rung) => (
                <tr key={rung}>
                  <th style={{ ...RUNG, position: "sticky", top: "auto" }}>{rung}</th>
                  {sizes.text_styles.map((style) => (
                    <Cell key={style}>{sizes.text[rung]?.[style]}</Cell>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="ce-body ce-2">
          Downscale moves text one rung:{" "}
          {sizes.text_rungs.map((r) => `${r}→${sizes.downscale.text[r]}`).join(" · ")}
        </p>
      </section>

      <section className="ce-stack-tight">
        <h3 className="ce-h2">
          Components · {sizes.component_rungs.length} rungs ×{" "}
          {sizes.component_roles.length} roles
        </h3>
        <div className="ce-scroller ce-micro" style={{ maxHeight: 420, overflowY: "auto" }}>
          <table style={{ borderCollapse: "separate", borderSpacing: 0 }}>
            <thead>
              <tr>
                <th style={{ ...RUNG, zIndex: 4 }}>rung</th>
                {sizes.component_roles.map((role) => (
                  <th key={role} style={HEAD}>
                    {role}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sizes.component_rungs.map((rung) => (
                <tr key={rung}>
                  <th style={{ ...RUNG, position: "sticky", top: "auto" }}>{rung}</th>
                  {sizes.component_roles.map((role) => (
                    <Cell key={role}>{sizes.components[rung]?.[role]}</Cell>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="ce-body ce-2">
          Downscale moves components three rungs, floor-clamped:{" "}
          {sizes.component_rungs
            .map((r) => `${r}→${sizes.downscale.components[r]}`)
            .join(" · ")}
        </p>
      </section>

      {/* There is no grid table here, and there is not meant to be. The ladder
          was deleted with ruling 4's amendment; printing a generated list of
          sizes would put one back in front of a reader as though it were the
          system. What IS the system is one multiplication, so that is shown. */}
      <section className="ce-stack-tight">
        <h3 className="ce-h2">The rule · base {sizes.base} (system constant)</h3>
        <p className="ce-body ce-2" style={{ fontFamily: "var(--font-mono)" }}>
          size = base × factor · {sizes.base} ×{" "}
          {label(sizes.factors.text?.L?.N5 ?? 0)} ={" "}
          {label(sizes.base * (sizes.factors.text?.L?.N5 ?? 0))}px at N5, rung L
          <br />a TEXT cell is an authored factor —{" "}
          {sizes.text_bands
            .map((b) => `${b.name} ${b.letter}1–${b.letter}${b.count}`)
            .join(" · ")}
          <br />a COMPONENT cell = base × rung factor × role ratio · rungs step by{" "}
          {sizes.factors.component_rung_step}
        </p>
        <p className="ce-body ce-2">
          Root {sizes.root_percent}% = {sizes.root_px}px at the browser default,
          and that is the base — so an emitted rem IS the factor, and every size
          scales with the reader's own font size. Radius, on its own authored
          base {sizes.radius_base}:{" "}
          {Object.entries(sizes.radius)
            .map(([k, v]) => (v >= 999 ? `${k} MAX (sentinel)` : `${k} ${label(v)}`))
            .join(" · ")}
          .
        </p>
      </section>
    </div>
  );
}

/** Every value of one ground, as numbers. The canvas shows it; this proves it. */
export function GroundTable({ ground }: { ground: ResolvedGround }) {
  const p = ground.palette;
  const rows: [string, string][] = [
    ["background", p.background ?? "—"],
    ["foreground", p.foreground],
    ["highlight-neutral bg", p.highlight_neutral_background],
    ["highlight-neutral fg", p.highlight_neutral_foreground],
    ["highlight-branded bg", p.highlight_branded_background],
    ["highlight-branded fg", p.highlight_branded_foreground],
    ["separator", p.separator.over ?? p.separator.css],
    ["border-full", p.border_full],
    ["border-half", p.border_half.over ?? p.border_half.css],
    ["border-branded", p.border_branded],
    ["focus", p.focus.over ?? p.focus.css],
    ["solid", p.solid],
    ["solid-inverse", p.solid_inverse ?? "—"],
    ["solid-brand", p.solid_brand],
    ["solid-brand-inverse", p.solid_brand_inverse],
    ["transparent", p.transparent.over ?? p.transparent.css],
    ["blur-background", p.blur_background.over ?? p.blur_background.css],
    ["shadow", `${p.shadow.kind} ${p.shadow.colour} @ ${p.shadow.opacity.toFixed(3)}`],
    ["hover", p.states.hover.over ?? p.states.hover.css],
    ["pressed", p.states.pressed],
    [
      "border widths",
      Object.entries(p.states.border_widths)
        .map(([k, v]) => `${k} ${v}`)
        .join(" · "),
    ],
    [
      "disabled / off",
      `${Math.round(p.disabled * 100)}% / ${Math.round(p.off * 100)}%`,
    ],
  ];

  return (
    <section className="ce-stack-tight">
      <h3 className="ce-h2">
        {ground.key} · L {ground.lightness.toFixed(4)} · {ground.polarity}
        {ground.in_warn_band && " · in the warn band"}
      </h3>
      <div className="ce-scroller ce-micro">
        <table style={{ width: "100%", borderCollapse: "separate", borderSpacing: 0 }}>
          <tbody>
            {rows.map(([name, value]) => (
              <tr key={name}>
                <td style={{ ...CELL, textAlign: "left", color: "var(--ce-fg-2)" }}>
                  {name}
                </td>
                <td style={{ ...CELL, textAlign: "left" }}>{value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="ce-stack-tight">
        {(["alternate", "alternate_inverse"] as const).map((name) => (
          <div key={name}>
            <span className="ce-micro ce-2">
              {name.replace("_", "-")} · {ground.palette[name].ink}
            </span>
            <div style={{ display: "flex", marginTop: 4 }}>
              {ground.palette[name].steps.map((step) => (
                <i
                  key={step.percent}
                  /* `aria-label`, NOT `title`. The step and its colour are what
                     the strip already shows; a hover surface repeating them is
                     the affordance section 7 deleted, and the screen reader
                     still gets the fact. */
                  aria-label={`${step.percent}% → ${step.over}`}
                  style={{
                    display: "block",
                    height: 12,
                    flex: 1,
                    background: step.over ?? undefined,
                  }}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
