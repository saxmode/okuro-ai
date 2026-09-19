/**
 * EVERY WRITE TO THE KIT STORE, AND THE REPAINT THAT BELONGS TO IT.
 *
 * THE CLASS THIS MODULE ENDS. `/engine.css` is a `<link>` the browser has
 * already fetched and the server regenerates per request, so the bytes a write
 * produces are live the instant the request returns and nothing tells the link.
 * Five instances of that one defect have been found and fixed one call site at
 * a time — the sheet-swap race, the pulse blob, the appearance switch, the old
 * page's save, and `saveBrand` on /ds-engine-codex, which never repainted at
 * all. Each fix taught one caller the rule. None of them stopped the next
 * caller from not knowing it.
 *
 * SO THE RULE MOVES INTO THE WRITE. A caller asks for a kit to be saved,
 * created or deleted; whether that changed what `/engine.css` serves is this
 * module's question, never the page's. A page that forgets to repaint is no
 * longer expressible, because there is nothing left for it to forget.
 *
 * THE SERVER DECIDES WHO IS ACTIVE, NOT THE CALLER. `_active_kit_id` resolves
 * the profile's `design.kit` with a fallback to okuro's own kit, so "is the kit
 * I just wrote the one painting the app" is a question only the server can
 * answer — deleting the active kit, for instance, moves the answer to a kit
 * nobody named. Every decision below reads it back from `/api/design-engine/kits`.
 *
 * AND NEVER FROM `/boot`. `/boot` carries `active` too, but both authoring
 * pages seed `brand`, `model`, `sheet` and `rung` from the boot payload in an
 * effect keyed on that object; react-query hands back a NEW object on every
 * refetch, so re-reading it would throw away the kit the user has open together
 * with every unsaved edit in it (2026-09-12). `/kits` answers the same question
 * from the same resolver and nothing is seeded from it.
 *
 * WRITE FIRST, RE-READ SECOND, RELOAD THIRD, and the order is load-bearing for
 * the same reason it is in `active-kit.ts`: a sheet fetch that races the write
 * is answered with the state before it.
 */

import { engineApi } from "@/components/design-engine/engine-api";
import type { BrandJson, KitRow } from "@/components/design-engine/types";
import { reloadEngineSheet } from "@/lib/theme";

export { setActiveKit } from "@/lib/active-kit";

/** What `/api/design-engine/kits` answers: the library, and who paints the app. */
export interface KitListing {
  kits: KitRow[];
  active: string;
}

/**
 * How this module re-reads the listing.
 *
 * IT IS INJECTABLE SO THE PAGE'S OWN QUERY IS THE READ. Both pages hold the
 * listing in react-query and render a badge off it, so a module that fetched
 * privately would leave the page's copy stale and cost a second round trip to
 * do it. A page therefore hands in its `refetch`, and the value this module
 * decides on is the same value the page is about to render. The default exists
 * for callers with no cache — and for the tests.
 */
export type ListKits = () => Promise<KitListing | null>;

const listFromServer: ListKits = async () => {
  const answer = await engineApi.kits();
  return { kits: answer.kits, active: answer.active };
};

export interface KitWriteOptions {
  /** The page's own re-read of `/api/design-engine/kits`. */
  listKits?: ListKits;
}

export interface KitWriteResult {
  /** The listing as the server answered it AFTER the write, or null if the
      caller's re-read had nothing to give. */
  listing: KitListing | null;
  /** Whether the write changed what `/engine.css` serves — so the link was
      reloaded and the app has re-themed. Pages use it for what they SAY, never
      to decide whether to repaint. */
  repainted: boolean;
}

/**
 * Tell the link its bytes are stale.
 *
 * Wrapped because a test renderer and a dev page without the `<link>` have
 * nothing to reload, and a write must not fail because the document it ran in
 * has no stylesheet to invalidate.
 */
function repaint(): void {
  try {
    reloadEngineSheet();
  } catch {
    /* no-op — nothing to reload is not a failed write */
  }
}

/**
 * Update a kit that already exists in the store.
 *
 * Repaints when the server says the saved kit is the active one. Saving a kit
 * nobody is painted with changes nothing the browser is showing, and a reload
 * there would be a request that fetches the same bytes back.
 */
export async function saveKit(
  brand: BrandJson,
  options: KitWriteOptions = {},
): Promise<KitWriteResult> {
  await engineApi.save(brand);
  return await settle(brand.id, options);
}

/**
 * Put a NEW kit in the store — a fork, a duplicate, a scan's result, or the
 * first save of a draft that has no file yet.
 *
 * THE SAME ACTIVE TEST AS `saveKit`, DELIBERATELY. A brand-new id is normally
 * not the active one, so this usually decides not to repaint — but `design.kit`
 * can name a kit whose file is missing, in which case `_active_kit_id` has been
 * falling back to okuro's own and creating that id makes it resolve for real.
 * Asking the server the same question in both writes is what keeps that case
 * from needing its own rule.
 */
export async function createKit(
  brand: BrandJson,
  options: KitWriteOptions = {},
): Promise<KitWriteResult> {
  await engineApi.create(brand);
  return await settle(brand.id, options);
}

/**
 * Remove a kit from the store.
 *
 * THE ACTIVE TEST RUNS BEFORE THE DELETE, because afterwards the answer is
 * unrecoverable: the id is gone from the listing either way, so "was it the one
 * painting the app" cannot be asked once the file is. Deleting the active kit
 * moves the server's answer to its fallback, which is a different sheet — the
 * same class as a save, in the other direction.
 */
export async function deleteKit(
  id: string,
  options: KitWriteOptions = {},
): Promise<KitWriteResult> {
  const list = options.listKits ?? listFromServer;
  const before = await list();
  await engineApi.remove(id);
  const listing = await list();
  const changed = before?.active === id;
  if (changed) repaint();
  return { listing, repainted: changed };
}

/** Re-read who is active, repaint if it is the kit just written, report both. */
async function settle(
  id: string,
  options: KitWriteOptions,
): Promise<KitWriteResult> {
  const list = options.listKits ?? listFromServer;
  const listing = await list();
  const changed = listing?.active === id;
  if (changed) repaint();
  return { listing, repainted: changed };
}
