# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: provides scene composition using LLM-driven motif primitives
# index:
#   imports
#   def _seed_for
#   def compose_via_bridge
#   def _normalize
#   def _invoke_bridge
#   def _extract_json
#   def compose_from_file
# AGENT_HEADER_END -->
"""Topic -> Scene. LLM-driven composition over the primitive grammar."""
from __future__ import annotations
import json
import hashlib
import subprocess
from pathlib import Path

from .scene import Scene


PROMPT = """\
You are an infographic composer for the "Architecture Noir" style.

OUTPUT: a single JSON object matching the Scene schema. NO prose, NO markdown.

STYLE (locked, not your concern beyond knowing it exists):
- pure black background
- hairline white strokes
- subtle RGB chromatic aberration on focal elements only
- one bright focal point per scene
- generous negative space (>= 60% empty)

PRIMITIVES you may compose (motif primitives in CAPS, supports in lowercase):

  SPATIAL motifs (use when topic has a clear spatial metaphor):
  - CONSTELLATION {id, n_stars, edges, bbox, seed}
        — networks, distributed systems, related entities, mappings
  - WAVE {id, waves, amp, wavelength, y, width, chromatic}
        — physics, signals, oscillation, frequency
  - CONE {id, apex, base_y, rings, base_rx, base_ry}
        — capture, funnel, narrowing scope, hierarchy top-down
  - ISO {id, cx, cy, size, shape: cube|prism|octa}
        — systems, containers, isolated volumes, modules
  - ARROWS {id, count, start, spacing, size}
        — flow, pipeline, supply chain, sequential steps
  - ORBIT_RINGS {id, cx, cy, rings, base_r, spacing, chromatic}
        — layered defense, atomic structure, agent loop, scope rings
  - LATTICE {id, cols, rows, cx, cy, spacing, node_r, show_edges, highlight}
        — grids, kv-cache, attention maps, structured data
  - PARTICLES {id, n, bbox, seed, radius, density_falloff: uniform|gaussian|ring, focus}
        — entropy, diffusion, probability cloud, noise

  TRANSFORMATION motifs (use when the TOPIC IS A PROCESS that turns A into B):
  - PERMUTATION {id, n, left_x, right_x, top_y, bottom_y, chromatic, seed}
        — encryption, hashing, routing, S-box, lookup mapping, dispatch
  - DUAL_PANE {id, left_kind, right_kind, cols, rows, spacing, seed, arrow}
        left/right_kind: lattice_ordered | lattice_scrambled | particles_uniform |
                         particles_clustered | constellation
        — before/after, plaintext/ciphertext, input/output, raw/processed,
          training before/after convergence, compression, transformation
  - SCRAMBLE {id, cols, rows, cx, cy, spacing, node_r, seed, chaos}
        — diffusion, avalanche, scrambling, chaos, randomization,
          adversarial perturbation

  Supports (always allowed):
  - focal {id, cx, cy, r, halo_r, chromatic}     — bright anchor (key/secret/agent)
  - field {id, pattern, opacity}  pattern: dotgrid|isoaxis|horizon
  - label {id, text, x, y, size, case}  case: upper|lower|as-is

ANIMATIONS: omit them. The "animations" array should be empty -- we are
focused on layouts right now. Set "animations": [] and move on.

TOPIC -> MOTIF COOKBOOK (use as starting point, not a hard rule):
  encryption, ciphering, hashing, S-box   -> PERMUTATION + focal-as-key
  obfuscation, scrambling, randomization  -> SCRAMBLE + shuffle anim
  before/after, compression, transformation -> DUAL_PANE
  diffusion, avalanche, noise             -> SCRAMBLE or PARTICLES + shuffle/drift
  routing, dispatch, gating               -> PERMUTATION or CONSTELLATION+focal
  capture, funnel, ingestion              -> CONE
  layered defense, zero trust, scope      -> ORBIT_RINGS
  attention maps, kv-cache, mesh, grid    -> LATTICE
  signals, physics, oscillation           -> WAVE
  flow, pipeline, sequential              -> ARROWS
  network, distributed, related entities  -> CONSTELLATION
  diffusion model, entropy, probability   -> PARTICLES (gaussian/ring)
  agent loop, observe-decide-act          -> ORBIT_RINGS + focal
  containers, modules, isolated systems   -> ISO

RULES:
1. Pick ONE dominant motif primitive from the list above.
   For PROCESS / TRANSFORMATION topics (encryption, training, hashing,
   compression, before/after) STRONGLY PREFER permutation, dual_pane, or
   scramble over generic central+grid compositions.
2. Add at most ONE focal point — only if it has SEMANTIC meaning (the key
   in encryption, the gate in MoE, the agent in a loop). DO NOT add a
   focal "just because". For DUAL_PANE and SCRAMBLE, focals usually hurt.
3. Add ONE field for atmosphere (usually dotgrid at low opacity 0.06-0.15).
4. Add 0-1 label, monospace, UPPERCASE, low opacity (0.5-0.7), size 12-14.
   The label should be a FUNCTIONAL HINT (e.g. "PLAINTEXT >> CIPHERTEXT"),
   not a topic restatement.
5. animations: []  -- skip animations entirely for now.
6. Composition: centered for SPATIAL motifs; horizontal-axis for
   TRANSFORMATION motifs (input on left, output on right).
7. Canvas: 1200 wide, 600 tall.

Topic: "{topic}"
Category: "{category}"
Seed: {seed}

Return JSON only.
"""


def _seed_for(topic: str, category: str | None) -> int:
    h = hashlib.sha256(f"{topic}|{category}".encode()).hexdigest()
    return int(h[:8], 16)


def compose_via_bridge(topic: str, category: str | None = None,
                       seed: int | None = None) -> Scene:
    """Use mcp__okuro__bridge_invoke (via okuro CLI) to generate the scene."""
    seed = seed if seed is not None else _seed_for(topic, category)
    prompt = (PROMPT
              .replace("{topic}", topic)
              .replace("{category}", category or "")
              .replace("{seed}", str(seed)))

    raw = _invoke_bridge(prompt)
    data = _extract_json(raw)
    data = _normalize(data, topic=topic, category=category, seed=seed)
    scene = Scene.model_validate(data)
    scene.seed = seed
    return scene


def _normalize(data: dict, topic: str, category: str | None, seed: int) -> dict:
    """Coerce common LLM drift back onto the schema.

    - lowercase top-level keys ("PRIMITIVES" -> "primitives") and kind values
    - "type" -> "kind" on every primitive/animation
    - inject missing top-level fields (topic/category/seed)
    - canvas {w,h} -> {width,height}
    - unwrap {"scene": {...}} envelopes
    """
    # lowercase top-level keys -- LLM may mirror CAPS used in the prompt
    if isinstance(data, dict):
        data = {(k.lower() if isinstance(k, str) else k): v for k, v in data.items()}
    # alias common synonyms LLM picks for the top-level list
    for alias in ("motifs", "elements", "shapes", "items"):
        if alias in data and "primitives" not in data:
            data["primitives"] = data.pop(alias)
            break

    # unwrap single-key envelope: {"scene": {...}} or {"infographic": {...}}
    if isinstance(data, dict) and "primitives" not in data:
        for key in ("scene", "infographic", "data", "result", "output"):
            if key in data and isinstance(data[key], dict) and "primitives" in data[key]:
                data = data[key]
                if isinstance(data, dict):
                    data = {(k.lower() if isinstance(k, str) else k): v
                            for k, v in data.items()}
                break

    data.setdefault("topic", topic)
    if category is not None:
        data.setdefault("category", category)
    data.setdefault("seed", seed)

    canvas = data.get("canvas") or {}
    if "w" in canvas and "width" not in canvas:
        canvas["width"] = canvas.pop("w")
    if "h" in canvas and "height" not in canvas:
        canvas["height"] = canvas.pop("h")
    if canvas:
        data["canvas"] = canvas

    def _kind_swap(obj: dict) -> dict:
        if "kind" not in obj and "type" in obj:
            obj["kind"] = obj.pop("type")
        # LLM may uppercase to mirror prompt emphasis -- canonicalize
        if isinstance(obj.get("kind"), str):
            obj["kind"] = obj["kind"].lower()
        return obj

    def _coerce_pair(v):
        if isinstance(v, dict):
            for kx, ky in (("x","y"), ("col","row"), ("c","r"), ("cx","cy")):
                if kx in v and ky in v:
                    return [v[kx], v[ky]]
        return v

    def _coerce_pair_list(v):
        """Coerce a list of pairs (lattice.highlight, constellation.edges)."""
        if isinstance(v, int):
            # LLM sent count instead of list -- drop, schema default is empty
            return []
        if not isinstance(v, list):
            return v
        out = []
        for item in v:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out.append(list(item))
            elif isinstance(item, dict):
                pair = _coerce_pair(item)
                if isinstance(pair, list) and len(pair) == 2:
                    out.append(pair)
                # else drop silently — unknown shape
            # flat int is ambiguous (no col-count here) — drop
        return out

    def _coerce_bbox(v):
        if isinstance(v, dict):
            x = v.get("x", 0); y = v.get("y", 0)
            w = v.get("w", v.get("width", 0))
            h = v.get("h", v.get("height", 0))
            x2 = v.get("x2", x + w)
            y2 = v.get("y2", y + h)
            return [x, y, x2, y2]
        return v

    PAIR_KEYS = {"apex", "start", "focus"}
    BBOX_KEYS = {"bbox"}
    PAIR_LIST_KEYS = {"highlight", "edges"}

    if isinstance(data.get("primitives"), list):
        prims = []
        for p in data["primitives"]:
            if not isinstance(p, dict): continue
            p = _kind_swap(p)
            for k in PAIR_KEYS & p.keys():
                p[k] = _coerce_pair(p[k])
            for k in BBOX_KEYS & p.keys():
                p[k] = _coerce_bbox(p[k])
            for k in PAIR_LIST_KEYS & p.keys():
                p[k] = _coerce_pair_list(p[k])
            prims.append(p)
        data["primitives"] = prims
    if isinstance(data.get("animations"), list):
        data["animations"] = [_kind_swap(a) for a in data["animations"] if isinstance(a, dict)]

    return data


def _invoke_bridge(prompt: str, capability: str = "quality",
                   provider: str | None = None) -> str:
    """Shell out to okuro's bridge. Falls through providers automatically."""
    cmd = ["okuro", "bridge", "invoke", "--capability", capability]
    if provider:
        cmd += ["--provider", provider]
    cmd.append(prompt)
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"bridge_invoke failed: {proc.stderr}")
    return proc.stdout


def _extract_json(raw: str) -> dict:
    """Robust JSON extraction from LLM response."""
    raw = raw.strip()
    # strip code fences if present
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0]
    # find first { ... last }
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in response:\n{raw}")
    return json.loads(raw[start:end + 1])


def compose_from_file(path: str | Path) -> Scene:
    """Load a hand-authored scene.json (bypass LLM, useful for tests)."""
    data = json.loads(Path(path).read_text())
    return Scene.model_validate(data)
