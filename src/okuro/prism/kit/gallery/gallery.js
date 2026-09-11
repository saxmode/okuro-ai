/* Gallery driver: brand/theme switch (data-theme on <html>, ?theme= for the
   harness), archetype fragment loader (fetch → ZoomStage embeds + 2D thumb
   grid), and simple in-page interactions (lens tabs, disclosure rows) so the
   specimens behave like a real deck. */
(function () {
  "use strict";
  var BRANDS = ["okuro", "northwind", "meridian"];
  var ARCHETYPES = [
    ["01-hero", "hero"], ["02-lens-reframe", "lens-reframe"],
    ["03-disclosure-list", "disclosure-list"], ["04-scenario-beats", "scenario-beats"],
    ["05-decision-matrix", "decision-matrix"], ["06-framework-tiles", "framework-tiles"],
    ["07-binary-choice", "binary-choice"], ["08-classification-table", "classification-table"],
    ["09-flow-sequence", "flow-sequence"], ["10-proportion-bars", "proportion-bars"],
    ["11-card-set", "card-set"], ["12-ask-cta", "ask-cta"]
  ];

  function currentBrand() {
    var q = new URLSearchParams(location.search).get("theme");
    if (q && BRANDS.indexOf(q) >= 0) return q;
    var saved = localStorage.getItem("prism-gallery-brand");
    return saved && BRANDS.indexOf(saved) >= 0 ? saved : BRANDS[0];
  }

  function setBrand(brand) {
    document.documentElement.setAttribute("data-theme", brand);
    localStorage.setItem("prism-gallery-brand", brand);
    document.querySelectorAll(".brand-switch button").forEach(function (b) {
      b.classList.toggle("active", b.dataset.brand === brand);
    });
    document.querySelectorAll("[data-brand-label]").forEach(function (el) { el.textContent = brand; });
    fillSwatches();
  }

  function wireBrandSwitch() {
    var sw = document.querySelector(".brand-switch");
    if (!sw) return;
    sw.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (b && b.dataset.brand) setBrand(b.dataset.brand);
    });
  }

  function updateViewport() {
    var el = document.querySelector(".viewport-readout");
    if (el) el.textContent = window.innerWidth + "×" + window.innerHeight;
  }

  function fillSwatches() {
    var cs = getComputedStyle(document.documentElement);
    document.querySelectorAll(".swatch[data-var]").forEach(function (sw) {
      var name = sw.dataset.var;
      var chip = sw.querySelector(".chip");
      var val = sw.querySelector(".val");
      if (chip) chip.style.background = "var(" + name + ")";
      if (val) val.textContent = cs.getPropertyValue(name).trim() || "—";
    });
  }

  function slideMarkup(html) {
    // fragment string -> the <section class="slide"> node
    var tpl = document.createElement("template");
    tpl.innerHTML = html.trim();
    return tpl.content.querySelector(".slide");
  }

  function stageWrap(slideNode, withControls) {
    var stage = document.createElement("div");
    stage.className = "zoom-stage";
    stage.dataset.zoomMode = "fit";
    var canvas = document.createElement("div");
    canvas.className = "zoom-canvas";
    canvas.appendChild(slideNode);
    stage.appendChild(canvas);
    if (withControls) {
      var ctl = document.createElement("div");
      ctl.className = "zoom-controls";
      ctl.innerHTML = '<button data-mode="fit" class="active">fit</button>' +
        '<button data-mode="out">−</button><span class="zoom-readout">100%</span>' +
        '<button data-mode="in">+</button><button data-mode="100">1:1</button>';
      stage.appendChild(ctl);
    }
    return stage;
  }

  function loadArchetypes() {
    var full = document.getElementById("archetype-stages");
    var grid = document.getElementById("thumb-grid");
    if (!full && !grid) return Promise.resolve();
    return Promise.all(ARCHETYPES.map(function (a) {
      return fetch("../board/archetypes/" + a[0] + ".html").then(function (r) { return r.text(); })
        .then(function (html) { return { id: a[0], arch: a[1], node: slideMarkup(html) }; });
    })).then(function (items) {
      items.forEach(function (it, i) {
        if (full) {
          var block = document.createElement("div");
          var embed = document.createElement("div");
          embed.className = "stage-embed";
          embed.appendChild(stageWrap(it.node.cloneNode(true), true));
          var cap = document.createElement("div");
          cap.className = "stage-caption";
          cap.innerHTML = '<span>' + String(i + 1).padStart(2, "0") + ' · ' + it.arch +
            '</span><span class="arch-id">data-archetype="' + it.arch + '"</span>';
          block.appendChild(embed); block.appendChild(cap);
          full.appendChild(block);
        }
        if (grid) {
          var thumb = document.createElement("div");
          thumb.className = "thumb";
          var canvas = document.createElement("div");
          canvas.className = "zoom-canvas";
          canvas.appendChild(it.node.cloneNode(true));
          var capd = document.createElement("div");
          capd.className = "thumb-cap";
          capd.innerHTML = '<span>' + String(i + 1).padStart(2, "0") + '</span><span>' + it.arch + '</span>';
          thumb.appendChild(canvas); thumb.appendChild(capd);
          grid.appendChild(thumb);
        }
      });
      // re-init zoom stages/thumbs now that DOM exists
      if (window.PrismZoomStage) {
        document.querySelectorAll(".zoom-stage").forEach(function (s) { window.PrismZoomStage.apply(s); });
      }
      window.dispatchEvent(new Event("prism-archetypes-loaded"));
    });
  }

  function wireDeckInteractions(root) {
    (root || document).addEventListener("click", function (e) {
      var tab = e.target.closest(".lens-tab");
      if (tab) {
        var tabs = tab.parentElement;
        var slide = tab.closest(".slide");
        var idx = Array.prototype.indexOf.call(tabs.children, tab);
        tabs.querySelectorAll(".lens-tab").forEach(function (t, i) { t.classList.toggle("active", i === idx); });
        var panes = slide.querySelectorAll(".lens-pane");
        panes.forEach(function (p, i) { p.classList.toggle("active", i === idx); });
        return;
      }
      var head = e.target.closest(".bullet-head");
      if (head) head.parentElement.classList.toggle("open");
    });
  }

  function boot() {
    setBrand(currentBrand());
    wireBrandSwitch();
    wireDeckInteractions();
    updateViewport();
    window.addEventListener("resize", updateViewport);
    loadArchetypes();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
