/* ═══════════════════════════════════════════════════════════════════════
   prism · board kit · reveal.js   (kit-spec 4 — scroll-reveal interaction)
   .reveal starts hidden (opacity:0 + translateY(16px), see components.css);
   this adds .in-view when the element scrolls into the viewport, with a
   per-item stagger of (i%6)*60ms. prefers-reduced-motion (and any browser
   without IntersectionObserver) reveals immediately — content is NEVER left
   permanently hidden. Re-scans when the gallery injects archetypes async
   (the 'prism-archetypes-loaded' event) and exposes window.PrismReveal.scan()
   so the SPA viewer (A4) can re-arm it after mounting slides.

   WHY THIS FILE EXISTS: without an observer, every .reveal element renders
   invisible. Shipping the interaction in the kit (not per-page) is the
   systematic fix — the class and its behaviour travel together.
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";
  var reduce = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function show(el, i) {
    el.style.setProperty("--reveal-delay", ((i % 6) * 60) + "ms");
    el.classList.add("in-view");
  }

  var io = null;
  if (!reduce && "IntersectionObserver" in window) {
    io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          var el = e.target;
          show(el, parseInt(el.dataset.revealIdx || "0", 10));
          io.unobserve(el);
        }
      });
    }, { root: null, rootMargin: "0px 0px -5% 0px", threshold: 0.01 });
  }

  function scan(root) {
    var els = (root || document).querySelectorAll(".reveal:not(.in-view)");
    Array.prototype.forEach.call(els, function (el, i) {
      if (el.dataset.revealIdx == null) el.dataset.revealIdx = String(i % 6);
      if (reduce || !io) show(el, i);       // no-motion / no-IO: show now
      else io.observe(el);
    });
  }

  // Force every .reveal to its shown state immediately, no scrolling/observer.
  // Used by static-capture paths (screenshot harness, print/PDF export) where
  // there is no scroll to trigger the observer — evidence must be post-settle.
  function revealAll(root) {
    var els = (root || document).querySelectorAll(".reveal:not(.in-view)");
    Array.prototype.forEach.call(els, function (el) {
      if (io) io.unobserve(el);
      el.style.setProperty("--reveal-delay", "0ms");
      el.classList.add("in-view");
    });
  }

  function boot() { scan(document); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
  window.addEventListener("prism-archetypes-loaded", function () { scan(document); });

  window.PrismReveal = { scan: scan, revealAll: revealAll };
})();
