# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Cross-repo code graph — links files across ALL ingested repos via
#   shared "anchors" (symbol names, route/path literals, tool-name literals) so
#   agents can find elements and connections that span repository boundaries.
# index:
#   ANCHOR_PREDICATES
#   COMMON_TOKENS
#   def build_cross_repo_graph
#   def cross_repo_connections
#   def cross_repo_search
#   def cross_repo_insights
# AGENT_HEADER_END -->
"""Workspace-wide cross-repo linking, language-agnostic.

Why this exists
---------------
Per-repo code graphs (cortex.codegraph) resolve imports/calls *inside one
project* — an import target that points at another repo is dropped as
"external". Across a polyglot repo set (TS + Java + Python + docs) there is no
shared package manager and no cross-language static call graph. The real
coupling is **contracts**: the same REST path, MCP tool name, or exported type
name appears in two different repos.

Model
-----
Bipartite, stored in the existing ``kg_triples`` table at ``project = NULL``
(workspace-global scope), so nothing is per-repo and no schema change is needed:

    <project>|<relpath>  --defines_anchor-->  code_anchor        (owner side)
    <project>|<relpath>  --uses_anchor----->  code_anchor        (reference side)

An ``code_anchor`` is one of:
    sym:<Name>      a symbol name defined somewhere (from per-repo ``defines``)
    route:</path>   a URL/route path literal
    tool:<name>     a quoted tool/name identifier (e.g. MCP tool registry)

A **cross-repo connection** is any anchor linked to files in >= 2 distinct
projects. Connections are computed on demand by joining on the shared anchor —
never materialised as O(n^2) file->file edges — so they are always fresh and
storage stays linear.

File node ids are project-qualified (``<project>|<relpath>``) because relpaths
collide across repos; the ``project`` column can't disambiguate at NULL scope.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from .ingestor import discover_project_files  # reused source-file walker
from .tiers import (
    TIER_AMBIGUOUS,
    TIER_CONFIDENCE,
    TIER_EXTRACTED,
    TIER_INFERRED,
)
from ...sense.kg import _triple_id

log = logging.getLogger(__name__)

# Baseline confidence a raw anchor triple is written at before its aggregated
# cross-repo tier is known (see cross_repo_tier + _stamp_anchor_tiers). Cross-
# repo links are literal co-reference (shared name / route / tool string), so
# INFERRED is the floor; only a directional route contract is promoted higher.
_ANCHOR_CONFIDENCE: float = TIER_CONFIDENCE[TIER_INFERRED]


def cross_repo_tier(kind: str, n_producers: int) -> str:
    """Map a cross-repo anchor's evidence strength to a provenance tier.

    Parity with the per-repo edge tiers (see codegraph.tiers). Only meaningful
    for anchors already flagged ``cross_repo``:

      - ``route`` → EXTRACTED. A cross-repo route is only flagged when a server
        DECLARES it and a client in another repo CALLS it — a proven directional
        contract, the cross-repo analogue of import-evidence promotion.
      - ``sym`` with exactly one producer repo → INFERRED. A single clear owner
        + a same-named reference elsewhere is a decent name-based signal.
      - ``sym`` with zero or >1 producer repos → AMBIGUOUS. No owner (used but
        unowned) or an ownership collision (two repos define the same type) —
        can't say which definition the reference resolves to, so never asserted.
      - ``tool`` (and any other registry literal) → INFERRED. A shared tool name
        is a reliable convention but still string-based.
    """
    if kind == "route":
        return TIER_EXTRACTED
    if kind == "sym":
        return TIER_INFERRED if n_producers == 1 else TIER_AMBIGUOUS
    return TIER_INFERRED

# Anchor-bearing triples live at project=NULL; distinct predicates keep them
# out of the per-repo god-node graph (which only reads calls/imports/defines).
ANCHOR_PREDICATES: tuple[str, ...] = ("defines_anchor", "uses_anchor")

# Symbol names too generic to be a meaningful cross-repo signal. Co-reference on
# these produces noise, not structure. Kept deliberately small + lowercased.
COMMON_TOKENS: frozenset[str] = frozenset(
    {
        "index", "main", "test", "tests", "setup", "config", "utils", "util",
        "types", "type", "model", "models", "data", "value", "values", "item",
        "items", "list", "name", "names", "result", "results", "response",
        "request", "error", "errors", "handler", "handlers", "service",
        "services", "client", "server", "router", "route", "routes", "app",
        "props", "state", "context", "provider", "component", "default",
        "options", "params", "args", "kwargs", "self", "true", "false", "null",
        "none", "string", "number", "boolean", "object", "array", "function",
        "class", "return", "async", "await", "const", "field", "fields",
        "entity", "entities", "repository", "controller", "manager", "base",
    }
)

# Generically-ambiguous type NOUNS — words that name a type in almost every
# codebase and so collide across repos by coincidence, not by shared contract
# (measured: Status ui-union ≠ directory-record; Source enum ≠ record; etc.).
# Dropping them as anchors is a distinctiveness stopword filter — the same idea
# as IR stopwords. Specific/compound names (DuplicateCandidate, ToolDefinition,
# PagedResponse, ErrorResponse, Manifest, Owner, Agent) are NOT here and survive.
GENERIC_TYPE_NOUNS: frozenset[str] = frozenset(
    {
        "status", "source", "action", "state", "phase", "step", "score",
        "version", "node", "edge", "duplicate", "kind", "level", "mode",
        "scope", "event", "message", "record", "entry", "category",
        "visibility", "research", "pins", "range", "point", "target",
        "detail", "summary", "meta", "info", "payload", "content",
    }
)

# A distinctive symbol: >= this many chars after dropping the common set.
_MIN_SYM_LEN = 4
# Cap per-file bytes read during the use-scan (skip giant generated files).
_MAX_FILE_BYTES = 512_000

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
# Symbol anchors are DATA-TYPE definitions ONLY — class/interface/enum/record/
# struct/trait/type-alias. Rationale (Phase-1, measured): every genuine cross-
# repo shared symbol in the gold set is a *type* (a wire DTO / contract), never
# a function or variable. Admitting functions/consts as anchors was the dominant
# false-positive source — same-named local helpers (commit, normalize, tokenize,
# close, init, loadYaml…) collide across repos without any real dependency.
# Language-agnostic so Java/TS/Py/etc. all contribute despite tree-sitter Java
# emitting no `defines` triples.
_TYPE_DEF_RES: tuple[re.Pattern, ...] = (
    # class / interface / enum / record / struct / trait Name
    re.compile(
        r"\b(?:class|interface|enum|record|struct|trait)\s+([A-Z][A-Za-z0-9_]{2,})"
    ),
    # [export] type Name =   (TS type aliases)
    re.compile(r"\btype\s+([A-Z][A-Za-z0-9_]{2,})\s*="),
)
# Strip a source line to the code that can hold a *type reference*, so symbol
# co-reference ignores mentions in comments, string/JSX literals, and imports
# (the second dominant false-positive source: a type name appearing in prose,
# a label string, or an external-lib import is not a real usage of the contract).
_COMMENT_PREFIX_RE = re.compile(r"^\s*(?://|#|\*|/\*|\*/|<!--|-->)")
_IMPORT_LINE_RE = re.compile(r"^\s*(?:import\b|from\s+\S+\s+import\b|@import\b)")
_STRING_LITERAL_RE = re.compile(r"""(['"])(?:\\.|(?!\1).)*\1""")
_INLINE_COMMENT_RE = re.compile(r"(?://|#).*$")


def _code_only(line: str) -> str | None:
    """Line reduced to code that can hold a type reference, or None to skip.

    Drops comment-only and import lines entirely; blanks out string/JSX literal
    contents and trailing inline comments. Bare type identifiers survive; names
    that only appear in prose/labels/imports do not.
    """
    if _COMMENT_PREFIX_RE.match(line) or _IMPORT_LINE_RE.match(line):
        return None
    line = _STRING_LITERAL_RE.sub(" ", line)
    line = _INLINE_COMMENT_RE.sub(" ", line)
    return line


# Quoted path literal: "/foo", "/foo/bar", '/api/v1/x' — >=2 chars after slash.
_ROUTE_RE = re.compile(r"""['"](/[a-zA-Z][A-Za-z0-9/_.\-:{}]{1,120})['"]""")
# Quoted tool/name registry value: name: "load_agent", tool: 'search_skills'.
_TOOL_RE = re.compile(
    r"""(?:name|tool|toolName|id|action|command)\s*[:=]\s*['"]"""
    r"""([a-z][a-z0-9_.\-]{2,60})['"]"""
)
# A route occurrence is a SERVER declaration (producer) when the line carries a
# resource/endpoint annotation or router registration; otherwise it is treated
# as a client CALL (consumer). Direction = consumer-repo depends on producer-repo.
_ROUTE_SERVER_RE = re.compile(
    r"@(?:Path|GET|POST|PUT|DELETE|PATCH|HEAD|"
    r"(?:Get|Post|Put|Delete|Patch|Request)Mapping)\b"
    r"|\b(?:app|router|api)\.(?:get|post|put|delete|patch|route|use)\s*\("
)
# JAX-RS/MicroProfile @RegisterRestClient interfaces DECLARE endpoints they CALL
# (they carry @Path too) — so a file marked as a rest-client is a consumer.
_RESTCLIENT_RE = re.compile(r"@RegisterRestClient\b")
# Import lines — the names they bind are provided by another module, so bare
# uses of them in this file are NOT references to a same-named cross-repo type
# (kills the jakarta NotFoundException-vs-local-NotFoundException collision).
_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?[\w.]+\.([A-Z]\w+)\s*;", re.M)
_TS_IMPORT_RE = re.compile(r"^\s*import\b([^;]*?)\bfrom\b", re.M)


def _imported_names(text: str) -> set[str]:
    """Simple names bound by import statements in this file (Java + TS/JS)."""
    names: set[str] = set(_JAVA_IMPORT_RE.findall(text))
    for clause in _TS_IMPORT_RE.findall(text):
        names.update(_IDENT_RE.findall(clause))
    return names


# Type names imported from a FRAMEWORK/stdlib package are framework types, not
# cross-repo contracts — even when another repo happens to define a same-named
# custom type (e.g. jakarta.ws.rs.NotFoundException vs a local NotFoundException).
# Collected per repo so a wildcard-imported use elsewhere in that repo is also
# suppressed. Package prefixes cover the common JVM/JS ecosystems.
_FRAMEWORK_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:static\s+)?"
    r"(?:jakarta|javax|java|scala|kotlin|org\.springframework|org\.junit|"
    r"org\.slf4j|org\.apache|com\.fasterxml|io\.smallrye|reactor|io\.quarkus|"
    r"org\.eclipse|org\.hibernate|com\.google)\.[\w.]*\.([A-Z]\w+)\s*;",
    re.M,
)


def _framework_import_names(text: str) -> set[str]:
    return set(_FRAMEWORK_IMPORT_RE.findall(text))


def _ingested_repos(db) -> list[tuple[str, str]]:
    """(project_id, root_path) for every managed repo the user has ingested.

    Sourced from the ``managed_repos`` registry (status=ready) — the repos
    surfaced by ``repo_list`` — so the graph spans exactly the set the user
    pulled in, independent of which languages the tree-sitter ingestor covers.
    """
    rows = db.fetchall(
        "SELECT project_id AS pid, path AS root FROM managed_repos "
        "WHERE status = 'ready'"
    )
    out = []
    for r in rows:
        root = r["root"]
        if root and Path(root).is_dir():
            out.append((r["pid"], root))
    return out


def _extract_def_names(text: str) -> set[str]:
    """Distinctive TYPE-definition names in a source file (language-agnostic)."""
    names: set[str] = set()
    for pat in _TYPE_DEF_RES:
        for name in pat.findall(text):
            low = name.lower()
            if (len(name) >= _MIN_SYM_LEN and low not in COMMON_TOKENS
                    and low not in GENERIC_TYPE_NOUNS):
                names.add(name)
    return names


def _reference_tokens(text: str) -> set[str]:
    """Identifier tokens in code positions only (comments/strings/imports out)."""
    toks: set[str] = set()
    for line in text.splitlines():
        code = _code_only(line)
        if code:
            toks.update(_IDENT_RE.findall(code))
    return toks


def _entity_id(name: str, etype: str) -> str:
    """Mirror kg._ensure_entity id scheme: sha256(name:type)."""
    return hashlib.sha256(f"{name}:{etype}".encode("utf-8")).hexdigest()


def _flush_workspace_graph(db, triples: list[tuple[str, str, str]]) -> int:
    """Replace all workspace anchor rows with a freshly-derived set.

    Anchor triples are *derived* facts, fully regenerated on every build — no
    temporal history to preserve — so we hard-delete and bulk-insert rather
    than close-and-reopen (which collides with kg_add's same-slice idempotency
    and would leave rows closed). project=NULL keeps them workspace-global.
    """
    ent_rows: dict[str, tuple] = {}
    tri_rows: list[tuple] = []
    seen: set[str] = set()
    for subject, predicate, obj in triples:
        for name, etype in ((subject, "code_ref"), (obj, "code_anchor")):
            eid = _entity_id(name, etype)
            ent_rows.setdefault(eid, (eid, name, etype, None, "{}"))
        tid = _triple_id(subject, predicate, obj, None)
        if tid in seen:
            continue
        seen.add(tid)
        tri_rows.append(
            (tid, subject, predicate, obj, None, None, None, None, None,
             _ANCHOR_CONFIDENCE)
        )
    # Use the raw connection from write() for all statements so the delete +
    # both inserts share ONE atomic transaction. (db.executemany opens its own
    # write() — nesting it here would raise "transaction within a transaction".)
    with db.write() as conn:
        conn.execute(
            """DELETE FROM kg_triples
               WHERE predicate IN ('defines_anchor', 'uses_anchor')
                 AND project IS NULL"""
        )
        conn.executemany(
            """INSERT OR IGNORE INTO kg_entities
               (id, name, type, project, properties) VALUES (?, ?, ?, ?, ?)""",
            list(ent_rows.values()),
        )
        conn.executemany(
            """INSERT OR IGNORE INTO kg_triples
               (id, subject, predicate, object, valid_from, valid_to,
                project, source_memory_id, source_artifact_id, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tri_rows,
        )
    return len(tri_rows)


def build_cross_repo_graph() -> dict:
    """(Re)derive the workspace cross-repo anchor graph over ALL ingested repos.

    Passes
    ------
    1. Owner index: every distinctive defined symbol -> ``defines_anchor``.
    2. Use scan: walk each repo's source files; emit ``uses_anchor`` for
       - symbol names owned by a *different* repo (cross-repo co-reference),
       - route/path literals, tool-name literals (shared-contract anchors).

    Returns a stats dict (projects, anchors, edges, cross-repo anchor count).
    """
    from okuro.db import get_db

    db = get_db()
    managed = _ingested_repos(db)
    projects = {p for p, _ in managed}
    if not projects:
        return {"projects": 0, "note": "no ingested (managed) repos found"}

    # Enumerate each repo's source files once; reused by both passes.
    repo_files: dict[str, tuple[Path, list[str]]] = {}
    for proj, root in managed:
        root_path = Path(root)
        repo_files[proj] = (root_path, sorted(discover_project_files(root_path)))

    def _read(root_path: Path, rel: str) -> str | None:
        fpath = root_path / rel
        try:
            if fpath.stat().st_size > _MAX_FILE_BYTES:
                return None
            return fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None

    triples: list[tuple[str, str, str]] = []
    anchors: set[str] = set()
    # name -> projects that DEFINE it (built in pass 1, consumed in pass 2).
    owner_names: dict[str, set[str]] = {}
    # repo -> names it imports from a framework/stdlib package (suppressed uses).
    repo_framework: dict[str, set[str]] = {}

    # Pass 1 — owner side: extract definitions across every repo (any language).
    for proj, (root_path, rels) in repo_files.items():
        for rel in rels:
            text = _read(root_path, rel)
            if text is None:
                continue
            repo_framework.setdefault(proj, set()).update(
                _framework_import_names(text))
            node = f"{proj}|{rel}"
            for name in _extract_def_names(text):
                anchor = f"sym:{name}"
                anchors.add(anchor)
                owner_names.setdefault(name, set()).add(proj)
                triples.append((node, "defines_anchor", anchor))

    # Pass 2 — use side. Symbols + tools + client-called routes are consumer
    # references (uses_anchor); server-declared routes are producers
    # (defines_anchor). Direction = consumer repo depends on producer repo.
    for proj, (root_path, rels) in repo_files.items():
        for rel in rels:
            text = _read(root_path, rel)
            if text is None:
                continue
            node = f"{proj}|{rel}"
            uses: set[str] = set()      # consumer anchors  -> uses_anchor
            provides: set[str] = set()  # producer routes   -> defines_anchor

            # (a) cross-repo symbol co-reference: a TYPE defined in ANOTHER repo,
            # referenced here in a code position — excluding names this file
            # IMPORTS (those resolve to the imported module, not a local type).
            imported = _imported_names(text) | repo_framework.get(proj, set())
            for tok in _reference_tokens(text):
                if tok in imported:
                    continue
                projs = owner_names.get(tok)
                if projs and proj not in projs:
                    uses.add(f"sym:{tok}")

            # (b) route literals — server-declaration (producer) vs client-call.
            is_restclient = bool(_RESTCLIENT_RE.search(text)) or "Client" in Path(rel).name
            for line in text.splitlines():
                for m in _ROUTE_RE.findall(line):
                    a = f"route:{m}"
                    if not is_restclient and _ROUTE_SERVER_RE.search(line):
                        provides.add(a)
                    else:
                        uses.add(a)

            # (c) tool/name registry literals (consumer references)
            for m in _TOOL_RE.findall(text):
                uses.add(f"tool:{m}")

            for a in provides:
                anchors.add(a)
                triples.append((node, "defines_anchor", a))
            for a in uses:
                anchors.add(a)
                triples.append((node, "uses_anchor", a))

    edges = _flush_workspace_graph(db, triples)
    tiered = _stamp_anchor_tiers(db)
    return {
        "projects": len(projects),
        "anchors": len(anchors),
        "edges": edges,
        "cross_repo_anchors": _cross_repo_anchor_count(db),
        "tiered_anchors": tiered,
    }


def _stamp_anchor_tiers(db) -> int:
    """Rewrite each cross-repo anchor's member triples to its aggregated tier's
    confidence, so the confidence column decodes to the right tier graph-wide
    (parity with the per-repo edges). Non-cross-repo anchors keep the inferred
    baseline. Returns the number of anchors restamped.

    Runs after ``_flush_workspace_graph`` (which writes every triple at the
    baseline) — the tier is a property of the aggregated anchor, only known once
    all producers/consumers are indexed.
    """
    idx = anchor_index(db)
    stamped = 0
    with db.write() as conn:
        for anchor, e in idx.items():
            if not e["cross_repo"]:
                continue
            conf = TIER_CONFIDENCE[e["tier"]]
            if conf == _ANCHOR_CONFIDENCE:
                continue  # already at the inferred baseline
            conn.execute(
                """UPDATE kg_triples SET confidence = ?
                   WHERE object = ? AND project IS NULL
                     AND predicate IN ('defines_anchor', 'uses_anchor')""",
                (conf, anchor),
            )
            stamped += 1
    return stamped


def anchor_index(db) -> dict[str, dict]:
    """All anchors with producer/consumer repo sets, members, and a kind-aware
    ``cross_repo`` flag. Single source of truth for every cross-repo query.

    Direction: ``producers`` declare/define the anchor (server route, type def);
    ``consumers`` reference it (client call, type use). A consumer repo depends
    on a producer repo. Cross-repo rule is kind-aware:
      - route → needs a producer AND a consumer in different repos (a route
        merely declared by two servers is not a shared dependency);
      - sym / tool → spans >= 2 repos.
    """
    rows = db.fetchall(
        """SELECT subject, predicate, object FROM kg_triples
           WHERE predicate IN ('defines_anchor','uses_anchor')
             AND project IS NULL AND (valid_to IS NULL OR valid_to = '')"""
    )
    idx: dict[str, dict] = {}
    for r in rows:
        proj, _, rel = r["subject"].partition("|")
        e = idx.setdefault(r["object"],
                           {"producers": set(), "consumers": set(), "members": []})
        producer = r["predicate"] == "defines_anchor"
        (e["producers"] if producer else e["consumers"]).add(proj)
        e["members"].append({"project": proj, "file": rel or "(repo)",
                             "role": "produces" if producer else "consumes"})
    for anchor, e in idx.items():
        kind = anchor.split(":", 1)[0]
        projs = e["producers"] | e["consumers"]
        e["kind"] = kind
        e["projects"] = sorted(projs)
        if kind == "route":
            e["cross_repo"] = (bool(e["producers"]) and bool(e["consumers"])
                               and len(projs) >= 2)
        else:
            e["cross_repo"] = len(projs) >= 2
        # Provenance tier of the cross-repo link (parity with per-repo edges).
        if e["cross_repo"]:
            e["tier"] = cross_repo_tier(kind, len(e["producers"]))
            e["confidence"] = TIER_CONFIDENCE[e["tier"]]
        else:
            e["tier"] = None
            e["confidence"] = None
    return idx


def cross_repo_index(db) -> dict[str, dict]:
    """Just the anchors that qualify as cross-repo."""
    return {a: e for a, e in anchor_index(db).items() if e["cross_repo"]}


def _cross_repo_anchor_count(db) -> int:
    return len(cross_repo_index(db))


def cross_repo_connections(node: str | None = None, anchor: str | None = None,
                           limit: int = 50) -> dict:
    """Find cross-repo connections for a file node or a specific anchor.

    - ``anchor`` given → every file (grouped by project) linked to it.
    - ``node`` (``<project>|<relpath>`` or bare relpath) given → its anchors,
      and for each the files in *other* repos that share it.
    """
    from okuro.db import get_db

    db = get_db()
    idx = anchor_index(db)

    if anchor:
        e = idx.get(anchor)
        if not e:
            return {"anchor": anchor, "cross_repo": False, "members": []}
        return {
            "anchor": anchor,
            "kind": e["kind"],
            "tier": e["tier"],
            "confidence": e["confidence"],
            "projects": e["projects"],
            "cross_repo": e["cross_repo"],
            "producers": sorted(e["producers"]),
            "consumers": sorted(e["consumers"]),
            "members": e["members"][:limit],
        }

    if not node:
        return {"error": "provide either node or anchor"}

    # Resolve a bare relpath to a project-qualified node if needed.
    if "|" not in node:
        for e in idx.values():
            hit = next((m for m in e["members"] if m["file"] == node), None)
            if hit:
                node = f"{hit['project']}|{node}"
                break

    my_proj, _, my_rel = node.partition("|")
    connections = []
    for anchor_id, e in idx.items():
        if not e["cross_repo"]:
            continue
        mine = [m for m in e["members"]
                if m["project"] == my_proj and m["file"] == my_rel]
        if not mine:
            continue
        others = [m for m in e["members"] if m["project"] != my_proj]
        if not others:
            continue
        # consumer depends on producer; producer is depended-on by consumer.
        direction = ("depends_on" if mine[0]["role"] == "consumes"
                     else "depended_on_by")
        connections.append({"anchor": anchor_id, "kind": e["kind"],
                            "tier": e["tier"], "direction": direction,
                            "linked": others})
    connections.sort(key=lambda c: -len(c["linked"]))
    return {"node": node, "connections": connections[:limit]}


def cross_repo_search(query: str, limit: int = 25) -> dict:
    """Substring search over anchors; return matches that span >= 2 repos first.

    Complements semantic cortex_search (which already spans repos for prose) by
    surfacing *structural* shared elements — the exact symbols/routes/tools that
    tie repos together.
    """
    from okuro.db import get_db

    db = get_db()
    q = query.lower()
    items = [(a, e) for a, e in anchor_index(db).items() if q in a.lower()]
    items.sort(key=lambda ae: (-int(ae[1]["cross_repo"]),
                               -len(ae[1]["projects"]), -len(ae[1]["members"])))
    return {
        "query": query,
        "results": [
            {"anchor": a, "kind": e["kind"], "tier": e["tier"],
             "projects": len(e["projects"]), "refs": len(e["members"]),
             "cross_repo": e["cross_repo"]}
            for a, e in items[:limit]
        ],
    }


def cross_repo_insights(top_n: int = 20) -> dict:
    """Workspace bridge hubs: anchors tying the most repos together.

    The cross-repo analogue of per-repo god-nodes — the shared contracts and
    types that, if changed, ripple across the widest set of repositories.
    """
    from okuro.db import get_db

    db = get_db()
    xr = cross_repo_index(db)
    ranked = sorted(xr.items(),
                    key=lambda ae: (-len(ae[1]["projects"]), -len(ae[1]["members"])))
    bridges = [
        {
            "anchor": a,
            "kind": e["kind"],
            "tier": e["tier"],
            "projects": len(e["projects"]),
            "refs": len(e["members"]),
            "producers": sorted(e["producers"]),
            "consumers": sorted(e["consumers"]),
        }
        for a, e in ranked[:top_n]
    ]
    # Tier rollup across all cross-repo bridges — the workspace analogue of the
    # per-repo audit's edge_tiers.
    tier_counts: dict[str, int] = {}
    for e in xr.values():
        tier_counts[e["tier"]] = tier_counts.get(e["tier"], 0) + 1
    return {"bridge_hubs": bridges, "count": len(xr), "tiers": tier_counts}
