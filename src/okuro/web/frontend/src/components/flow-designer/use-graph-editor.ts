// <!-- AGENT_HEADER
// role: code
// purpose: The SAVE COORDINATOR shared by /flow and /workflows — debounce,
//   one-write-at-a-time, the empty-canvas clobber guard, base_rev conflict
//   adoption, and stale-save cancellation on a document switch.
// AGENT_HEADER_END -->
import React from "react";

import { ApiError } from "@/lib/api";

/** Long enough that a burst of edits coalesces into one write, short enough
 *  that the badge does not feel stuck. Pinned by the characterization suite. */
export const DEBOUNCE_MS = 800;

/**
 * One write coordinator, two editors.
 *
 * /flow and /workflows had ~120 near-identical lines of this each, and it is
 * the part that breaks SILENTLY and costs a user their work — so a divergence
 * between the two copies is a bug nobody sees until a graph is gone. The rules
 * it enforces are the ones the /flow characterization suite pins:
 *
 *   * a blank or stale canvas must never overwrite a populated document
 *   * exactly one save in flight; a second trigger RE-ARMS the debounce rather
 *     than racing, which is what stops a burst of edits creating duplicate rows
 *   * a document switch mid-save invalidates that save (`loadGen`), so a slow
 *     response can never land on the document the user moved to
 *   * a 409 is resolved by ADOPTING the server copy, never by retrying
 *
 * What is deliberately NOT in here: what makes a document DIRTY. /flow marks
 * dirty from an effect over the graph arrays; /workflows calls markDirty() at
 * real mutation sites and filters selection and measurement out. Those are
 * different editing models, and merging them is a separate step — this hook
 * only owns what happens once something IS dirty.
 */

export interface SaveBody {
  name: string;
  description: string;
  graph: unknown;
  origin: string;
  base_rev?: number;
}

export interface SavedDoc {
  id: string;
  rev?: number;
}

export interface GraphEditorApi<D extends SavedDoc> {
  create: (body: SaveBody) => Promise<D>;
  upsert: (id: string, body: SaveBody) => Promise<D>;
}

export interface GraphEditorOptions<D extends SavedDoc> {
  api: GraphEditorApi<D>;
  /** Identifies THIS editor's writes, so its own live-sync echo can be ignored. */
  origin: string;
  /** Used when the name field is blank — a document is never saved unnamed. */
  defaultName: string;
  /** The graph blob to persist. MUST read from refs, not from closed-over state:
   *  doSave is a useCallback that captured an early render where the graph was
   *  still empty, and serializing that would autosave a blank canvas over the
   *  real one — the original clobber. */
  serialize: () => { nodes?: unknown[] } & Record<string, unknown>;
  getName: () => string;
  getDescription: () => string;
  /** Live node count on the CANVAS. The clobber guard asks the canvas, not the
   *  serialized blob, because the two disagree exactly during the load race the
   *  guard exists to catch. */
  getNodeCount: () => number;
  /** Hydrate the editor from a server document (the 409 path). */
  adopt: (doc: D) => void;
  flash: (message: string) => void;
  /** Shown when the server copy is adopted after a 409. */
  conflictMessage: string;
  /** Called after a successful write, before the badge flips to "saved". */
  onSaved?: (doc: D) => void;
  /** Document id this editor mounts on, if any. Read ONCE — the ref is the
   *  live value afterwards. Without it the first save of an existing document
   *  would CREATE a second row instead of updating it. */
  initialId?: string | null;
  /** Start locked, for an editor that mounts straight into a load. Anything the
   *  canvas does before hydration finishes is not a user edit. */
  initiallyLoading?: boolean;
}

export type SaveState = "idle" | "saving" | "saved" | "error";

/** Point the browser's URL at the document that was just created, so a reload
 *  lands on it. A failure here is cosmetic and must never lose the save. */
function syncUrlId(id: string) {
  try {
    const u = new URL(window.location.href);
    u.searchParams.set("id", id);
    window.history.replaceState({}, "", u);
  } catch {
    /* history is a convenience */
  }
}

export function useGraphEditor<D extends SavedDoc>(opts: GraphEditorOptions<D>) {
  const [saveState, setSaveState] = React.useState<SaveState>("idle");

  // Every coordination flag is a REF, not state: doSave reads them at the
  // moment it runs, and a re-render must never be required for correctness.
  const currentIdRef = React.useRef<string | null>(opts.initialId ?? null);
  const revRef = React.useRef(0); // server rev of the loaded doc; sent as base_rev
  const dirtyRef = React.useRef(false);
  const savingRef = React.useRef(false); // one save in flight at a time
  const loadingRef = React.useRef(!!opts.initiallyLoading); // hydrating — not user edits
  const loadGenRef = React.useRef(0); // bumped on every load/new — stale saves self-cancel
  const loadedNodeCountRef = React.useRef(0); // blocks an empty canvas clobbering a populated doc
  const saveTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  // Options change identity every render (they close over live state). Read
  // them through a ref so every callback below can stay stable — a changing
  // scheduleSave would re-arm timers on unrelated renders.
  const optsRef = React.useRef(opts);
  optsRef.current = opts;

  const clearTimer = React.useCallback(() => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = null;
  }, []);

  // doSave is referenced by scheduleSave and re-references it (re-arm on busy).
  // A ref breaks the definition cycle and keeps scheduleSave stable.
  const doSaveRef = React.useRef<(() => void) | null>(null);
  const scheduleSave = React.useCallback(() => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => doSaveRef.current && doSaveRef.current(), DEBOUNCE_MS);
  }, []);

  const doSave = React.useCallback(async () => {
    const o = optsRef.current;
    if (!dirtyRef.current) return;
    // Serialize writes. A second trigger just re-arms the debounce so it runs
    // AFTER the first completes — by which point currentIdRef is set, which is
    // what prevents duplicate-create rows.
    if (savingRef.current) {
      scheduleSave();
      return;
    }
    // Never autosave-CREATE an empty document: a blank canvas should not spawn
    // a row until it has content. An existing document always saves.
    if (!currentIdRef.current && o.getNodeCount() === 0) {
      dirtyRef.current = false;
      setSaveState("idle");
      return;
    }
    // Never autosave an EMPTY canvas over a document that loaded non-empty.
    // This is the load/autosave race: switching away and back can leave the
    // canvas transiently blank while the id and rev already point at the
    // populated document, and an autosave here would wipe it. A deliberate
    // clear-all is therefore a no-op for autosave; delete the document instead.
    if (currentIdRef.current && o.getNodeCount() === 0 && loadedNodeCountRef.current > 0) {
      dirtyRef.current = false;
      setSaveState("saved");
      return;
    }

    const gen = loadGenRef.current; // capture: discard the result if the doc switches
    const graph = o.serialize();
    const name = (o.getName() || o.defaultName).trim() || o.defaultName;
    savingRef.current = true;
    setSaveState("saving");
    // Cleared BEFORE the request, not after it. Everything above is now IN the
    // payload, so anything that arrives from here on is a NEW edit that this
    // write does not contain. Clearing on success instead is how an edit made
    // during an in-flight save used to vanish: the server kept the old graph
    // while the badge read "saved".
    dirtyRef.current = false;
    try {
      const body: SaveBody = {
        name,
        description: o.getDescription(),
        graph,
        origin: o.origin,
      };
      const doc = currentIdRef.current
        ? await o.api.upsert(currentIdRef.current, { ...body, base_rev: revRef.current })
        : await o.api.create(body);
      if (gen !== loadGenRef.current) return; // switched documents — leave the new one alone
      currentIdRef.current = doc.id; // sync immediately so the next save upserts, not creates
      revRef.current = doc.rev ?? 0;
      loadedNodeCountRef.current = (graph.nodes || []).length;
      syncUrlId(doc.id);
      o.onSaved?.(doc);
      // An edit landed while this write was in flight — it is NOT on the server.
      // Keep the document dirty and write again rather than reporting "saved".
      if (dirtyRef.current) {
        scheduleSave();
      } else {
        setSaveState("saved");
      }
    } catch (e) {
      // 409 = someone else (an agent, another tab) saved since we loaded. Do NOT
      // clobber: adopt the server's version. The overwritten local edits stay
      // recoverable through history.
      // The write did not land, so the local graph is still unsaved — restore the
      // flag we cleared before sending. The 409 branch below overrides this by
      // adopting the server copy, which resets it again.
      dirtyRef.current = true;
      if (e instanceof ApiError && e.status === 409 && gen === loadGenRef.current) {
        const cur = (e.details as { current?: D } | undefined)?.current;
        if (cur) {
          o.adopt(cur);
          o.flash(o.conflictMessage);
        } else {
          setSaveState("error");
        }
      } else {
        setSaveState("error");
      }
    } finally {
      savingRef.current = false;
    }
  }, [scheduleSave]);

  doSaveRef.current = doSave; // keep the scheduled-save target current each render

  // BUG 2 — flush a pending save when the tab goes away.
  //
  // Without this the last edit before a close, a navigation or a bfcache freeze
  // dies inside the 800ms debounce, and the user is never told. `pagehide` is
  // the event that fires in every one of those cases, including on iOS where
  // `beforeunload` does not — the same choice components/people/graph-storage.ts
  // already made for the people graph.
  //
  // The write is a normal request, so it is best-effort: a browser may cut it
  // off mid-flight. Best-effort beats the guaranteed loss of not trying, and
  // the debounce means at most one edit is ever at stake.
  React.useEffect(() => {
    const flush = () => {
      if (!dirtyRef.current || savingRef.current) return;
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = null;
      doSaveRef.current?.();
    };
    window.addEventListener("pagehide", flush);
    return () => window.removeEventListener("pagehide", flush);
  }, []);

  /** Something really changed — start the clock. Ignored during hydration. */
  const markDirty = React.useCallback(() => {
    if (loadingRef.current) return;
    dirtyRef.current = true;
    setSaveState("saving");
    scheduleSave();
  }, [scheduleSave]);

  /** Bookkeeping for adopting a server document. The CALLER applies the graph —
   *  that part is the editor's own shape — and this records the identity the
   *  save coordinator needs, while cancelling anything the old document had in
   *  flight. */
  const adoptMeta = React.useCallback(
    (doc: D & { graph?: { nodes?: unknown[] } }) => {
      loadGenRef.current += 1; // invalidate any in-flight/pending save from the old doc
      clearTimer();
      currentIdRef.current = doc.id; // sync immediately — don't wait for a render
      revRef.current = doc.rev ?? 0;
      loadedNodeCountRef.current = (doc.graph?.nodes || []).length;
      dirtyRef.current = false;
      setSaveState("saved");
    },
    [clearTimer],
  );

  /** Reset to an unsaved blank document (New, or the open one deleted elsewhere). */
  const resetMeta = React.useCallback(() => {
    clearTimer();
    loadGenRef.current += 1;
    loadingRef.current = true;
    currentIdRef.current = null;
    revRef.current = 0;
    loadedNodeCountRef.current = 0;
    dirtyRef.current = false;
    setSaveState("idle");
  }, [clearTimer]);

  return {
    saveState,
    setSaveState,
    scheduleSave,
    doSave,
    markDirty,
    adoptMeta,
    resetMeta,
    clearTimer,
    currentIdRef,
    revRef,
    dirtyRef,
    savingRef,
    loadingRef,
    loadGenRef,
    loadedNodeCountRef,
    saveTimer,
  };
}
