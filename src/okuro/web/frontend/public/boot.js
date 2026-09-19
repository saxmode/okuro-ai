// SPDX-License-Identifier: Apache-2.0
// ===========================================================================
// THE SHELL'S TWO ENGINE REPAIRS, AND WHY THEY ARE A FILE RATHER THAN INLINE
// ===========================================================================
// This ran as an inline <script> in index.html and NEVER EXECUTED IN THE APP.
// okuro serves `script-src 'self' 'wasm-unsafe-eval' 'unsafe-eval'` with no
// 'unsafe-inline' and no nonce, so CSP blocked it — silently, because a blocked
// script reports nothing to the page. Measured 2026-09-17: the tag was in the
// DOM, 3225 characters of it, and neither attribute it sets existed. A static
// file server with no CSP ran it fine, which is exactly how it passed review.
//
// AS A FILE IT IS SAME-ORIGIN, so `script-src 'self'` allows it and the policy
// does not have to be weakened for a geometry fix. It must stay a CLASSIC
// script tag (no `defer`, no `type=module`) placed AFTER /engine.css: it reads
// the authored base out of that sheet, and both the attributes it sets have to
// exist before the first paint.
//
// The two repairs it performs are documented at their own blocks below.

(function () {
  var html = document.documentElement;

  // --- does backdrop-filter actually PAINT here? -------------------
  try {
    var ua = navigator.userAgent;
    if (/AppleWebKit/.test(ua) && !/Chrome|Chromium|Edg\//.test(ua) && /(X11|Linux)/.test(ua)) {
      html.setAttribute("data-backdrop", "none");
    }
  } catch (e) {
    /* a failed probe must never keep the app from booting */
  }

  // --- is the authored rem base the one we actually got? -----------
  // Returns true once it has an authored percentage to work from, so the
  // caller knows whether it still has to look again later.
  function repairRemBase() {
    try {
      // The percentage the sheets authored, read from the cascade rather
      // than copied here. `/engine.css` declares `html { font-size: 50% }`
      // and a stylesheet link blocks this script until it is applied, so
      // the usual path resolves before the first paint.
      var pct = null;
      var sheets = document.styleSheets;
      for (var i = 0; i < sheets.length; i++) {
        var rules;
        try {
          rules = sheets[i].cssRules;
        } catch (e) {
          continue; // cross-origin sheet — not ours
        }
        for (var j = 0; j < rules.length; j++) {
          var r = rules[j];
          if (!r.selectorText || !r.style || !r.style.fontSize) continue;
          if (!/(^|[,\s])html\b|:root/.test(r.selectorText)) continue;
          var m = /^([\d.]+)%$/.exec(r.style.fontSize.trim());
          if (m) pct = parseFloat(m[1]);
        }
      }
      if (pct === null) return false;

      // The engine's own default, asked for rather than assumed.
      var prev = html.style.fontSize;
      html.style.fontSize = "medium";
      var uaDefault = parseFloat(getComputedStyle(html).fontSize);
      html.style.fontSize = prev;

      var want = (uaDefault * pct) / 100;
      var got = parseFloat(getComputedStyle(html).fontSize);
      // Absolute values are not clamped, so restating it is the repair.
      if (want > 0 && Math.abs(got - want) > 0.01) {
        html.style.fontSize = want + "px";
        html.setAttribute("data-rem-repaired", got + "->" + want);
      }
      return true;
    } catch (e) {
      return true; // never retry into an exception loop
    }
  }

  // A SECOND LOOK, AND IT IS NOT BELT-AND-BRACES. The first pass reads
  // whatever has loaded by this line — normally `/engine.css`, which
  // declares the base. If that sheet ever fails, moves the declaration or
  // is served as something else, the only remaining copy is in the app
  // bundle's own stylesheet, which is linked below this script. Then the
  // repair still lands, one paint later, instead of silently not at all.
  if (!repairRemBase()) {
    document.addEventListener("DOMContentLoaded", repairRemBase, { once: true });
  }
})();
