/**
 * What survived the right rail.
 *
 * `DerivationGraph` IS DELETED, and this is the one deletion worth stating in
 * full. Measured: 1114 × 1460px of diagram inside a 280 × 711px window — 12.2 %
 * visible, 7 of 48 nodes, 10 labels truncated (worst: 366px of text into a
 * 152px box), two nested scroll axes, and IDENTICAL at 1440 / 1920 / 2560,
 * because +1120px of screen bought zero extra nodes. Seven grounds would be 336
 * nodes. Researched across four best-in-class design-system surfaces: none of
 * them renders a node-link derivation graph anywhere — not in a panel, not
 * inline, not behind a toggle.
 *
 * Its job was real and it is REASSIGNED rather than dropped:
 *
 *   "why is this value what it is"      -> `inspector.tsx`, anchored where the
 *                                          value was clicked, from every scene
 *   "where did this token come from"    -> the TOKENS legend row, permanently
 *                                          visible provenance
 *   "what changes if I move this"       -> a filtered list and a sentence, from
 *                                          the same `graph.edges` data
 *
 * What is left here is the lightness strip and one lookup.
 */

import { useLayoutEffect, useRef, useState } from "react";
import type { Derivation, ResolvedGround } from "./types";

/* ------------------------------------------------- the lightness strip */

interface Point {
  label: string;
  colour: string;
  lightness: number;
}

const ROW = 20;
const TOP = 34;

/**
 * Where every authored colour sits against the ONE threshold.
 *
 * One number decides the whole system, so it is worth seeing how close each
 * colour is to flipping. Three things changed. It has a TITLE and a LEGEND, so
 * a reader who has never seen it knows what the threshold line is. It sits in the
 * TOKENS scene, at full canvas width, instead of in a 280px slot where its
 * labels landed on top of each other. And the rows are assigned by MEASURING
 * the real label boxes rather than by `i % 4` — the modulo staggered four rows
 * whether or not anything overlapped, which both wasted 60px of height and
 * still collided whenever five colours clustered.
 */
export function LightnessStrip({
  points,
  threshold,
  warnBand,
}: {
  points: Point[];
  threshold: number;
  warnBand: [number, number];
}) {
  const host = useRef<HTMLDivElement>(null);
  const [rows, setRows] = useState<number[]>([]);

  /* MEASURED COLLISION AVOIDANCE. Each label is placed on the lowest row where
     its real box does not overlap a box already on that row. A label that has
     the strip to itself stays on row 0, which is what makes the common case one
     line tall instead of four. */
  useLayoutEffect(() => {
    const node = host.current;
    if (!node) return;
    const labels = Array.from(
      node.querySelectorAll<HTMLElement>("[data-point]"),
    );
    const placed: DOMRect[][] = [];
    const next = labels.map((el) => {
      const box = el.getBoundingClientRect();
      for (let row = 0; row < placed.length; row += 1) {
        const clash = (placed[row] as DOMRect[]).some(
          (other) => box.left < other.right + 8 && other.left < box.right + 8,
        );
        if (!clash) {
          (placed[row] as DOMRect[]).push(box);
          return row;
        }
      }
      placed.push([box]);
      return placed.length - 1;
    });
    setRows((prev) =>
      prev.length === next.length && prev.every((v, i) => v === next[i]) ? prev : next,
    );
  }, [points]);

  const depth = (rows.length ? Math.max(...rows) : 0) + 1;

  return (
    <section className="ce-stack-tight" data-lightness-strip>
      <h3 className="ce-h2">Every authored colour, against the one threshold</h3>
      <p className="ce-body ce-2">
        A ground below {threshold} is dark and takes the brand's white; above it
        is light and takes the brand's black. The shaded band is where the two
        are close enough that the engine flags its own answer.
      </p>
      <div
        ref={host}
        className="ce-surface"
        style={{ position: "relative", height: TOP + depth * ROW + 8 }}
      >
        <div
          aria-hidden
          style={{
            position: "absolute",
            inset: "0 auto",
            top: 0,
            bottom: 0,
            left: `${warnBand[0] * 100}%`,
            width: `${(warnBand[1] - warnBand[0]) * 100}%`,
            background: "var(--ce-notice)",
            opacity: 0.12,
          }}
        />
        <div
          style={{
            position: "absolute",
            top: 0,
            bottom: 0,
            width: 1,
            background: "var(--ce-fg)",
            left: `${threshold * 100}%`,
          }}
        >
          <span
            className="ce-micro"
            style={{ position: "absolute", top: 4, left: 6, whiteSpace: "nowrap" }}
          >
            {threshold}
          </span>
        </div>
        {points.map((point, i) => (
          <div
            key={`${point.label}-${i}`}
            data-point={point.label}
            className="ce-row"
            style={{
              position: "absolute",
              gap: 4,
              left: `${point.lightness * 100}%`,
              top: TOP + (rows[i] ?? 0) * ROW,
              transform:
                point.lightness > 0.88
                  ? "translateX(-100%)"
                  : point.lightness < 0.12
                    ? "none"
                    : "translateX(-50%)",
              whiteSpace: "nowrap",
            }}
          >
            <span
              className="ce-swatch"
              style={{ background: point.colour, width: 12, height: 12 }}
            />
            <span className="ce-micro ce-2">
              {point.label} · {point.lightness.toFixed(3)}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ lookup */

export function derivationBySlot(
  ground: ResolvedGround | null,
  slot: string | null,
): Derivation | null {
  if (!ground || !slot) return null;
  return ground.derivations.find((d) => d.slot === slot) ?? null;
}
