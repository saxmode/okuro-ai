# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Shared scope-guardrail hints for cortex search surfaces (MCP tools + HTTP API).
# index: imports | def slug_stem | def sibling_roots | def scope_annotation
# AGENT_HEADER_END -->
"""Scope guardrail hints — single source of truth for both cortex search surfaces.

Consumed by:
- ``cortex/mcp_tools.py``       (the MCP tool layer agents call)
- ``orchestrator/api/cortex.py`` (the HTTP/web search surface)

Purpose: stop a scoped or empty search result from being misread as "the code
does not exist". This module has no knowledge of *how* a search ran — it only
annotates the result with (a) a naming-collision warning when a scoped slug has
sibling roots (e.g. a stale local mirror vs the managed ``default__*`` clone
that holds the real code), and (b) guidance that an empty hit set is not proof
of absence. Keeping it in one place means the two surfaces can never drift.
"""

from __future__ import annotations

from typing import Optional


def slug_stem(slug: Optional[str]) -> str:
    """Strip the ``<workspace>__`` prefix from a project slug.

    Managed-repo slugs are ``default__meridian``, ``default__meridian-api``, …;
    a local mirror registered directly is bare ``meridian``. Comparing stems
    surfaces the mirror-vs-managed naming collision.
    """
    return slug.split("__", 1)[-1] if slug else (slug or "")


def sibling_roots(project: Optional[str]) -> list[dict]:
    """Registered roots that are naming siblings of ``project``.

    A sibling shares the stem (after stripping the workspace prefix): the same
    stem, or a ``stem-<suffix>`` extension in either direction. Catches the
    failure this guardrail exists for: scoping to a bare ``meridian`` local
    mirror while the source-of-truth code lives in ``default__meridian``,
    ``default__meridian-api``, … Returns [] when no project is given.
    """
    from .roots import registered_roots

    if not project:
        return []
    target = slug_stem(project)
    if not target:
        return []
    out: list[dict] = []
    for r in registered_roots():
        if not r.project or r.project == project:
            continue
        stem = slug_stem(r.project)
        if stem == target or stem.startswith(target + "-") or target.startswith(stem + "-"):
            out.append({"project": r.project, "path": str(r.path)})
    return out


def scope_annotation(project: Optional[str], result_count: int) -> Optional[dict]:
    """Guardrail metadata to attach to a search/route result.

    Two failure modes this closes at the surface layer (no agent discipline
    required):
    - collision: ``project=X`` was scoped, but sibling roots (e.g. a mirror vs
      ``default__*`` clones) exist and hold the real code.
    - empty read as absence: an empty hit set is NOT proof code is missing — it
      usually means wrong scope or wrong tool.

    Returns None on the happy path (results present, no siblings) so that output
    stays clean.
    """
    from .roots import registered_roots

    ann: dict = {}
    siblings = sibling_roots(project)
    if siblings:
        ann["sibling_roots"] = siblings
        ann["collision_warning"] = (
            f"project='{project}' has {len(siblings)} naming-sibling root(s) "
            "(commonly a local mirror vs managed 'default__*' clones). These "
            f"results are scoped to '{project}' ONLY — the source-of-truth code "
            "may live in a sibling. Re-scope to the intended slug, or drop "
            "project= to search every root."
        )
    if result_count == 0:
        total_roots = sum(1 for r in registered_roots() if r.project)
        ann["no_results"] = (
            "Empty result is NOT proof of absence. Before concluding the code "
            "does not exist: (1) for an exact symbol/string use cortex_search_code "
            "— literal ripgrep, needs no scope, searches ALL roots; (2) drop or "
            "change project=; (3) call cortex_scope to list all "
            f"{total_roots} indexed roots; (4) for callers / imports / "
            "blast-radius use codegraph_insights / cross_repo_search."
        )
    return ann or None
