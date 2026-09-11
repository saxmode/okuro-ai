# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Generate a self-contained offline survey HTML (v2 lens) — no server, exports answer JSON.
# index:
#   _TEMPLATE
#   def build_offline_survey_html
# AGENT_HEADER_END -->
"""Offline survey generator (v2 profession-agnostic lens).

Emits ONE self-contained HTML file (vanilla JS, zero network). The recipient
self-identifies, declares their knowledge areas + depth, answers a few
profession-agnostic cognitive/communication questions as forced-choice cards,
and the page downloads a {identity, survey} JSON. The user imports it
(POST /api/people/offline/import) where the SAME deterministic scorer
(peer/survey.py) builds the lens. Scoring is NOT in the page — it only
collects raw answers.

This is the offline twin of the React /q/ questionnaire — it follows the SAME
stepped, choice-first flow (one thing per screen), the same card→value
mapping, recency + weakness-mode pills, and the same null-omission rule
(untouched cognitive/angle axes are never sent as confident data). See
docs/research/people-profiling-v2/ui-spec.md §7.

Works on every install, behind any NAT, even airgapped. The
okuro.inthemachine.io relay is the optional connected alternative.
"""

from __future__ import annotations

import json
from typing import Any, Optional

_TEMPLATE = r"""<!doctype html>
<html lang="__LANG__">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Communication profile</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#0c0c0d; color:#e8e8ea;
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  .wrap { max-width:600px; margin:0 auto; padding:40px 20px 80px; }
  .eyebrow { font-size:10px; letter-spacing:.12em; text-transform:uppercase; color:#8a8a90; }
  h1 { font-size:24px; margin:4px 0 0; font-weight:600; }
  h2 { font-size:16px; margin:0 0 4px; font-weight:600; color:#e8e8ea; }
  .hint { font-size:13px; color:#8a8a90; margin:0 0 16px; }
  label.f { display:block; margin-bottom:14px; }
  label.f .l { font-size:12px; color:#8a8a90; margin-bottom:4px; }
  label.f.sub { margin-left:12px; border-left:1px solid #2a2a2e; padding-left:12px; }
  input,select { width:100%; padding:12px; border-radius:8px; border:1px solid #2a2a2e;
    background:#141416; color:#e8e8ea; font-size:14px; }
  input:focus,select:focus { outline:none; border-color:#5a5a62; }
  .row { display:flex; gap:8px; }
  .row input { flex:1; }
  button.btn { min-height:44px; padding:10px 16px; border-radius:8px; border:1px solid #2a2a2e;
    background:#141416; color:#e8e8ea; font-size:14px; cursor:pointer; }
  button.primary { background:#fff; color:#0c0c0d; border-color:#fff; font-weight:600; }
  button.primary:disabled { opacity:.4; cursor:not-allowed; }
  .chips { display:flex; flex-wrap:wrap; gap:8px; }
  .chip { min-height:36px; display:inline-flex; align-items:center; gap:4px; border-radius:999px;
    border:1px solid #2a2a2e; background:#141416; color:#8a8a90; padding:6px 14px; font-size:12px; cursor:pointer; }
  .chip.on { border-color:#fff; background:rgba(255,255,255,.14); color:#e8e8ea; }
  /* Forced-choice cards (one thing per screen) */
  .cards { display:flex; flex-direction:column; gap:8px; }
  .card { min-height:48px; display:flex; align-items:flex-start; gap:8px; width:100%; text-align:left;
    border-radius:8px; border:1px solid #2a2a2e; background:#141416; color:#b6b6bb; padding:12px; cursor:pointer; }
  .card.on { border-color:#fff; background:rgba(255,255,255,.14); color:#e8e8ea; }
  .card .ck { width:14px; flex:0 0 14px; color:transparent; font-weight:700; }
  .card.on .ck { color:#fff; }
  .card .lbl { font-size:14px; font-weight:500; color:#e8e8ea; }
  .card .help { font-size:12px; color:#8a8a90; margin-top:2px; }
  /* Segmented pill group (recency / weakness mode) */
  .pills { display:flex; flex-wrap:wrap; gap:8px; }
  .pill { min-height:44px; display:inline-flex; align-items:center; gap:4px; border-radius:999px;
    border:1px solid #2a2a2e; background:#141416; color:#8a8a90; padding:8px 14px; font-size:13px; cursor:pointer; }
  .pill.on { border-color:#fff; background:rgba(255,255,255,.14); color:#e8e8ea; }
  .group { border:1px solid #2a2a2e; background:#141416; border-radius:10px; padding:12px; }
  .glabel { font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:#8a8a90; margin-bottom:8px; }
  .weakrow { display:flex; flex-wrap:wrap; align-items:center; gap:8px; border:1px solid #2a2a2e;
    background:#141416; border-radius:8px; padding:8px; margin-top:8px; }
  .weakrow .nm { margin-right:auto; font-size:14px; }
  .x { min-width:44px; min-height:44px; background:none; border:none; color:#8a8a90; cursor:pointer; font-size:16px; }
  input[type=range] { width:100%; accent-color:#fff; padding:0; }
  input[type=range].muted { opacity:.4; }
  .slabels { display:flex; justify-content:space-between; gap:12px; font-size:13px; line-height:1.3; color:#b6b6bb; margin-top:6px; }
  .slabels .v { color:#fff; font-weight:600; }
  /* Sticky stepped progress */
  .prog { margin-bottom:18px; }
  .prog .step { font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:#8a8a90; margin-bottom:6px; }
  .bar { height:4px; border-radius:999px; background:#141416; overflow:hidden; }
  .bar > div { height:100%; background:#fff; transition:width .3s; }
  .nav { display:flex; justify-content:space-between; gap:12px; margin-top:28px; }
  .recap { border:1px solid #2a2a2e; background:#141416; border-radius:8px; padding:12px;
    font-size:14px; color:#b6b6bb; display:flex; flex-direction:column; gap:6px; }
  .recap .na { color:#5a5a62; }
  .skip { background:none; border:none; color:#8a8a90; font-size:12px; cursor:pointer;
    text-decoration:underline; text-underline-offset:2px; align-self:flex-start; }
  .done { border:1px solid #2a4a2e; background:#11210f; border-radius:10px; padding:20px; }
  .done textarea { width:100%; margin-top:8px; padding:10px; border-radius:8px; border:1px solid #2a2a2e;
    background:#0c0c0d; color:#b6b6bb; font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;
    resize:vertical; }
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow" id="eyebrow"></div>
  <h1 id="who"></h1>
  <div id="app"></div>
</div>
<script>
const FORM = __FORM_JSON__;
const META = __META_JSON__;

// Card copy, depth helpers, step headings and every button label arrive on
// FORM from survey_form(lang) — this template holds no recipient-facing
// sentence. That is what makes a language a catalogue entry rather than a
// second translation of the whole page.
const T = FORM.ui || {};
const t = (k, fallback) => (T[k] !== undefined ? T[k] : (fallback || ""));
const AXES = {};
(FORM.cognitive || []).concat(FORM.angle || []).forEach(a => { AXES[a.id] = a; });
const cardsOf = (axis) => ((AXES[axis] || {}).cards || []).map(c => [c.value, c.label, c.helper]);
const ANGLE_IDS = (FORM.angle || []).map(a => a.id);
const DEPTH_HELP = FORM.knowledge.depth_helpers || [];

// Step order, names and the core/module tier come from survey_form()['steps']
// (peer/survey.py::_STEPS) — never restated here. A second copy is the drift
// tests/peer/test_survey_form_parity.py refuses. The depth loop is one logical
// step, internally paged.
const ALL_STEPS = FORM.steps || [];
const STEP_BY_ID = {};
ALL_STEPS.forEach(st => { STEP_BY_ID[st.id] = st; });
const sc = (id, k, fallback) => {
  const st = STEP_BY_ID[id] || {};
  return st[k] !== undefined ? st[k] : (fallback || "");
};
const MODULES = FORM.modules || [];

const S = {
  step: 0,
  wantModules: null,    // null = not asked, false = "I'm done" (skip modules)
  depthIdx: 0,
  identity: { display_name: META.display_name || "", profession: "", function: "", seniority: "" },
  areas: [],            // {area, depth: number|null, recency, detail}
  weaknesses: [],       // {area, mode}
  confidence: null,     // null until the slider is touched
  cognitive: Object.fromEntries(FORM.cognitive.map(s => [s.id, null])),
  angle: Object.fromEntries(FORM.angle.map(s => [s.id, null])),
};

const app = document.getElementById("app");
const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const opt = (v) => '<option value="' + esc(v) + '">' + esc(v) + '</option>';

function hasAreas() { return S.areas.length > 0; }

// A step is skipped when it cannot apply: the depth loop with no areas, and
// every progressive module once the recipient has said they're done. One
// predicate, so forward and backward navigation cannot disagree.
function visible(s) {
  if (!s) return false;
  if (s.tier === "module" && S.wantModules === false) return false;
  if (s.id === "depth" && !hasAreas()) return false;
  return true;
}
function shownSteps() { return ALL_STEPS.filter(visible); }
function walk(from, dir) {
  let i = from + dir;
  while (i > 0 && i < ALL_STEPS.length - 1 && !visible(ALL_STEPS[i])) i += dir;
  return Math.max(0, Math.min(i, ALL_STEPS.length - 1));
}

function addArea(name) {
  const n = (name || "").trim();
  if (!n || S.areas.some(a => a.area.toLowerCase() === n.toLowerCase())) return;
  S.areas.push({ area: n, depth: null, recency: "current", detail: "" });
  render();
}
function toggleArea(name) {
  const i = S.areas.findIndex(a => a.area.toLowerCase() === (name || "").toLowerCase());
  if (i >= 0) { S.areas.splice(i, 1); render(); } else { addArea(name); }
}
function addWeak(name) {
  const n = (name || "").trim();
  if (!n || S.weaknesses.some(w => w.area.toLowerCase() === n.toLowerCase())) return;
  S.weaknesses.push({ area: n, mode: "explain" });
  render();
}

// ── card / pill renderers ──────────────────────────────────────────────
function cardsHtml(bank, current, onpick) {
  return '<div class="cards">' + bank.map(([val, lbl, help]) =>
    '<button class="card ' + (current === val ? "on" : "") + '" onclick="' + onpick + '(' + val + ')">' +
    '<span class="ck">✓</span><span><span class="lbl">' + esc(lbl) + '</span>' +
    '<span class="help">' + esc(help) + '</span></span></button>').join("") + '</div>';
}
function pillsHtml(opts, current, onpick) {
  return '<div class="pills">' + opts.map(([val, lbl]) =>
    '<button class="pill ' + (current === val ? "on" : "") + '" onclick="' + onpick + '(\'' + val.replace(/'/g, "\\'") + '\')">' +
    (current === val ? '✓ ' : '') + esc(lbl) + '</button>').join("") + '</div>';
}
function cardLabel(bank, v) {
  if (v === null || v === undefined) return null;
  const f = bank.find(([val]) => val === v);
  return f ? f[1] : null;
}
function confCaption(v) { return v <= 33 ? sc("confidence", "low") : (v <= 66 ? sc("confidence", "mid") : sc("confidence", "high")); }

// ── per-step body builders ──────────────────────────────────────────────
function stepBody(id) {
  const H = (k, f) => esc(sc(id, k, f));
  const head = () => '<h2>' + H("heading") + '</h2><p class="hint">' + H("hint") + '</p>';

  if (id === "intro") {
    return head() + '<button class="btn primary" onclick="next()">' + esc(t("start", "Start")) + '</button>';
  }
  if (id === "you") {
    return head() +
      '<label class="f"><div class="l">' + esc(t("your_name", "Your name")) + ' *</div><input id="nm" value="' + esc(S.identity.display_name) + '" oninput="setid(\'display_name\',this.value)"></label>' +
      '<label class="f"><div class="l">' + H("profession_label") + '</div><input value="' + esc(S.identity.profession) + '" placeholder="' + H("profession_placeholder") + '" oninput="setid(\'profession\',this.value)"></label>' +
      '<label class="f sub"><div class="l">' + H("function_label") + '</div><select onchange="setid(\'function\',this.value)"><option value="">—</option>' +
        FORM.identity.function_options.map(v => '<option value="' + esc(v) + '"' + (S.identity.function === v ? ' selected' : '') + '>' + esc(v) + '</option>').join("") + '</select></label>' +
      '<label class="f"><div class="l">' + H("seniority_label") + '</div><select onchange="setid(\'seniority\',this.value)"><option value="">—</option>' +
        // value = the stable key the scorer reads; text = the translated
        // label. Sending the label would make the answer depend on which
        // language the recipient happened to read.
        FORM.identity.seniority_options.map(v => '<option value="' + esc(v) + '"' + (S.identity.seniority === v ? ' selected' : '') + '>' + esc((FORM.identity.seniority_labels || {})[v] || v) + '</option>').join("") + '</select></label>';
  }
  if (id === "areas") {
    let h = head();
    h += '<div class="chips">' + FORM.knowledge.starter_domains.map(d =>
      '<button class="chip ' + (S.areas.some(a => a.area === d) ? "on" : "") + '" onclick="toggleArea(\'' + d.replace(/'/g, "\\'") + '\')">' +
      (S.areas.some(a => a.area === d) ? '✓ ' : '') + esc(d) + '</button>').join("") + '</div>';
    h += '<div class="row" style="margin-top:12px"><input id="na" placeholder="' + esc(t("type_and_enter")) + '" onkeydown="if(event.key===\'Enter\'){addArea(this.value);this.value=\'\'}"><button class="btn" onclick="var e=document.getElementById(\'na\');addArea(e.value);e.value=\'\'">' + esc(t("add", "Add")) + '</button></div>';
    if (!hasAreas()) h += '<p class="hint" style="margin-top:12px">' + esc(t("no_areas")) + '</p>';
    return h;
  }
  if (id === "depth") {
    const a = S.areas[S.depthIdx];
    if (!a) return '';
    const counter = t("of_count", "{i} of {n}").replace("{i}", S.depthIdx + 1).replace("{n}", S.areas.length);
    let h = '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">' +
      '<h2>' + esc(sc(id, "heading").replace("{area}", a.area)) + '</h2>' +
      '<span class="hint" style="margin:0;white-space:nowrap">' + esc(counter) + '</span></div>' +
      '<p class="hint">' + H("hint") + '</p>';
    h += '<div class="cards">' + FORM.knowledge.depth_anchors.map((lbl, di) =>
      '<button class="card ' + (a.depth === di ? "on" : "") + '" onclick="setdepth(' + di + ')">' +
      '<span class="ck">✓</span><span><span class="lbl">' + esc(lbl) + '</span>' +
      '<span class="help">' + esc(DEPTH_HELP[di] || "") + '</span></span></button>').join("") + '</div>';
    h += '<button class="skip" style="margin-top:10px" onclick="skipArea()">' + esc(t("skip_area")) + '</button>';
    h += '<div style="margin-top:14px"><div class="l" style="font-size:12px;color:#8a8a90;margin-bottom:6px">' + H("recency_label") + '</div>' +
      pillsHtml(FORM.knowledge.recency_options.map(r => [r, (FORM.knowledge.recency_labels || {})[r] || r]), a.recency, "setrecency") + '</div>';
    h += '<label class="f" style="margin-top:14px"><div class="l">' + esc(FORM.knowledge.detail_prompt) + '</div>' +
      '<input value="' + esc(a.detail) + '" oninput="setdetail(this.value)"></label>';
    return h;
  }
  if (id === "weak") {
    let h = head() +
      '<div class="row"><input id="nw" placeholder="' + H("placeholder") + '" onkeydown="if(event.key===\'Enter\'){addWeak(this.value);this.value=\'\'}"><button class="btn" onclick="var e=document.getElementById(\'nw\');addWeak(e.value);e.value=\'\'">' + esc(t("add", "Add")) + '</button></div>';
    h += S.weaknesses.map((w, i) =>
      '<div class="weakrow"><span class="nm">' + esc(w.area) + '</span>' +
      pillsHtml([["explain", sc(id, "explain")], ["skip", sc(id, "skip")]], w.mode, "setmode_" + i) +
      '<button class="x" aria-label="' + esc(t("remove", "Remove {name}").replace("{name}", w.area)) + '" onclick="delWeak(' + i + ')">✕</button></div>').join("");
    if (S.weaknesses.length === 0) h += '<p class="hint" style="margin-top:12px">' + esc(t("nothing_here")) + '</p>';
    return h;
  }
  if (id === "more") {
    return head() +
      (FORM.modules || []).map(m => '<div class="group" style="margin-bottom:8px"><div class="lbl" style="font-size:14px;font-weight:500;color:#e8e8ea">' +
        esc(m.name) + '</div><div class="help" style="font-size:12px;color:#8a8a90;margin-top:2px">' + esc(m.blurb) + '</div></div>').join("") +
      '<button class="btn" style="margin-top:8px" onclick="gate(false)">' + esc(t("im_done")) + '</button>';
  }
  if (id === "decide") {
    const st = STEP_BY_ID[id] || {};
    const axes = st.axes || [];
    return head() +
      axes.map((axis, i) => '<div class="group"' + (i ? ' style="margin-top:12px"' : '') + '><div class="glabel">' +
        esc(st[i === 0 ? "group_rational" : "group_experiential"] || "") + '</div>' +
        cardsHtml(cardsOf(axis), S.cognitive[axis], "setcog_" + axis) + '</div>').join("");
  }
  if (id === "confidence") {
    const touched = S.confidence !== null;
    const v = touched ? S.confidence : 50;
    // Every node the slider can change is emitted UP FRONT, empty if need
    // be. Revealing one must never require a re-render: re-rendering an
    // <input type=range> mid-drag drops the gesture, so the value jumps once
    // and then stops following the finger.
    return head() +
      '<input type="range" class="' + (touched ? "" : "muted") + '" min="0" max="100" step="5" value="' + v + '" oninput="setconf(this.value)">' +
      '<div class="slabels"><span>' + H("low") + '</span>' +
      '<span class="v">' + (touched ? esc(confCaption(v)) : '') + '</span>' +
      '<span>' + H("high") + '</span></div>' +
      '<p class="hint" id="confhint" style="margin-top:6px' + (touched ? ';display:none' : '') + '">' +
      H("untouched") + '</p>';
  }
  if (id === "recap") {
    return head() + recapHtml();
  }
  // Every remaining step is a single-axis card screen, rendered generically.
  const st = STEP_BY_ID[id] || {};
  const axis = (st.axes || [])[0];
  if (axis) {
    const store = ANGLE_IDS.indexOf(axis) >= 0 ? S.angle : S.cognitive;
    const setter = (ANGLE_IDS.indexOf(axis) >= 0 ? "setang_" : "setcog_") + axis;
    return head() + cardsHtml(cardsOf(axis), store[axis], setter);
  }
  return '';
}

function recapHtml() {
  const R = STEP_BY_ID["recap"] || {};
  const L = (k) => R[k] || "";
  const na = '<span class="na">' + esc(t("not_answered", "—")) + '</span>';
  const lines = [];
  const labelFor = (axis) => {
    const store = ANGLE_IDS.indexOf(axis) >= 0 ? S.angle : S.cognitive;
    return cardLabel(cardsOf(axis), store[axis]);
  };
  const idBits = [];
  if (S.identity.profession.trim()) idBits.push(esc(S.identity.profession.trim()));
  if (S.identity.function) idBits.push(esc(L("closest_to").replace("{fn}", S.identity.function)));
  if (S.identity.seniority) idBits.push(esc(S.identity.seniority));
  if (idBits.length) lines.push(esc(L("you_are")).replace("{bits}", idBits.join(", ")));
  const deep = S.areas.filter(a => a.depth !== null && a.depth >= 3).map(a => esc(a.area));
  if (deep.length) lines.push(esc(L("deep_in")).replace("{areas}", deep.join(", ")));
  const explain = S.weaknesses.filter(w => w.mode === "explain").map(w => esc(w.area));
  const skip = S.weaknesses.filter(w => w.mode === "skip").map(w => esc(w.area));
  if (explain.length) lines.push(esc(L("want_explained")).replace("{areas}", explain.join(", ")));
  if (skip.length) lines.push(esc(L("skip")).replace("{areas}", skip.join(", ")));

  // One line per measured axis, in step order — a new axis shows up here
  // without anyone remembering to add a line.
  const RECAP_KEY = {
    need_for_cognition: "reasoning", visual_verbal: "format", density: "density",
    ambiguity: "uncertainty", numeracy: "numbers", graph_literacy: "charts",
    construal: "focus", regulatory_focus: "framing",
  };
  ALL_STEPS.forEach(st => {
    if (st.id === "decide") {
      lines.push(esc(L("decisions"))
        .replace("{a}", labelFor("rational") ? esc(labelFor("rational")) : na)
        .replace("{b}", labelFor("experiential") ? esc(labelFor("experiential")) : na));
      return;
    }
    (st.axes || []).forEach(axis => {
      const key = RECAP_KEY[axis];
      if (!key) return;
      const v = labelFor(axis);
      lines.push(esc(L(key)) + ": " + (v ? esc(v) : na) + ".");
    });
  });
  if (S.confidence !== null) lines.push(esc(L("confidence")) + ": " + esc(confCaption(S.confidence)) + ".");
  return '<div class="recap">' + lines.map(l => '<div>' + l + '</div>').join("") + '</div>';
}

// ── render ──────────────────────────────────────────────────────────────
function render() {
  document.getElementById("eyebrow").textContent = t("eyebrow", "Communication profile");
  document.getElementById("who").textContent = S.identity.display_name || sc("intro", "heading", "");
  const cur = ALL_STEPS[S.step];
  if (!cur) { app.innerHTML = '<p class="hint">' + esc(t("unavailable", "This form is unavailable.")) + '</p>'; return; }
  const id = cur.id;
  let h = "";
  if (id !== "intro") {
    // Progress counts only the steps this recipient will see, so declining
    // the modules shortens the bar instead of stranding it.
    const shown = shownSteps();
    const at = Math.max(0, shown.findIndex(s => s.id === id)) + 1;
    const pct = Math.round((at / shown.length) * 100);
    h += '<div class="prog"><div class="step">' +
      esc(t("step_of", "Step {n} of {total}").replace("{n}", at).replace("{total}", shown.length)) +
      ' — ' + esc(cur.name) + '</div>' +
      '<div class="bar"><div style="width:' + pct + '%"></div></div></div>';
  }
  h += stepBody(id);
  if (id !== "intro" && id !== "more") {
    let rightDisabled = false;
    let rightLabel = t("next", "Next");
    if (id === "recap") { rightLabel = t("finish_download", "Finish & download"); }
    else if (id === "depth") {
      const a = S.areas[S.depthIdx];
      rightDisabled = a ? (a.depth === null) : false;
      rightLabel = (S.depthIdx < S.areas.length - 1) ? t("next_area", "Next area") : t("next", "Next");
    } else if (id === "decide") {
      rightDisabled = (S.cognitive.rational === null || S.cognitive.experiential === null);
    } else if (id === "you") {
      rightDisabled = !S.identity.display_name.trim();
    }
    const onclick = id === "recap" ? "finish()" : "next()";
    h += '<div class="nav"><button class="btn" onclick="back()">' + esc(t("back", "Back")) + '</button>' +
      '<button class="btn primary" ' + (rightDisabled ? "disabled" : "") + ' onclick="' + onclick + '">' + esc(rightLabel) + '</button></div>';
  } else if (id === "more") {
    h += '<div class="nav"><button class="btn" onclick="back()">' + esc(t("back", "Back")) + '</button>' +
      '<button class="btn primary" onclick="gate(true)">' + esc(t("keep_going", "Keep going")) + '</button></div>';
  }
  app.innerHTML = h;
}

// ── navigation ────────────────────────────────────────────────────────────
window.next = function () {
  const id = ALL_STEPS[S.step].id;
  if (id === "depth" && S.depthIdx < S.areas.length - 1) { S.depthIdx += 1; render(); return; }
  S.step = walk(S.step, 1);
  if (ALL_STEPS[S.step].id === "depth") S.depthIdx = 0;
  render();
};
window.back = function () {
  const id = ALL_STEPS[S.step].id;
  if (id === "depth" && S.depthIdx > 0) { S.depthIdx -= 1; render(); return; }
  S.step = walk(S.step, -1);
  render();
};
// The gate answers and advances in one action.
window.gate = function (want) { S.wantModules = want; window.next(); };

// ── state setters ─────────────────────────────────────────────────────────
// NEVER re-render on a keystroke. render() rebuilds app.innerHTML wholesale,
// which destroys the <input> the recipient is typing into: focus is lost
// after every character, and the freshly-built input is populated from the
// TRIMMED state, so a space inside a name ("Anna Maria") is swallowed the
// moment the next letter arrives. The only thing a keystroke needs to change
// is the heading and whether Next is enabled — so change exactly those.
//
// The value is stored raw and trimmed at submit; trimming per keystroke is
// what made the space unstable.
window.setid = (k, v) => {
  S.identity[k] = v;
  if (k !== "display_name") return;
  const who = document.getElementById("who");
  if (who) who.textContent = v.trim() || sc("intro", "heading", "");
  const nextBtn = document.querySelector(".nav button.primary");
  if (nextBtn) nextBtn.disabled = !v.trim();
};
window.addArea = addArea;
window.toggleArea = toggleArea;
window.addWeak = addWeak;
window.skipArea = () => { S.areas.splice(S.depthIdx, 1); if (S.depthIdx > 0 && S.depthIdx >= S.areas.length) S.depthIdx -= 1; if (!hasAreas()) { window.next(); } else { render(); } };
window.setdepth = (di) => { S.areas[S.depthIdx].depth = di; render(); };
window.setrecency = (v) => { S.areas[S.depthIdx].recency = v; render(); };
window.setdetail = (v) => { S.areas[S.depthIdx].detail = v; };
window.delWeak = (i) => { S.weaknesses.splice(i, 1); render(); };
// Same class as setid: re-rendering on `oninput` destroys the control the
// recipient is currently interacting with. On a range slider that means the
// drag is dropped after the first pixel of movement — the value jumps once
// and then stops following the finger. Update the caption in place instead.
// setdepth/setrecency are click-driven and re-render safely: the gesture is
// already over by the time the DOM is replaced.
window.setconf = (v) => {
  S.confidence = Number(v);
  const el = document.querySelector(".slabels .v");
  if (el) el.textContent = confCaption(S.confidence);
  const range = document.querySelector('input[type=range]');
  if (range) range.classList.remove("muted");
  const hint = document.getElementById("confhint");
  if (hint) hint.style.display = "none";
};
// Bound from the FORM, not from a hand-written list — a new cognitive axis
// gets its setter automatically instead of silently having none.
FORM.cognitive.forEach(s => {
  window["setcog_" + s.id] = (v) => { S.cognitive[s.id] = v; render(); };
});
FORM.angle.forEach(s => {
  window["setang_" + s.id] = (v) => { S.angle[s.id] = v; render(); };
});
// per-weakness mode setters are bound lazily by index
for (let i = 0; i < 64; i++) {
  window["setmode_" + i] = ((idx) => (v) => { if (S.weaknesses[idx]) { S.weaknesses[idx].mode = v; render(); } })(i);
}

function slug(s){return (s||"answers").toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-|-$/g,"")||"answers";}

function dropNulls(obj) {
  const out = {};
  for (const k in obj) if (obj[k] !== null && obj[k] !== undefined) out[k] = obj[k];
  return out;
}

window.finish = function () {
  const areas = S.areas.filter(a => a.depth !== null).map(a => {
    const o = { area: a.area, depth: a.depth, recency: a.recency };
    if ((a.detail || "").trim()) o.detail = a.detail.trim();
    return o;
  });
  const knowledge = { areas: areas, weaknesses: S.weaknesses };
  if (S.confidence !== null) knowledge.confidence = S.confidence;
  const identity = {};
  for (const k in S.identity) {
    identity[k] = typeof S.identity[k] === "string" ? S.identity[k].trim() : S.identity[k];
  }
  const payload = {
    identity: identity,
    survey: {
      identity: { profession: identity.profession, function: identity.function, seniority: identity.seniority },
      knowledge: knowledge,
      cognitive: dropNulls(S.cognitive),
      angle: dropNulls(S.angle),
    },
  };
  if (META.person_id) payload.person_id = META.person_id;
  const fname = "okuro-survey-" + (META.person_id || slug(identity.display_name)) + ".json";
  const text = JSON.stringify(payload, null, 2);

  // THE DOWNLOAD IS BEST-EFFORT. a.download + Blob is unsupported or
  // silently blocked on iOS Safari, in in-app browsers (Gmail, Outlook,
  // WhatsApp) and inside sandboxed email previews — which is exactly where
  // a file sent by email gets opened. It used to be the ONLY way to return
  // answers, so a recipient on a phone finished the survey and had nothing
  // to send back. The visible text below is the guarantee; the file is the
  // convenience.
  let downloadOk = false;
  try {
    const blob = new Blob([text], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = fname;
    // A browser that does not implement the download attribute would open
    // the blob instead of saving it, so treat missing support as failure
    // rather than claiming a file the recipient will never find.
    if (!("download" in a)) throw new Error("no download support");
    document.body.appendChild(a); a.click(); a.remove();
    downloadOk = true;
  } catch (e) { downloadOk = false; }

  app.innerHTML = '<div class="done"><b>' + esc(t("download_title", "Done — thank you.")) + '</b>' +
    // Only claim a download that actually happened. Telling someone on a
    // phone to "send the file back" when no file exists is what turns a
    // completed survey into a lost one.
    (downloadOk
      ? '<div class="hint" style="margin-top:6px">' +
        esc(t("download_body", "")).replace("{file}", '<b>' + esc(fname) + '</b>') + '</div>'
      : '') +
    '<div class="hint" style="margin-top:14px">' + esc(t("copy_hint", "")) + '</div>' +
    '<textarea id="payload" readonly rows="8" onclick="this.select()">' + esc(text) + '</textarea>' +
    '<button class="btn primary" style="margin-top:8px" onclick="copyPayload()">' +
    esc(t("copy_button", "Copy")) + '</button>' +
    '<span id="copied" class="hint" style="margin-left:10px"></span></div>';
};

window.copyPayload = function () {
  const ta = document.getElementById("payload");
  ta.select();
  ta.setSelectionRange(0, ta.value.length);   // iOS needs the explicit range
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  if (!ok && navigator.clipboard) {
    navigator.clipboard.writeText(ta.value).then(
      () => { document.getElementById("copied").textContent = t("copied", "Copied"); },
      () => {});
    return;
  }
  // execCommand is deprecated but still the only path that works inside the
  // in-app browsers this fallback exists for; clipboard.writeText needs a
  // secure context, and this page is opened from a file.
  document.getElementById("copied").textContent = ok
    ? t("copied", "Copied")
    : "";
};

render();
</script>
</body>
</html>
"""


def build_offline_survey_html(
    form: dict[str, Any],
    person: Optional[dict[str, Any]] = None,
) -> str:
    """Return a self-contained v2 offline survey HTML page.

    ``form`` is ``okuro.peer.survey.survey_form()``. ``person`` None → blank
    self-identify (recipient enters name; import creates the person).
    ``person`` dict (id, display_name) → prefilled name for re-profiling.
    """
    if person:
        meta = {"person_id": person.get("id"),
                "display_name": person.get("display_name") or person.get("id")}
    else:
        meta = {"person_id": None, "display_name": None}
    # The document's own lang attribute, not just the strings. A German
    # survey served as lang="en" makes a screen reader pronounce it in
    # English and breaks hyphenation — the copy would be translated and the
    # document would still be lying about itself.
    return (
        _TEMPLATE
        .replace("__LANG__", str(form.get("lang") or "en"))
        .replace("__FORM_JSON__", json.dumps(form))
        .replace("__META_JSON__", json.dumps(meta))
    )


__all__ = ["build_offline_survey_html"]
