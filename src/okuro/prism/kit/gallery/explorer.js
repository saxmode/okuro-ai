/* Selection map — a read-only view of the W2 fit contract.
 *
 * Reads kit/board/component-manifest.json (the SAME artifact the solver's
 * solver/manifest.py loads) and pivots it by claim-shape: for a shape, show
 * every component ranked by its fit score, split at the 0.50 candidate
 * threshold the solver uses. This is data, not a re-implementation of the
 * solver — capacity(item-count) + anti-sameness(deck context) also decide the
 * final winner and are noted but cannot be shown from a static manifest.
 */
(function () {
  "use strict";

  var CANDIDATE_CUTOFF = 0.5; // authoring/translate.py fit-set threshold
  var tabsEl = document.getElementById("shape-tabs");
  var rankEl = document.getElementById("ranking");
  var state = { manifest: null, shape: null };

  function fmtCapacity(cap) {
    if (!cap) return "";
    var noun = cap.item_noun || "item";
    if (cap.unbounded) return cap.min + "–∞ " + noun;
    if (cap.min === cap.max) return cap.max + " " + noun;
    return cap.min + "–" + cap.max + " " + noun;
  }

  function fmtCols(size) {
    if (!size) return "";
    if (size.min_cols === size.max_cols) return size.min_cols + " col";
    return size.min_cols + "–" + size.max_cols + " col";
  }

  function tag(text, cls) {
    var s = document.createElement("span");
    s.className = "tag" + (cls ? " " + cls : "");
    s.textContent = text;
    return s;
  }

  function renderRow(comp, shape) {
    var fit = (comp.fit && comp.fit[shape]) || 0;
    var row = document.createElement("div");
    row.className = "comp-row" + (fit < CANDIDATE_CUTOFF ? " below" : "");

    var fitCell = document.createElement("div");
    fitCell.className = "fit-cell";
    var bar = document.createElement("div");
    bar.className = "fit-bar";
    var fill = document.createElement("i");
    fill.style.width = Math.round(fit * 100) + "%";
    bar.appendChild(fill);
    var num = document.createElement("div");
    num.className = "fit-num";
    num.textContent = "fit " + fit.toFixed(2);
    fitCell.appendChild(bar);
    fitCell.appendChild(num);

    var main = document.createElement("div");
    main.className = "comp-main";
    var id = document.createElement("div");
    id.className = "comp-id";
    id.textContent = comp.id;
    var role = document.createElement("div");
    role.className = "comp-role";
    role.textContent = comp.role || "";
    var tags = document.createElement("div");
    tags.className = "meta-tags";
    if (comp.tier) tags.appendChild(tag(comp.tier, "tier"));
    tags.appendChild(tag(fmtCapacity(comp.capacity)));
    tags.appendChild(tag(fmtCols(comp.size)));
    (comp.emphasis || []).forEach(function (e) { tags.appendChild(tag(e)); });
    if (comp.provenance) tags.appendChild(tag("✓ provenance", "prov"));
    if (comp.inline) tags.appendChild(tag("inline"));

    main.appendChild(id);
    main.appendChild(role);
    main.appendChild(tags);
    row.appendChild(fitCell);
    row.appendChild(main);
    return row;
  }

  function renderRanking() {
    var m = state.manifest, shape = state.shape;
    rankEl.innerHTML = "";
    var scored = m.components
      .map(function (c) { return { c: c, fit: (c.fit && c.fit[shape]) || 0 }; })
      .sort(function (a, b) { return b.fit - a.fit || a.c.id.localeCompare(b.c.id); });

    var candidates = scored.filter(function (x) { return x.fit >= CANDIDATE_CUTOFF; });
    var below = scored.filter(function (x) { return x.fit < CANDIDATE_CUTOFF; });

    var head = document.createElement("div");
    head.className = "rank-head";
    var h3 = document.createElement("h3");
    h3.textContent = shape;
    var sub = document.createElement("span");
    sub.className = "rank-sub";
    sub.textContent = candidates.length + " candidate" + (candidates.length === 1 ? "" : "s") +
      " (fit ≥ " + CANDIDATE_CUTOFF.toFixed(2) + ") · " + m.components.length + " scored";
    head.appendChild(h3);
    head.appendChild(sub);
    rankEl.appendChild(head);

    if (!candidates.length) {
      var none = document.createElement("p");
      none.className = "empty-note";
      none.textContent = "No component reaches the 0.50 candidate threshold for this shape — the solver would fall back to its default/narrative component.";
      rankEl.appendChild(none);
    }
    candidates.forEach(function (x) { rankEl.appendChild(renderRow(x.c, shape)); });

    if (below.length) {
      var cut = document.createElement("div");
      cut.className = "cut-note";
      cut.textContent = "— below threshold (" + below.length + ") — not considered for this shape —";
      rankEl.appendChild(cut);
      below.forEach(function (x) { rankEl.appendChild(renderRow(x.c, shape)); });
    }
  }

  function selectShape(shape) {
    state.shape = shape;
    Array.prototype.forEach.call(tabsEl.children, function (b) {
      b.classList.toggle("active", b.dataset.shape === shape);
    });
    renderRanking();
  }

  function renderTabs() {
    tabsEl.innerHTML = "";
    state.manifest.claim_shapes.forEach(function (shape) {
      var b = document.createElement("button");
      b.dataset.shape = shape;
      b.textContent = shape;
      b.addEventListener("click", function () { selectShape(shape); });
      tabsEl.appendChild(b);
    });
  }

  fetch("../board/component-manifest.json")
    .then(function (r) { return r.json(); })
    .then(function (m) {
      state.manifest = m;
      renderTabs();
      selectShape(m.claim_shapes[0]);
    })
    .catch(function (e) {
      rankEl.innerHTML = '<p class="empty-note">Failed to load component-manifest.json — ' + e + "</p>";
    });
})();
