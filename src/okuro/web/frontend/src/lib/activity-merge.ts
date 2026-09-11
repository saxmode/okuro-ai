import type { ActivityEvent } from "@/types/api";

/**
 * Phase 4 — activity fidelity + scale.
 *
 * The task-detail feed merges two sources: the polled `/activity` list
 * (authoritative history) and the live WS tail. On every reconnect the
 * polled query is invalidated + refetched, so the events that already
 * arrived live are now present in BOTH lists → the feed duplicated every
 * row on each reconnect (no dedupe key). Activity events carry no id/seq,
 * so identity is a composite of the fields that uniquely pin one emission.
 *
 * mergeActivity dedupes (keeping the first — i.e. the polled/historical —
 * occurrence), orders by ts ascending (the feed auto-scrolls to the newest
 * at the bottom), and bounds the total with an EXPLICIT hidden-older count
 * so a very long stream never silently drops its middle.
 */

export function activityKey(ev: ActivityEvent): string {
  const e = ev as ActivityEvent & { name?: string; text?: string };
  // ts is ISO with sub-second precision; the same emission from poll vs WS
  // is byte-identical, so this composite collides only for true duplicates.
  const text = (e.text ?? e.name ?? "").slice(0, 64);
  return [
    ev.ts ?? "",
    ev.type ?? "",
    ev.subtask_id ?? "",
    e.role ?? "",
    text,
  ].join("");
}

export interface MergedActivity {
  events: ActivityEvent[];
  /** How many older events were dropped by the `max` bound (0 when none). */
  hiddenOlder: number;
}

export function mergeActivity(
  polled: ActivityEvent[] | undefined,
  ws: ActivityEvent[] | undefined,
  max = 2000,
): MergedActivity {
  const seen = new Set<string>();
  const deduped: ActivityEvent[] = [];
  for (const ev of [...(polled ?? []), ...(ws ?? [])]) {
    const k = activityKey(ev);
    if (seen.has(k)) continue;
    seen.add(k);
    deduped.push(ev);
  }

  // Stable sort by ts ascending. Array.prototype.sort is stable, so events
  // sharing a ts keep insertion order (polled before ws → history first).
  deduped.sort((a, b) => (a.ts ?? "").localeCompare(b.ts ?? ""));

  if (deduped.length <= max) {
    return { events: deduped, hiddenOlder: 0 };
  }
  // Keep the most recent `max` — the feed shows newest at the bottom, so
  // the OLDEST are the ones to drop, and we report exactly how many.
  const hiddenOlder = deduped.length - max;
  return { events: deduped.slice(hiddenOlder), hiddenOlder };
}
