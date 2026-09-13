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
