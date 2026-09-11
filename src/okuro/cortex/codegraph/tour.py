# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Deterministic tour-builder — topo-sorts KG code triples into a dependency-ordered walkthrough and emits a tour: <project> report artifact.
# index:
#   imports
#   LAYER_ORDER
#   def build_tour
#   def emit_tour_artifact
# AGENT_HEADER_END -->
"""Tour-builder — deterministic Python implementation of the role's algorithm.

Faster + free vs an LLM round-trip; the role YAML stays as the
LLM-driven alternative for cases where pedagogical phrasing matters
more than determinism.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable


LAYER_ORDER: tuple[str, ...] = (
    "config",
    "infra",
    "data",
    "domain",
    "api",
    "ui",
    "test",
    "docs",
    "unknown",
)


def build_tour(
    *,
    project: str,
    max_steps: int = 20,
    focus_layer: str | None = None,
    start_from: str | None = None,
) -> dict:
    """Construct a TourBody dict from the project's KG.

    Returns the JSON body the tour-builder role specifies. Empty
    ``steps`` array when the project has no code-graph triples — caller
    decides whether to emit anyway.
    """
    from okuro.db import get_db

    db = get_db()

    # 1. Layer map for this project's files
    layer_rows = db.fetchall(
        """SELECT subject, object FROM kg_triples
           WHERE predicate = 'in_layer'
             AND (valid_to IS NULL OR valid_to = '')
             AND (project = ? OR project IS NULL)""",
        (project,),
    )
    layer_map: dict[str, str] = {r["subject"]: r["object"] for r in layer_rows}
    if focus_layer:
        layer_map = {k: v for k, v in layer_map.items() if v == focus_layer}

    project_files = set(layer_map)
    if not project_files:
        return _empty_body(project, max_steps, focus_layer)

    # 2. Intra-project import edges (drop external)
    placeholders = ",".join("?" * len(project_files))
    edge_rows = db.fetchall(
        f"""SELECT subject, object FROM kg_triples
            WHERE predicate = 'imports'
              AND (valid_to IS NULL OR valid_to = '')
              AND (project = ? OR project IS NULL)
              AND subject IN ({placeholders})
              AND object IN ({placeholders})""",
        (project, *project_files, *project_files),
    )
    out_edges: dict[str, set[str]] = defaultdict(set)
    in_degree: dict[str, int] = defaultdict(int)
    for r in edge_rows:
        if r["subject"] == r["object"]:
            continue
        if r["object"] not in out_edges[r["subject"]]:
            out_edges[r["subject"]].add(r["object"])
            in_degree[r["object"]] += 1
    for f in project_files:
        in_degree.setdefault(f, 0)

    # 3. Defines map — top 3 symbol qualnames per file
    define_rows = db.fetchall(
        f"""SELECT subject, object FROM kg_triples
            WHERE predicate = 'defines'
              AND (valid_to IS NULL OR valid_to = '')
              AND (project = ? OR project IS NULL)
              AND subject IN ({placeholders})""",
        (project, *project_files),
    )
    symbols_by_file: dict[str, list[str]] = defaultdict(list)
    for r in define_rows:
        sym = r["object"].split("::", 1)[-1]
        if sym not in symbols_by_file[r["subject"]]:
            symbols_by_file[r["subject"]].append(sym)
    for files in symbols_by_file.values():
        files.sort(key=lambda s: (len(s), s))

    # 4. Sort: layer order, then in-degree, then path length, then alpha.
    # start_from anchors at position 1 regardless of layer.
    def sort_key(path: str) -> tuple:
        layer = layer_map.get(path, "unknown")
        try:
            layer_idx = LAYER_ORDER.index(layer)
        except ValueError:
            layer_idx = len(LAYER_ORDER)
        return (layer_idx, in_degree[path], len(path), path)

    ordered = sorted(project_files, key=sort_key)
    if start_from and start_from in project_files:
        ordered = [start_from] + [p for p in ordered if p != start_from]

    ordered = ordered[:max_steps]
    pos: dict[str, int] = {p: i + 1 for i, p in enumerate(ordered)}

    # 5. Steps + prerequisites (in-tour, by index)
    steps = []
    for i, path in enumerate(ordered, start=1):
        prereq_indices = sorted(
            pos[dep] for dep in out_edges.get(path, ())
            if dep in pos and pos[dep] < i
        )
        layer = layer_map.get(path, "unknown")
        symbols = symbols_by_file.get(path, [])[:3]
        steps.append({
            "index": i,
            "file": path,
            "layer": layer,
            "symbols": symbols,
            "why": _summarise(path, layer, symbols),
            "prerequisites": prereq_indices,
        })

    return {
        "project": project,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "focus_layer": focus_layer,
        "max_steps": max_steps,
        "steps": steps,
    }


def _summarise(path: str, layer: str, symbols: Iterable[str]) -> str:
    """One-sentence why-it-matters string — deterministic, no LLM."""
    syms = ", ".join(list(symbols)[:2])
    if syms:
        return f"{layer.title()} layer — defines {syms}."
    return f"{layer.title()} layer file."


def _empty_body(project: str, max_steps: int, focus_layer: str | None) -> dict:
    return {
        "project": project,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "focus_layer": focus_layer,
        "max_steps": max_steps,
        "steps": [],
    }


def emit_tour_artifact(body: dict, *, project: str) -> str:
    """Write the tour as a ``kind=report, title='tour: <project>'`` artifact."""
    from okuro.sense.artifacts import artifact_write

    return artifact_write(
        kind="report",
        title=f"tour: {project}",
        body=json.dumps(body, indent=2),
        project=project,
        confidence=0.9,
    )
