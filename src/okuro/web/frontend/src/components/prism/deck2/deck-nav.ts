// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism deck v2 — PURE navigation state machine (PRISM v4 §4 +
//   v4.1 §D, axis FROZEN). TOPICS DOWN (↑/↓ = prev/next topic at the SAME depth) ·
//   DEPTH RIGHT (→ deeper; at the deepest level → wraps to the next topic's
//   shallowest; ← shallower; at the shallowest → previous topic's deepest, or the
//   hero at topic 0). The last topic's deepest + → is the TERMINAL (no silent loop
//   — the caller shows an "End of deck" toast). Extracted from the runtime so the
//   axis model + wrap + zero-dead-key boundaries are unit-testable in isolation.
// index: navigate | jumpToTopic | firstCoord | lastCoord | positionLabel
// AGENT_HEADER_END -->
import type { DeckDoc, DeckLevel, DeckTopic, GridCoord, GridRow } from "./deck-types";
import { DECK_LEVELS, hasCell, levelDisplayNum } from "./deck-types";

/** Which visual axis a move animates: "x" = depth (→/←), "y" = topic (↑/↓). */
export type NavAxis = "x" | "y" | null;
/** A non-silent boundary the caller surfaces (toast/flash) — never a dead no-op. */
export type NavEvent = "terminal" | "top" | "bottom" | "exit" | null;
export interface NavResult { coord: GridCoord; axis: NavAxis; event: NavEvent }
export type NavDir = "up" | "down" | "left" | "right";

export function isLevel(row: GridRow): row is DeckLevel {
  return row === "L0" || row === "L1" || row === "L2" || row === "L3";
}
/** Levels a topic actually has, shallow → deep. */
export function existingLevels(topic: DeckTopic): DeckLevel[] {
  return DECK_LEVELS.filter((l) => hasCell(topic, l));
}
function firstLevel(topic: DeckTopic): DeckLevel | null {
  return existingLevels(topic)[0] ?? null;
}
function lastLevel(topic: DeckTopic): DeckLevel | null {
  const ex = existingLevels(topic);
  return ex[ex.length - 1] ?? null;
}
/** Snap a target level to the nearest the topic actually has (tie → shallower). */
export function nearestLevel(topic: DeckTopic, level: DeckLevel): DeckLevel | null {
  if (hasCell(topic, level)) return level;
  const ex = existingLevels(topic);
  if (!ex.length) return null;
  const li = DECK_LEVELS.indexOf(level);
  let best = ex[0]!;
  let bestD = Infinity;
  for (const l of ex) {
    const d = Math.abs(DECK_LEVELS.indexOf(l) - li);
    if (d < bestD) { bestD = d; best = l; }
  }
  return best;
}

/** The first content coordinate (topic 0, shallowest) — where hero → enters. */
export function firstCoord(deck: DeckDoc): GridCoord {
  const t0 = deck.topics[0];
  const lvl = t0 ? firstLevel(t0) : null;
  return lvl ? { row: lvl, col: 0 } : { row: "index", col: 0 };
}
/** The last topic at its shallowest level — where End lands. */
export function lastCoord(deck: DeckDoc): GridCoord {
  const i = deck.topics.length - 1;
  const t = deck.topics[i];
  const lvl = t ? firstLevel(t) : null;
  return lvl && i >= 0 ? { row: lvl, col: i } : { row: "hero", col: 0 };
}

/**
 * The one navigation transition. Pure: (deck, coord, dir) → (coord, axis, event).
 * `event` is set on every boundary so the caller can give feedback — there is no
 * silent no-op anywhere in the grammar.
 */
export function navigate(deck: DeckDoc, c: GridCoord, dir: NavDir): NavResult {
  const N = deck.topics.length;

  // Hero / index are deck-level entries above the topic grid.
  if (c.row === "hero" || c.row === "index") {
    if (dir === "right" || dir === "down") {
      return { coord: firstCoord(deck), axis: dir === "right" ? "x" : "y", event: null };
    }
    // ↑/← at the very top of the deck — a real edge, announced not swallowed.
    return { coord: c, axis: null, event: "top" };
  }

  const t = c.col;
  const topic = deck.topics[t];
  if (!topic || !isLevel(c.row)) return { coord: c, axis: null, event: null };
  const ex = existingLevels(topic);
  const li = ex.indexOf(c.row);

  if (dir === "up") {                                   // prev topic, same depth
    if (t <= 0) return { coord: c, axis: null, event: "top" };
    const prev = deck.topics[t - 1]!;
    const lvl = nearestLevel(prev, c.row) ?? firstLevel(prev) ?? c.row;
    return { coord: { row: lvl, col: t - 1 }, axis: "y", event: null };
  }
  if (dir === "down") {                                 // next topic, same depth
    if (t >= N - 1) return { coord: c, axis: null, event: "bottom" };
    const next = deck.topics[t + 1]!;
    const lvl = nearestLevel(next, c.row) ?? firstLevel(next) ?? c.row;
    return { coord: { row: lvl, col: t + 1 }, axis: "y", event: null };
  }
  if (dir === "right") {                                // deeper
    if (li >= 0 && li < ex.length - 1) {
      return { coord: { row: ex[li + 1]!, col: t }, axis: "x", event: null };
    }
    if (t < N - 1) {                                    // wrap → next topic shallowest
      const lvl = firstLevel(deck.topics[t + 1]!);
      if (lvl) return { coord: { row: lvl, col: t + 1 }, axis: "x", event: null };
    }
    return { coord: c, axis: null, event: "terminal" }; // last topic, deepest
  }
  // dir === "left" — shallower
  if (li > 0) return { coord: { row: ex[li - 1]!, col: t }, axis: "x", event: null };
  if (t > 0) {                                          // wrap → prev topic deepest
    const lvl = lastLevel(deck.topics[t - 1]!);
    if (lvl) return { coord: { row: lvl, col: t - 1 }, axis: "x", event: null };
  }
  return { coord: { row: "hero", col: 0 }, axis: "x", event: "exit" }; // back out to cover
}

/** Number-key jump: topic N at the CURRENT depth (frozen R25), snapped to a level
 *  the target topic has. From hero/index, lands at the topic's shallowest. */
export function jumpToTopic(deck: DeckDoc, c: GridCoord, topicIndex: number): GridCoord | null {
  const topic = deck.topics[topicIndex];
  if (!topic) return null;
  const cur = isLevel(c.row) ? c.row : null;
  const lvl = (cur && nearestLevel(topic, cur)) || firstLevel(topic);
  return lvl ? { row: lvl, col: topicIndex } : { row: "index", col: topicIndex };
}

export interface Position { topic: number; topics: number; level: number; levels: number }
/** 1-indexed "topic X/N · L k/4" position for the always-visible counter. */
export function positionLabel(deck: DeckDoc, c: GridCoord): Position | null {
  if (!isLevel(c.row)) return null;
  return { topic: c.col + 1, topics: deck.topics.length, level: levelDisplayNum(c.row), levels: 4 };
}
