/* ═══════════════════════════════════════════════════════════════════════
   prism · board kit · calibration.js   (PRISM v4 W1 §5 — height calibration)

   Renders the CALIBRATION CORPUS: every placeable component instantiated across
   a parametric sweep of  item_count × char_band × col_span  inside a fixed-width
   container equal to its col-span pixel width on the 1600px deck grid. Each
   specimen carries data-* attributes; tests/calibrate_heights.py measures
   offsetHeight per specimen and fits a per-component height model, then reports
   held-out p90 error vs the W1 exit-gate threshold.

   The kit OWNS styling (components.css / components-ext.css); this only decides
   WHAT one item is per component and how to grow it — the factory contract the
   calibrated model is fit against. Zero styling here.
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  // deck grid: 1600px canvas, 64px side padding -> 1472px usable, 12 columns.
  var USABLE = 1472, COLS = 12;
  function colWidthPx(span) { return Math.round((span / COLS) * USABLE); }

  // char-band -> chars per text field (short / typical / long). Non-text
  // components use a fixed nominal label length (model text term ~ 0).
  var CHAR_BANDS = { short: 24, typical: 72, long: 160 };
  var WORDS = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do "
    + "eiusmod tempor incididunt ut labore et dolore magna aliqua enim ad minim "
    + "veniam quis nostrud exercitation ullamco laboris nisi aliquip ex ea").split(" ");
  function lorem(chars) {
    if (chars <= 0) return "";
    var out = "", i = 0;
    while (out.length < chars) { out += (out ? " " : "") + WORDS[i % WORDS.length]; i++; }
    out = out.slice(0, chars).trim();
    return out.charAt(0).toUpperCase() + out.slice(1);
  }
  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  // ── per-component factories: (n items, chars per text field) -> HTMLElement ──
  // Each returns the component's ROOT element (no .slide wrapper — the specimen
  // container provides the width). Content scales with n and chars.
  function rep(n, fn) { var a = []; for (var i = 0; i < n; i++) a.push(fn(i)); return a.join(""); }
  var TONES = ["", "safe", "caution", "danger"], GR = ["a", "b", "c", "d"], RISK = ["high", "med", "low"];

  var F = {
    "statement-title": function (n, c) {
      return el("h1", "slide-title", lorem(c) + ' <span class="accent">' + lorem(6) + "</span>");
    },
    "lede": function (n, c) { return el("p", "slide-lede", lorem(c)); },
    "prose": function (n, c) {
      return el("div", "prose", "<h2>" + lorem(24) + "</h2><p>" + lorem(c) + " " + lorem(c)
        + "</p><p>" + lorem(c) + "</p>");
    },
    "disclosure-list": function (n, c) {
      return el("ul", "bullet-list", rep(n, function (i) {
        return '<li class="bullet-row' + (i === 0 ? " open" : "") + '"><div class="bullet-head">'
          + '<span class="bullet-mark">' + (i + 1) + '</span><div><p class="bullet-title">'
          + lorem(Math.min(c, 40)) + '</p><div class="bullet-sub">' + lorem(Math.min(c, 30))
          + '</div></div><span class="bullet-toggle">Detail <span class="chevron">▾</span></span></div>'
          + '<div class="bullet-body"><p>' + lorem(c) + "</p></div></li>";
      }));
    },
    "card-set": function (n, c) {
      return el("div", "card-grid", rep(n, function () {
        return '<div class="card"><div class="card-eyebrow">' + lorem(10) + '</div><h3>'
          + lorem(Math.min(c, 30)) + '</h3><p>' + lorem(c) + '</p><div class="card-footer">'
          + lorem(12) + "</div></div>";
      }));
    },
    "metric-tiles": function (n, c) {
      return el("div", "card-grid", rep(n, function () {
        return '<div class="card"><div class="card-label">' + lorem(12) + '</div>'
          + '<div class="card-value">−92%</div><div class="card-desc">' + lorem(Math.min(c, 30)) + "</div></div>";
      }));
    },
    "callout": function (n, c) {
      return el("div", "callout", '<div class="callout-label">Note</div><h3>' + lorem(24)
        + '</h3><p>' + lorem(c) + "</p>");
    },
    "option-grid": function (n, c) {
      return el("div", "option-grid", rep(n, function (i) {
        var lines = rep(3, function () {
          return '<div class="option-line"><span class="k">' + lorem(8) + '</span><span class="v">'
            + lorem(Math.min(c, 24)) + "</span></div>";
        });
        return '<div class="option-card' + (i === 0 ? " recommended" : "") + '"><div class="option-label">'
          + String.fromCharCode(65 + i) + '</div><h3>' + lorem(Math.min(c, 16)) + '</h3>' + lines + "</div>";
      }));
    },
    "beat-list": function (n, c) {
      return el("ul", "beat-list", rep(n, function (i) {
        return '<li><span class="t-mark">T+' + i + 's</span><div class="t-body">' + lorem(c)
          + '<span class="who">' + lorem(8) + "</span></div></li>";
      }));
    },
    "stat-row": function (n, c) {
      return el("div", "stat-row", rep(n, function (i) {
        return '<div class="stat-cell"><div class="stat-num ' + (TONES[i % 4]) + '">' + (i + 1)
          + 'k</div><div class="stat-lbl">' + lorem(Math.min(c, 18)) + "</div></div>";
      }));
    },
    "persona-row": function (n, c) {
      return el("div", "persona-row", rep(n, function (i) {
        return '<div class="persona-pill"><span class="persona-mark ' + "abc"[i % 3]
          + '"></span><span class="persona-name">' + lorem(10) + '</span><span class="persona-lens">'
          + lorem(Math.min(c, 16)) + "</span></div>";
      }));
    },
    "lens-reframe": function (n, c) {
      var tabs = rep(n, function (i) {
        return '<button class="lens-tab' + (i === 0 ? " active" : "") + '"><span class="persona-mark '
          + "abc"[i % 3] + '"></span><span class="lens-label"><span class="lens-name">' + lorem(8)
          + '</span><span class="lens-frame">' + lorem(6) + "</span></span></button>";
      });
      var panes = rep(n, function (i) {
        return '<div class="lens-pane' + (i === 0 ? " active" : "") + '"><p style="color:var(--fg-body)">'
          + lorem(c) + "</p></div>";
      });
      return el("div", null, '<div class="lens-tabs">' + tabs + "</div>" + panes);
    },
    "story-grid": function (n, c) {
      return el("div", "story-grid", rep(n, function (i) {
        return '<a class="story-card" href="#"><div class="chap-num">0' + (i + 1) + '</div>'
          + '<div class="chap-title">' + lorem(Math.min(c, 24)) + '</div><div class="chap-desc">'
          + lorem(c) + '</div><div class="chap-links"><span class="chap-go">Read →</span></div></a>';
      }));
    },
    "proportion-bars": function (n, c) {
      return el("div", null, rep(n, function (i) {
        var w = 80 + (i * 40) % 340;
        return '<div class="token-bar-wrap" style="margin-bottom:10px"><span class="token-bar '
          + (TONES[1 + i % 3]) + '" style="width:' + w + 'px"></span><span class="token-val '
          + (TONES[1 + i % 3]) + '">' + lorem(Math.min(c, 14)) + "</span></div>";
      }));
    },
    "flow-steps": function (n, c) {
      return el("div", null, rep(n, function (i) {
        return '<div class="flow-step"><span class="flow-num">' + (i + 1) + '</span><span class="flow-what">'
          + lorem(c) + '</span><span class="flow-tool">' + lorem(10) + "</span></div>";
      }));
    },
    "pipeline-table": function (n, c) {
      var head = '<div class="pipeline-header"><span>Layer</span><span>Edge</span><span>Cloud</span><span>Cost</span></div>';
      var rows = rep(n, function () {
        return '<div class="pipeline-row"><span class="layer-name">' + lorem(8) + '</span>'
          + '<span class="edge-cell">' + lorem(Math.min(c, 24)) + '</span><span class="cloud-cell">'
          + lorem(Math.min(c, 24)) + '</span><span class="cost-cell">$' + "0" + "</span></div>";
      });
      return el("div", null, head + rows);
    },
    "verb-grid": function (n, c) {
      return el("div", "verb-grid", rep(n, function () {
        return '<div class="verb-card"><div class="verb-name">' + lorem(8) + '</div><div class="verb-desc">'
          + lorem(c) + '</div><div class="verb-params">' + lorem(Math.min(c, 20))
          + '</div><div class="verb-ack"><span>ack</span><span class="ack-val">sync</span></div></div>';
      }));
    },
    "cmp-table": function (n, c) {
      var head = "<thead><tr><th>Aspect</th><th>Before</th><th>After</th><th>Tag</th></tr></thead>";
      var rows = "<tbody>" + rep(n, function () {
        return "<tr><td>" + lorem(Math.min(c, 24)) + '</td><td class="v1">' + lorem(Math.min(c, 18))
          + '</td><td class="v2">' + lorem(Math.min(c, 18)) + '</td><td><span class="tag full">full</span></td></tr>';
      }) + "</tbody>";
      return el("table", "cmp-table", head + rows);
    },
    "transform-grid": function (n, c) {
      return el("div", "transform-grid", rep(n, function () {
        return '<div class="transform-card"><div class="transform-header">' + lorem(12)
          + '</div><div class="transform-body"><div class="transform-before">' + lorem(Math.min(c, 20))
          + '</div><div class="transform-arrow-cell">→</div><div class="transform-after">'
          + lorem(Math.min(c, 20)) + '</div></div><div class="transform-label">' + lorem(Math.min(c, 20)) + "</div></div>";
      }));
    },
    "phase-row": function (n, c) {
      return el("div", "phase-row", rep(n, function (i) {
        var st = i === 0 ? " done" : (i === 1 ? " next" : "");
        var arrow = i < n - 1 ? '<span class="phase-arrow">→</span>' : "";
        return '<span class="phase-item' + st + '">' + lorem(Math.min(c, 14)) + "</span>" + arrow;
      }));
    },
    "verdict-callout": function (n, c) {
      var stats = rep(n, function (i) {
        return '<span class="vc-stat"><span class="vc-dot" style="background:var(--' + (i % 2 ? "warn" : "ok")
          + ')"></span>' + lorem(Math.min(c, 16)) + "</span>";
      });
      return el("div", "verdict-callout", "<span>" + lorem(Math.min(c, 18)) + "</span>" + stats);
    },
    "ask-box": function (n, c) {
      return el("div", "ask-box", '<div class="ask-box-title">' + lorem(Math.min(c, 40)) + '</div><ul>'
        + rep(n, function () { return "<li>" + lorem(Math.min(c, 50)) + "</li>"; }) + "</ul>");
    },
    "arch-diagram": function (n, c) {
      return el("div", "arch-diagram", rep(n, function (i) {
        var st = i % 2 ? " broken" : " ok";
        return '<div class="arch-item' + st + '"><div class="arch-item-label">' + lorem(8)
          + '</div><div class="arch-item-value">' + lorem(Math.min(c, 16)) + "</div></div>";
      }));
    },
    // ── ports ──
    "provenance": function (n, c) {
      return el("div", "provenance", '<span class="prov-label">Sources</span>' + rep(n, function () {
        return '<span class="prov-ref"><a class="prov-src" href="#">' + lorem(Math.min(c, 20))
          + '</a><span class="prov-as-of">2026-07</span></span>';
      }));
    },
    "evidence-list": function (n, c) {
      return el("div", "evidence-list", rep(n, function (i) {
        var pips = rep(3, function (j) { return '<span class="pip' + (j <= i % 3 ? " on" : "") + '"></span>'; });
        return '<div class="evidence-row"><div class="evidence-claim">' + lorem(c)
          + '</div><span class="evidence-grade ' + GR[i % 4] + '">' + GR[i % 4].toUpperCase()
          + '</span><span class="evidence-conf">' + pips + '</span><span class="evidence-as-of">2026-07</span></div>';
      }));
    },
    "bias-check": function (n, c) {
      return el("div", "bias-check", rep(n, function (i) {
        return '<div class="bias-row"><div class="bias-name">' + lorem(Math.min(c, 24))
          + '</div><span class="bias-risk ' + RISK[i % 3] + '">' + RISK[i % 3]
          + '</span><div class="bias-counter">' + lorem(c) + "</div></div>";
      }));
    },
    "chart": function (n, c) {
      var W = 600, H = 260, pad = 30, bw = (W - pad * 2) / n;
      var bars = rep(n, function (i) {
        var h = 40 + (i * 37) % 170, x = pad + i * bw + 4, y = H - pad - h;
        return '<rect class="bar" x="' + x + '" y="' + y + '" width="' + (bw - 8) + '" height="' + h + '"></rect>';
      });
      var svg = '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="xMidYMid meet">'
        + '<line class="axis" x1="' + pad + '" y1="' + (H - pad) + '" x2="' + (W - pad) + '" y2="' + (H - pad) + '"></line>'
        + bars + "</svg>";
      return el("div", "chart", '<div class="chart-head"><span class="chart-title">' + lorem(Math.min(c, 30))
        + '</span><span class="chart-unit">count</span></div>' + svg
        + '<div class="chart-annotation">' + lorem(Math.min(c, 60)) + "</div>");
    },
    "flow-embed": function (n, c) {
      return el("div", "flow-embed", '<div class="flow-embed-head"><span class="flow-embed-icon">⌘</span>'
        + '<span class="flow-embed-title">' + lorem(Math.min(c, 24)) + '</span><span class="flow-embed-id">flow_'
        + '7f3a</span><button class="flow-embed-max">Maximize</button></div>'
        + '<div class="flow-embed-canvas"><span class="flow-node">start</span><span class="flow-node">act</span>'
        + '<span class="flow-node">end</span></div>');
    },
    "statement": function (n, c) {
      return el("div", "statement", '<div class="statement-kicker">' + lorem(10) + '</div>'
        + '<p class="statement-text">' + lorem(c) + ' <span class="accent">' + lorem(8) + "</span></p>");
    },
    "pull-quote": function (n, c) {
      return el("div", "pull-quote", '<div class="pull-quote-text">' + lorem(c)
        + '</div><div class="pull-quote-attr"><span class="who">' + lorem(12) + "</span>, " + lorem(14) + "</div>");
    },
    "matrix": function (n, c) {
      var cols = 4;
      var head = "<thead><tr><th class='feature-col'>Feature</th>" + rep(cols, function (j) {
        return "<th" + (j === 1 ? " class='winner-col'" : "") + ">" + lorem(8) + "</th>";
      }) + "</tr></thead>";
      var body = "<tbody>" + rep(n, function () {
        return '<tr><td class="feature-cell">' + lorem(Math.min(c, 30)) + "</td>" + rep(cols, function (j) {
          var mark = j === 1 ? '<span class="cell-yes">✓</span>' : (j % 2 ? '<span class="cell-no">✗</span>' : '<span class="cell-partial">~</span>');
          return "<td" + (j === 1 ? " class='winner-col'" : "") + ">" + mark + "</td>";
        }) + "</tr>";
      }) + "</tbody>";
      return el("div", "matrix-wrap", "<table class='matrix'>" + head + body + "</table>");
    },
    // ── high-cardinality ──
    "dense-table": function (n, c) {
      var cols = 5;
      var head = "<thead><tr>" + rep(cols, function (j) { return "<th>" + lorem(8) + "</th>"; }) + "</tr></thead>";
      var body = "<tbody>" + rep(n, function () {
        return "<tr>" + rep(cols, function (j) {
          return j === cols - 1 ? '<td class="num">' + ((Math.random() * 900) | 0) + "</td>"
            : "<td>" + lorem(Math.min(c, 20)) + "</td>";
        }) + "</tr>";
      }) + "</tbody>";
      return el("div", "dense-table-wrap", "<table class='dense-table'>" + head + body + "</table>");
    },
    "two-column-list": function (n, c) {
      return el("div", "two-col-list", rep(n, function () {
        return '<div class="tcl-item"><span class="tcl-mark"></span><div><div class="tcl-title">'
          + lorem(Math.min(c, 30)) + '</div><div class="tcl-sub">' + lorem(Math.min(c, 24)) + "</div></div></div>";
      }));
    },
    "tag-wall": function (n, c) {
      return el("div", "tag-wall", rep(n, function (i) {
        var t = i % 5 === 0 ? " accent" : (i % 7 === 0 ? " ok" : "");
        return '<span class="tw-tag' + t + '">' + lorem(Math.min(c, 16)) + "</span>";
      }));
    }
  };

  // ── sweep planner: read the manifest, expand item×char×col×viewport points ──
  function itemSamples(cap) {
    var lo = cap.min, hi = cap.max;
    if (hi <= lo) return [lo];
    var set = {};
    [lo, Math.round(lo + (hi - lo) / 3), Math.round(lo + 2 * (hi - lo) / 3), hi].forEach(function (v) {
      set[Math.max(lo, Math.min(hi, v))] = 1;
    });
    return Object.keys(set).map(Number).sort(function (a, b) { return a - b; });
  }
  function colSamples(size) {
    return [4, 6, 8, 12].filter(function (s) { return s >= size.min_cols && s <= size.max_cols; })
      .concat(size.max_cols < 4 ? [size.max_cols] : []);
  }

  function build(manifest) {
    var root = document.getElementById("cal-root");
    root.innerHTML = "";
    var made = 0;
    manifest.components.forEach(function (comp) {
      if (comp.inline || !F[comp.id]) return;                 // chips / uncalibrated skip
      var items = itemSamples(comp.capacity);
      var cols = colSamples(comp.size);
      var bands = comp.content_schema && comp.content_schema._text_bearing
        ? ["short", "typical", "long"] : ["typical"];
      items.forEach(function (n) {
        cols.forEach(function (span) {
          bands.forEach(function (band) {
            var chars = CHAR_BANDS[band];
            var box = document.createElement("div");
            box.className = "cal-specimen";
            box.style.width = colWidthPx(span) + "px";
            box.setAttribute("data-component", comp.id);
            box.setAttribute("data-items", n);
            box.setAttribute("data-chars", chars);
            box.setAttribute("data-colspan", span);
            box.setAttribute("data-colpx", colWidthPx(span));
            box.setAttribute("data-band", band);
            try { box.appendChild(F[comp.id](n, chars)); }
            catch (e) { box.setAttribute("data-error", String(e)); }
            root.appendChild(box);
            made++;
          });
        });
      });
    });
    root.setAttribute("data-specimen-count", made);
    window.__CAL_READY = true;
    window.dispatchEvent(new Event("prism-cal-ready"));
  }

  function boot() {
    var brand = new URLSearchParams(location.search).get("theme") || "okuro";
    document.documentElement.setAttribute("data-theme", brand);
    fetch("../board/component-manifest.json").then(function (r) { return r.json(); }).then(build);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
