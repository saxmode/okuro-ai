/* ═══════════════════════════════════════════════════════════════════════
   prism · board kit · zoom-stage.js   (plan 1 — responsiveness)
   ZoomStage: a fixed 1600×900 .zoom-canvas inside a .zoom-stage is scaled to
   fit the stage's available box via ResizeObserver → transform: scale(min(
   w/1600, h/900)). Scales UP on large/ultra screens and down on small.
   Zoom modes: fit (default) · 100% · manual ± (keyboard +/- when focused).
   Thumbnails (.thumb .zoom-canvas, top-left origin) get their own scale.
   No framework — plain ES modules-free script, safe to inline or <script src>.
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";
  var CW = 1600, CH = 900;
  var MANUAL_MIN = 0.1, MANUAL_MAX = 3;

  function fitScale(stage) {
    var w = stage.clientWidth, h = stage.clientHeight;
    if (!w || !h) return 1;
    return Math.min(w / CW, h / CH);
  }

  function apply(stage) {
    var canvas = stage.querySelector(":scope > .zoom-canvas");
    if (!canvas) return;
    var mode = stage.dataset.zoomMode || "fit";
    var s;
    if (mode === "100") {
      s = 1;
    } else if (mode === "manual") {
      s = parseFloat(stage.dataset.zoomManual || "1");
    } else {
      s = fitScale(stage);
    }
    s = Math.max(MANUAL_MIN, Math.min(MANUAL_MAX, s));
    canvas.style.setProperty("--zoom", String(s));
    var readout = stage.querySelector(".zoom-readout");
    if (readout) readout.textContent = Math.round(s * 100) + "%";
    var btns = stage.querySelectorAll(".zoom-controls button[data-mode]");
    btns.forEach(function (b) { b.classList.toggle("active", b.dataset.mode === mode); });
  }

  function wireControls(stage) {
    var ctl = stage.querySelector(".zoom-controls");
    if (!ctl) return;
    ctl.addEventListener("click", function (e) {
      var btn = e.target.closest("button");
      if (!btn) return;
      var act = btn.dataset.mode || btn.dataset.act;
      if (act === "fit" || act === "100") {
        stage.dataset.zoomMode = act;
      } else if (act === "in" || act === "out") {
        stage.dataset.zoomMode = "manual";
        var cur = parseFloat(stage.dataset.zoomManual || String(fitScale(stage)));
        stage.dataset.zoomManual = String(act === "in" ? cur * 1.1 : cur / 1.1);
      }
      apply(stage);
    });
    stage.tabIndex = stage.tabIndex >= 0 ? stage.tabIndex : 0;
    stage.addEventListener("keydown", function (e) {
      if (e.key === "+" || e.key === "=") { stage.dataset.zoomMode = "manual"; bump(stage, 1.1); e.preventDefault(); }
      else if (e.key === "-" || e.key === "_") { stage.dataset.zoomMode = "manual"; bump(stage, 1 / 1.1); e.preventDefault(); }
      else if (e.key === "0") { stage.dataset.zoomMode = "fit"; apply(stage); }
    });
  }

  function bump(stage, factor) {
    var cur = parseFloat(stage.dataset.zoomManual || String(fitScale(stage)));
    stage.dataset.zoomManual = String(cur * factor);
    apply(stage);
  }

  function scaleThumb(thumb) {
    var canvas = thumb.querySelector(".zoom-canvas");
    if (!canvas) return;
    var s = thumb.clientWidth / CW;   // top-left origin, width-driven
    canvas.style.setProperty("--thumb-scale", String(s));
  }

  function initStages() {
    var stages = document.querySelectorAll(".zoom-stage");
    stages.forEach(function (stage) {
      wireControls(stage);
      apply(stage);
      if (typeof ResizeObserver !== "undefined") {
        var ro = new ResizeObserver(function () { apply(stage); });
        ro.observe(stage);
      } else {
        window.addEventListener("resize", function () { apply(stage); });
      }
    });
  }

  function initThumbs() {
    var thumbs = document.querySelectorAll(".thumb");
    thumbs.forEach(function (thumb) {
      scaleThumb(thumb);
      if (typeof ResizeObserver !== "undefined") {
        var ro = new ResizeObserver(function () { scaleThumb(thumb); });
        ro.observe(thumb);
      } else {
        window.addEventListener("resize", function () { scaleThumb(thumb); });
      }
    });
  }

  function boot() { initStages(); initThumbs(); }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.PrismZoomStage = { apply: apply, fitScale: fitScale };
})();
