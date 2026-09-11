// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism deck v2 — PERSISTENT orientation mini-map (PRISM v4 §4:
//   "persistent mini-map, current cell lit"). A compact topic×depth DOT MATRIX
//   docked on the deck at all times (de-modalises the old `g`-only grid): rows =
//   topics DOWN, columns = depth L1→L4 RIGHT (axis-true), the current cell lit,
//   every filled dot clickable. This is the always-on answer to "where am I?";
//   the full thumbnail matrix stays behind `g`.
// AGENT_HEADER_END -->
import { Fragment } from "react";
import type { DeckDoc, GridCoord } from "./deck-types";
import { DECK_LEVELS, DECK_LEVEL_LABELS, hasCell } from "./deck-types";
import { isLevel } from "./deck-nav";

interface DeckMiniMapProps {
  deck: DeckDoc;
  current: GridCoord;
  onNavigate: (c: GridCoord) => void;
}

export function DeckMiniMap({ deck, current, onNavigate }: DeckMiniMapProps) {
  const onLevel = isLevel(current.row);
  return (
    <div className="deck-minimap" data-testid="deck-minimap" role="group" aria-label="Deck position map — topics down, depth right">
      <div className="mm-grid" style={{ gridTemplateColumns: `14px repeat(${DECK_LEVELS.length}, 12px)` }}>
        <div className="mm-corner" aria-hidden />
        {DECK_LEVELS.map((l) => (
          <div key={l} className="mm-collabel" aria-hidden>{DECK_LEVEL_LABELS[l].replace("L", "")}</div>
        ))}
        {deck.topics.map((t, ti) => (
          <Fragment key={t.id}>
            <div className="mm-rowlabel" title={t.title}>{ti + 1}</div>
            {DECK_LEVELS.map((l) => {
              const has = hasCell(t, l);
              const active = onLevel && current.col === ti && current.row === l;
              return (
                <button
                  key={l}
                  type="button"
                  className={`mm-dot${has ? "" : " empty"}${active ? " active" : ""}`}
                  data-testid={active ? "mm-dot-active" : undefined}
                  disabled={!has}
                  aria-current={active ? "true" : undefined}
                  title={`${t.title} · ${DECK_LEVEL_LABELS[l]}`}
                  onClick={() => has && onNavigate({ row: l, col: ti })}
                />
              );
            })}
          </Fragment>
        ))}
      </div>
    </div>
  );
}
