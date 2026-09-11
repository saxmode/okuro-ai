# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: provides a module for composing and loading visual briefs using an LLM
# index:
#   imports
#   def _seed_for
#   def _key
#   def cache_path
#   def compose_brief
#   def load_or_compose_brief
#   def load_or_compose_scene
#   def _invoke_bridge
#   def _extract_json
#   def _normalize
# AGENT_HEADER_END -->
"""Topic -> Brief (single LLM call). The Brief is a structured semantic
contract: it captures the LLM's understanding of the topic and selects a
template + slots. The renderer (templates.py) then builds the Scene
deterministically from the Brief.

This separates SEMANTIC THINKING (LLM) from LAYOUT (Python templates).
The LLM no longer picks pixel coordinates; it picks meaning.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

from .templates import Brief, render_brief
from .scene import Scene


# ---------------------------------------------------------------- prompt

BRIEF_PROMPT = """\
You translate a topic into a structured visual brief for an editorial
infographic engine. The engine renders Swiss-style diagrams in a
strict Architecture-Noir aesthetic (pure black, monospace, hairline
white strokes, one chromatic accent). You do NOT pick coordinates,
sizes, fonts, or geometry. You pick MEANING and a TEMPLATE.

OUTPUT: a single JSON object matching the Brief schema. NO prose,
NO markdown, NO code fences.

----------------------------------------------------------------------
Step 1 -- understand the topic.
Ask yourself, internally, only:
  (a) What KIND of thing is it? Is it a STATIC structure (atom, mesh,
      hierarchy) or a PROCESS (encryption, training, compression)?
  (b) What is the conceptual TENSION? input vs output, ordered vs
      chaotic, scattered vs convergent, paired, layered, cyclical, flow?
  (c) What are its discrete PARTS, if any?
  (d) Is there a clear AXIS (left->right, top->bottom, in->out)?

Step 2 -- pick exactly one TEMPLATE.

  transformation_bipartite -- something turns A into B with a
                               transform/key in the middle.
                               (encryption, hashing, compression,
                                tokenization, encoding, training)
                               Slots: motif (permutation|dual_pane|
                                scramble), n, left_label, right_label,
                                center_label, measure_label,
                                pane_left, pane_right.

  central_radial            -- something protected/anchored at center
                               with concentric layers around it.
                               (zero trust, atom, scope, agent loop,
                                defense in depth)
                               Slots: motif (orbit_rings|iso|
                                constellation), rings, iso_shape,
                                center_label.

  axis_flow                 -- a sequence of N labelled stages along
                               an axis with a clear arrow at the end.
                               (pipeline, supply chain, sequence)
                               Slots: parts (list of stage names).

  stratified                -- N stacked horizontal layers, each
                               labelled.
                               (memory hierarchy, abstraction layers,
                                osi model)
                               Slots: parts (list of layer names).

  field_with_focus          -- a sparse field of nodes with one
                               bright anchor and selected edges.
                               (attention, distributed, network,
                                routing)
                               Slots: n, center_label.

  contrast_pair             -- two paired blocks comparing states.
                               (signal/noise, raw/processed,
                                source/target)
                               Slots: pane_left, pane_right,
                                left_label, right_label.

  wave_stack                -- waves filling content area.
                               (physics, oscillation, frequency,
                                signal)
                               Slots: measure_label.

  sankey                    -- many-to-many flows with PROPORTIONAL
                               ribbon widths. Use when each source
                               node distributes its quantity across
                               multiple target nodes.
                               (data lineage, traffic split, energy
                                budget, market share, request fan-out)
                               Slots: sankey_left (list of source
                                names), sankey_right (list of target
                                names), sankey_flows (list of
                                [src_idx, dst_idx, weight] triples).

  tree                      -- top-down hierarchical tree of named
                               nodes with parent indices.
                               (taxonomies, dependency trees, file
                                trees, decision trees, org charts)
                               Slots: tree_nodes (list of names),
                                tree_parents (list of int indices,
                                -1 for root).

  marey_grid                -- Marey-style space-time chart: stations on
                               the left axis (rows), time on the bottom
                               axis, n diagonal trajectories. Slope =
                               speed. Use for pipelines with a time
                               dimension, training-curve comparisons,
                               version evolution, transit/flow with
                               schedule-like data.
                               Slots: stations (list of stage names),
                                trajectories (list of [(time_norm,
                                station_idx), ...] waypoint lists),
                                trajectory_labels (list[str]), focal_idx
                                (int — which trajectory is chromatic),
                                time_ticks (int).

  ridgeline_stack           -- Joy Division 'Unknown Pleasures' ridgeline:
                               n stacked hairline density curves, one row
                               chromatically focal. Use for model/category
                               comparisons across a shared x-axis.
                               Slots: ridges (list of [name, [v0..vn 0..1]]),
                                focal_idx (int).

  sorted_matrix             -- Bertin-style reorderable matrix: rows × cols
                               of cells, each glyph encodes a 0..1 value.
                               Sort exposes diagonals/clusters. Use for
                               confusion matrix, attention map, feature×
                               model performance grid, co-occurrence.
                               Slots: matrix_rows (row names), matrix_cols
                                (col names), matrix (2D 0..1 values),
                                cluster_box ([r0,c0,r1,c1] inclusive,
                                optional), matrix_glyph ('fill'|'dot').

  flow_with_focal_thread    -- Sankey but with most flows desaturated and
                               ONE chromatic thread traced source→sink.
                               Answers 'where does X actually go?' Use for
                               energy/budget/attention/data lineage with a
                               single highlighted path.
                               Slots: sankey_left, sankey_right,
                                sankey_flows (same as sankey), focal_idx
                                (index into sankey_flows for the traced
                                lineage).

  small_multiples           -- 3x4 grid of mini-plates, each cell a tiny
                               sparkline + title + stat, one cell
                               chromatically highlighted (Tufte / Bloomberg
                               contact sheet). Use for n variants of the
                               same shape: A/B, year-over-year, country-by-
                               country, model-by-model, per-week patterns.
                               Slots: multiples (list of [title, stat,
                                values]), highlight_cell_idx (int),
                                verbosity. Up to 12 cells.

  annotated_specimen        -- ONE focal subject in the center surrounded
                               by a radial halo of leader lines pointing to
                               k labelled facts in the margins. Patek
                               Philippe technical drawing meets editorial
                               figure. Use for "anatomy of X", model card,
                               benchmark profile, single-entity portraits.
                               REQUIRES the LLM to supply 4-8 callouts (each
                               a [tag, value] pair) — they ARE the motif.
                               Slots: motif (orbit_rings|iso|constellation),
                                center_label, callouts (4-8 short pairs).

  volume_3d                 -- WebGL-rendered 3D volume. Pick this only
                               when the topic is INHERENTLY 3D — i.e.
                               flat 2D would erase the meaning. Three
                               variants:
                               * cube_lattice      -- voxel grid /
                                 tensor / 3D memory; one optional
                                 highlighted cell. (voxel grid, 3D
                                 cache line, attention block, gpu
                                 sram tile, video frame stack)
                               * embedding_cloud   -- point cloud in
                                 R^3 with k clusters. (word/sentence/
                                 image embeddings, latent space,
                                 t-sne, pca projection, knn)
                               * axis_3d           -- right-handed XYZ
                                 axes with optional vector. (rotation,
                                 frustum, basis change, coordinate
                                 transform, camera pose)
                               Slots: volume_kind (one of the three),
                                n_3d (cardinality: voxels per side or
                                num points), clusters (embedding only),
                                highlight_cell ([x,y,z] int triple
                                inside [0..n_3d), cube_lattice only),
                                vector_to ([x,y,z] floats in [-1,1],
                                axis_3d only).

Step 3 -- fill the slots.
  Always provide:
    eyebrow  -- short uppercase eyebrow with a category prefix.
                Pattern: "NN // DOMAIN" e.g. "07 // CRYPTOGRAPHY".
    title    -- 1-3 word UPPERCASE title that reads with the visual.
                NOT the bare topic; a sharper editorial title.
    meta     -- short bottom-right tag, e.g. "AES-256 // n=10",
                "5 stages", "n=22 // 1 query".
    austerity -- "editorial" (default, full type chrome) OR
                 "poster" (title only, hero/single-motif plates with
                 maximum negative space). Pick "poster" when the
                 visual alone makes the point and chrome would
                 dilute it (e.g. central_radial+orbit_rings hero,
                 a single iso volume, a sun-and-cone).
    accent    -- chromatic accent color: "green" (default, neutral),
                 "amber" (warm/energy/CPU), "red" (alert/security),
                 "blue" (data/cool), "violet" (research/abstract),
                 "cyan" (network/signal). Pick from the topic's tonal
                 cue. When in doubt, leave "green".
    verbosity -- "lean" (default, just the motif) OR "rich" (auto-
                 dressed with leader-line callouts + a corner legend
                 block). Pick "rich" when the topic carries 2-4 named
                 facts the reader benefits from seeing surfaced
                 (e.g. "router has 8 experts, top-2 routed, n=8",
                 "L1 cache 32KB hit 1ns").
    callouts  -- list of [tag, value] pairs, ONLY when verbosity is
                 "rich". 2-4 short pairs max. Tag is a short
                 lowercase word (e.g. "router", "depth", "n",
                 "latency"). Value is a short string (e.g. "8",
                 "1 ns", "256-bit"). Templates anchor these to the
                 motif and route them as architectural leader lines
                 to the margins.

  Provide where applicable:
    parts        -- ordered list, used by axis_flow / stratified
    left_label   -- short word for left zone (e.g. "plaintext")
    right_label  -- short word for right zone (e.g. "ciphertext")
    center_label -- short word for the central anchor (e.g. "key")
    measure_label-- value tag for a dimension line (e.g. "n=10",
                    "256 bit", "amp")
    motif        -- only for templates that allow alternates
    pane_left, pane_right -- for transformation_bipartite/contrast_pair:
        lattice_ordered | lattice_scrambled |
        particles_uniform | particles_clustered | constellation
    iso_shape    -- cube|prism|octa  (central_radial only)
    n            -- cardinality where natural
    rings        -- for central_radial+orbit_rings
    chaos        -- for transformation_bipartite+scramble (0..1)
    edges        -- for field_with_focus on graph topics: list of
                    [from_idx, to_idx] with from/to in [0..n).
                    Express the topology (e.g. router=0 connected
                    to expert nodes 1..n-1 -> [[0,1],[0,2],...]).
    sankey_left, sankey_right, sankey_flows -- only for sankey.
    tree_nodes, tree_parents -- only for tree.
    volume_kind, n_3d, clusters, highlight_cell, vector_to -- only for
        volume_3d.

Use eyebrows that suggest a series number ("01 //", "02 //", ...)
based on the topic's domain order in your head. Domain options:
CRYPTOGRAPHY, SYSTEMS, NETWORKING, INFO THEORY, ML / TRANSFORMER,
PHYSICS, ARCHITECTURE, OBSERVABILITY, CI/CD, SECURITY, DATA, AGENTS,
PIPELINE, COMPRESSION, MEMORY, GEOMETRY, EMBEDDINGS, GPU.

Topic: "{topic}"
Category hint: "{category}"
Seed: {seed}

Return ONLY the Brief JSON.
"""


# ---------------------------------------------------------------- runtime

CACHE_ROOT = Path(
    os.environ.get(
        "TM_ILL_BRIEF_CACHE",
        str(Path.home() / ".cache" / "tm-illustrator" / "briefs"),
    )
)


def _seed_for(topic: str, category: str | None) -> int:
    h = hashlib.sha256(f"{topic}|{category}".encode()).hexdigest()
    return int(h[:8], 16)


def _key(topic: str, category: str | None, seed: int | None) -> str:
    payload = json.dumps(
        {"topic": topic, "category": category or "", "seed": seed},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def cache_path(topic: str, category: str | None = None,
               seed: int | None = None) -> Path:
    return CACHE_ROOT / f"{_key(topic, category, seed)}.json"


def compose_brief(topic: str, category: str | None = None,
                  seed: int | None = None) -> Brief:
    """Single LLM call. Returns a validated Brief."""
    seed = seed if seed is not None else _seed_for(topic, category)
    prompt = (BRIEF_PROMPT
              .replace("{topic}", topic)
              .replace("{category}", category or "")
              .replace("{seed}", str(seed)))
    raw = _invoke_bridge(prompt)
    data = _extract_json(raw)
    data = _normalize(data, topic=topic, category=category, seed=seed)
    return Brief.model_validate(data)


def load_or_compose_brief(topic: str, category: str | None = None,
                           seed: int | None = None,
                           force: bool = False) -> tuple[Brief, bool]:
    """Return (brief, cache_hit)."""
    p = cache_path(topic, category, seed)
    if p.exists() and not force:
        try:
            return Brief.model_validate_json(p.read_text()), True
        except Exception:
            pass
    brief = compose_brief(topic, category=category, seed=seed)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(brief.model_dump_json(indent=2))
    return brief, False


def load_or_compose_scene(topic: str, category: str | None = None,
                          seed: int | None = None,
                          force: bool = False) -> tuple[Scene, bool]:
    """Convenience: brief -> scene in one call."""
    brief, hit = load_or_compose_brief(topic, category=category,
                                        seed=seed, force=force)
    return render_brief(brief), hit


# ---------------------------------------------------------------- helpers

def _invoke_bridge(prompt: str, capability: str = "quality") -> str:
    """Compose via okuro's NATIVE bridge (in-process) — routed to whatever model
    okuro has available for ``capability``, no subprocess, no hardcoded model.
    This is the port's core: the illustrator now uses okuro's model registry.
    ``tool=True`` is the stateless tooling bridge (no agent context — faster)."""
    from okuro.bridge.invoke import invoke

    res = invoke(prompt=prompt, capability=capability, tool=True, timeout=180)
    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError(f"bridge_invoke failed: {(res or {}).get('error') if isinstance(res, dict) else 'invoke failed'}")
    return res.get("output") or ""


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0]
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in response:\n{raw}")
    return json.loads(raw[start:end + 1])


def _normalize(data: dict, topic: str, category: str | None,
               seed: int) -> dict:
    """Coerce common LLM drift on a Brief."""
    if isinstance(data, dict):
        data = {(k.lower() if isinstance(k, str) else k): v
                for k, v in data.items()}
    # Strip explicit nulls — LLMs sometimes emit `"rings": null` for slots
    # they don't use, which collides with our typed defaults.
    data = {k: v for k, v in data.items() if v is not None}
    # unwrap envelope
    for key in ("brief", "result", "output", "data"):
        if key in data and isinstance(data[key], dict) and "template" in data[key]:
            data = data[key]
            data = {(k.lower() if isinstance(k, str) else k): v
                    for k, v in data.items()}
            break
    data.setdefault("topic", topic)
    if category is not None:
        data.setdefault("category", category)
    data.setdefault("seed", seed)
    if isinstance(data.get("template"), str):
        data["template"] = data["template"].lower()
    # alias: template names sometimes hyphenated
    tmap = {
        "transformation-bipartite": "transformation_bipartite",
        "central-radial":           "central_radial",
        "axis-flow":                "axis_flow",
        "field-with-focus":         "field_with_focus",
        "contrast-pair":            "contrast_pair",
        "wave-stack":                "wave_stack",
        "volume-3d":                "volume_3d",
        "3d":                        "volume_3d",
        "three_d":                   "volume_3d",
        "annotated-specimen":       "annotated_specimen",
        "specimen":                  "annotated_specimen",
        "specimen-card":            "annotated_specimen",
        "small-multiples":          "small_multiples",
        "multiples":                 "small_multiples",
        "battery":                   "small_multiples",
        "marey":                    "marey_grid",
        "marey-grid":               "marey_grid",
        "space-time":               "marey_grid",
        "ridgeline":                "ridgeline_stack",
        "ridgeline-stack":          "ridgeline_stack",
        "joyplot":                  "ridgeline_stack",
        "joy":                       "ridgeline_stack",
        "matrix":                    "sorted_matrix",
        "confusion-matrix":         "sorted_matrix",
        "reorderable-matrix":       "sorted_matrix",
        "focal-thread":             "flow_with_focal_thread",
        "focal-flow":               "flow_with_focal_thread",
    }
    if data.get("template") in tmap:
        data["template"] = tmap[data["template"]]
    # A missing/unknown template must not crash compose — the LLM occasionally
    # omits it. Default deterministically from the seed so the same topic stays
    # stable, instead of raising a Brief ValidationError.
    from okuro.media.illustrator.templates import TEMPLATES
    if data.get("template") not in TEMPLATES:
        keys = sorted(TEMPLATES)
        data["template"] = keys[int(seed) % len(keys)] if keys else data.get("template")
    # parts may arrive as comma-string
    if isinstance(data.get("parts"), str):
        data["parts"] = [p.strip() for p in data["parts"].split(",") if p.strip()]
    # sankey_flows: coerce dict-shape into tuple-shape
    if isinstance(data.get("sankey_flows"), list):
        out = []
        for f in data["sankey_flows"]:
            if isinstance(f, dict):
                out.append([
                    f.get("src", f.get("from", f.get("source", 0))),
                    f.get("dst", f.get("to", f.get("target", 0))),
                    float(f.get("value", f.get("weight", 1))),
                ])
            elif isinstance(f, (list, tuple)) and len(f) >= 3:
                out.append([f[0], f[1], float(f[2])])
        data["sankey_flows"] = out
    # tree_parents: accept None as -1
    if isinstance(data.get("tree_parents"), list):
        data["tree_parents"] = [-1 if p in (None, "root") else int(p)
                                 for p in data["tree_parents"]]
    # volume_kind: hyphenated -> underscored
    vmap = {
        "cube-lattice":    "cube_lattice",
        "embedding-cloud": "embedding_cloud",
        "axis-3d":         "axis_3d",
        "axis_3":          "axis_3d",
    }
    if isinstance(data.get("volume_kind"), str):
        vk = data["volume_kind"].strip().lower()
        data["volume_kind"] = vmap.get(vk, vk)
    # highlight_cell: accept {x,y,z} dict
    hc = data.get("highlight_cell")
    if isinstance(hc, dict):
        data["highlight_cell"] = [int(hc.get("x", 0)), int(hc.get("y", 0)),
                                    int(hc.get("z", 0))]
    elif isinstance(hc, (list, tuple)) and len(hc) >= 3:
        data["highlight_cell"] = [int(hc[0]), int(hc[1]), int(hc[2])]
    # vector_to: accept {x,y,z} dict
    vt = data.get("vector_to")
    if isinstance(vt, dict):
        data["vector_to"] = [float(vt.get("x", 0)), float(vt.get("y", 0)),
                              float(vt.get("z", 0))]
    elif isinstance(vt, (list, tuple)) and len(vt) >= 3:
        data["vector_to"] = [float(vt[0]), float(vt[1]), float(vt[2])]
    # edges as flat list of dicts {from,to}
    if isinstance(data.get("edges"), list):
        out = []
        for e in data["edges"]:
            if isinstance(e, dict):
                out.append([e.get("from", e.get("src", 0)),
                            e.get("to",   e.get("dst", 0))])
            elif isinstance(e, (list, tuple)) and len(e) >= 2:
                out.append([e[0], e[1]])
        data["edges"] = out
    # trajectories: accept dict-shape waypoints {t,station} → tuples
    if isinstance(data.get("trajectories"), list):
        out = []
        for traj in data["trajectories"]:
            if isinstance(traj, list):
                wp = []
                for w in traj:
                    if isinstance(w, dict):
                        t = w.get("t") or w.get("time") or w.get("x") or 0
                        s = w.get("station") or w.get("s") or w.get("y") or 0
                        wp.append([float(t), float(s)])
                    elif isinstance(w, (list, tuple)) and len(w) >= 2:
                        wp.append([float(w[0]), float(w[1])])
                out.append(wp)
        data["trajectories"] = out
    # ridges: accept dict-shape rows {name,values}
    if isinstance(data.get("ridges"), list):
        out = []
        for r in data["ridges"]:
            if isinstance(r, dict):
                name = r.get("name") or r.get("label") or ""
                vals = r.get("values") or r.get("data") or []
                if isinstance(vals, list):
                    vals = [float(v) for v in vals if isinstance(v, (int, float))]
                out.append([str(name), vals])
            elif isinstance(r, (list, tuple)) and len(r) >= 2:
                vals = r[1] if isinstance(r[1], list) else []
                vals = [float(v) for v in vals if isinstance(v, (int, float))]
                out.append([str(r[0]), vals])
        data["ridges"] = out
    # matrix: ensure 2D float list
    if isinstance(data.get("matrix"), list):
        out = []
        for row in data["matrix"]:
            if isinstance(row, list):
                out.append([float(v) if isinstance(v, (int, float)) else 0.0
                            for v in row])
        data["matrix"] = out
    # cluster_box: accept dict {r0,c0,r1,c1}
    cb = data.get("cluster_box")
    if isinstance(cb, dict):
        data["cluster_box"] = [int(cb.get("r0", 0)), int(cb.get("c0", 0)),
                                int(cb.get("r1", 0)), int(cb.get("c1", 0))]
    elif isinstance(cb, (list, tuple)) and len(cb) >= 4:
        data["cluster_box"] = [int(cb[0]), int(cb[1]), int(cb[2]), int(cb[3])]
    # multiples: accept list of dicts {title,stat,values} → coerce to triples
    if isinstance(data.get("multiples"), list):
        out = []
        for m in data["multiples"]:
            if isinstance(m, dict):
                title = m.get("title") or m.get("name") or m.get("label") or ""
                stat = m.get("stat") or m.get("value") or m.get("v") or ""
                vals = m.get("values") or m.get("series") or m.get("data") or []
                if isinstance(vals, list):
                    vals = [float(v) for v in vals if isinstance(v, (int, float))]
                out.append([str(title), str(stat), vals])
            elif isinstance(m, (list, tuple)) and len(m) >= 3:
                vals = m[2]
                if isinstance(vals, list):
                    vals = [float(v) for v in vals if isinstance(v, (int, float))]
                out.append([str(m[0]), str(m[1]), vals])
        data["multiples"] = out
    # callouts: accept dicts {tag,value} / {label,value} / {key,value}
    if isinstance(data.get("callouts"), list):
        out = []
        for c in data["callouts"]:
            if isinstance(c, dict):
                tag = c.get("tag") or c.get("label") or c.get("key") or ""
                val = c.get("value") or c.get("val") or c.get("v") or ""
                out.append([str(tag), str(val)])
            elif isinstance(c, (list, tuple)) and len(c) >= 2:
                out.append([str(c[0]), str(c[1])])
        data["callouts"] = out
    # verbosity normalizer
    if isinstance(data.get("verbosity"), str):
        v = data["verbosity"].strip().lower()
        if v in ("editorial", "full", "rich+", "verbose"):
            v = "rich"
        if v in ("minimal", "bare", "naked"):
            v = "lean"
        data["verbosity"] = v if v in ("lean", "rich") else "lean"
    return data
