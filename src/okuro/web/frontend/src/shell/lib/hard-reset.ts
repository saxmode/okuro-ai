// SPDX-License-Identifier: Apache-2.0
/**
 * HARD REFRESH IS NOT A RELOAD, AND THAT IS WHY IT LIVES IN ITS OWN FILE NOW.
 *
 * okuro registers a caching service worker that intercepts every fetch, and the
 * app runs in a desktop window with no DevTools and no working Ctrl+Shift+R —
 * so a plain reload can serve a stale bundle forever, which is exactly the state
 * the owner was stuck in. The sequence: unregister every service worker, delete
 * every Cache Storage entry, then navigate with a cache-busting param so even an
 * HTTP-cached index.html is bypassed.
 *
 * IT MOVED OUT OF `Sidebar.tsx` BECAUSE ITS BUTTON DID. The hard-refresh glyph
 * went to the bottom corner on 2026-09-19, and `Sidebar` imports `Corner` — so
 * importing the helper back the other way would have been a cycle. A shared
 * function in `lib/` is the seam that costs neither component anything.
 */

export async function hardResetAndReload(): Promise<void> {
  try {
    if ("serviceWorker" in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map((r) => r.unregister()));
    }
    if (window.caches) {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k)));
    }
  } catch {
    /* best-effort — reload regardless, a failed purge is better than no reload */
  }
  const u = new URL(window.location.href);
  u.searchParams.set("_r", Date.now().toString());
  window.location.replace(u.toString());
}
