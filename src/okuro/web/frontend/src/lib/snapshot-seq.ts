import type { TaskSnapshot } from "@/types/api";

/**
 * Canonical-state Phase 0 — freshness guard.
 *
 * The snapshot reaches the cache from two racing sources: the 8s poll
 * (`useTaskSnapshot`) and the WS `snapshot` push (`use-websocket`). Both
 * carry `seq` (last_updated epoch-ms, stamped by the BE), which ORDERS
 * generations of persisted state. The guard keeps whichever is fresher so
 * a slow poll can never overwrite a newer push — the flicker /
 * backwards-jump class. Snapshots from before the seq field existed report
 * 0, so they never beat a real one but still apply on first paint.
 *
 * seq is NOT a content cursor, whatever its old docstring said: read-time
 * fields move without it. `content_hash` (P3.5) breaks same-seq ties.
 */

export function snapshotSeq(snap: unknown): number {
  if (snap && typeof snap === "object" && "seq" in snap) {
    const s = (snap as { seq?: unknown }).seq;
    if (typeof s === "number" && Number.isFinite(s)) return s;
  }
  return 0;
}

/**
 * Return the fresher of two snapshots by seq. Ties keep `prev` (already
 * applied — no churn). A null/undefined `prev` always yields `next`.
 */
export function contentHash(snap: unknown): string {
  if (snap && typeof snap === "object" && "content_hash" in snap) {
    const h = (snap as { content_hash?: unknown }).content_hash;
    if (typeof h === "string") return h;
  }
  return "";
}

export function pickFresherSnapshot<T extends TaskSnapshot | undefined | null>(
  prev: T,
  next: T,
): T {
  if (prev == null) return next;
  if (next == null) return prev;
  if (snapshotSeq(next) > snapshotSeq(prev)) return next;
  if (snapshotSeq(next) < snapshotSeq(prev)) return prev;

  // SAME seq. `seq` orders generations of persisted state; it does NOT track
  // content, because several snapshot fields are derived at READ time and
  // move while task.yaml sits still — engine_state (the liveness overlay,
  // uncached by design) and blocker.stale (flips at 48 h). Two reads then
  // carry the same seq and different content.
  //
  // This used to be a per-field exemption for engine_state alone, which is
  // why blocker.stale silently did not work: a gate crossing 48 h on an open
  // page never showed its note, because the poll carrying it lost the guard.
  // content_hash (BE, P3.5) covers every field including the ones nobody has
  // written yet. A snapshot with no hash — an older backend — falls back to
  // the previous keep-prev behaviour rather than churning on every poll.
  const nextHash = contentHash(next);
  if (nextHash || contentHash(prev)) {
    // At least one side is hash-bearing — the general mechanism decides.
    return nextHash && nextHash !== contentHash(prev) ? next : prev;
  }

  // NEITHER side carries a hash: a backend older than P3.5. Fall back to the
  // engine_state overlay this used to rely on exclusively, so the liveness
  // banner does not regress on that path. Deliberately kept rather than
  // deleted — it is the one read-time field with a user-visible banner, and
  // "the new mechanism is better" is not a reason to break the old one for a
  // client that cannot use it yet.
  if (
    next.engine_state !== undefined &&
    next.engine_state !== prev.engine_state
  ) {
    return { ...prev, engine_state: next.engine_state };
  }
  return prev;
}
