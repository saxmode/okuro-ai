// <!-- AGENT_HEADER
// role: code
// purpose: Ambient handover source registry + a global "open handover" bus, so
//   hand-over becomes always-available (Cmd+K / anywhere) instead of a per-tool
//   button. The open page PASSIVELY declares how to build a ContentIR from its
//   current selection; the command palette reads it and opens one shared dialog.
// AGENT_HEADER_END -->
import type { ContentIR } from "@/lib/handover-api";

type Builder = () => ContentIR | null;

// The current page's source builder (top of a tiny stack — last registrant wins,
// which matches "the visible tool owns the selection").
let builder: Builder | null = null;

/** A page declares its handover source. Returns an unregister fn — call it on
 *  unmount / when the page loses ownership. */
export function registerHandoverSource(fn: Builder): () => void {
  builder = fn;
  return () => {
    if (builder === fn) builder = null;
  };
}

/** Build an IR from whatever is currently selected, or null when nothing here
 *  can be handed over. Never throws. */
export function getHandoverIR(): ContentIR | null {
  try {
    return builder ? builder() : null;
  } catch {
    return null;
  }
}

// ── L2 snapshot context: opt-in richer page data for the snapshot fallback ────
//
// A page passively declares the entity/data behind the current view (e.g. the
// person record already in the react-query cache), so a captured snapshot hands
// over the DATA, not just the pixels. Absent → the snapshot still works with the
// generic L3 baseline (route + visible text). Mirrors registerHandoverSource.

type SnapshotContext = Record<string, unknown>;
type SnapshotBuilder = () => SnapshotContext | null;

let snapshotBuilder: SnapshotBuilder | null = null;

/** A page declares its rich snapshot context. Returns an unregister fn — call it
 *  on unmount / when the page loses ownership. */
export function registerSnapshotContext(fn: SnapshotBuilder): () => void {
  snapshotBuilder = fn;
  return () => {
    if (snapshotBuilder === fn) snapshotBuilder = null;
  };
}

/** The current page's rich snapshot context, or {} when none is registered.
 *  Never throws. */
export function getSnapshotContext(): SnapshotContext {
  try {
    return (snapshotBuilder ? snapshotBuilder() : null) || {};
  } catch {
    return {};
  }
}

// ── open bus: the palette triggers the one shared dialog hosted in the shell ──

type OpenListener = (ir: ContentIR) => void;
let openListener: OpenListener | null = null;

export function onOpenHandover(fn: OpenListener): () => void {
  openListener = fn;
  return () => {
    if (openListener === fn) openListener = null;
  };
}

export function openHandover(ir: ContentIR): void {
  openListener?.(ir);
}
