# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Cortex module tools — codebase navigation, search, routing.
# index: imports | def _py_grep | def _text | def _resolve_path | def get_tools | def handle_tool | def _dispatch
# AGENT_HEADER_END -->
"""Cortex module tools — codebase navigation, search, routing.

Extracted from cortex/mcp_server.py for use by the unified okuro.mcp.server.

Multi-project awareness (post-025):
- Every search hit carries a `project` field (slug of the owning root or null).
- cortex_search / cortex_search_code / cortex_route accept `project=<slug>`
  to scope results.
- cortex_read_* resolve paths under ANY registered root, not only OKURO_ROOT.
- cortex_scope() is a cheap introspection endpoint listing roots + counts so
  agents can verify coverage before trusting results.
"""

import json
import logging
import os
import re as _re
import shutil
from pathlib import Path
from typing import Optional

from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)

# Detect ripgrep at import time.
_HAS_RG = shutil.which("rg") is not None

_FALLBACK_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", ".next", ".turbo", ".pnpm", "build",
}
_FALLBACK_MAX_FILE_BYTES = 1_000_000


def _py_grep(query: str, root: Path, max_matches: int) -> list[dict]:
    """Python fallback for `rg --json`."""
    try:
        pattern = _re.compile(query)
    except _re.error:
        pattern = _re.compile(_re.escape(query))

    matches: list[dict] = []

    def _iter_files(base: Path):
        if base.is_file():
            yield base
            return
        for p in base.rglob("*"):
            if any(part in _FALLBACK_SKIP_DIRS for part in p.parts):
                continue
            if p.is_file():
                yield p

    for fp in _iter_files(root):
        if len(matches) >= max_matches:
            break
        try:
            if fp.stat().st_size > _FALLBACK_MAX_FILE_BYTES:
                continue
            with open(fp, "r", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if pattern.search(line):
                        matches.append({
                            "path": str(fp),
                            "line": i,
                            "text": line.rstrip("\n"),
                        })
                        if len(matches) >= max_matches:
                            break
        except (OSError, UnicodeDecodeError):
            continue

    return matches


def _text(data) -> list[TextContent]:
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


# Floor for per-root collection. Ranking can only reorder what was collected,
# and ripgrep emits in its own directory-walk order, so a thin sample means the
# definition may simply never be a candidate: with a floor of 10, searching
# `_open_todos` collected ten test-file mentions from okuro and never reached
# the definition in src/. A wider sample per root is what gives the scorer
# something real to choose between.
#
# There is deliberately NO ceiling: capping collection below `n` would make a
# multi-root search return fewer results than the same query scoped to one
# root, and that shortfall would then read as exhaustive. Pool size is bounded
# by n x roots, and n is the caller's own number.
_MIN_COLLECT_PER_ROOT = 60

# A line that starts with one of these is a DEFINITION, not a mention. Kept
# deliberately cross-language and anchored to the start of the (stripped) line
# so `import foo` or `# see def foo` never scores as one.
_DEFINITION_LINE = _re.compile(
    r"^\s*(?:@\w+\s*)?"                       # a decorator line above is fine
    r"(?:export\s+|public\s+|private\s+|protected\s+|static\s+|async\s+|pub\s+)*"
    r"(?:def|class|function|fn|func|interface|struct|enum|impl|trait|type|"
    r"const|let|var|module)\b",
    _re.I,
)

# Path segments that mean "not our code" — a hit here is almost never the one
# the agent wants, even when it matches perfectly.
_VENDOR_SEGMENTS = frozenset({
    "node_modules", "vendor", ".venv", "venv", "site-packages", "dist", "build",
    ".git", "__pycache__", "target", ".next", ".cache", "third_party",
})


def _rank_match(m: dict, prefer_project: Optional[str] = None) -> tuple:
    """Relevance key for one search hit — higher sorts first.

    Ordering used to be an accident of the walk: results came back in root
    registration order, and which hits survived the cap was simply whichever
    ripgrep emitted first. A definition in src/ and a passing mention in a
    vendored bundle were indistinguishable to the caller.

    Signals, strongest first — all derived from data already on the hit, so
    this stays a pure function with no I/O and no parse of the (regex) query:

      * vendored/build path      — heavily demoted, these are rarely the answer
      * definition-shaped line   — `def foo(` beats `foo()` beats `# foo`
      * kind                     — code > test > doc (see _classify_match_path)
      * prefer_project           — a nudge, ranked BELOW the intrinsic signals
      * shallower path           — src/okuro/x.py beats a/b/c/d/e/x.py

    ``prefer_project`` is the caller saying "I am working in this repo". It is
    deliberately a tiebreak rather than a lead signal: a definition elsewhere
    still beats a passing mention at home. It is also deliberately NOT inferred
    from the process CWD — for an MCP server the CWD belongs to the client, and
    in the session that motivated this it was the workspace repo while the work was in
    okuro, so guessing would have boosted precisely the wrong root.

    Every component is numeric and higher-is-better, so callers sort with
    reverse=True. Ties are broken by (path, line) via a prior stable sort —
    keeping strings out of this tuple avoids trying to negate them.
    """
    path = m.get("path") or ""
    parts = path.replace("\\", "/").split("/")
    vendored = any(seg in _VENDOR_SEGMENTS for seg in parts)
    return (
        0 if vendored else 1,
        1 if _DEFINITION_LINE.match(m.get("text") or "") else 0,
        {"code": 2, "test": 1, "doc": 0}.get(m.get("kind"), 0),
        1 if (prefer_project and m.get("project") == prefer_project) else 0,
        -len(parts),
    )


def _classify_match_path(path_str: str) -> str:
    """Classify a search hit by its file path so agents can tell a route/symbol
    DEFINITION apart from a test or a doc that merely mentions it.

    Returns one of: "test", "doc", "code". Deterministic, path-only (no I/O).
    """
    p = path_str.lower()
    parts = p.replace("\\", "/").split("/")
    name = parts[-1] if parts else p
    if (
        any(seg in {"test", "tests", "__tests__", "spec", "specs", "e2e"} for seg in parts)
        or name.startswith("test_")
        or name.endswith(("_test.py", "_test.go", ".test.ts", ".test.tsx", ".test.js", ".spec.ts", ".spec.js"))
        or "test" in name and name.endswith(".java")  # FooTest.java, FooIT.java-style
    ):
        return "test"
    if name.endswith((".md", ".mdx", ".rst", ".txt", ".adoc")) or "docs" in parts or "doc" in parts:
        return "doc"
    return "code"


def _resolve_path(path_arg: str) -> Path:
    """Resolve a tool-supplied path under any registered root.

    Raises FileNotFoundError with a message listing attempted roots so the
    caller can surface a useful error via TextContent.
    """
    from .roots import registered_roots, resolve_under_roots

    resolved = resolve_under_roots(path_arg)
    if resolved is not None:
        return resolved

    roots_preview = [
        f"{r.project or '(unregistered)'}:{r.path}"
        for r in registered_roots()
    ]
    raise FileNotFoundError(
        f"Path '{path_arg}' did not resolve under any registered root. "
        f"Tried: {', '.join(roots_preview) or '(none)'}"
    )


# Scope-guardrail hints now live in cortex/scope_hints.py so the HTTP search
# API (orchestrator/api/cortex.py) shares one source of truth with these MCP
# tools. Imported under the old private name to keep call sites unchanged.
from .scope_hints import scope_annotation as _scope_annotation  # noqa: E402


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="cortex_search",
            description=(
                "Semantic search across the indexed codebase. Returns ranked file paths with relevance scores, "
                "code snippets, AND the owning project slug so you can spot cross-project noise at rank time. "
                "Use this instead of Grep for broad searches — it understands intent, not just string matching. "
                "Pass `project=<slug>` to scope results to one registered project (see cortex_scope). "
                "ROUTING — three retrieval paths, pick by question shape: concept/intent → this tool; "
                "exact symbol/string/import → cortex_search_code (literal, needs no scope); "
                "callers/imports/blast-radius/subsystem map → codegraph_insights / cross_repo_search. "
                "An empty result is NOT proof of absence — check the `scope` field for scope/tool hints."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language description of what you're looking for"},
                    "n": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20, "description": "Number of results to return"},
                    "file_type": {"type": "string", "enum": ["code", "doc", "config", "data", "template", "test"], "description": "Filter by file classification"},
                    "project": {"type": "string", "description": "Restrict to this project slug (exact match on cortex_docs.project). Prefer this over path_prefix for multi-repo workspaces."},
                    "path_prefix": {"type": "string", "description": "Legacy: restrict results to files whose path contains this substring. Kept for backward compat — prefer `project`."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="cortex_search_code",
            description=(
                "Literal code pattern search using ripgrep (or Python regex fallback). "
                "Use this when you need exact string/regex matches — function names, imports, error messages. "
                "Returns file path, line number, matching line text, the owning project slug, AND a `kind` "
                "tag (\"code\" | \"test\" | \"doc\") so you can tell a definition apart from a test or doc mention. "
                "With NO project/path it walks EVERY registered root and splits `n` fairly between them, so no "
                "single large repo can crowd the others out — an exact symbol is findable without any scope "
                "discipline (this is the fast path to 'where is X defined/used' across all repos). A BROAD "
                "pattern still fills the budget: when it does, the reply carries a `truncated` block naming the "
                "roots that hit their share — treat that result as a sample, never as proof of absence, and "
                "narrow the pattern or raise n. "
                "Pass `project=<slug>` to narrow the walk. "
                "Use cortex_search for semantic/conceptual queries, this tool for exact patterns, and "
                "codegraph_insights / cross_repo_search for structural navigation (callers, imports, hubs)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Exact string or regex pattern to search for"},
                    "path": {"type": "string", "description": "Restrict search to this subdirectory (absolute, or relative to a registered root)"},
                    "project": {"type": "string", "description": "Restrict the walk to this registered project slug (see cortex_scope)."},
                    "prefer_project": {"type": "string", "description": "Soft preference, NOT a filter: still searches every root, but breaks ties toward this slug. Use it for the repo you are working in when you still want cross-repo hits. A definition elsewhere still outranks a mention here."},
                    "n": {"type": "integer", "default": 10, "description": "Maximum number of matches to return"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="cortex_route",
            description=(
                "Quick navigation lookup — returns top 5 files most relevant to a concept, each tagged with project slug. "
                "Lightweight version of cortex_search with fixed result count. "
                "Pass `project=<slug>` when you want to route within one repo only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Concept or feature to locate (e.g., 'bootstrap assembly', 'GPU allocation')"},
                    "project": {"type": "string", "description": "Restrict to this project slug."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="cortex_scope",
            description=(
                "Introspection — returns indexed roots, per-project document counts, and the cortex schema "
                "version. Call this FIRST when searching across multi-repo workspaces so you know which "
                "projects are actually indexed before trusting (or mistrusting) results."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="cortex_read_header",
            description=(
                "Read only the AGENT_HEADER block from a file — returns purpose, role classification, "
                "and section index. Accepts absolute paths OR paths relative to any registered root "
                "(see cortex_scope). Use this BEFORE reading the full file."
            ),
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path: absolute, or relative to any registered root."}},
                "required": ["path"],
            },
        ),
        Tool(
            name="cortex_read_section",
            description=(
                "Read a specific line range from a file. Use after cortex_read_header to read only "
                "the section you need, avoiding full-file reads. Accepts absolute or registered-root-relative paths."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path: absolute, or relative to any registered root."},
                    "start_line": {"type": "integer", "minimum": 1, "description": "First line to read (1-based)"},
                    "end_line": {"type": "integer", "minimum": 1, "description": "Last line to read (1-based, inclusive)"},
                },
                "required": ["path", "start_line", "end_line"],
            },
        ),
        Tool(
            name="cortex_read_file",
            description="Read an entire file. Prefer cortex_read_header + cortex_read_section for large files. Accepts absolute or registered-root-relative paths.",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path: absolute, or relative to any registered root."}},
                "required": ["path"],
            },
        ),
        Tool(
            name="cortex_navigate",
            description=(
                "List files in a directory relative to a path. 'up' = parent directory contents, "
                "'down' = children of a directory, 'siblings' = same-level files. Accepts absolute "
                "or registered-root-relative paths."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory path: absolute, or relative to any registered root."},
                    "direction": {"type": "string", "enum": ["up", "down", "siblings"], "description": "Navigation direction"},
                },
                "required": ["path", "direction"],
            },
        ),
        Tool(
            name="cortex_context",
            description=(
                "Quick file overview: returns the AGENT_HEADER plus first 2000 chars of content. "
                "Use when you want a fast preview of what a file does without reading the whole thing. "
                "Accepts absolute or registered-root-relative paths."
            ),
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path: absolute, or relative to any registered root."}},
                "required": ["path"],
            },
        ),
    ]



# ── index provenance ─────────────────────────────────────────────────

# Past this, a hit is old enough that quoting it as current is a real risk.
# 7 days is one nightly-sync cycle plus slack — a repo that misses a single run
# is not yet suspect; one that misses seven is.
_STALE_AFTER_DAYS = 7


def _provenance() -> dict[str, dict]:
    """project slug → {sha, synced, age_days} for every MANAGED repo.

    Cortex answers looked identical whether a file was indexed two minutes or two
    months ago. Between 2026-07-13 and 2026-09-03 twelve repos served seven-week-old
    code with no signal, which is how a broken credential turns into a confident
    wrong answer. One query, reused across every hit in the response.
    """
    from datetime import datetime, timezone

    try:
        from okuro.db import get_db

        rows = get_db().fetchall(
            "SELECT project_id, last_indexed_sha, last_synced_at, status FROM managed_repos"
        )
    except Exception:
        return {}  # provenance is an annotation; never fail a search over it

    now = datetime.now(timezone.utc)
    out: dict[str, dict] = {}
    for r in rows:
        slug = r["project_id"]
        if not slug:
            continue
        synced = r["last_synced_at"]
        age = None
        if synced:
            try:
                dt = datetime.fromisoformat(str(synced).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age = (now - dt).days
            except Exception:
                age = None
        out[slug] = {
            "sha": (r["last_indexed_sha"] or "")[:9] or None,
            "synced": synced,
            "age_days": age,
            "status": r["status"],
        }
    return out


def _stamp(results: list[dict], prov: dict[str, dict]) -> dict:
    """Mark stale hits inline; return the per-project detail for the payload.

    Only the OUTLIERS get an inline field. Repeating a fresh timestamp on every
    row is clutter that trains the reader to skip the column — the one case that
    must not be skipped is the stale one, so that is the only one that speaks.
    """
    touched: dict[str, dict] = {}
    for r in results:
        pr = prov.get(r.get("project") or "")
        if not pr:
            continue
        touched[r["project"]] = pr
        if pr["status"] == "error":
            r["stale"] = "index may be behind — this repo's credential is failing"
        elif pr["age_days"] is not None and pr["age_days"] >= _STALE_AFTER_DAYS:
            r["stale"] = f"indexed {pr['age_days']}d ago @ {pr['sha']}"
    return touched


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch a cortex tool, then disclose worktree context if there is any.

    The disclosure is attached HERE, not inside each handler, because every
    path-taking cortex tool has the identical exposure and the list keeps
    growing — one wrapper covers cortex_read_header / read_section / read_file
    / navigate / search_code / context and whatever is added next. Six copies
    of the same note would be six places to forget it (DP10).

    It rides as its own TextContent block rather than being folded into the
    JSON payload: no re-parsing of a handler's output, and the warning reads as
    a warning instead of as one more field the agent may skip.
    """
    blocks = await _dispatch(name, arguments)
    try:
        from .worktrees import note_for_path

        note = note_for_path((arguments or {}).get("path"))
    except Exception:
        # A disclosure is an addition to a working answer. It must never be
        # the reason a read fails.
        note = None
    if note:
        return [*blocks, TextContent(type="text", text=note)]
    return blocks


def _prepare_scope(project: Optional[str]) -> Optional[str]:
    """Build or refresh a worktree overlay when the caller names its slug.

    Naming the slug IS the laziness gate: 43 worktrees exist and only the one
    an agent is actually working in ever costs anything. Refreshing here (not
    on a timer) also closes the same-session edit gap — an agent that edits a
    file and immediately searches for it finds the edit, because the refresh
    re-stats the diff set on the way in.

    Detection is EXPLICIT, never inferred from the process CWD. Measured
    2026-08-04 across five live okuro MCP servers: not one had a CWD inside a
    worktree — three sat in the workspace repo and two in
    /usr/lib/claude-desktop, because an MCP server is a child of its client.
    A CWD heuristic would have fired zero times for the case it was meant to
    serve. Agents learn the slug from the worktree disclosure block that every
    cortex path answer carries, and from cortex_scope.

    Failure is silent and non-fatal: a search scoped to an overlay that could
    not be built still runs, and returns base results.
    """
    if not project:
        return project
    try:
        from .overlay import ensure
        from .worktrees import parse_overlay_slug, worktree_for_slug

        if parse_overlay_slug(project) is None:
            return project
        ctx = worktree_for_slug(project)
        if ctx is not None:
            ensure(ctx)
    except Exception:
        pass
    return project


async def _dispatch(name: str, arguments: dict) -> list[TextContent]:
    from .roots import project_for_path, registered_roots

    if name == "cortex_scope":
        from .vectorstore import VectorStore

        roots = [
            {"path": str(r.path), "project": r.project}
            for r in registered_roots()
        ]
        stats = VectorStore().get_stats()
        # Overlays are how a branch's own changes become searchable, and they
        # are only reachable by naming the slug — so the list of live slugs
        # has to be discoverable somewhere. This is that somewhere.
        try:
            from .overlay import known_slugs

            overlays = known_slugs()
        except Exception:
            overlays = []
        payload = {
            "schema_version": "025",
            "indexed_roots": roots,
            "document_counts": {
                "total": stats["total_documents"],
                "files": stats["files_indexed"],
                "sections": stats["sections_indexed"],
                "content_chunks": stats["content_chunks"],
            },
            "per_project": stats["per_project"],
            "embedding_model": stats["embedding_model"],
        }
        if overlays:
            payload["worktree_overlays"] = {
                "slugs": overlays,
                "note": "Pass one as project= to search a branch's changed "
                        "files alongside base, with the base copy of each "
                        "shadowed. Never included in an unscoped search.",
            }
        return _text(payload)

    if name == "cortex_route":
        from okuro.cortex.vectorstore import VectorStore
        vs = VectorStore()
        results = vs.search(
            arguments["query"],
            n_results=5,
            project=_prepare_scope(arguments.get("project")),
        )
        payload = {
            "query": arguments["query"],
            "results": [
                {
                    "path": str(r.file_path),
                    "project": r.project or project_for_path(r.file_path),
                    "score": r.relevance,
                    "snippet": r.snippet,
                    "matched_section": r.matched_section,
                    "keyword_score": r.keyword_score,
                }
                for r in results
            ],
        }
        sources = _stamp(payload["results"], _provenance())
        if sources:
            payload["sources"] = sources
        scope = _scope_annotation(arguments.get("project"), len(results))
        if scope:
            payload["scope"] = scope
        return _text(payload)

    if name == "cortex_search":
        from okuro.cortex.vectorstore import VectorStore
        vs = VectorStore()
        # file_type was previously named "role" — accept both for backwards compat
        file_type = arguments.get("file_type") or arguments.get("role")
        results = vs.search(
            arguments["query"],
            n_results=arguments.get("n", 5),
            filter_role=file_type,
            filter_path_prefix=arguments.get("path_prefix"),
            project=_prepare_scope(arguments.get("project")),
        )
        payload = {
            "results": [
                {
                    "path": str(r.file_path),
                    "project": r.project or project_for_path(r.file_path),
                    "score": r.relevance,
                    "snippet": r.snippet,
                    "matched_section": r.matched_section,
                    "keyword_score": r.keyword_score,
                }
                for r in results
            ],
        }
        sources = _stamp(payload["results"], _provenance())
        if sources:
            payload["sources"] = sources
        scope = _scope_annotation(arguments.get("project"), len(results))
        if scope:
            payload["scope"] = scope
        return _text(payload)

    if name == "cortex_read_header":
        from dataclasses import asdict, is_dataclass
        from okuro.cortex.core import parse_header, resolve_index_lines
        try:
            path = _resolve_path(arguments["path"])
        except FileNotFoundError as e:
            return _text(str(e))
        content = path.read_text(errors="replace")
        header = parse_header(content, file_path=path)
        source = "inline"
        if header is None:
            # Fallback: files we don't own carry no inline AGENT_HEADER, but the
            # per-directory .okuro-index.yaml sidecar holds the same metadata.
            # Without this, header-first reads are unusable on external repos and
            # agents abandon the discipline (see cortex gauntlet, 2026-07-14).
            from okuro.cortex import sidecar as _sidecar
            try:
                entries = _sidecar.load(path.parent)
            except Exception:
                entries = {}
            entry = entries.get(path.name)
            if entry is not None:
                header = _sidecar.entry_to_header(entry)
                source = "sidecar"
        if header is None:
            return _text(f"No AGENT_HEADER or sidecar entry found in {arguments['path']}")
        # Header text carries no line numbers, so resolve them against the file
        # itself — without this every entry is 0/0 and cortex_read_section
        # (start_line >= 1) rejects the chain this tool exists to start.
        header.index = resolve_index_lines(header.index, content)
        payload = asdict(header) if is_dataclass(header) else dict(header)
        payload["path"] = str(path)
        payload["project"] = project_for_path(path)
        payload["source"] = source
        return _text(payload)

    if name == "cortex_read_section":
        try:
            path = _resolve_path(arguments["path"])
        except FileNotFoundError as e:
            return _text(str(e))
        start = arguments["start_line"]
        end = arguments["end_line"]
        lines = path.read_text().splitlines()
        section = "\n".join(lines[start - 1 : end])
        return _text({
            "path": str(path),
            "project": project_for_path(path),
            "start_line": start,
            "end_line": end,
            "content": section,
        })

    if name == "cortex_read_file":
        try:
            path = _resolve_path(arguments["path"])
        except FileNotFoundError as e:
            return _text(str(e))
        return _text(path.read_text())

    if name == "cortex_navigate":
        try:
            path = _resolve_path(arguments["path"])
        except FileNotFoundError as e:
            return _text(str(e))
        direction = arguments["direction"]
        if direction == "up":
            parent = path.parent
            entries = [f.name for f in parent.iterdir() if not f.name.startswith(".")]
            return _text({
                "parent": str(parent),
                "project": project_for_path(parent),
                "entries": sorted(entries),
            })
        elif direction == "down":
            if path.is_dir():
                entries = [f.name for f in path.iterdir() if not f.name.startswith(".")]
                return _text({
                    "path": str(path),
                    "project": project_for_path(path),
                    "entries": sorted(entries),
                })
            return _text({"path": str(path), "project": project_for_path(path), "entries": []})
        else:  # siblings
            entries = [f.name for f in path.parent.iterdir() if not f.name.startswith(".")]
            return _text({
                "path": str(path),
                "project": project_for_path(path),
                "siblings": sorted(entries),
            })

    if name == "cortex_search_code":
        n = int(arguments.get("n", 10))
        query = arguments["query"]
        prefer_project = arguments.get("prefer_project") or None

        # Scope resolution: explicit path > project slug > walk every root.
        scopes: list[tuple[Path, Optional[str]]] = []
        if arguments.get("path"):
            try:
                base = _resolve_path(arguments["path"])
            except FileNotFoundError as e:
                return _text(str(e))
            scopes.append((base, project_for_path(base)))
        elif arguments.get("project"):
            slug = arguments["project"]
            match = next(
                (r for r in registered_roots() if r.project == slug),
                None,
            )
            if match:
                scopes.append((match.path, match.project))
            else:
                # A worktree overlay slug names a real tree even though it is
                # not a registered root. Refusing it here would be a trap of
                # our own making: the disclosure block tells agents to use
                # this slug, so every surface that takes project= must accept
                # it. ripgrep needs no overlay — it walks the worktree itself.
                from .worktrees import worktree_for_slug

                ctx = worktree_for_slug(slug)
                if ctx is None:
                    return _text(
                        f"Unknown project slug '{slug}'. "
                        f"Known: {[r.project for r in registered_roots() if r.project]}"
                    )
                scopes.append((ctx.tree, slug))
        else:
            scopes = [(r.path, r.project) for r in registered_roots()]

        backend = "rg" if _HAS_RG else "python"

        # Budget allocation across roots. The previous loop spent `n` strictly
        # in registration order and broke as soon as it was gone, so the FIRST
        # root could consume the entire budget and roots 2..N were never
        # searched at all. With OKURO_ROOT (the workspace repo, 7579 files) first and
        # 36 roots registered, any broad pattern made 35 repos invisible —
        # reproduced 2026-07-25: 'daily_brief|daily_digest|def brief' n=20
        # returned 20 workspace hits and 0 from okuro, which reads exactly
        # like "okuro is not indexed". It is (1964 files, 19,112 documents).
        #
        # Collect generously per root, then RANK and cut. Ordering used to be
        # an accident of the walk — root registration order, and within a root
        # whatever ripgrep printed first — so a definition in src/ and a
        # mention inside node_modules were interchangeable to the caller.
        #
        # Over-collecting also removes the multi-cycle budget redistribution an
        # equal-share split needed: one rg call per root, one global selection.
        # rg has to scan the whole root either way, so asking it for more lines
        # is close to free; re-invoking it per cycle was not.
        #
        # A single-root scope (path=/project=) is unaffected.
        collect_per_root = max(n, _MIN_COLLECT_PER_ROOT)
        found: dict[int, list[dict]] = {}
        searched: list[str] = []

        def _collect(base: Path, project_slug: Optional[str], want: int) -> list[dict]:
            got: list[dict] = []
            if want <= 0:
                return got
            if _HAS_RG:
                import subprocess
                # NOTE: rg's -m/--max-count is PER FILE, not a total, so it can
                # never bound the result set on its own — the caller must count.
                # The old code passed the whole remaining budget here and relied
                # on it as if it were a total.
                cmd = ["rg", "--json", "-m", str(want), query, str(base)]
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                except Exception:  # noqa: BLE001 — one bad root must not kill the search
                    return got
                for line in proc.stdout.splitlines():
                    if len(got) >= want:
                        break
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "match":
                            d = obj["data"]
                            _p = d["path"]["text"]
                            got.append({
                                "path": _p,
                                "project": project_slug,
                                "line": d["line_number"],
                                "text": d["lines"]["text"].strip(),
                                "kind": _classify_match_path(_p),
                            })
                    except (json.JSONDecodeError, KeyError):
                        pass
            else:
                for m in _py_grep(query, base, want):
                    if len(got) >= want:
                        break
                    got.append({
                        **m,
                        "project": project_slug,
                        "kind": _classify_match_path(m["path"]),
                    })
            return got

        # One pass — every root searched, each offering its best candidates.
        for i, (base, project_slug) in enumerate(scopes):
            found[i] = _collect(base, project_slug, collect_per_root)
            searched.append(project_slug or str(base))

        # Dedupe across NESTED roots. 19 of the 36 registered roots live inside
        # the same workspace root, so every file in one of them is found
        # twice — once scanning the parent, once the child — and both copies
        # compete for the budget. Measured 2026-07-25: a 12-result search
        # returned 3 duplicate pairs, a quarter of the reply spent saying the
        # same lines twice.
        #
        # The copies are not equivalent: _collect labels a hit with the slug of
        # the root it was scanning, so the parent-scan copy of
        # apps/active/watchdog/pipeline.py claims the workspace project.
        # That contradicts this module's own ownership rule — longest matching
        # root wins, see roots.py::project_for_path — so the deepest root's
        # copy is the correct one and the shallower one is dropped.
        by_key: dict[tuple, tuple[int, dict]] = {}
        for i in range(len(scopes)):
            depth = len(scopes[i][0].parts)
            for m in found[i]:
                key = (m.get("path"), m.get("line"))
                prev = by_key.get(key)
                if prev is None or depth > prev[0]:
                    by_key[key] = (depth, m)
        pool = [m for _, m in by_key.values()]

        # Stable two-stage sort: (path, line) first so equal-rank hits come out
        # in a fixed order, then rank descending on top of it.
        pool.sort(key=lambda m: (m.get("path") or "", m.get("line") or 0))
        pool.sort(key=lambda m: _rank_match(m, prefer_project), reverse=True)

        # Split the budget between RELEVANCE and REPRESENTATION.
        #
        # Pure ranking would rebuild the monopoly this was built to remove: the
        # top-scoring hits can all sit in one repo. But reserving a slot per
        # root FIRST is just as wrong in the other direction — with 36 roots
        # and n=6 the reservation consumes the entire budget, so a search for
        # `_open_todos` came back with six unrelated roots' incidental mentions
        # and never showed the definition at all.
        #
        # So: the first half of the budget goes to the best hits outright, the
        # rest introduces roots not yet represented, and anything left over
        # falls back to rank. A specific symbol surfaces its definition; a
        # broad sweep still reaches the small repos.
        matches: list[dict] = []
        picked: set = set()

        rank_slots = max(1, n // 2) if len(scopes) > 1 else n
        for m in pool:
            if len(matches) >= rank_slots:
                break
            matches.append(m)
            picked.add(id(m))

        if len(scopes) > 1:
            seen_roots = {m.get("project") or "" for m in matches}
            for m in pool:
                if len(matches) >= n:
                    break
                key = m.get("project") or ""
                if id(m) not in picked and key not in seen_roots:
                    seen_roots.add(key)
                    matches.append(m)
                    picked.add(id(m))

        for m in pool:
            if len(matches) >= n:
                break
            if id(m) not in picked:
                matches.append(m)
                picked.add(id(m))
        # Reserved-slot picks were appended in rank order but interleaved with
        # the fill, so re-sort the final cut for a relevance-ordered reply.
        matches.sort(key=lambda m: (m.get("path") or "", m.get("line") or 0))
        matches.sort(key=lambda m: _rank_match(m, prefer_project), reverse=True)

        out = {"matches": matches, "backend": backend}

        # Truncation is now REPORTED. It used to be silent, and the annotation
        # was attached only to scoped calls on the reasoning that "an unscoped
        # walk already covers every root, so empty = absent is meaningful
        # there" — which was exactly backwards: the unscoped capped walk is the
        # one an agent cannot tell apart from a complete one.
        if len(pool) > len(matches):
            # A root that filled its collection quota had more to give — those
            # are the ones whose coverage is genuinely partial. Roots that came
            # in under quota were searched exhaustively, even if ranking then
            # cut their hits from the reply.
            capped = sorted({
                (scopes[i][1] or str(scopes[i][0]))
                for i in range(len(scopes)) if len(found[i]) >= collect_per_root
            })
            out["truncated"] = {
                "limit": n,
                "roots_searched": len(scopes),
                "roots_at_cap": capped,
                "dropped_by_rank": len(pool) - len(matches),
                "hint": (
                    "Result hit the limit, so it is NOT exhaustive and absence "
                    "here proves nothing. Matches are ordered by relevance "
                    "(definitions and non-vendored code first). Narrow the "
                    "pattern (a full symbol or signature beats an alternation), "
                    "raise n, or pass project=<slug> to search one root."
                ),
            }

        scope = _scope_annotation(arguments.get("project"), len(matches)) \
            if arguments.get("project") else None
        if scope:
            out["scope"] = scope
        return _text(out)

    if name == "cortex_context":
        from dataclasses import asdict, is_dataclass
        from okuro.cortex.core import parse_header
        try:
            path = _resolve_path(arguments["path"])
        except FileNotFoundError as e:
            return _text(str(e))
        full = path.read_text()
        header = parse_header(full)
        header_payload = (
            asdict(header) if (header is not None and is_dataclass(header)) else header
        )
        return _text({
            "path": str(path),
            "project": project_for_path(path),
            "header": header_payload,
            "preview": full[:2000],
        })

    return _text(f"Unknown tool: {name}")
