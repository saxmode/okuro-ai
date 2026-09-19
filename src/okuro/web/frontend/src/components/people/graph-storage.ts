/**
 * Graph layout persistence — what the user has arranged on the People graph.
 *
 * TWO TIERS. localStorage (`okuro.people-graph.*`) is a SYNCHRONOUS CACHE;
 * `/api/people/layout` is the source of truth. The cache exists because
 * PeopleGraph.tsx reads this module from `useState`/`useRef` initialisers
 * and from inside a requestAnimationFrame loop — both places where an
 * async read is impossible. So every read* stays synchronous and every
 * write* stays fire-and-forget, exactly as before; the server sync happens
 * around them.
 *
 * READ PATH. `hydrateGraphLayout()` must be awaited BEFORE PeopleGraph
 * mounts (people.tsx gates on it). It pulls the server copy into
 * localStorage, so the component's synchronous initialisers then read
 * already-correct data and no component-internal change is needed.
 *
 * WRITE PATH. write* updates localStorage immediately and schedules a
 * debounced PUT of the complete document. The debounce is not a nicety:
 * the spring simulation calls writePositions() every 30 RAF ticks while
 * nodes settle, so an un-debounced push would issue a request every few
 * hundred milliseconds for the duration of every drag.
 */

import { peopleApi } from "@/lib/people-api";

export type ConnectionStyle =
  | "straight"
  | "bezier"
  | "simple-bezier"
  | "step"
  | "arc";

const CONNECTION_STYLES: readonly ConnectionStyle[] = [
  "straight",
  "bezier",
  "simple-bezier",
  "step",
  "arc",
];

export type Group = {
  id: string;
  name: string;
  memberIds: string[];
  /** Stored gravitational anchor — orbit-spring pulls members toward this
   *  point. Optional for backwards compat: groups stored before centers
   *  were added are migrated on first read by deriving from member positions. */
  center?: { x: number; y: number };
  /** Visible radius of the dashed circle, also the drop-zone radius.
   *  Optional for backwards compat. */
  radius?: number;
  /** Geometry version. Missing/<2 means the stored center was computed
   *  from RF top-left positions (off by NODE_HALF). On read, anything
   *  without v=2 gets re-snapshotted from member CENTERS. */
  geomVersion?: number;
};

export const GROUP_GEOM_VERSION = 3;

export type Position = { x: number; y: number };

const POSITIONS_KEY = "okuro.people-graph.positions.v1";
const GROUPS_KEY = "okuro.people-graph.groups.v1";
const SETTINGS_KEY = "okuro.people-graph.settings.v1";
/** Last local arrangement a server hydrate overwrote. Recovery net only —
 *  never read during normal operation, so it cannot resurrect stale state
 *  on its own. Restore with restoreGraphBackup() from the console. */
const BACKUP_KEY = "okuro.people-graph.backup.v1";

type Settings = {
  connectionStyle: ConnectionStyle;
};

const DEFAULT_SETTINGS: Settings = { connectionStyle: "bezier" };

function readJson<T>(key: string, fallback: T): T {
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object" ? (parsed as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* quota / disabled — fall back to in-memory */
  }
}

export function readPositions(): Record<string, Position> {
  return readJson<Record<string, Position>>(POSITIONS_KEY, {});
}

export function writePositions(positions: Record<string, Position>): void {
  writeJson(POSITIONS_KEY, positions);
  schedulePush();
}

export function readGroups(): Group[] {
  const raw = readJson<unknown>(GROUPS_KEY, []);
  if (!Array.isArray(raw)) return [];
  return raw.filter(
    (g): g is Group =>
      g !== null &&
      typeof g === "object" &&
      typeof (g as Group).id === "string" &&
      typeof (g as Group).name === "string" &&
      Array.isArray((g as Group).memberIds),
  );
}

export function writeGroups(groups: Group[]): void {
  writeJson(GROUPS_KEY, groups);
  schedulePush();
}

export function readSettings(): Settings {
  const raw = readJson<Partial<Settings>>(SETTINGS_KEY, {});
  return {
    connectionStyle: CONNECTION_STYLES.includes(
      raw.connectionStyle as ConnectionStyle,
    )
      ? (raw.connectionStyle as ConnectionStyle)
      : DEFAULT_SETTINGS.connectionStyle,
  };
}

export function writeSettings(settings: Settings): void {
  writeJson(SETTINGS_KEY, settings);
  schedulePush();
}

export function newGroupId(): string {
  return `g_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
}

// ── Server sync ──────────────────────────────────────────────────────

const PUSH_DEBOUNCE_MS = 800;

/** Nothing is pushed before the first hydrate resolves. Without this a
 *  write racing hydration would upload the browser's stale cache over the
 *  server copy we are in the middle of fetching — the exact data loss this
 *  module exists to prevent. */
let hydrated = false;
let pushTimer: ReturnType<typeof setTimeout> | null = null;
let flushListenersBound = false;

/**
 * THE DOCUMENT THE SERVER IS KNOWN TO HOLD, as a fingerprint. `null` means
 * "never written" (the `saved_at === null` case) and therefore differs from
 * everything, which is what makes first-run seeding still work.
 *
 * WHY IT EXISTS — a write* call is not evidence of an edit. Three of this
 * module's callers fire with NO user gesture on every single mount of
 * `/deliver/people`:
 *
 *   · `PeopleGraph.tsx:698`  — the group-geometry migration, when any stored
 *                              group predates `GROUP_GEOM_VERSION`;
 *   · `PeopleGraph.tsx:1093` — the spring loop's 30-tick checkpoint;
 *   · `PeopleGraph.tsx:1113` — the spring loop's FINAL REST WRITE, which has
 *                              no `positionsChanged` guard at all and so runs
 *                              on every mount of an already-settled graph.
 *
 * So merely LOOKING at the page PUT the whole layout back, value-identical,
 * and moved `updated_at` on every row — the field `_layout_saved_at()` uses as
 * the hydration discriminator. Measured 2026-09-15: 17 positions + 5 groups +
 * settings, all values equal to what the server already held.
 *
 * THE GUARD IS AT THIS LAYER AND NOT AT THE THREE CALL SITES on purpose
 * (DP11). The class is "a persistence layer that cannot tell a user edit from
 * a machine re-derivation", and a fourth machine writer is one refactor away.
 * Suppressing by VALUE rather than by gesture is also the honest test: a
 * spring that genuinely moves a node has changed the arrangement and still
 * saves, while a re-push of identical numbers does not.
 */
let serverFingerprint: string | null = null;

/** The complete document, read from the cache at flush time rather than
 *  passed in — write* callers only ever hold one of the three collections,
 *  and the PUT replaces all of them. */
function snapshot() {
  return {
    positions: readPositions(),
    groups: readGroups(),
    settings: { connectionStyle: readSettings().connectionStyle },
  };
}

/**
 * A key-order-independent identity for the document.
 *
 * `JSON.stringify(snapshot())` WOULD NOT DO. `positions` is a Record whose
 * iteration order is insertion order, and the spring rebuilds it by spreading
 * and re-assigning; a group object arrives from the API in the server's field
 * order and leaves the migration in the spread's. Both produce a different
 * string for identical values, which would report every mount as an edit and
 * put the defect straight back. `memberIds` is sorted for the same reason the
 * backup diff below already sorts it — membership is a set, not a sequence.
 */
function fingerprint(): string {
  const doc = snapshot();
  return JSON.stringify({
    positions: Object.keys(doc.positions)
      .sort()
      .map((id) => [id, doc.positions[id]!.x, doc.positions[id]!.y]),
    groups: doc.groups.map((g) => [
      g.id,
      g.name,
      [...g.memberIds].sort(),
      g.center ? [g.center.x, g.center.y] : null,
      g.radius ?? null,
      g.geomVersion ?? null,
    ]),
    settings: doc.settings.connectionStyle,
  });
}

function pushNow(): void {
  if (!hydrated) return;
  if (pushTimer !== null) {
    clearTimeout(pushTimer);
    pushTimer = null;
  }
  const fp = fingerprint();
  // Nothing the server does not already hold. See `serverFingerprint`.
  if (fp === serverFingerprint) return;

  // Fire-and-forget by design: a failed save must never break dragging.
  // The next write re-pushes the whole document, so a dropped request
  // costs nothing beyond that one lost round-trip — which is why the
  // fingerprint is advanced optimistically and ROLLED BACK on failure:
  // without the rollback a dropped request would make the retry look like
  // a no-op and the edit would never reach the server.
  const previous = serverFingerprint;
  serverFingerprint = fp;
  peopleApi.putLayout(snapshot()).catch((err) => {
    if (serverFingerprint === fp) serverFingerprint = previous;
    console.warn("[people-graph] layout save failed", err);
  });
}

function schedulePush(): void {
  if (!hydrated) return;
  if (typeof window === "undefined") return;

  // A write that changed no value schedules nothing, and CANCELS a pending
  // push whose change has since been undone. This is also what makes the
  // `pagehide` flush below fire only for a real edit: there is no timer to
  // flush unless some value actually moved.
  if (fingerprint() === serverFingerprint) {
    if (pushTimer !== null) {
      clearTimeout(pushTimer);
      pushTimer = null;
    }
    return;
  }

  if (pushTimer !== null) clearTimeout(pushTimer);
  pushTimer = setTimeout(() => {
    pushTimer = null;
    pushNow();
  }, PUSH_DEBOUNCE_MS);

  if (!flushListenersBound) {
    flushListenersBound = true;
    // Closing the tab mid-debounce would otherwise silently discard the
    // last arrangement. `pagehide` fires on close/navigate/bfcache in every
    // browser; `visibilitychange` covers mobile task-switching, where
    // pagehide is unreliable.
    const flush = () => {
      if (pushTimer !== null) pushNow();
    };
    window.addEventListener("pagehide", flush);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") flush();
    });
  }
}

/**
 * Pull the server's arrangement into the local cache. Await before
 * mounting the graph.
 *
 * Resolution rules:
 *  - `saved_at === null` — the server has never been written. The browser
 *    cache is the better source, so it is left alone and pushed UP. This
 *    is what carries an existing user's arrangement over on first run
 *    instead of discarding it.
 *  - otherwise — the server wins outright, including empty collections,
 *    which mean "the user deleted these", not "there is nothing here".
 *
 * A failed fetch resolves rather than rejects: offline or a backend that
 * predates the endpoint should degrade to the old browser-local behaviour,
 * not an unrenderable graph. Pushes stay disabled in that case so a
 * transient failure cannot overwrite a good server copy.
 */
export async function hydrateGraphLayout(): Promise<void> {
  if (typeof window === "undefined") return;

  let remote: Awaited<ReturnType<typeof peopleApi.getLayout>>;
  try {
    remote = await peopleApi.getLayout();
  } catch (err) {
    console.warn("[people-graph] layout hydrate failed, staying local", err);
    return;
  }

  if (remote.saved_at === null) {
    hydrated = true;
    // Only seed if there is something local worth keeping; a first-run
    // browser has nothing to say and should not write an empty document.
    const local = snapshot();
    if (
      Object.keys(local.positions).length > 0 ||
      local.groups.length > 0
    ) {
      pushNow();
    }
    return;
  }

  // The server is about to overwrite whatever this browser holds. That is
  // correct — it is how the arrangement follows you between machines — but
  // it is also destructive and irreversible, and it already cost one real
  // set of groups: a browser whose cache seeded the server made every other
  // browser adopt that state, with nothing to roll back to.
  //
  // So take a snapshot of the local groups first. Only when they are
  // non-empty and actually differ, so a normal same-state hydrate does not
  // churn the key or overwrite a good backup with an identical one.
  const localGroups = readGroups();
  if (localGroups.length) {
    const remoteGroups = Array.isArray(remote.groups) ? remote.groups : [];
    const differs =
      JSON.stringify(localGroups.map((g) => [g.id, g.name, [...g.memberIds].sort()])) !==
      JSON.stringify(remoteGroups.map((g) => [g.id, g.name, [...g.memberIds].sort()]));
    if (differs) {
      writeJson(BACKUP_KEY, {
        replaced_at: new Date().toISOString(),
        reason: `server copy from ${remote.saved_at} replaced local groups`,
        groups: localGroups,
        positions: readPositions(),
      });
    }
  }

  writeJson(POSITIONS_KEY, remote.positions ?? {});
  writeJson(GROUPS_KEY, Array.isArray(remote.groups) ? remote.groups : []);
  // Route through readSettings' validator so an unrecognised style stored
  // by a newer frontend falls back to the default instead of reaching
  // React Flow as an unknown edge type.
  const style = remote.settings?.connectionStyle;
  writeJson(SETTINGS_KEY, {
    connectionStyle: CONNECTION_STYLES.includes(style as ConnectionStyle)
      ? (style as ConnectionStyle)
      : DEFAULT_SETTINGS.connectionStyle,
  });

  hydrated = true;
  // The cache now IS the server copy, so record what the server holds. Taken
  // after the three writes rather than from `remote`, so it is the document as
  // this client re-serialises it — the same path every later comparison takes.
  serverFingerprint = fingerprint();
}

export type GraphBackup = {
  replaced_at: string;
  reason: string;
  groups: Group[];
  positions: Record<string, Position>;
};

/** The local arrangement the last server hydrate overwrote, if any. */
export function readGraphBackup(): GraphBackup | null {
  const raw = readJson<GraphBackup | null>(BACKUP_KEY, null);
  return raw && Array.isArray(raw.groups) ? raw : null;
}

/**
 * Put the backed-up arrangement back and push it to the server, making
 * this browser authoritative again. Returns false when there is nothing
 * to restore. Exposed on window as `okuroRestoreGraph()` so recovery
 * needs no rebuild — see the assignment below.
 */
export function restoreGraphBackup(): boolean {
  const backup = readGraphBackup();
  if (!backup) return false;
  writeJson(GROUPS_KEY, backup.groups);
  if (backup.positions) writeJson(POSITIONS_KEY, backup.positions);
  pushNow();
  return true;
}

if (typeof window !== "undefined") {
  // Recovery is only useful if it is reachable when the UI is already
  // showing the wrong thing. A console one-liner beats a rebuild.
  (window as unknown as Record<string, unknown>).okuroRestoreGraph =
    restoreGraphBackup;
  (window as unknown as Record<string, unknown>).okuroReadGraphBackup =
    readGraphBackup;
}

/** Test seam — resets the module's sync state between cases. */
export function __resetGraphSyncForTests(): void {
  hydrated = false;
  serverFingerprint = null;
  if (pushTimer !== null) clearTimeout(pushTimer);
  pushTimer = null;
}
