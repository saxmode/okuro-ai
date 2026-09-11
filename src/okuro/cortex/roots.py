# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Registered cortex roots — single source of truth for "what does cortex index?".
# index: imports | def _okuro_root | def _cwd_root | def _db_project_paths | def registered_roots | def project_for_path | def register_root | def resolve_under_roots | def looks_like_project | def auto_discover_candidates | def deactivate_root
# AGENT_HEADER_END -->
"""Registered cortex roots.

Pre-025 cortex was pinned to one root (OKURO_ROOT). Post-025 cortex indexes
every active project that has a `path` set in the projects table. This module
is the only place that decides "what counts as a registered root" — scanner,
vectorstore, daemon reindex, and the MCP tools all go through here.

Rule:
- OKURO_ROOT is included when explicitly set. Its project slug is resolved by
  walking projects.path; if none matches, slug is None — a declared root is
  honoured even when unregistered.
- When OKURO_ROOT is NOT set, the process CWD is included only if it is itself
  a registered project. An arbitrary CWD is not a codebase anyone asked to
  index, and for an MCP server it belongs to the client, not the work.
- Any additional active project whose `path` exists on disk is included.
- Roots are deduplicated by resolved absolute path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Root:
    path: Path
    project: Optional[str]  # projects.id (slug) or None if unregistered


def _okuro_root() -> Optional[Path]:
    """The explicitly-declared primary root, or None.

    Falls back to the process CWD only when that CWD is a registered project —
    see :func:`registered_roots`. An unset OKURO_ROOT used to mean "index
    whatever directory this process happens to have started in", which for an
    MCP server is not a codebase at all: the server is a child of the client,
    so the CWD is the client's. Measured 2026-07-25 — the okuro MCP server
    serving Claude Desktop runs with CWD /usr/lib/claude-desktop, so 414 MB and
    1081 files of application bundle were registered as root #1 on every
    search, under the slug None.
    """
    declared = os.environ.get("OKURO_ROOT")
    if declared:
        return Path(declared).resolve()
    return None


def _cwd_root() -> Path:
    return Path(".").resolve()


def _db_project_paths() -> dict[Path, str]:
    """Absolute path → project slug, for every project cortex may scan.

    Split out of :func:`registered_roots` so the root-selection policy can be
    tested without a database — the policy is what decides whether an arbitrary
    CWD becomes a search root, and that deserves direct coverage.
    """
    from okuro.db import get_db

    try:
        # `indexed`, not `active`: this is a question about what cortex may
        # SCAN. `active` answers a different question — whether agents may
        # see the project's context — and reading it here is what let an
        # indexing decision cause project amnesia (migration 100).
        rows = get_db().fetchall(
            "SELECT id, path FROM projects "
            "WHERE indexed = 1 AND path IS NOT NULL"
        )
    except Exception:
        rows = []

    path_to_slug: dict[Path, str] = {}
    for r in rows:
        try:
            p = Path(r["path"]).resolve()
        except (OSError, ValueError):
            continue
        if p.exists():
            path_to_slug.setdefault(p, r["id"])
    return path_to_slug


def registered_roots() -> list[Root]:
    """Return every root cortex is allowed to touch, in priority order.

    Priority:
      1. OKURO_ROOT (with slug if it matches a registered project path)
      2. Every active project with an existing path, excluding dups.
    """
    path_to_slug = _db_project_paths()

    roots: list[Root] = []
    seen: set[Path] = set()

    # The primary root is honoured when it was DECLARED (OKURO_ROOT), even if
    # it maps to no project — that is a deliberate instruction. An undeclared
    # CWD only earns a slot when it is itself a registered project; otherwise
    # it is just wherever the process was launched, and indexing it means
    # scanning an unrelated directory on every query and returning hits from it
    # under a null slug.
    primary = _okuro_root()
    if primary is None:
        cwd = _cwd_root()
        primary = cwd if cwd in path_to_slug else None
    if primary is not None and primary.exists():
        roots.append(Root(path=primary, project=path_to_slug.get(primary)))
        seen.add(primary)

    for p, slug in path_to_slug.items():
        if p in seen:
            continue
        roots.append(Root(path=p, project=slug))
        seen.add(p)

    return roots


def project_for_path(file_path: Path) -> Optional[str]:
    """Given an absolute file path, return the slug of the root that owns it.

    Longest matching root wins (so a project nested inside another project
    gets its own slug, not the parent's).
    """
    try:
        fp = file_path.resolve()
    except (OSError, ValueError):
        return None

    best_match: Optional[Root] = None
    best_len = -1
    for root in registered_roots():
        try:
            if fp.is_relative_to(root.path):
                length = len(root.path.parts)
                if length > best_len:
                    best_len = length
                    best_match = root
        except (AttributeError, ValueError):
            continue

    return best_match.project if best_match else None


def register_root(
    path: str,
    slug: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    """Register a directory as an indexed cortex root.

    Upsert semantics: if a project row already exists with the same slug
    OR the same absolute path, it's reactivated / updated in place rather
    than producing a conflict. Returns a status dict suitable for CLI /
    HTTP responses:

        {
          "slug": <id>, "name": <display>, "path": <abs>,
          "action": "created" | "updated" | "already_active",
          "pre_existing_slug": <slug|None>,
        }

    Raises ValueError for bad input (missing path, not a directory, etc.).
    """
    from okuro.db import get_db

    if not path:
        raise ValueError("path is required")

    abs_path = Path(path).expanduser().resolve()
    if not abs_path.exists():
        raise ValueError(f"Path does not exist: {abs_path}")
    if not abs_path.is_dir():
        raise ValueError(f"Path is not a directory: {abs_path}")

    # Slug default = lowercase directory name, spaces → hyphens.
    derived_slug = (slug or abs_path.name).strip().lower().replace(" ", "-")
    display_name = name or abs_path.name
    if not derived_slug:
        raise ValueError("Could not derive slug from path; pass slug explicitly.")

    db = get_db()

    # Path-based existing row wins over slug-based — a path should only
    # ever have one slug, and user might pass a different slug for an
    # already-registered path.
    existing_by_path = db.fetchone(
        "SELECT id, name, active FROM projects WHERE path = ?",
        (str(abs_path),),
    )
    existing_by_slug = db.fetchone(
        "SELECT id, path, active FROM projects WHERE id = ?",
        (derived_slug,),
    )

    if existing_by_path:
        pre_slug = existing_by_path["id"]
        if existing_by_path["active"] == 1 and pre_slug == derived_slug:
            _backfill_project_tag(abs_path, derived_slug)
            return {
                "slug": pre_slug,
                "name": existing_by_path["name"],
                "path": str(abs_path),
                "action": "already_active",
                "pre_existing_slug": pre_slug,
            }
        # Re-index + optionally rename the slug in place. Sets BOTH flags:
        # registering a root is an explicit "scan this" (indexed) and, since
        # the human is pointing okuro at a live project, "agents may see it"
        # (active) — the natural reading of a registration. Only the split
        # makes that a deliberate choice rather than an accident of sharing
        # one column (migration 100).
        db.execute(
            "UPDATE projects SET id = ?, name = ?, indexed = 1, active = 1, "
            "updated_at = datetime('now') WHERE path = ?",
            (derived_slug, display_name, str(abs_path)),
        )
        db.conn.commit()
        _backfill_project_tag(abs_path, derived_slug)
        return {
            "slug": derived_slug,
            "name": display_name,
            "path": str(abs_path),
            "action": "updated",
            "pre_existing_slug": pre_slug,
        }

    if existing_by_slug:
        # A slug bound to a path that does not exist on disk is not a
        # conflict — it is an UNBOUND project, and refusing it is what keeps
        # it unbound forever. Two ways a row gets one: registered before the
        # code landed (placeholders like 'unknown/<slug>'), or its directory
        # was moved or deleted. Measured 2026-08-30: a registered tool project sat
        # at such a placeholder path, so it never became a root, cortex
        # never counted a document under its slug, and bootstrap told every
        # agent the code was not indexed. Adopting the real path is the only
        # outcome that can be right. A conflict with a path that DOES exist
        # still raises — that is a genuine collision between two codebases.
        prior = existing_by_slug["path"]
        prior_exists = False
        if prior:
            try:
                prior_exists = Path(prior).expanduser().exists()
            except (OSError, ValueError):
                prior_exists = False
        if prior_exists:
            raise ValueError(
                f"Slug '{derived_slug}' is already used by another path: "
                f"{prior}. Pass --slug to choose a different one."
            )
        db.execute(
            "UPDATE projects SET path = ?, name = ?, indexed = 1, active = 1, "
            "updated_at = datetime('now') WHERE id = ?",
            (str(abs_path), display_name, derived_slug),
        )
        db.conn.commit()
        _backfill_project_tag(abs_path, derived_slug)
        return {
            "slug": derived_slug,
            "name": display_name,
            "path": str(abs_path),
            "action": "rebound",
            "pre_existing_slug": derived_slug,
            "previous_path": prior,
        }

    db.execute(
        "INSERT INTO projects (id, name, path, active, indexed) "
        "VALUES (?, ?, ?, 1, 1)",
        (derived_slug, display_name, str(abs_path)),
    )
    db.conn.commit()
    _backfill_project_tag(abs_path, derived_slug)
    return {
        "slug": derived_slug,
        "name": display_name,
        "path": str(abs_path),
        "action": "created",
        "pre_existing_slug": None,
    }


def _backfill_project_tag(abs_path: Path, slug: str) -> int:
    """Tag any cortex_docs rows under ``abs_path`` that have a NULL project.

    Files indexed before a path was registered (or before project tagging
    landed) sit in cortex_docs with project=NULL. They're invisible to
    project-scoped searches forever unless we backfill on registration.
    Returns the number of rows updated. Best-effort — silently swallows
    schema/version drift so registration never fails on this.
    """
    from okuro.db import get_db

    try:
        db = get_db()
        prefix = f"{abs_path}/%"
        with db.write():
            cur = db.execute(
                "UPDATE cortex_docs SET project = ? "
                "WHERE file_path LIKE ? AND project IS NULL "
                "AND deleted_at IS NULL",
                (slug, prefix),
            )
            return cur.rowcount or 0
    except Exception:
        return 0


def resolve_path(path: str) -> Path:
    """Public path resolver — used by callers that want to *fail loud* when
    a path doesn't sit under any registered root.

    Wraps ``resolve_under_roots`` and raises ``FileNotFoundError`` with the
    list of attempted roots when nothing resolves. Same semantics the cortex
    MCP tools use internally; exposed here so non-cortex code (role-handover
    validators, orchestrator hooks) can lean on the same logic without
    private-symbol imports.

    Returns an absolute Path on success.
    """
    resolved = resolve_under_roots(path)
    if resolved is not None:
        return resolved
    roots_preview = [
        f"{r.project or '(unregistered)'}:{r.path}"
        for r in registered_roots()
    ]
    raise FileNotFoundError(
        f"Path '{path}' did not resolve under any registered root. "
        f"Tried: {', '.join(roots_preview) or '(none)'}"
    )


_PROJECT_MARKERS: tuple[str, ...] = (
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "tsconfig.json",
    "compose.yaml",
    "docker-compose.yml",
    "Dockerfile",
)

def _default_search_parents() -> tuple[Path, ...]:
    """Home + the machine's own workspace parents, from conventions.

    ``cortex.search_parents`` (list of paths, ``~`` ok) names workspace
    directories whose children are candidate projects — a nested layout
    like a monorepo of app dirs. The install dir is always a candidate.
    """
    from okuro.yu.conventions import get_convention

    extra = get_convention("cortex.search_parents", []) or []
    return tuple(
        dict.fromkeys(
            [Path.home()]
            + [Path(p).expanduser() for p in extra]
            + [Path.home() / "okuro"]
        )
    )


_DEFAULT_SEARCH_PARENTS: tuple[Path, ...] = _default_search_parents()


def looks_like_project(directory: Path, *, min_code_files: int = 10) -> tuple[int, bool, bool]:
    """Heuristic: is ``directory`` a real source-code project worth indexing?

    Returns ``(code_file_count, has_git, has_marker)``. A directory qualifies
    when:

    - it contains at least one project marker file (pyproject / package.json /
      Dockerfile / etc.) **or** a ``.git`` directory, and
    - it carries at least ``min_code_files`` indexable source files.

    The walk reuses the central exclusion matcher so noise dirs (node_modules,
    .venv, …) are skipped during the count. Cap is 500 to keep the probe fast.
    """
    from .exclusions import build_matcher

    if not directory.is_dir():
        return 0, False, False
    has_git = (directory / ".git").is_dir()
    has_marker = any((directory / m).exists() for m in _PROJECT_MARKERS)
    if not (has_git or has_marker):
        return 0, has_git, has_marker

    matcher = build_matcher(directory)
    code_exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".sh",
                 ".yaml", ".yml", ".sql", ".rs", ".go", ".html", ".css"}
    count = 0
    import os
    for dirpath, dirnames, filenames in os.walk(directory, onerror=lambda _e: None):
        dirnames[:] = [d for d in dirnames if not matcher.is_pruned_dir(d)]
        for fn in filenames:
            if Path(fn).suffix.lower() in code_exts:
                count += 1
                if count >= 500:
                    return count, has_git, has_marker
    return count, has_git, has_marker


def auto_discover_candidates(
    parents: Optional[list[Path]] = None,
    *,
    min_code_files: int = 10,
) -> list[dict]:
    """Find directories under ``parents`` that look like real projects but
    are not yet registered.

    Returns a list of dicts with ``path``, ``code_files``, ``has_git``,
    ``has_marker``, ``suggested_slug`` keys. Caller decides which to register.

    Skips: anything already in the registry, hidden directories, dirs whose
    name suggests a worktree (``-wt-``), and dirs without enough source files
    to be worth indexing.
    """
    registered_paths = {Path(str(r.path)).resolve() for r in registered_roots()}
    search_parents = parents or list(_DEFAULT_SEARCH_PARENTS)

    out: list[dict] = []
    for parent in search_parents:
        if not parent.is_dir():
            continue
        for d in parent.iterdir():
            try:
                if not d.is_dir():
                    continue
                if d.name.startswith("."):
                    continue
                # Legacy sibling worktrees (`<main>-wt-<topic>`). Since
                # 2026-09-10 new trees live at `<main>/worktrees/<topic>`,
                # which discovery never reaches — it reads one level under
                # each search parent, and a nested tree's parent is a repo
                # that is already registered. Kept for the sibling trees that
                # predate the move; the marker check below is the general
                # answer either way.
                if "-wt-" in d.name or d.name.endswith("-worktree"):
                    continue
                if (d / ".okuro-worktree").is_file():
                    continue
                resolved = d.resolve()
                if resolved in registered_paths:
                    continue
                count, has_git, has_marker = looks_like_project(
                    d, min_code_files=min_code_files
                )
                if count < min_code_files:
                    continue
                out.append({
                    "path": str(resolved),
                    "code_files": count,
                    "has_git": has_git,
                    "has_marker": has_marker,
                    "suggested_slug": resolved.name.lower().replace(" ", "-"),
                })
            except (PermissionError, OSError):
                continue
    out.sort(key=lambda x: -x["code_files"])
    return out


def deactivate_root(slug: str) -> dict:
    """Mark a registered project as inactive so cortex stops indexing it.

    Returns ``{"slug": …, "action": "deactivated" | "not_found" | "already_inactive"}``.
    Non-destructive: the projects row stays, only ``active`` flips to 0.
    Reactivation goes through :func:`register_root` again.
    """
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT id, indexed FROM projects WHERE id = ?", (slug,))
    if not row:
        return {"slug": slug, "action": "not_found"}
    if row["indexed"] == 0:
        return {"slug": slug, "action": "already_inactive"}
    # Clear `indexed` ONLY. This function retires a root from scanning; it
    # must not touch `active`, which governs whether agents can still see the
    # project's charter, memories and progress. Before migration 100 these
    # were one flag, so retiring a duplicate root on 2026-07-13 also hid 46
    # of that project's memories from every subsequent bootstrap — silently, because
    # the caller had no way to know it was writing the sense layer's flag.
    db.execute(
        "UPDATE projects SET indexed = 0, updated_at = datetime('now') "
        "WHERE id = ?",
        (slug,),
    )
    db.conn.commit()
    return {"slug": slug, "action": "deactivated"}


def resolve_under_roots(path: str) -> Optional[Path]:
    """Resolve `path` to an absolute file that exists under some registered root.

    Accepts:
      - absolute paths (must exist and lie under a registered root, or inside
        a bound git worktree of one)
      - paths relative to any registered root (first hit wins, OKURO_ROOT priority)

    A RELATIVE path never resolves into a worktree, deliberately: it carries no
    branch context, so "src/x.py" from an agent in a worktree is indistinguishable
    from the same string typed anywhere else, and picking a tree would be a guess.
    Absolute paths say which checkout they mean.

    Returns None if nothing resolves. Callers should raise with a helpful
    message when this returns None.
    """
    candidate = Path(path)
    roots = registered_roots()

    if candidate.is_absolute():
        try:
            abs_path = candidate.resolve()
        except (OSError, ValueError):
            return None
        if not abs_path.exists():
            return None
        for root in roots:
            try:
                if abs_path.is_relative_to(root.path):
                    return abs_path
            except (AttributeError, ValueError):
                continue
        # Under no root — but it may be a bound git worktree OF one. `okuro wt
        # add` creates <main>-wt-<topic> BESIDE the base repo, so worktrees fall
        # outside every root and every cortex path tool hard-failed on them.
        #
        # This returns the worktree's OWN file, not the base copy. Every caller
        # of this function reads from disk (mcp_tools' six path tools, plus
        # role-handover ref validation), so the worktree file is the correct
        # answer — substituting base would turn a loud failure into a silent
        # wrong read. The index is a separate question, and mcp_tools attaches
        # a per-file divergence note that says so.
        from .worktrees import worktree_for_path

        if worktree_for_path(abs_path, roots=roots) is not None:
            return abs_path
        return None

    # Relative — probe each root in order.
    for root in roots:
        candidate_abs = (root.path / path).resolve()
        try:
            if not candidate_abs.is_relative_to(root.path):
                # Traversal attempt (e.g. ../../etc/passwd). Skip this root.
                continue
        except (AttributeError, ValueError):
            continue
        if candidate_abs.exists():
            return candidate_abs

    # Fallback: agents often prefix a path with the repo name that IS the root
    # (e.g. "agent-runtime/src" when the root is .../agent-runtime), which the
    # probe above turns into .../agent-runtime/agent-runtime/src and misses.
    # Retry after stripping a leading segment equal to the root's dir name.
    head = path.split("/", 1)[0]
    for root in roots:
        if head != root.path.name:
            continue
        rest = path[len(head) + 1:]
        if not rest:
            continue
        candidate_abs = (root.path / rest).resolve()
        try:
            if not candidate_abs.is_relative_to(root.path):
                continue
        except (AttributeError, ValueError):
            continue
        if candidate_abs.exists():
            return candidate_abs

    return None
