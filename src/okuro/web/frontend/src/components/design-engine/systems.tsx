/**
 * THE LIBRARY — every design system this okuro can open, on one screen.
 *
 * The owner, 2026-09-03: *"we need to make sure, that a user sees all the design
 * systems in an overview, knows that the okuro default cannot be changed (only
 * doublicated and adjusted)."*
 *
 * Both halves of that sentence are the design. The list is the easy half; the
 * hard half is that a shipped kit has to REFUSE legibly. Before this screen the
 * refusal existed only as a 409 from `PUT /kits/okuro-ds` — a builder learned
 * that okuro's own system is read-only by editing it, pressing Save, and
 * reading an error. The badge says it before the first keystroke, and the FORK
 * button sits in the same row so the sentence finishes itself: not editable,
 * here is what to do instead.
 *
 * WHAT REPLACED WHAT. The only kit list that existed was a row of `id · origin`
 * chips buried inside the "Start a new brand" sheet, under a hairline reading
 * "Or open one that exists" — a library as a footnote to a creation form. It is
 * kept there, because opening a kit while you are deciding whether to author one
 * is a real move; this screen is the one you go to when the library IS the
 * question.
 *
 * A ROW IS A SWATCH, A NAME AND A TYPEFACE. Those are the three things a design
 * system is recognised by on sight, and an id alone is not one of them — four
 * rows of slugs say nothing about which one is the yellow one. They arrive on
 * `GET /kits`, resolved server-side, so the screen paints once rather than
 * fanning out a request per row.
 *
 * NO `window.confirm` ANYWHERE. A native dialog blocks the page's event loop,
 * which takes every Playwright gate in this suite with it. The delete
 * confirmation is a second press on the same button — the destructive act still
 * costs two deliberate gestures, and the page stays a page.
 */

import { useState } from "react";

import type { KitRow } from "./types";

export interface SystemsSceneProps {
  kits: KitRow[];
  /** The kit `/engine.css` is painting the app with, from the server. */
  active: string | null;
  /** The kit the EDITOR currently has open — not the same question as `active`. */
  opened: string | null;
  busy: boolean;
  onOpen: (id: string) => void;
  onFork: (id: string) => void;
  onDelete: (id: string) => void;
  /** Start a kit from a website. THE SECOND WAY TO DUPLICATE, and it sits in
      the header rather than in a row because it forks the shipped base rather
      than any listed system. It moved here from Settings on 2026-09-06 — see
      scan-kit-dialog.tsx for why that screen kept only the choice. */
  onScan: () => void;
}

export function SystemsScene({
  kits,
  active,
  opened,
  busy,
  onOpen,
  onFork,
  onDelete,
  onScan,
}: SystemsSceneProps) {
  /* The id awaiting a second press. One at a time: arming a second row
     disarms the first, so there is never more than one loaded gun. */
  const [armed, setArmed] = useState<string | null>(null);

  return (
    <section className="ce-scene" data-scene-body="systems">
      <header className="ce-scene-intro">
        <div className="ce-stack-tight">
          <span className="ce-kicker">System library / {kits.length} available</span>
          <h2 className="ce-display">One source for every product surface.</h2>
          <p className="ce-body ce-2" style={{ maxWidth: "var(--ce-measure)" }}>
            Open a system to inspect or author it. The shipped base is immutable:
            duplicate it, edit the copy, then choose that copy in Settings to
            paint the application.
          </p>
        </div>
        <button
          type="button"
          className="ce-primary"
          data-action="scan"
          disabled={busy}
          onClick={onScan}
        >
          Duplicate from a website
        </button>
      </header>

      <div className="ce-metric-band" aria-label="library summary">
        <div><strong>{kits.length}</strong><span>systems</span></div>
        <div><strong>{active ?? "none"}</strong><span>painting okuro</span></div>
        <div><strong>1 base</strong><span>immutable by design</span></div>
      </div>

      <section className="ce-stack-tight" aria-labelledby="systems-table-title">
        <div className="ce-section-heading">
          <div>
            <span className="ce-kicker">Portfolio</span>
            <h3 id="systems-table-title" className="ce-title">Available systems</h3>
          </div>
          <span className="ce-body ce-2">Settings chooses. The engine authors.</span>
        </div>
        <div className="ce-table-wrap" data-systems-list>
          <table className="ce-table">
            <thead>
              <tr>
                <th>Identity</th>
                <th>Typeface</th>
                <th>Origin</th>
                <th>Status</th>
                <th><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
        {kits.map((kit) => {
          const isActive = kit.id === active;
          const isOpen = kit.id === opened;
          const isArmed = armed === kit.id;
          return (
            <tr
              key={kit.id}
              data-system-row={kit.id}
              data-origin={kit.origin}
              data-active={isActive ? "true" : "false"}
              data-open={isOpen ? "true" : undefined}
            >
              <td>
                <span className="ce-identity-cell">
                  <span
                    className="ce-swatch"
                    data-swatch={kit.id}
                    aria-hidden="true"
                    style={{ background: kit.colour ?? "transparent" }}
                  />
                  <span className="ce-stack-tight" style={{ minWidth: 0 }}>
                    <span className="ce-value" data-system-name>{kit.id}</span>
                    <span className="ce-micro ce-2">{kit.colour ?? "unresolved"}</span>
                  </span>
                </span>
              </td>
              <td className="ce-value">{kit.font ?? "—"}</td>
              <td><span className="ce-chip" style={{ cursor: "default" }}>{kit.origin}</span></td>
              <td>
                <span className="ce-stack-tight">
                  {kit.unreadable ? (
                    <span data-system-broken style={{ color: "var(--ce-danger)" }}>
                      ▲ cannot parse
                    </span>
                  ) : (
                    <span
                      className="ce-micro ce-2"
                      data-badge={kit.editable ? "yours" : "shipped"}
                    >
                      {kit.editable ? "editable copy" : "shipped · immutable · not editable"}
                    </span>
                  )}
                  {isActive && <span className="ce-micro" data-badge="active" style={{ color: "var(--ce-positive)" }}>● painting the app</span>}
                  {isOpen && <span className="ce-micro">◆ open in engine</span>}
                </span>
              </td>
              <td><div className="ce-table-actions">
              <button
                type="button"
                className="ce-chip"
                data-action="open"
                data-kit={kit.id}
                disabled={busy || Boolean(kit.unreadable)}
                onClick={() => onOpen(kit.id)}
              >
                {kit.editable ? "Edit" : "View"}
              </button>
              <button
                type="button"
                className="ce-chip"
                data-action="fork"
                data-kit={kit.id}
                disabled={busy || Boolean(kit.unreadable)}
                onClick={() => onFork(kit.id)}
              >
                Duplicate
              </button>
              {/* DELETE EXISTS ONLY WHERE IT CAN SUCCEED. The route refuses a
                  shipped id with a 409, and a button whose only outcome is an
                  error is worse than no button — it teaches that the refusal is
                  a malfunction. The badge above already says why it is absent. */}
              {kit.editable && (
                <button
                  type="button"
                  className="ce-chip"
                  data-action={isArmed ? "delete-confirm" : "delete"}
                  data-kit={kit.id}
                  disabled={busy}
                  aria-label={isArmed ? `Confirm deleting ${kit.id}` : `Delete ${kit.id}`}
                  style={isArmed ? { color: "var(--ce-danger)", borderColor: "var(--ce-danger)" } : undefined}
                  onClick={() => {
                    if (isArmed) {
                      setArmed(null);
                      onDelete(kit.id);
                    } else {
                      setArmed(kit.id);
                    }
                  }}
                  onBlur={() => setArmed((prev) => (prev === kit.id ? null : prev))}
                >
                  {isArmed ? "Really?" : "Delete"}
                </button>
              )}
              </div></td>
            </tr>
          );
        })}
            </tbody>
          </table>
        </div>
      </section>

      <section className="ce-stack" aria-labelledby="lifecycle-title">
        <div className="ce-section-heading">
          <div>
            <span className="ce-kicker">Governance</span>
            <h3 id="lifecycle-title" className="ce-title">The system lifecycle</h3>
          </div>
          <p className="ce-body ce-2">One safe path; no hidden override of the shipped base.</p>
        </div>
        <div className="ce-flow-grid">
          {[
            ["01", "Duplicate", "Start from okuro-ds or scan a website into a copy."],
            ["02", "Author", "Adjust identity inputs; the engine recomputes every dependent value."],
            ["03", "Activate", "Choose the finished system in Settings to repaint okuro."],
          ].map(([index, title, body]) => (
            <article key={index} className="ce-flow-step">
              <span className="ce-kicker">{index}</span>
              <h4 className="ce-h2">{title}</h4>
              <p className="ce-body ce-2">{body}</p>
            </article>
          ))}
        </div>
      </section>
    </section>
  );
}

export default SystemsScene;
