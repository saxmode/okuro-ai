/**
 * THE ONE WRITE THAT CHANGES WHICH DESIGN SYSTEM PAINTS OKURO.
 *
 * `design.kit` on the user profile is the only thing the profile says about
 * appearance (charter, 2026-09-06), and `/engine.css` resolves it server-side
 * because a bare `<link rel="stylesheet">` carries no state. So "set as
 * default" is exactly two steps: PATCH the key, then tell the link its bytes
 * are stale.
 *
 * WHY IT IS A MODULE AND NOT A SECOND COPY. This lived inside
 * `pages/settings.tsx::onPersist` and nowhere else, because /settings was the
 * only place that could choose. The owner's 2026-09-12 rulings give the authoring
 * pages a "Set as active" control and a leave-prompt that can also set the
 * default — three callers for one write. Three copies of a two-step write is
 * how one of them ends up doing only the first step: the PATCH lands, the link
 * is never told, and the app looks like it ignored the choice. That is the
 * literal defect the preset select's own comment records ("the select moves,
 * the sheet does not"), and the class the charter names four instances of.
 *
 * PERSIST FIRST, RE-FETCH SECOND, and the order is load-bearing. The select
 * used to call `reloadEngineSheet` alongside the PATCH; the sheet fetch raced
 * the PATCH and usually won, so the server answered with the OLD kit and the
 * app only re-themed on the next full reload.
 *
 * THE KIT-STORE WRITES ARE ITS SIBLING. `lib/kit-writes.ts` holds `saveKit`,
 * `createKit` and `deleteKit` and re-exports this function, so "every write that
 * can change what `/engine.css` serves" is one import. This file stays the one
 * writer of `design.kit`; that one re-exports it rather than copying it.
 *
 * `brand` IS NULL-DELETED IN THE SAME PATCH. The control that wrote it is gone,
 * nothing has read it since v0, and the backend's `_deep_merge` pops a key
 * whose incoming value is null. A key that outlives its last reader is the
 * shape all three post-p10 defects had.
 */

import { onboardingApi } from "@/lib/api";
import { reloadEngineSheet } from "@/lib/theme";

/** The `design` section of the profile, as far as this module cares. */
type DesignPatch = Record<string, unknown>;

/**
 * Make `id` the design system that paints okuro, and repaint.
 *
 * THE CURRENT SECTION IS READ BACK RATHER THAN PASSED IN. A caller holding a
 * cached profile can be arbitrarily stale — the codex page never fetches one at
 * all — and spreading a stale section over the patch would re-write whatever it
 * last saw. One GET on a deliberate user action is cheaper than that class of
 * bug, and it keeps every caller's code identical.
 */
export async function setActiveKit(id: string): Promise<void> {
  const profile = await onboardingApi.profile();
  const current = ((profile?.design as DesignPatch | undefined) ?? {}) as DesignPatch;
  await onboardingApi.patchProfile({
    design: { ...current, kit: id, brand: null } satisfies DesignPatch,
  });
  try {
    reloadEngineSheet();
  } catch {
    /* no-op — a dev environment without the `<link>` has nothing to reload */
  }
}

/** The viewport classes the engine can spell. Two, and the engine knows no more. */
export type ViewportClass = "desktop" | "mobile";

/** Which rung each viewport class opens on. Mirrors `schema.ViewportRungs`. */
export type ViewportRungs = Record<ViewportClass, string>;

/**
 * SET THE RUNG A VIEWPORT CLASS OPENS ON, and repaint.
 *
 * His ruling, 2026-09-17: *"The font sizes are everywhere the same. The font
 * rungs aren't. ... It should use the same size over all design systems,
 * standard for the viewer: if desktop standard is L, for mobile it might be M."*
 * And: *"okuro-design-system has a configuration item for this. Standard can be
 * defined."* This is the write half of that item.
 *
 * WHY IT LIVES BESIDE `setActiveKit` AND NOT IN THE KIT FORM. `design.rung` is
 * PROFILE state, exactly like `design.kit` — it is not part of any brand and
 * must never ride along on a kit save. A control inside the authoring form would
 * make "save this design system" silently also mean "change how big everything
 * is for every design system", which is the opposite of what the ruling says.
 *
 * AND IT IS THE SAME TWO STEPS, IN THE SAME ORDER. PATCH, then tell the `<link>`
 * its bytes are stale. `/engine.css` is regenerated per request and the rung is
 * now part of what that route resolves, so a write without the reload is a
 * setting that only takes effect on the next full reload — the literal defect
 * this module was extracted to end, and the reason the order is load-bearing:
 * a sheet fetch that races the PATCH is answered with the state before it.
 */
export async function setViewportRung(
  viewport: ViewportClass,
  rung: string,
): Promise<void> {
  const profile = await onboardingApi.profile();
  const current = ((profile?.design as DesignPatch | undefined) ?? {}) as DesignPatch;
  // THE SIBLING CLASS IS READ BACK, NOT ASSUMED. Patching `{desktop}` alone
  // would drop `mobile` on the floor, because the backend merges the `design`
  // section key by key and `rung` is one key holding both.
  const rungs = ((current.rung as Partial<ViewportRungs> | undefined) ?? {});
  await onboardingApi.patchProfile({
    design: { ...current, rung: { ...rungs, [viewport]: rung } } satisfies DesignPatch,
  });
  try {
    reloadEngineSheet();
  } catch {
    /* no-op — a dev environment without the `<link>` has nothing to reload */
  }
}
