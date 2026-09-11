# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Unified knowledge-graph API — memory + thoughts + artifacts + progress + kg_triples as one filterable graph.
# index:
#   imports
#   models
#   helpers
#   def _get_node_detail
#   def _load_edges
#   def _bfs_neighborhood
#   def _compute_facets
#   def _get_graph
# AGENT_HEADER_END -->
"""Unified knowledge graph endpoint backing the /knowledge web route.

Two modes:
  - global (focus=None): top-N items by recency, filtered, with edges between them.
  - local  (focus=<id>): BFS up to `depth` hops from a focus node.

Single graph spans memory + thoughts + artifacts + progress + kg_triples.
Frontend filters which entity types render via the FacetBar; the API returns
everything that matches and lets the UI hide/show client-side.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger("okuro.orchestrator.api.knowledge_graph")

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


# ── Models ───────────────────────────────────────────────────────────


NodeType = Literal["memory", "thought", "artifact", "progress", "kg_entity"]
EdgeType = Literal[
    "parent", "supersedes", "memory_ref", "tunnel", "kg_triple", "progress_ref"
]


class GraphNode(BaseModel):
    id: str
    type: NodeType
    label: str
    topic: Optional[str] = None
    project: Optional[str] = None
    confidence: Optional[float] = None
    status: Optional[str] = None  # thoughts, progress
    kind: Optional[str] = None  # artifacts: report|evidence|plan
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    is_supersedes_head: bool = False  # latest in a supersession chain
    body_preview: Optional[str] = None  # first ~140 chars for hover/card
    layer: Optional[str] = None  # architectural layer for kg_entity code_refs (in_layer triple)


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    type: EdgeType
    label: Optional[str] = None  # predicate for kg_triple, concept for tunnel
    valid_from: Optional[str] = None  # bi-temporal kg
    valid_to: Optional[str] = None


class FacetCount(BaseModel):
    value: str
    count: int


class GraphFacets(BaseModel):
    entity_types: list[FacetCount]
    topics: list[FacetCount]
    projects: list[FacetCount]
    statuses: list[FacetCount]
    confidence_bins: list[FacetCount]  # "0.0-0.2", "0.2-0.4", ...


class GraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    facets: GraphFacets
    generated_at: str
    truncated: bool
    mode: Literal["global", "local"]
    focus: Optional[str] = None


# ── Detail-view models (for /node/{id}) ──────────────────────────────


class RelationItem(BaseModel):
    """One related node, grouped by edge type for the detail drawer."""
    node: GraphNode
    edge_type: EdgeType
    edge_label: Optional[str] = None  # predicate / concept
    direction: Literal["outgoing", "incoming"]


class NodeRelations(BaseModel):
    """Edges grouped by semantic role for the relation strip."""
    supersedes: list[RelationItem] = []      # what this supersedes
    superseded_by: list[RelationItem] = []   # what supersedes this
    parents: list[RelationItem] = []         # this -> parent
    children: list[RelationItem] = []        # things parented by this
    memory_refs_out: list[RelationItem] = [] # this -> referenced memories
    memory_refs_in: list[RelationItem] = []  # things that reference this
    tunnels: list[RelationItem] = []         # shared-concept memories
    kg_triples_out: list[RelationItem] = []  # this -> object
    kg_triples_in: list[RelationItem] = []   # subject -> this
    progress_refs_out: list[RelationItem] = []
    progress_refs_in: list[RelationItem] = []


class NodeDetail(BaseModel):
    """Full node body + grouped relations for the right-side drawer."""
    node: GraphNode
    body: Optional[str] = None  # full content (memory.content, thought.content, artifact.body, progress.summary)
    extras: dict = {}  # provenance, agent, role, etc.
    relations: NodeRelations
    generated_at: str


# ── Helpers ──────────────────────────────────────────────────────────


def _parse_types(types: Optional[str]) -> set[str]:
    """Comma-list parser. Empty/None → all types."""
    default = {"memory", "thought", "artifact", "progress", "kg"}
    if not types:
        return default
    requested = {t.strip().lower() for t in types.split(",") if t.strip()}
    return requested & default if requested else default


def _parse_json_list(raw) -> list:
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return raw if isinstance(raw, list) else []


def _preview(text: Optional[str], n: int = 140) -> Optional[str]:
    if not text:
        return None
    s = " ".join(text.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _confidence_bin(c: Optional[float]) -> str:
    if c is None:
        return "unknown"
    if c < 0.2:
        return "0.0-0.2"
    if c < 0.4:
        return "0.2-0.4"
    if c < 0.6:
        return "0.4-0.6"
    if c < 0.8:
        return "0.6-0.8"
    return "0.8-1.0"


def _date_clause(
    col: str,
    date_from: Optional[str],
    date_to: Optional[str],
    params: list,
) -> str:
    clauses = []
    if date_from:
        clauses.append(f"{col} >= ?")
        params.append(date_from)
    if date_to:
        clauses.append(f"{col} <= ?")
        params.append(date_to)
    return " AND ".join(clauses)


def _confidence_clause(
    col: str,
    conf_min: Optional[float],
    conf_max: Optional[float],
    params: list,
) -> str:
    clauses = []
    if conf_min is not None:
        clauses.append(f"{col} >= ?")
        params.append(conf_min)
    if conf_max is not None:
        clauses.append(f"{col} <= ?")
        params.append(conf_max)
    return " AND ".join(clauses)


def _combine_where(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    return ("WHERE " + " AND ".join(parts)) if parts else ""


# ── Loaders ──────────────────────────────────────────────────────────


def _load_memory_nodes(db, filters: dict, per_type_cap: int) -> list[GraphNode]:
    params: list = []
    where = []
    if filters.get("topic"):
        where.append("topic = ?")
        params.append(filters["topic"])
    if filters.get("project"):
        where.append("project = ?")
        params.append(filters["project"])
    cf = _confidence_clause("confidence", filters.get("confidence_min"), filters.get("confidence_max"), params)
    if cf:
        where.append(cf)
    dt = _date_clause("created_at", filters.get("date_from"), filters.get("date_to"), params)
    if dt:
        where.append(dt)
    sql = f"""
        SELECT id, topic, content, project, confidence, supersedes,
               created_at, last_accessed
          FROM agent_memory
          {_combine_where(where)}
         ORDER BY created_at DESC
         LIMIT ?
    """
    params.append(per_type_cap)
    rows = db.fetchall(sql, tuple(params))

    # Compute supersedes-chain heads: a node is "head" if nothing else
    # in the loaded set has supersedes pointing AT it. We compute over the
    # loaded slice (cheap) and merge later if needed.
    superseded_ids = {r["supersedes"] for r in rows if r["supersedes"]}
    out: list[GraphNode] = []
    for r in rows:
        out.append(
            GraphNode(
                id=f"mem:{r['id']}",
                type="memory",
                label=(r["topic"] or "memory"),
                topic=r["topic"],
                project=r["project"],
                confidence=r["confidence"],
                created_at=r["created_at"],
                updated_at=r["last_accessed"] or r["created_at"],
                is_supersedes_head=(r["id"] not in superseded_ids),
                body_preview=_preview(r["content"]),
            )
        )
    return out


def _load_thought_nodes(db, filters: dict, per_type_cap: int) -> list[GraphNode]:
    params: list = []
    where = []
    if filters.get("project"):
        where.append("project = ?")
        params.append(filters["project"])
    if filters.get("status"):
        where.append("status = ?")
        params.append(filters["status"])
    dt = _date_clause("created_at", filters.get("date_from"), filters.get("date_to"), params)
    if dt:
        where.append(dt)
    sql = f"""
        SELECT id, content, status, project, metadata, created_at, updated_at
          FROM thoughts
          {_combine_where(where)}
         ORDER BY created_at DESC
         LIMIT ?
    """
    params.append(per_type_cap)
    rows = db.fetchall(sql, tuple(params))
    out: list[GraphNode] = []
    for r in rows:
        meta = {}
        if r["metadata"]:
            try:
                meta = json.loads(r["metadata"]) or {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
        out.append(
            GraphNode(
                id=f"thg:{r['id']}",
                type="thought",
                label=meta.get("category") or "thought",
                topic=meta.get("category"),
                project=r["project"],
                status=r["status"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                body_preview=_preview(r["content"]),
            )
        )
    return out


def _load_artifact_nodes(db, filters: dict, per_type_cap: int) -> list[GraphNode]:
    params: list = []
    where = []
    if filters.get("project"):
        where.append("project = ?")
        params.append(filters["project"])
    cf = _confidence_clause("confidence", filters.get("confidence_min"), filters.get("confidence_max"), params)
    if cf:
        where.append(cf)
    dt = _date_clause("created_at", filters.get("date_from"), filters.get("date_to"), params)
    if dt:
        where.append(dt)
    sql = f"""
        SELECT id, kind, title, summary, project, confidence,
               supersedes, created_at, updated_at
          FROM artifacts
          {_combine_where(where)}
         ORDER BY created_at DESC
         LIMIT ?
    """
    params.append(per_type_cap)
    rows = db.fetchall(sql, tuple(params))
    superseded_ids = {r["supersedes"] for r in rows if r["supersedes"]}
    out: list[GraphNode] = []
    for r in rows:
        out.append(
            GraphNode(
                id=f"art:{r['id']}",
                type="artifact",
                label=r["title"] or "artifact",
                project=r["project"],
                confidence=r["confidence"],
                kind=r["kind"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                is_supersedes_head=(r["id"] not in superseded_ids),
                body_preview=_preview(r["summary"]),
            )
        )
    return out


def _load_progress_nodes(db, filters: dict, per_type_cap: int) -> list[GraphNode]:
    params: list = []
    where = []
    if filters.get("project"):
        where.append("project = ?")
        params.append(filters["project"])
    if filters.get("status"):
        where.append("status = ?")
        params.append(filters["status"])
    dt = _date_clause("updated_at", filters.get("date_from"), filters.get("date_to"), params)
    if dt:
        where.append(dt)
    sql = f"""
        SELECT id, project, agent, status, summary, next_steps,
               started_at, updated_at
          FROM progress
          {_combine_where(where)}
         ORDER BY updated_at DESC
         LIMIT ?
    """
    params.append(per_type_cap)
    rows = db.fetchall(sql, tuple(params))
    out: list[GraphNode] = []
    for r in rows:
        out.append(
            GraphNode(
                id=f"prg:{r['id']}",
                type="progress",
                label=f"{r['project']}/{r['agent']}",
                project=r["project"],
                status=r["status"],
                created_at=r["started_at"],
                updated_at=r["updated_at"],
                body_preview=_preview(r["summary"]),
            )
        )
    return out


def _load_kg_entity_nodes_for_triples(
    db, triple_rows: list[dict], filters: dict, cap: int
) -> list[GraphNode]:
    """KG nodes are inferred from triples — entity per unique subject/object string.

    Code-graph entities (those with an active in_layer triple) get their
    layer attached so the /knowledge graph can color them.
    """
    names: set[str] = set()
    for r in triple_rows:
        names.add(r["subject"])
        names.add(r["object"])
        if len(names) >= cap:
            break

    layer_map = _load_layer_map(db, names, filters.get("project"))

    out: list[GraphNode] = []
    for n in names:
        out.append(
            GraphNode(
                id=f"kge:{n}",
                type="kg_entity",
                label=n,
                project=filters.get("project"),
                layer=layer_map.get(n),
            )
        )
    return out


def _load_layer_map(db, names: set[str], project: str | None) -> dict[str, str]:
    """Bulk-load active in_layer triples for the given subject names."""
    if not names:
        return {}
    placeholders = ",".join("?" * len(names))
    sql = f"""SELECT subject, object FROM kg_triples
              WHERE predicate = 'in_layer'
                AND (valid_to IS NULL OR valid_to = '')
                AND subject IN ({placeholders})"""
    params: list = list(names)
    if project:
        sql += " AND (project = ? OR project IS NULL)"
        params.append(project)
    rows = db.fetchall(sql, tuple(params))
    return {r["subject"]: r["object"] for r in rows}


def _load_kg_triples(db, filters: dict, cap: int) -> list[dict]:
    params: list = []
    where = ["invalidated_at IS NULL"]
    if filters.get("project"):
        where.append("project = ?")
        params.append(filters["project"])
    cf = _confidence_clause("confidence", filters.get("confidence_min"), filters.get("confidence_max"), params)
    if cf:
        where.append(cf)
    sql = f"""
        SELECT id, subject, predicate, object, valid_from, valid_to,
               project, source_memory_id, source_artifact_id, confidence,
               created_at
          FROM kg_triples
          {_combine_where(where)}
         ORDER BY created_at DESC
         LIMIT ?
    """
    params.append(cap)
    return db.fetchall(sql, tuple(params))


# ── Edge discovery ───────────────────────────────────────────────────


def _load_edges(db, node_ids: set[str], triple_rows: list[dict]) -> list[GraphEdge]:
    """Return all edges where BOTH endpoints are in node_ids (plus kg triples)."""
    edges: list[GraphEdge] = []

    mem_ids = {nid[4:] for nid in node_ids if nid.startswith("mem:")}
    art_ids = {nid[4:] for nid in node_ids if nid.startswith("art:")}
    prg_ids = {nid[4:] for nid in node_ids if nid.startswith("prg:")}

    # supersedes (memory)
    if mem_ids:
        placeholders = ",".join("?" * len(mem_ids))
        rows = db.fetchall(
            f"""SELECT id, supersedes FROM agent_memory
                 WHERE supersedes IS NOT NULL AND id IN ({placeholders})""",
            tuple(mem_ids),
        )
        for r in rows:
            src = f"mem:{r['id']}"
            tgt = f"mem:{r['supersedes']}"
            if tgt in node_ids:
                edges.append(GraphEdge(
                    id=f"sup:{r['id']}", source=src, target=tgt, type="supersedes"
                ))

    # parent + supersedes + memory_refs (artifacts)
    if art_ids:
        placeholders = ",".join("?" * len(art_ids))
        rows = db.fetchall(
            f"""SELECT id, parent_id, supersedes, memory_refs
                  FROM artifacts WHERE id IN ({placeholders})""",
            tuple(art_ids),
        )
        for r in rows:
            src = f"art:{r['id']}"
            if r["parent_id"]:
                tgt = f"art:{r['parent_id']}"
                if tgt in node_ids:
                    edges.append(GraphEdge(
                        id=f"par:{r['id']}", source=src, target=tgt, type="parent"
                    ))
            if r["supersedes"]:
                tgt = f"art:{r['supersedes']}"
                if tgt in node_ids:
                    edges.append(GraphEdge(
                        id=f"asu:{r['id']}", source=src, target=tgt, type="supersedes"
                    ))
            for mref in _parse_json_list(r["memory_refs"]):
                tgt = f"mem:{mref}"
                if tgt in node_ids:
                    edges.append(GraphEdge(
                        id=f"mrf:{r['id']}:{mref}", source=src, target=tgt, type="memory_ref"
                    ))

    # progress_ref (progress.memory_keys → memory)
    if prg_ids:
        placeholders = ",".join("?" * len(prg_ids))
        rows = db.fetchall(
            f"""SELECT id, memory_keys FROM progress WHERE id IN ({placeholders})""",
            tuple(prg_ids),
        )
        for r in rows:
            src = f"prg:{r['id']}"
            for key in _parse_json_list(r["memory_keys"]):
                tgt = f"mem:{key}"
                if tgt in node_ids:
                    edges.append(GraphEdge(
                        id=f"pmr:{r['id']}:{key}",
                        source=src, target=tgt, type="progress_ref",
                    ))

    # tunnels (memory ↔ memory via shared concept)
    if mem_ids:
        placeholders = ",".join("?" * len(mem_ids))
        rows = db.fetchall(
            f"""SELECT concept, memory_id FROM memory_tunnels
                 WHERE memory_id IN ({placeholders})""",
            tuple(mem_ids),
        )
        by_concept: dict[str, list[str]] = {}
        for r in rows:
            by_concept.setdefault(r["concept"], []).append(r["memory_id"])
        for concept, ids in by_concept.items():
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    src = f"mem:{a}"
                    tgt = f"mem:{b}"
                    if src in node_ids and tgt in node_ids:
                        edges.append(GraphEdge(
                            id=f"tun:{concept}:{a}:{b}",
                            source=src, target=tgt, type="tunnel", label=concept,
                        ))

    # kg_triple edges (subject → object via predicate)
    for r in triple_rows:
        src = f"kge:{r['subject']}"
        tgt = f"kge:{r['object']}"
        if src in node_ids and tgt in node_ids:
            edges.append(GraphEdge(
                id=f"kg:{r['id']}",
                source=src, target=tgt, type="kg_triple",
                label=r["predicate"],
                valid_from=r["valid_from"], valid_to=r["valid_to"],
            ))
        # provenance edges: kg entity → source memory/artifact
        if r["source_memory_id"]:
            prov = f"mem:{r['source_memory_id']}"
            if prov in node_ids and src in node_ids:
                edges.append(GraphEdge(
                    id=f"kgm:{r['id']}",
                    source=src, target=prov, type="memory_ref",
                ))
        if r["source_artifact_id"]:
            prov = f"art:{r['source_artifact_id']}"
            if prov in node_ids and src in node_ids:
                edges.append(GraphEdge(
                    id=f"kga:{r['id']}",
                    source=src, target=prov, type="memory_ref",
                ))

    return edges


# ── BFS for local-graph mode ─────────────────────────────────────────


def _neighbors_of(db, node_id: str, type_filter: set[str]) -> set[str]:
    """Return all node ids directly connected to node_id (one-hop)."""
    out: set[str] = set()
    if ":" not in node_id:
        return out
    prefix, real = node_id.split(":", 1)

    # memory ↔ memory (supersedes) — both directions
    if prefix == "mem":
        # this memory supersedes another
        r = db.fetchone("SELECT supersedes FROM agent_memory WHERE id = ?", (real,))
        if r and r["supersedes"]:
            out.add(f"mem:{r['supersedes']}")
        # other memories supersede this one
        rows = db.fetchall("SELECT id FROM agent_memory WHERE supersedes = ?", (real,))
        for x in rows:
            out.add(f"mem:{x['id']}")
        # artifacts that reference this memory
        rows = db.fetchall(
            "SELECT id FROM artifacts WHERE memory_refs LIKE ?",
            (f'%"{real}"%',),
        )
        for x in rows:
            out.add(f"art:{x['id']}")
        # tunnels: other memories sharing a concept with this one
        rows = db.fetchall(
            """SELECT DISTINCT t2.memory_id
                 FROM memory_tunnels t1
                 JOIN memory_tunnels t2 ON t1.concept = t2.concept
                WHERE t1.memory_id = ? AND t2.memory_id != ?""",
            (real, real),
        )
        for x in rows:
            out.add(f"mem:{x['memory_id']}")
        # progress entries linking to this memory
        rows = db.fetchall(
            "SELECT id FROM progress WHERE memory_keys LIKE ?",
            (f'%"{real}"%',),
        )
        for x in rows:
            out.add(f"prg:{x['id']}")
        # kg_triples sourced by this memory
        if "kg" in type_filter:
            rows = db.fetchall(
                "SELECT subject, object FROM kg_triples WHERE source_memory_id = ?",
                (real,),
            )
            for x in rows:
                out.add(f"kge:{x['subject']}")
                out.add(f"kge:{x['object']}")

    elif prefix == "art":
        r = db.fetchone(
            "SELECT parent_id, supersedes, memory_refs FROM artifacts WHERE id = ?",
            (real,),
        )
        if r:
            if r["parent_id"]:
                out.add(f"art:{r['parent_id']}")
            if r["supersedes"]:
                out.add(f"art:{r['supersedes']}")
            for mref in _parse_json_list(r["memory_refs"]):
                out.add(f"mem:{mref}")
        # artifacts that supersede or parent this artifact
        rows = db.fetchall(
            "SELECT id FROM artifacts WHERE parent_id = ? OR supersedes = ?",
            (real, real),
        )
        for x in rows:
            out.add(f"art:{x['id']}")
        # kg_triples sourced by this artifact
        if "kg" in type_filter:
            rows = db.fetchall(
                "SELECT subject, object FROM kg_triples WHERE source_artifact_id = ?",
                (real,),
            )
            for x in rows:
                out.add(f"kge:{x['subject']}")
                out.add(f"kge:{x['object']}")

    elif prefix == "prg":
        r = db.fetchone("SELECT memory_keys FROM progress WHERE id = ?", (real,))
        if r:
            for key in _parse_json_list(r["memory_keys"]):
                out.add(f"mem:{key}")

    elif prefix == "kge":
        # kg entity: connected via triples where it's subject or object
        rows = db.fetchall(
            """SELECT subject, object, source_memory_id, source_artifact_id
                 FROM kg_triples
                WHERE (subject = ? OR object = ?) AND invalidated_at IS NULL""",
            (real, real),
        )
        for x in rows:
            if x["subject"] != real:
                out.add(f"kge:{x['subject']}")
            if x["object"] != real:
                out.add(f"kge:{x['object']}")
            if x["source_memory_id"]:
                out.add(f"mem:{x['source_memory_id']}")
            if x["source_artifact_id"]:
                out.add(f"art:{x['source_artifact_id']}")

    # respect type_filter — strip prefixes the caller didn't ask for
    prefix_for_type = {
        "memory": "mem", "thought": "thg", "artifact": "art",
        "progress": "prg", "kg": "kge",
    }
    allowed = {prefix_for_type[t] for t in type_filter if t in prefix_for_type}
    return {nid for nid in out if nid.split(":", 1)[0] in allowed}


def _bfs_neighborhood(
    db, focus_id: str, depth: int, type_filter: set[str]
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Expand outward from focus up to `depth` hops. Returns nodes + edges."""
    visited_ids: set[str] = {focus_id}
    frontier: set[str] = {focus_id}

    for _ in range(depth):
        next_frontier: set[str] = set()
        for nid in frontier:
            neighbors = _neighbors_of(db, nid, type_filter)
            for n in neighbors:
                if n not in visited_ids:
                    next_frontier.add(n)
        if not next_frontier:
            break
        visited_ids |= next_frontier
        frontier = next_frontier

    # Resolve all visited ids to full nodes
    nodes: list[GraphNode] = []
    for nid in visited_ids:
        n = _load_one_node(db, nid)
        if n is not None:
            nodes.append(n)

    # Load edges between resolved nodes
    triple_rows: list[dict] = []
    if "kg" in type_filter:
        # load triples touching any visited kg entity OR sourced by any visited mem/art
        kge_names = [nid[4:] for nid in visited_ids if nid.startswith("kge:")]
        mem_ids = [nid[4:] for nid in visited_ids if nid.startswith("mem:")]
        art_ids = [nid[4:] for nid in visited_ids if nid.startswith("art:")]
        if kge_names or mem_ids or art_ids:
            clauses = ["invalidated_at IS NULL"]
            params: list = []
            sub_clauses = []
            if kge_names:
                ph = ",".join("?" * len(kge_names))
                sub_clauses.append(f"subject IN ({ph}) OR object IN ({ph})")
                params.extend(kge_names + kge_names)
            if mem_ids:
                ph = ",".join("?" * len(mem_ids))
                sub_clauses.append(f"source_memory_id IN ({ph})")
                params.extend(mem_ids)
            if art_ids:
                ph = ",".join("?" * len(art_ids))
                sub_clauses.append(f"source_artifact_id IN ({ph})")
                params.extend(art_ids)
            clauses.append("(" + " OR ".join(sub_clauses) + ")")
            triple_rows = db.fetchall(
                f"""SELECT id, subject, predicate, object, valid_from, valid_to,
                           project, source_memory_id, source_artifact_id,
                           confidence, created_at
                      FROM kg_triples
                     WHERE {' AND '.join(clauses)}
                     LIMIT 500""",
                tuple(params),
            )

    edges = _load_edges(db, visited_ids, triple_rows)
    return nodes, edges


def _load_one_node(db, nid: str) -> Optional[GraphNode]:
    """Resolve a single node by composite id (prefix:realid)."""
    if ":" not in nid:
        return None
    prefix, real = nid.split(":", 1)
    if prefix == "mem":
        r = db.fetchone(
            """SELECT id, topic, content, project, confidence, supersedes,
                      created_at, last_accessed
                 FROM agent_memory WHERE id = ?""",
            (real,),
        )
        if not r:
            return None
        return GraphNode(
            id=nid, type="memory", label=r["topic"] or "memory",
            topic=r["topic"], project=r["project"], confidence=r["confidence"],
            created_at=r["created_at"], updated_at=r["last_accessed"] or r["created_at"],
            body_preview=_preview(r["content"]),
        )
    if prefix == "thg":
        r = db.fetchone(
            """SELECT id, content, status, project, metadata, created_at, updated_at
                 FROM thoughts WHERE id = ?""",
            (real,),
        )
        if not r:
            return None
        meta = {}
        try:
            meta = json.loads(r["metadata"] or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            pass
        return GraphNode(
            id=nid, type="thought", label=meta.get("category") or "thought",
            topic=meta.get("category"), project=r["project"], status=r["status"],
            created_at=r["created_at"], updated_at=r["updated_at"],
            body_preview=_preview(r["content"]),
        )
    if prefix == "art":
        r = db.fetchone(
            """SELECT id, kind, title, summary, project, confidence,
                      created_at, updated_at
                 FROM artifacts WHERE id = ?""",
            (real,),
        )
        if not r:
            return None
        return GraphNode(
            id=nid, type="artifact", label=r["title"] or "artifact",
            project=r["project"], confidence=r["confidence"], kind=r["kind"],
            created_at=r["created_at"], updated_at=r["updated_at"],
            body_preview=_preview(r["summary"]),
        )
    if prefix == "prg":
        r = db.fetchone(
            """SELECT id, project, agent, status, summary, started_at, updated_at
                 FROM progress WHERE id = ?""",
            (real,),
        )
        if not r:
            return None
        return GraphNode(
            id=nid, type="progress", label=f"{r['project']}/{r['agent']}",
            project=r["project"], status=r["status"],
            created_at=r["started_at"], updated_at=r["updated_at"],
            body_preview=_preview(r["summary"]),
        )
    if prefix == "kge":
        return GraphNode(id=nid, type="kg_entity", label=real)
    return None


# ── Facets ───────────────────────────────────────────────────────────


def _compute_facets(nodes: list[GraphNode]) -> GraphFacets:
    et: dict[str, int] = {}
    tp: dict[str, int] = {}
    pj: dict[str, int] = {}
    st: dict[str, int] = {}
    cb: dict[str, int] = {}
    for n in nodes:
        et[n.type] = et.get(n.type, 0) + 1
        if n.topic:
            tp[n.topic] = tp.get(n.topic, 0) + 1
        if n.project:
            pj[n.project] = pj.get(n.project, 0) + 1
        if n.status:
            st[n.status] = st.get(n.status, 0) + 1
        cb_key = _confidence_bin(n.confidence)
        cb[cb_key] = cb.get(cb_key, 0) + 1

    def _sort(d: dict[str, int]) -> list[FacetCount]:
        return [
            FacetCount(value=k, count=v)
            for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    return GraphFacets(
        entity_types=_sort(et),
        topics=_sort(tp),
        projects=_sort(pj),
        statuses=_sort(st),
        confidence_bins=_sort(cb),
    )


# ── Endpoint ─────────────────────────────────────────────────────────


@router.get("/graph", response_model=GraphResponse)
def _get_graph(
    focus: Optional[str] = Query(None, description="Composite node id (e.g. mem:abc) to center the graph"),
    depth: int = Query(2, ge=1, le=3, description="Hops from focus (local mode only)"),
    types: Optional[str] = Query(None, description="Comma list: memory,thought,artifact,progress,kg"),
    topic: Optional[str] = Query(None),
    project: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    confidence_min: Optional[float] = Query(None, ge=0.0, le=1.0),
    confidence_max: Optional[float] = Query(None, ge=0.0, le=1.0),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None),
    hide_orphans: bool = Query(False),
    per_type_cap: int = Query(200, ge=10, le=500),
) -> GraphResponse:
    """Unified knowledge graph for the /knowledge web route.

    Global mode (focus=None): returns top per_type_cap of each requested
    entity type, ordered by recency, filtered, with all discoverable
    edges between them.

    Local mode (focus=<id>): BFS up to `depth` hops from the focus node.
    """
    from okuro.db import get_db

    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    type_filter = _parse_types(types)
    filters = {
        "topic": topic,
        "project": project,
        "status": status,
        "confidence_min": confidence_min,
        "confidence_max": confidence_max,
        "date_from": date_from,
        "date_to": date_to,
    }

    try:
        if focus:
            nodes, edges = _bfs_neighborhood(db, focus, depth, type_filter)
            mode: Literal["global", "local"] = "local"
            truncated = False
        else:
            nodes = []
            if "memory" in type_filter:
                nodes += _load_memory_nodes(db, filters, per_type_cap)
            if "thought" in type_filter:
                nodes += _load_thought_nodes(db, filters, per_type_cap)
            if "artifact" in type_filter:
                nodes += _load_artifact_nodes(db, filters, per_type_cap)
            if "progress" in type_filter:
                nodes += _load_progress_nodes(db, filters, per_type_cap)

            triple_rows: list[dict] = []
            if "kg" in type_filter:
                triple_rows = _load_kg_triples(db, filters, per_type_cap)
                nodes += _load_kg_entity_nodes_for_triples(db, triple_rows, filters, per_type_cap * 2)

            node_ids = {n.id for n in nodes}
            edges = _load_edges(db, node_ids, triple_rows)

            # Hide-orphans: drop nodes with zero edges (only in global mode;
            # local mode is by definition connected to the focus).
            if hide_orphans:
                connected: set[str] = set()
                for e in edges:
                    connected.add(e.source)
                    connected.add(e.target)
                nodes = [n for n in nodes if n.id in connected]

            mode = "global"
            truncated = any(
                k in type_filter and per_type_cap == per_type_cap
                for k in ["memory", "thought", "artifact", "progress"]
            )  # conservative: if any cap was applied we mark truncated

    except Exception as exc:
        logger.exception("knowledge graph query failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"graph query failed: {exc}")

    facets = _compute_facets(nodes)

    return GraphResponse(
        nodes=nodes,
        edges=edges,
        facets=facets,
        generated_at=now,
        truncated=truncated,
        mode=mode,
        focus=focus,
    )


# ── Node detail endpoint ─────────────────────────────────────────────


def _fetch_body(db, prefix: str, real: str) -> tuple[Optional[str], dict]:
    """Fetch full body + extras (provenance) for a node."""
    if prefix == "mem":
        r = db.fetchone(
            """SELECT content, source_agent, role, verified, last_accessed
                 FROM agent_memory WHERE id = ?""",
            (real,),
        )
        if not r:
            return None, {}
        return r["content"], {
            "source_agent": r["source_agent"],
            "role": r["role"],
            "verified": bool(r["verified"]),
            "last_accessed": r["last_accessed"],
        }
    if prefix == "thg":
        r = db.fetchone(
            "SELECT content, metadata, last_surfaced, surface_count FROM thoughts WHERE id = ?",
            (real,),
        )
        if not r:
            return None, {}
        meta = {}
        try:
            meta = json.loads(r["metadata"] or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            pass
        return r["content"], {
            "metadata": meta,
            "last_surfaced": r["last_surfaced"],
            "surface_count": r["surface_count"],
        }
    if prefix == "art":
        r = db.fetchone(
            """SELECT body, summary, media_type, created_by, memory_refs
                 FROM artifacts WHERE id = ?""",
            (real,),
        )
        if not r:
            return None, {}
        return r["body"] or r["summary"], {
            "media_type": r["media_type"],
            "created_by": r["created_by"],
            "memory_refs": _parse_json_list(r["memory_refs"]),
        }
    if prefix == "prg":
        r = db.fetchone(
            """SELECT summary, agent, next_steps, blockers, branch,
                      files_touched, memory_keys, history
                 FROM progress WHERE id = ?""",
            (real,),
        )
        if not r:
            return None, {}
        return r["summary"], {
            "agent": r["agent"],
            "next_steps": r["next_steps"],
            "blockers": r["blockers"],
            "branch": r["branch"],
            "files_touched": _parse_json_list(r["files_touched"]),
            "memory_keys": _parse_json_list(r["memory_keys"]),
        }
    if prefix == "kge":
        # KG entity has no body — derived from triples
        return None, {}
    return None, {}


def _make_relation(db, target_id: str, edge_type: EdgeType,
                   edge_label: Optional[str], direction: Literal["outgoing", "incoming"]) -> Optional[RelationItem]:
    n = _load_one_node(db, target_id)
    if n is None:
        return None
    return RelationItem(node=n, edge_type=edge_type, edge_label=edge_label, direction=direction)


def _gather_relations(db, node_id: str) -> NodeRelations:
    """Walk every relation source for this node, grouped by semantic role."""
    rel = NodeRelations()
    if ":" not in node_id:
        return rel
    prefix, real = node_id.split(":", 1)

    if prefix == "mem":
        # supersedes (out): this memory supersedes another
        r = db.fetchone("SELECT supersedes FROM agent_memory WHERE id = ?", (real,))
        if r and r["supersedes"]:
            ri = _make_relation(db, f"mem:{r['supersedes']}", "supersedes", None, "outgoing")
            if ri: rel.supersedes.append(ri)
        # superseded_by (in): other memories supersede this
        rows = db.fetchall("SELECT id FROM agent_memory WHERE supersedes = ?", (real,))
        for x in rows:
            ri = _make_relation(db, f"mem:{x['id']}", "supersedes", None, "incoming")
            if ri: rel.superseded_by.append(ri)
        # memory_refs_in: artifacts that cite this memory
        rows = db.fetchall("SELECT id FROM artifacts WHERE memory_refs LIKE ?", (f'%"{real}"%',))
        for x in rows:
            ri = _make_relation(db, f"art:{x['id']}", "memory_ref", None, "incoming")
            if ri: rel.memory_refs_in.append(ri)
        # tunnels: other memories sharing a concept
        rows = db.fetchall(
            """SELECT DISTINCT t2.memory_id, t1.concept
                 FROM memory_tunnels t1
                 JOIN memory_tunnels t2 ON t1.concept = t2.concept
                WHERE t1.memory_id = ? AND t2.memory_id != ?""",
            (real, real),
        )
        for x in rows:
            ri = _make_relation(db, f"mem:{x['memory_id']}", "tunnel", x["concept"], "outgoing")
            if ri: rel.tunnels.append(ri)
        # progress_refs_in: progress entries linking to this memory
        rows = db.fetchall("SELECT id FROM progress WHERE memory_keys LIKE ?", (f'%"{real}"%',))
        for x in rows:
            ri = _make_relation(db, f"prg:{x['id']}", "progress_ref", None, "incoming")
            if ri: rel.progress_refs_in.append(ri)
        # kg_triples sourced by this memory (out): subject -> object
        rows = db.fetchall(
            """SELECT id, subject, predicate, object FROM kg_triples
                WHERE source_memory_id = ? AND invalidated_at IS NULL
                LIMIT 200""",
            (real,),
        )
        for x in rows:
            # treat as outgoing kg_triple from this memory to the object
            ri = _make_relation(db, f"kge:{x['object']}", "kg_triple",
                                f"{x['subject']} {x['predicate']}", "outgoing")
            if ri: rel.kg_triples_out.append(ri)

    elif prefix == "art":
        r = db.fetchone(
            "SELECT parent_id, supersedes, memory_refs FROM artifacts WHERE id = ?",
            (real,),
        )
        if r:
            if r["parent_id"]:
                ri = _make_relation(db, f"art:{r['parent_id']}", "parent", None, "outgoing")
                if ri: rel.parents.append(ri)
            if r["supersedes"]:
                ri = _make_relation(db, f"art:{r['supersedes']}", "supersedes", None, "outgoing")
                if ri: rel.supersedes.append(ri)
            for mref in _parse_json_list(r["memory_refs"]):
                ri = _make_relation(db, f"mem:{mref}", "memory_ref", None, "outgoing")
                if ri: rel.memory_refs_out.append(ri)
        # children: artifacts parented by this
        rows = db.fetchall("SELECT id FROM artifacts WHERE parent_id = ?", (real,))
        for x in rows:
            ri = _make_relation(db, f"art:{x['id']}", "parent", None, "incoming")
            if ri: rel.children.append(ri)
        # superseded_by: artifacts that supersede this
        rows = db.fetchall("SELECT id FROM artifacts WHERE supersedes = ?", (real,))
        for x in rows:
            ri = _make_relation(db, f"art:{x['id']}", "supersedes", None, "incoming")
            if ri: rel.superseded_by.append(ri)
        # kg_triples sourced by this artifact (out)
        rows = db.fetchall(
            """SELECT id, subject, predicate, object FROM kg_triples
                WHERE source_artifact_id = ? AND invalidated_at IS NULL
                LIMIT 200""",
            (real,),
        )
        for x in rows:
            ri = _make_relation(db, f"kge:{x['object']}", "kg_triple",
                                f"{x['subject']} {x['predicate']}", "outgoing")
            if ri: rel.kg_triples_out.append(ri)

    elif prefix == "prg":
        r = db.fetchone("SELECT memory_keys FROM progress WHERE id = ?", (real,))
        if r:
            for key in _parse_json_list(r["memory_keys"]):
                ri = _make_relation(db, f"mem:{key}", "progress_ref", None, "outgoing")
                if ri: rel.progress_refs_out.append(ri)

    elif prefix == "kge":
        # triples where this entity is subject (out) or object (in)
        rows = db.fetchall(
            """SELECT id, subject, predicate, object, source_memory_id, source_artifact_id
                 FROM kg_triples
                WHERE subject = ? AND invalidated_at IS NULL
                LIMIT 100""",
            (real,),
        )
        for x in rows:
            ri = _make_relation(db, f"kge:{x['object']}", "kg_triple",
                                x["predicate"], "outgoing")
            if ri: rel.kg_triples_out.append(ri)
            # provenance reaches back to source memory/artifact
            if x["source_memory_id"]:
                ri2 = _make_relation(db, f"mem:{x['source_memory_id']}",
                                     "memory_ref", "source", "incoming")
                if ri2: rel.memory_refs_in.append(ri2)
        rows = db.fetchall(
            """SELECT id, subject, predicate, object FROM kg_triples
                WHERE object = ? AND invalidated_at IS NULL LIMIT 100""",
            (real,),
        )
        for x in rows:
            ri = _make_relation(db, f"kge:{x['subject']}", "kg_triple",
                                x["predicate"], "incoming")
            if ri: rel.kg_triples_in.append(ri)

    return rel


@router.get("/node/{node_id:path}", response_model=NodeDetail)
def _get_node_detail(node_id: str) -> NodeDetail:
    """Full node body + grouped relations for the detail drawer.

    node_id is the composite id (mem:uuid | thg:uuid | art:uuid |
    prg:uuid | kge:name). Path-converter accepts slashes in case
    kg entity names contain them.
    """
    from okuro.db import get_db

    db = get_db()
    if ":" not in node_id:
        raise HTTPException(status_code=400, detail="node_id must include type prefix")

    prefix, real = node_id.split(":", 1)
    if prefix not in {"mem", "thg", "art", "prg", "kge"}:
        raise HTTPException(status_code=400, detail=f"unknown node prefix '{prefix}'")

    node = _load_one_node(db, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")

    body, extras = _fetch_body(db, prefix, real)
    relations = _gather_relations(db, node_id)

    return NodeDetail(
        node=node,
        body=body,
        extras=extras,
        relations=relations,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


# ── Saved queries (chip bookmarks for /knowledge) ────────────────────


class SavedQuery(BaseModel):
    id: str
    name: str
    filter_dsl: dict
    color_group: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_used_at: Optional[str] = None


class SavedQueryCreate(BaseModel):
    name: str
    filter_dsl: dict = {}
    color_group: Optional[str] = None


def _row_to_saved_query(r) -> SavedQuery:
    raw_dsl = r["filter_dsl"]
    dsl: dict
    if isinstance(raw_dsl, str):
        try:
            parsed = json.loads(raw_dsl)
            dsl = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            dsl = {}
    elif isinstance(raw_dsl, dict):
        dsl = raw_dsl
    else:
        dsl = {}
    return SavedQuery(
        id=r["id"],
        name=r["name"],
        filter_dsl=dsl,
        color_group=r["color_group"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        last_used_at=r["last_used_at"],
    )


@router.get("/saved-queries", response_model=list[SavedQuery])
def _list_saved_queries() -> list[SavedQuery]:
    """List all saved knowledge-graph filter bookmarks, most-recently-used first."""
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        """SELECT id, name, filter_dsl, color_group,
                  created_at, updated_at, last_used_at
             FROM knowledge_saved_queries
            ORDER BY COALESCE(last_used_at, created_at) DESC"""
    )
    return [_row_to_saved_query(r) for r in rows]


@router.post("/saved-queries", response_model=SavedQuery, status_code=201)
def _create_saved_query(payload: SavedQueryCreate) -> SavedQuery:
    """Create a new bookmark. Name need not be unique — UI shows them by recency."""
    from okuro.db import get_db
    import uuid as _uuid

    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    db = get_db()
    sq_id = str(_uuid.uuid4())
    db.execute(
        """INSERT INTO knowledge_saved_queries
               (id, name, filter_dsl, color_group)
           VALUES (?, ?, ?, ?)""",
        (sq_id, name, json.dumps(payload.filter_dsl or {}), payload.color_group),
    )
    r = db.fetchone(
        """SELECT id, name, filter_dsl, color_group,
                  created_at, updated_at, last_used_at
             FROM knowledge_saved_queries WHERE id = ?""",
        (sq_id,),
    )
    if not r:
        raise HTTPException(status_code=500, detail="saved query insert failed")
    return _row_to_saved_query(r)


@router.delete("/saved-queries/{sq_id}", status_code=204)
def _delete_saved_query(sq_id: str) -> None:
    from okuro.db import get_db

    db = get_db()
    db.execute("DELETE FROM knowledge_saved_queries WHERE id = ?", (sq_id,))


@router.post("/saved-queries/{sq_id}/touch", response_model=SavedQuery)
def _touch_saved_query(sq_id: str) -> SavedQuery:
    """Mark a saved query as just-used (orders it first in the chip row)."""
    from okuro.db import get_db

    db = get_db()
    db.execute(
        "UPDATE knowledge_saved_queries SET last_used_at = datetime('now') WHERE id = ?",
        (sq_id,),
    )
    r = db.fetchone(
        """SELECT id, name, filter_dsl, color_group,
                  created_at, updated_at, last_used_at
             FROM knowledge_saved_queries WHERE id = ?""",
        (sq_id,),
    )
    if not r:
        raise HTTPException(status_code=404, detail="saved query not found")
    return _row_to_saved_query(r)
