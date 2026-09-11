# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Code-graph insights harvested from Graphify — Louvain community
#   detection (subsystems) + degree-centrality "god nodes" (hubs), computed over
#   the KG code triples with networkx. Communities persist as in_community triples
#   (mirrors in_layer) so /knowledge can color subsystems; god nodes are on-demand.
# index:
#   def build_symbol_graph
#   def build_file_graph
#   def god_nodes
#   def detect_communities
#   def write_communities_to_kg
#   def insights_summary
# AGENT_HEADER_END -->
"""Graph-analysis insights over the code-graph KG.

Harvested from Graphify's ideas, implemented with networkx (no Leiden/igraph
dependency — Louvain is the networkx-native member of the same family):

  - god_nodes: the most-connected symbols/files (degree centrality). Agents jump
    straight to the hubs everything flows through, instead of scanning blindly.
  - detect_communities: Louvain partition of the file import graph into
    subsystems, persisted as in_community triples so the /knowledge graph colors
    them (same mechanism as architectural layers).

All reads are scoped to a project and only consider active (non-invalidated)
triples, matching tour.py / kg_code.ripple conventions.
"""

from __future__ import annotations

from collections import defaultdict

from .tiers import (
    RESOLVED_TIERS,
    TIER_AMBIGUOUS,
    TIER_EXTRACTED,
    TIER_ORDER,
    build_symbol_index,
    classify_edge,
    tier_of,
)

# Predicates whose confidence encodes a provenance tier (see codegraph.tiers).
_TIERED_PREDICATES: tuple[str, ...] = (
    "defines",
    "imports",
    "calls",
    "inherits_from",
)
# Languages the Tree-sitter ingester extracts symbols/edges for. Everything
# else is sha-only (a degraded, structure-less tier) — surfaced by the audit.
_PRECISE_LANGS: frozenset[str] = frozenset(
    {"py", "js", "jsx", "mjs", "cjs", "ts", "tsx", "java"}
)


# ── KG reads ─────────────────────────────────────────────────────────


def _active_triples(db, project: str | None, predicates: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """Return (subject, predicate, object) for active triples in the project."""
    ph = ",".join("?" * len(predicates))
    where = f"predicate IN ({ph}) AND (valid_to IS NULL OR valid_to = '')"
    params: list = list(predicates)
    if project:
        # Code-graph triples always carry their repo's project slug. NULL-project
        # triples that reuse code predicates (defines/calls/imports) for semantic
        # relationships in other subsystems must NOT leak into a repo's code graph.
        where += " AND project = ?"
        params.append(project)
    rows = db.fetchall(
        f"SELECT subject, predicate, object FROM kg_triples WHERE {where}",
        tuple(params),
    )
    return [(r["subject"], r["predicate"], r["object"]) for r in rows]


def _layer_map(db, project: str | None) -> dict[str, str]:
    rows = _active_triples(db, project, ("in_layer",))
    return {s: o for (s, _p, o) in rows}


# ── Graph construction ───────────────────────────────────────────────


def build_symbol_graph(db, project: str | None):
    """Directed graph over symbols/files from calls + imports + inherits + defines."""
    import networkx as nx

    g = nx.DiGraph()
    for s, p, o in _active_triples(db, project, ("calls", "imports", "inherits_from", "defines")):
        if s and o:
            g.add_edge(s, o, predicate=p)
    return g


def _project_files(db, project: str | None) -> set[str]:
    """The set of real repo files — subjects of defines or in_layer triples."""
    files: set[str] = set()
    for s, _p, _o in _active_triples(db, project, ("defines", "in_layer")):
        files.add(s)
    return files


def _to_file(node: str, files: set[str]) -> str | None:
    """Map a symbol/file node to its containing project file, or None if external."""
    base = node.split("::", 1)[0]
    return base if base in files else None


def build_file_graph(db, project: str | None):
    """Undirected file→file graph for subsystem detection.

    Projects every structural edge (calls / imports / inherits / defines) down to
    file level: two files are linked when a symbol in one relates to a symbol in
    the other. Relying on the symbol graph (not raw import relpath resolution)
    means module-style import targets still yield real subsystem structure.
    Edge weight = number of underlying symbol relations.
    """
    import networkx as nx

    files = _project_files(db, project)
    g = nx.Graph()
    g.add_nodes_from(files)
    for s, _p, o in _active_triples(db, project, ("calls", "imports", "inherits_from", "defines")):
        fs = _to_file(s, files)
        fo = _to_file(o, files)
        if fs and fo and fs != fo:
            if g.has_edge(fs, fo):
                g[fs][fo]["weight"] += 1
            else:
                g.add_edge(fs, fo, weight=1)
    return g


# ── God nodes (centrality) ───────────────────────────────────────────


def god_nodes(project: str | None, top_n: int = 15) -> list[dict]:
    """Return the most-connected symbols/files, ranked by total degree.

    Degree centrality on the symbol graph surfaces the hubs — the functions,
    classes and files that everything else depends on or calls into.
    """
    from okuro.db import get_db

    db = get_db()
    g = build_symbol_graph(db, project)
    if g.number_of_nodes() == 0:
        return []

    layers = _layer_map(db, project)
    scored = []
    for node in g.nodes():
        indeg = g.in_degree(node)
        outdeg = g.out_degree(node)
        file = node.split("::", 1)[0]
        scored.append(
            {
                "node": node,
                "degree": indeg + outdeg,
                "in": indeg,
                "out": outdeg,
                "layer": layers.get(file),
            }
        )
    scored.sort(key=lambda d: (-d["degree"], d["node"]))
    return scored[:top_n]


# ── Communities (Louvain) ────────────────────────────────────────────


def detect_communities(db, project: str | None) -> dict[str, int]:
    """Louvain partition of the file graph → {file: community_index}.

    Deterministic seed so re-runs on unchanged input give stable ids.
    """
    import networkx as nx

    g = build_file_graph(db, project)
    if g.number_of_nodes() == 0:
        return {}
    communities = nx.community.louvain_communities(g, seed=42)
    # Sort communities by size (desc) then lexical first member for stable ids.
    ordered = sorted(communities, key=lambda c: (-len(c), min(c)))
    mapping: dict[str, int] = {}
    for idx, members in enumerate(ordered):
        for m in members:
            mapping[m] = idx
    return mapping


def write_communities_to_kg(project: str | None) -> int:
    """Persist community membership as in_community triples (one per file).

    Mirrors architecture.write_layers_to_kg: closes stale in_community triples
    for the project, then asserts current membership. Returns files tagged.
    """
    from okuro.db import get_db
    from okuro.sense.kg_code import set_file_community

    db = get_db()
    mapping = detect_communities(db, project)
    if not mapping:
        return 0

    # Invalidate previous in_community triples for this project (full refresh).
    with db.write():
        db.execute(
            """UPDATE kg_triples SET valid_to = datetime('now'), invalidated_at = datetime('now')
               WHERE predicate = 'in_community'
                 AND (valid_to IS NULL OR valid_to = '')
                 AND (project = ? OR (? IS NULL AND project IS NULL))""",
            (project, project),
        )

    n = 0
    for file, community in mapping.items():
        set_file_community(file, f"community-{community}", project=project)
        n += 1
    return n


# ── Repo stats + graph data (for the /repos/:id dashboard) ───────────


def _community_map(db, project: str | None) -> dict[str, str]:
    rows = _active_triples(db, project, ("in_community",))
    return {s: o for (s, _p, o) in rows}


def repo_stats(project: str | None) -> dict:
    """Index-proof counts for a repo: files, symbols, edges, languages, layers."""
    from collections import Counter
    from okuro.db import get_db

    db = get_db()

    def cnt(pred: str) -> int:
        row = db.fetchone(
            "SELECT COUNT(*) c FROM kg_triples WHERE project = ? AND predicate = ? "
            "AND (valid_to IS NULL OR valid_to = '')",
            (project, pred),
        )
        return row["c"] if row else 0

    files = _project_files(db, project)
    langs = Counter()
    for f in files:
        ext = f.rsplit(".", 1)[-1].lower() if "." in f else "?"
        langs[ext] += 1

    layers = Counter(_layer_map(db, project).values())
    communities = _community_map(db, project)

    def safe_count(sql: str) -> int:
        # vec_cortex / cortex_docs are runtime-created by the vectorstore; a
        # freshly-migrated DB (or one never indexed) may lack them.
        try:
            row = db.fetchone(sql, (project,))
            return row["c"] if row else 0
        except Exception:
            return 0

    return {
        "project": project,
        "files": len(files),
        "symbols": cnt("defines"),
        "imports": cnt("imports"),
        "calls": cnt("calls"),
        "inherits": cnt("inherits_from"),
        "languages": dict(langs.most_common()),
        "layers": dict(sorted(layers.items(), key=lambda kv: -kv[1])),
        "communities": len(set(communities.values())),
        "cortex_docs": safe_count("SELECT COUNT(*) c FROM cortex_docs WHERE project = ?"),
        "has_vectors": safe_count("SELECT COUNT(*) c FROM vec_cortex WHERE project = ?") > 0,
    }


def file_graph_data(project: str | None, max_nodes: int = 400) -> dict:
    """File-level code graph for @xyflow: nodes=files, edges=structural links.

    Each node carries its layer, community and degree so the UI can color and
    size it. Capped to the ``max_nodes`` highest-degree files so a huge repo
    still renders; the drop count is reported.
    """
    from okuro.db import get_db

    db = get_db()
    g = build_file_graph(db, project)
    layers = _layer_map(db, project)
    communities = _community_map(db, project)

    ranked = sorted(g.nodes(), key=lambda n: -g.degree(n))
    kept = set(ranked[:max_nodes])
    dropped = max(0, g.number_of_nodes() - len(kept))

    nodes = [
        {
            "id": f,
            "label": f.rsplit("/", 1)[-1],
            "path": f,
            "layer": layers.get(f),
            "community": communities.get(f),
            "degree": g.degree(f),
        }
        for f in kept
    ]
    edges = [
        {"source": u, "target": v, "weight": d.get("weight", 1)}
        for u, v, d in g.edges(data=True)
        if u in kept and v in kept
    ]
    return {
        "project": project,
        "nodes": nodes,
        "edges": edges,
        "truncated": dropped > 0,
        "dropped": dropped,
    }


def file_detail(project: str | None, relpath: str) -> dict:
    """Symbols, imports, out-calls and incoming callers for one file.

    Powers the code-graph node click: what this file defines, what it pulls in,
    and — the useful part — which OTHER files call into it.
    """
    from okuro.db import get_db

    db = get_db()

    def objects_where(pred: str, subject_like: str) -> list[str]:
        rows = db.fetchall(
            "SELECT DISTINCT object FROM kg_triples WHERE project = ? AND predicate = ? "
            "AND subject LIKE ? AND (valid_to IS NULL OR valid_to = '') ORDER BY object",
            (project, pred, subject_like),
        )
        return [r["object"] for r in rows]

    symbols = objects_where("defines", relpath)  # relpath -> symbol
    imports = objects_where("imports", relpath)

    # Out-calls: subjects are this file's symbols (relpath::sym).
    out_calls = objects_where("calls", f"{relpath}::%")

    # Incoming callers: a call whose object is a symbol in this file, or the file
    # itself. Report the distinct caller FILES.
    rows = db.fetchall(
        "SELECT DISTINCT subject FROM kg_triples WHERE project = ? AND predicate = 'calls' "
        "AND (object LIKE ? OR object = ?) AND (valid_to IS NULL OR valid_to = '')",
        (project, f"{relpath}::%", relpath),
    )
    callers = sorted({s["subject"].split("::", 1)[0] for s in rows if s["subject"].split("::", 1)[0] != relpath})

    layer = _layer_map(db, project).get(relpath)
    community = _community_map(db, project).get(relpath)

    # Provenance tiers for this file's outgoing edges (subject = file or its
    # symbols) — extracted / inferred / ambiguous / external.
    tier_rows = db.fetchall(
        """SELECT confidence, COUNT(*) AS c FROM kg_triples
           WHERE predicate IN ('calls', 'inherits_from', 'imports')
             AND (valid_to IS NULL OR valid_to = '')
             AND project = ?
             AND (subject = ? OR subject LIKE ?)
           GROUP BY confidence""",
        (project, relpath, f"{relpath}::%"),
    )
    edge_tiers: dict[str, int] = {}
    for r in tier_rows:
        t = tier_of(r["confidence"])
        edge_tiers[t] = edge_tiers.get(t, 0) + r["c"]
    edge_tiers = {t: edge_tiers[t] for t in TIER_ORDER if t in edge_tiers}

    return {
        "path": relpath,
        "layer": layer,
        "community": community,
        "symbols": [s.split("::", 1)[-1] for s in symbols],
        "imports": imports,
        "out_calls": out_calls[:50],
        "called_by": callers,
        "edge_tiers": edge_tiers,
    }


# ── Summary (for MCP / API) ──────────────────────────────────────────


def insights_summary(project: str | None, top_n: int = 15) -> dict:
    """God nodes + community sizes for a project — the agent-facing overview."""
    from okuro.db import get_db

    db = get_db()
    mapping = detect_communities(db, project)
    sizes: dict[int, int] = defaultdict(int)
    for c in mapping.values():
        sizes[c] += 1
    communities = [
        {"community": f"community-{cid}", "files": n}
        for cid, n in sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {
        "project": project,
        "god_nodes": god_nodes(project, top_n=top_n),
        "communities": communities,
        "community_count": len(sizes),
        "edge_tiers": _edge_tier_counts(db, project),
    }


# ── Provenance tiers + index self-audit ──────────────────────────────


def _edge_tier_counts(db, project: str | None) -> dict[str, dict[str, int]]:
    """Per-predicate ``{tier: count}`` over the tiered code predicates.

    Decodes each triple's confidence back to its provenance tier
    (``codegraph.tiers.tier_of``) so callers see how much of the graph is
    directly parsed vs inferred vs ambiguous.
    """
    ph = ",".join("?" * len(_TIERED_PREDICATES))
    rows = db.fetchall(
        f"""SELECT predicate, confidence, COUNT(*) AS c FROM kg_triples
            WHERE predicate IN ({ph})
              AND (valid_to IS NULL OR valid_to = '')
              AND project = ?
            GROUP BY predicate, confidence""",
        (*_TIERED_PREDICATES, project),
    )
    out: dict[str, dict[str, int]] = {p: {} for p in _TIERED_PREDICATES}
    for r in rows:
        bucket = out[r["predicate"]]
        t = tier_of(r["confidence"])
        bucket[t] = bucket.get(t, 0) + r["c"]
    # Stable tier ordering for display.
    return {
        p: {t: counts[t] for t in TIER_ORDER if t in counts}
        for p, counts in out.items()
    }


def _resolution_context(db, project: str | None):
    """(symbol_index, imports_by_file) for read-only ambiguity recomputation.

    Mirrors the reads ``kg_code.resolve_edges`` does, but never writes — used by
    the audit to list current ambiguous edges with their live candidate sets.
    """
    defines = _active_triples(db, project, ("defines",))
    symbol_index, project_files = build_symbol_index(
        [(s, o) for (s, _p, o) in defines]
    )
    imports_by_file: dict[str, set[str]] = {}
    for s, _p, o in _active_triples(db, project, ("imports",)):
        if o in project_files:
            imports_by_file.setdefault(s, set()).add(o)
    return symbol_index, imports_by_file


def _ambiguous_edges(db, project: str | None, limit: int) -> list[dict]:
    """Current ambiguous call/inherit edges with their live candidate targets.

    The candidate list is recomputed (not stored) so it always reflects the
    present graph — the "needs review" section of the index audit.
    """
    symbol_index, imports_by_file = _resolution_context(db, project)
    edges = _active_triples(db, project, ("calls", "inherits_from"))
    out: list[dict] = []
    for subject, predicate, obj in edges:
        caller_file = subject.split("::", 1)[0]
        res = classify_edge(
            obj,
            caller_file=caller_file,
            symbol_index=symbol_index,
            imported_files=imports_by_file.get(caller_file, set()),
        )
        if res.tier == TIER_AMBIGUOUS:
            out.append({
                "from": subject,
                "predicate": predicate,
                "name": obj,
                "candidates": list(res.candidates),
            })
    out.sort(key=lambda e: (-len(e["candidates"]), e["from"]))
    return out[:limit]


def index_audit(project: str | None, top_n: int = 15) -> dict:
    """Standing self-audit of a project's code index.

    okuro's answer to `C-code-intelligence` REQ-CI-023 and graphify's
    ``GRAPH_REPORT.md``: report how deterministic the index is, where it is
    guessing, and where language coverage is degraded — so a stale or
    low-confidence index can never answer as if it were precise.

    Sections:
      - ``edge_tiers``   per-predicate extracted/inferred/ambiguous/external
      - ``determinism``  extracted vs total for call/inherit edges (+ ratio)
      - ``ambiguous``    the top ambiguous edges with live candidate sets
      - ``language_coverage``  precise (symbol-extracted) vs sha-only languages
      - ``god_nodes``    the highest-degree hubs (change-here-ripples-widest)
      - ``unclassified`` code triples with a non-canonical confidence (re-ingest)
    """
    from okuro.db import get_db

    db = get_db()

    edge_tiers = _edge_tier_counts(db, project)

    # Determinism ratio over the *resolved* edges (calls + inherits): of the
    # edges that point at a project symbol, how many are import-proven?
    resolved_counts: dict[str, int] = defaultdict(int)
    for pred in ("calls", "inherits_from"):
        for t, c in edge_tiers.get(pred, {}).items():
            if t in RESOLVED_TIERS:
                resolved_counts[t] += c
    resolved_total = sum(resolved_counts.values())
    determinism = {
        "extracted": resolved_counts.get(TIER_EXTRACTED, 0),
        "inferred": resolved_counts.get("inferred", 0),
        "ambiguous": resolved_counts.get(TIER_AMBIGUOUS, 0),
        "resolved_total": resolved_total,
        "ratio": round(resolved_counts.get(TIER_EXTRACTED, 0) / resolved_total, 3)
        if resolved_total else None,
    }

    # Language coverage: which languages are symbol-extracted vs sha-only.
    stats = repo_stats(project)
    languages = stats.get("languages", {})
    precise = {ext: n for ext, n in languages.items() if ext in _PRECISE_LANGS}
    degraded = {ext: n for ext, n in languages.items() if ext not in _PRECISE_LANGS}

    # Unclassified: code triples whose confidence is not a canonical tier value
    # (legacy rows from before tiering). Flag them for re-ingest.
    unclassified = sum(
        counts.get("unclassified", 0) for counts in edge_tiers.values()
    )

    return {
        "project": project,
        "files": stats.get("files", 0),
        "edge_tiers": edge_tiers,
        "determinism": determinism,
        "ambiguous": _ambiguous_edges(db, project, limit=top_n),
        "language_coverage": {
            "precise": precise,
            "degraded_sha_only": degraded,
        },
        "god_nodes": god_nodes(project, top_n=top_n),
        "cross_repo": _cross_repo_for_project(db, project, limit=top_n),
        "unclassified_triples": unclassified,
    }


def _cross_repo_for_project(db, project: str | None, limit: int) -> dict:
    """Cross-repo bridges this project participates in, tiered — the workspace
    edges folded into the per-project audit.

    Buckets the project's cross-repo anchors by provenance tier and lists the
    widest bridges (with this project's role on each). Empty when the project
    is not part of any cross-repo link.
    """
    from .crossrepo import anchor_index

    idx = anchor_index(db)
    tier_counts: dict[str, int] = {}
    bridges: list[dict] = []
    for anchor, e in idx.items():
        if not e["cross_repo"] or project not in e["projects"]:
            continue
        tier_counts[e["tier"]] = tier_counts.get(e["tier"], 0) + 1
        role = ("produces" if project in e["producers"] else "consumes")
        bridges.append({
            "anchor": anchor,
            "kind": e["kind"],
            "tier": e["tier"],
            "role": role,
            "projects": len(e["projects"]),
            "linked": sorted(p for p in e["projects"] if p != project),
        })
    bridges.sort(key=lambda b: (-b["projects"], b["anchor"]))
    return {
        "tiers": {t: tier_counts[t] for t in TIER_ORDER if t in tier_counts},
        "bridges": bridges[:limit],
        "total": len(bridges),
    }
