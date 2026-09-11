// Service worker + auto-reload-on-deploy.
//
// Loaded as an EXTERNAL script from /sw-register.js so it satisfies
// `script-src 'self'` without needing 'unsafe-inline' in the CSP.
//
// When a new sw.js is served (byte-diff on BUILD_ID), the browser
// installs it, the new SW calls skipWaiting() + clients.claim(), and
// `controllerchange` fires on every open client. We reload ONCE so
// the tab picks up the new bundle without the user having to hard-refresh.
if ("serviceWorker" in navigator) {
  var hadController = !!navigator.serviceWorker.controller;
  var refreshing = false;
  navigator.serviceWorker.addEventListener("controllerchange", function () {
    if (!hadController) return; // first-ever install — bundle already fresh
    if (refreshing) return;
    refreshing = true;
    location.reload();
  });
  navigator.serviceWorker.register("/sw.js").then(function (reg) {
    // SPA tabs don't reload HTML, so register() only fires once per tab.
    // Poll for a new sw.js every 60s + whenever the tab becomes visible,
    // so long-lived tabs notice deploys and auto-reload shortly after.
    setInterval(function () { reg.update().catch(function () {}); }, 60000);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") reg.update().catch(function () {});
    });
  }).catch(function () {});
}
