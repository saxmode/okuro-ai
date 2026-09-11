// Okuro service worker — pass-through + per-build version stamp.
// The BUILD_ID below changes every build so the browser detects a new SW,
// installs it, and clients.claim() triggers `controllerchange` in the page —
// which the inline bootstrap in index.html turns into a single reload.
// Vite plugin `okuro-sw-build-stamp` replaces __BUILD_ID__ at build time.
const BUILD_ID = "__BUILD_ID__";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) =>
  e.waitUntil(
    (async () => {
      // Purge ALL Cache Storage left by an older caching SW (pre-kill-switch
      // builds shipped a workbox/precache worker that served stale bundles
      // for-ever — the source of "my fix didn't show up after a hard
      // refresh"). The no-op fetch handler below never populates a cache, so
      // deleting every entry is safe and evicts any poisoned bundle the
      // moment this worker takes over.
      try {
        const keys = await caches.keys();
        await Promise.all(keys.map((k) => caches.delete(k)));
      } catch (_) {
        /* caches API unavailable — nothing to purge */
      }
      await self.clients.claim();
    })(),
  ),
);

// No caching. Okuro is a local server — always reachable.
//
// Do NOT intercept Server-Sent Events: routing an EventSource through
// respondWith(fetch()) makes the browser buffer the streamed body, so
// token-by-token responses (Studio chat) arrive all at once. Returning
// without calling respondWith lets the browser handle the EventSource
// natively, preserving the stream.
self.addEventListener("fetch", (e) => {
  const accept = e.request.headers.get("accept") || "";
  if (accept.includes("text/event-stream")) return;
  e.respondWith(fetch(e.request));
});
