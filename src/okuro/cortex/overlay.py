# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Diff-only cortex overlays for worktree branches — index what differs from base, and only that.
# index: imports | def _state_path | def _load_state | def _save_state | def touch | def last_touched | def forget | def is_worthy | def refresh | def evict | def known_slugs | def ensure | def reap
# AGENT_HEADER_END -->
"""Diff-only cortex overlays.

A worktree's files are byte-identical to base except for the ones it changed.
For an unchanged file the base document is not stale, it is *correct* — so an
overlay that indexes only the diff is not an approximation of a full index, it
is exactly equivalent for the question being asked, at a fraction of the cost.
Measured across okuro's 43-worktree fleet: 158 changed files (~1,609 docs,
+1.1% of the index) versus 1,013,639 docs for a full root per worktree.

Overlays index under slug ``<base>@<branch>``, reusing ``cortex_docs.project``
and the ``vec_cortex`` partition key — no schema change, the same shape as a
managed clone's ``workspace__repo``.

**Overlays are never auto-created.** ``ensure`` is called when something names
the slug, which is the "touched" gate: 43 worktrees exist, but only the one or
two an agent is actually working in ever cost anything. The registered-roots
table is untouched — ``register_root`` sets ``active=1`` and would inject a
phantom project into every bootstrap, which is the trap that produced 141,738
stale duplicate documents in May 2026.

State (last-touch times) is a JSON file, not a migration — the same call made
for design-system assignments. An overlay is a cache; losing the file costs a
re-index, not data.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from .worktrees import (
    WorktreeContext,
    divergent_files,
    is_merged,
    overlay_slug,
    parse_overlay_slug,
    worktree_for_slug,
)
from okuro.db.engine import okuro_home

# An overlay untouched for this long is garbage someone forgot about.
DEFAULT_TTL_DAYS = 14


def _state_path() -> Path:
    return Path(
        os.environ.get("OKURO_CORTEX_OVERLAY_STATE")
        or okuro_home() / "cortex" / "overlays.json"
    )


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True))
    except OSError:
        pass  # a lost touch costs a re-index, never correctness


def touch(slug: str, *, now: Optional[float] = None) -> None:
    """Record that something asked for this overlay."""
    state = _load_state()
    state[slug] = now if now is not None else time.time()
    _save_state(state)


def last_touched(slug: str) -> Optional[float]:
    value = _load_state().get(slug)
    return value if isinstance(value, (int, float)) else None


def forget(slug: str) -> None:
    state = _load_state()
    if state.pop(slug, None) is not None:
        _save_state(state)


def is_worthy(ctx: WorktreeContext) -> bool:
    """Is an overlay for this worktree worth its cost?

    It reduces to one question — does anything differ from base? — and that
    single git read subsumes the whole gate the strategy specified as four
    terms. A merged branch's three-dot diff against base is empty by
    definition (its merge-base IS its head), so "merged" needs no separate
    check; and a merged branch with UNCOMMITTED edits still reports those,
    which is why it survives. The strategy's flat "merged -> purge" rule would
    have deleted files that exist nowhere else.

    Deliberately loose otherwise. The cost of indexing a worktree that did not
    need it is a few hundred KB and an eviction obligation; the cost of NOT
    indexing one that did is an agent reading base content for a file it just
    changed, concluding wrongly, and committing — unrecoverable, and invisible.
    That asymmetry only stays affordable because the overlay is diff-sized.
    """
    members = divergent_files(ctx)
    if members is None:
        return False  # git could not answer; do not guess
    return bool(members)


def refresh(ctx: WorktreeContext, *, force: bool = False, store=None) -> dict:
    """Bring the overlay in line with what currently differs from base.

    Indexes every current member and drops documents for files that are no
    longer members — a file reverted to base content must lose its overlay
    doc, or the shadow rule would keep serving a copy that no longer exists as
    a difference.
    """
    from .vectorstore import VectorStore, is_indexable_file

    slug = overlay_slug(ctx)
    members = divergent_files(ctx)
    if members is None:
        return {"slug": slug, "error": "git-unavailable"}

    vs = store or VectorStore()
    wanted = {p for p in members if is_indexable_file(p)}

    indexed = 0
    for path in sorted(wanted):
        try:
            if vs.index_file(path, force=force, root=ctx.tree, project=slug):
                indexed += 1
        except (OSError, UnicodeDecodeError):
            continue

    known = {
        Path(r["file_path"])
        for r in vs._db.fetchall(
            "SELECT DISTINCT file_path FROM cortex_docs WHERE project = ?",
            (slug,),
        )
    }
    removed = 0
    for path in known - wanted:
        vs._remove_file(path)
        removed += 1

    touch(slug)
    return {
        "slug": slug,
        "tree": str(ctx.tree),
        "members": len(wanted),
        "indexed": indexed,
        "removed": removed,
    }


def evict(slug: str, *, store=None) -> int:
    """Delete every document belonging to an overlay. Returns files removed.

    Worktree paths belong to exactly one overlay, so removing by file_path
    cannot touch a base document.
    """
    from .vectorstore import VectorStore

    vs = store or VectorStore()
    paths = [
        Path(r["file_path"])
        for r in vs._db.fetchall(
            "SELECT DISTINCT file_path FROM cortex_docs WHERE project = ?",
            (slug,),
        )
    ]
    for path in paths:
        vs._remove_file(path)
    forget(slug)
    return len(paths)


def known_slugs(*, store=None) -> list[str]:
    """Every overlay slug with documents in the index."""
    from .vectorstore import VectorStore

    vs = store or VectorStore()
    rows = vs._db.fetchall(
        "SELECT DISTINCT project FROM cortex_docs "
        "WHERE project LIKE '%@%' AND project IS NOT NULL"
    )
    return sorted(
        r["project"] for r in rows if parse_overlay_slug(r["project"])
    )


def ensure(ctx: WorktreeContext, *, store=None) -> Optional[str]:
    """Create-or-refresh the overlay for `ctx`; return its slug, or None.

    This IS the laziness gate — nothing calls it on `okuro wt add`, only a
    caller that names the overlay.

    Worthiness is read off the refresh itself rather than checked first: both
    answers come from the same git read, and doing it twice would double the
    cost of every search that touches a worktree.
    """
    result = refresh(ctx, store=store)
    if result.get("error") or not result["members"]:
        return None
    return result["slug"]


def reap(*, ttl_days: int = DEFAULT_TTL_DAYS, store=None, now=None) -> dict:
    """Evict overlays that no longer earn their space.

    Three triggers, each independently sufficient:

    1. **Worktree gone** — read from `git worktree list`, never from directory
       existence: a manual `rm -rf` leaves a stale admin entry until
       `git worktree prune`, `git worktree remove` leaves nothing, and only
       git's own list covers both.
    2. **Merged with nothing uncommitted** — base holds the content now.
    3. **TTL** — nothing has asked for it in `ttl_days`.

    `cortex_prune_stale` cannot do this job: it tombstones only files that
    vanished from disk, and a merged-but-still-present worktree never
    qualifies. That gap is exactly how 141,738 stale duplicates survived
    indefinitely.
    """
    now = time.time() if now is None else now
    cutoff = now - ttl_days * 86400
    evicted: list[dict] = []
    kept: list[str] = []

    for slug in known_slugs(store=store):
        ctx = worktree_for_slug(slug)
        if ctx is None:
            evicted.append({"slug": slug, "reason": "worktree-gone",
                            "files": evict(slug, store=store)})
            continue
        if not is_worthy(ctx):
            reason = ("merged-and-clean" if is_merged(ctx) is True
                      else "no-divergence")
            evicted.append({"slug": slug, "reason": reason,
                            "files": evict(slug, store=store)})
            continue
        seen = last_touched(slug)
        if seen is not None and seen < cutoff:
            evicted.append({"slug": slug, "reason": f"idle>{ttl_days}d",
                            "files": evict(slug, store=store)})
            continue
        kept.append(slug)

    # The work-list above comes from the DOC TABLE, so any per-slug state that
    # lives outside it is never reached — an overlay whose documents are all
    # gone stops being enumerated, and its touch entry outlives it forever.
    # Observed live: okuro@feat/cortex-overlay merged, refresh removed all five
    # of its documents, and the entry stayed. One entry per branch ever
    # overlaid, accreting with nothing to collect it.
    #
    # Swept here rather than at each eviction site because the leak is a
    # property of the work-list, not of any one caller: anything else keyed by
    # slug and stored outside cortex_docs would leak identically. A touch time
    # for a slug with no documents carries no information — TTL only decides
    # the fate of documents.
    live = set(known_slugs(store=store))
    orphans = [s for s in _load_state() if s not in live]
    for slug in orphans:
        forget(slug)

    return {"evicted": evicted, "kept": kept, "state_pruned": orphans}
