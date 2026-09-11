# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Git worktrees as cortex territory — resolve paths inside them and disclose per-file divergence from base.
# index: imports | class WorktreeContext | def _parse_marker | def _find_marker | def worktree_for_path | def overlay_slug | def parse_overlay_slug | def worktrees_of | def worktree_for_slug | def base_ref | def is_merged | def divergent_files | def base_counterpart | def divergence | def note_for_path
# AGENT_HEADER_END -->
"""Git worktrees as cortex territory.

`okuro wt add <topic>` creates a worktree at `<main>/worktrees/<topic>` and
drops a `.okuro-worktree` marker naming the base project slug and the bound
branch. Resolution here is driven ENTIRELY by that marker — `_find_marker`
walks up from the path until it finds one — so the layout is free to change
without touching this module. It did change on 2026-09-10: trees used to sit
BESIDE the base repo as `<main>-wt-<topic>`, which put one directory in $HOME
per topic and left orphans behind that `git status` could not even report on.

The original problem this module solves is unchanged and still applies to the
sibling trees that predate the move: a tree outside every registered root made
`resolve_under_roots` return None for every file in it, so each cortex path
tool answered "did not resolve under any registered root" and an agent working
in a worktree could not read its own files through cortex at all.

Two facts decide the shape of the fix:

- **Every consumer of `resolve_under_roots` reads the file from DISK**, never
  from the index — `cortex_read_header`, `cortex_read_section`,
  `cortex_read_file`, `cortex_navigate`, `cortex_search_code` (ripgrep) and
  `cortex_context`, plus role-handover ref validation. So returning the
  worktree's own file is not a workaround; it is the correct answer, and it is
  strictly more correct than substituting the base copy would be. Substituting
  base would convert today's loud failure into a silent wrong read.
- **The index is a separate question.** Worktrees are not indexed, and this
  module does not index them. Pathless index-backed tools (`cortex_search`,
  `cortex_route`) still answer from the base checkout only. That limitation is
  real, so it is stated in the note this module produces rather than left for
  the agent to discover by being wrong.

Divergence is decided by comparing bytes against the base copy, not by asking
git. Byte comparison catches uncommitted edits — which is most of what an agent
in a worktree has — needs no subprocess, and answers the question actually
being asked: is what I just read the same as what the index holds?
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

MARKER = ".okuro-worktree"

# Overlay slug separator. `okuro@feat/x` reuses the existing
# cortex_docs.project column and the vec_cortex partition key — no schema
# change, same shape as managed clones' `workspace__repo` slugs. A base slug
# can never contain "@", so the split is unambiguous.
OVERLAY_SEP = "@"

# Git is fast here (tens of ms) but a hung git must not hang a search.
_GIT_TIMEOUT_S = 15

# Divergence verdicts.
SAME = "same"
DIFFERS = "differs"
ONLY_IN_WORKTREE = "only-in-worktree"
DIRECTORY = "directory"
MISSING = "missing"
UNKNOWN = "unknown"

# Walking up from a deep file is a handful of stats; a symlink loop is not.
# `okuro wt add` puts the marker at the tree root, so it is always within a
# few levels of any file inside the tree.
_MAX_WALK_UP = 64


@dataclass(frozen=True)
class WorktreeContext:
    """A bound worktree and the registered root it is a worktree OF."""

    tree: Path           # worktree root (the directory holding the marker)
    base: Path           # base repo root — a registered cortex root
    base_project: str    # project slug from the marker's `repo=` line
    branch: str          # bound branch from the marker's `branch=` line


def _parse_marker(marker: Path) -> tuple[Optional[str], Optional[str]]:
    """Return (repo_slug, branch) from a `.okuro-worktree` file.

    The marker is `key=value` lines with `#` comments — the same format
    `okuro wt add` writes and the pre-commit BIND guard reads.
    """
    repo: Optional[str] = None
    branch: Optional[str] = None
    try:
        text = marker.read_text(errors="replace")
    except OSError:
        return None, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key == "repo":
            repo = value or None
        elif key == "branch":
            branch = value or None
    return repo, branch


def _find_marker(start: Path) -> Optional[Path]:
    """Walk up from `start` to the first directory holding a marker file."""
    try:
        current = start if start.is_dir() else start.parent
    except OSError:
        return None
    for _ in range(_MAX_WALK_UP):
        candidate = current / MARKER
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            return None
        if current.parent == current:
            break
        current = current.parent
    return None


def worktree_for_path(
    path,
    roots: Optional[Sequence] = None,
) -> Optional[WorktreeContext]:
    """Return the bound worktree containing `path`, or None.

    None means "not cortex territory via a worktree" and covers four cases,
    all deliberate:

    - no `.okuro-worktree` marker above the path — an unbound tree is
      unmanaged, and guessing at unmanaged directories is how an MCP server's
      client CWD once became search root #1;
    - a malformed marker missing `repo=` or `branch=`;
    - a marker naming a project that is not a registered root — cortex has no
      business in a repo nobody asked it to index;
    - a marker sitting in the base repo itself, which would make a root its own
      worktree.

    `roots` is accepted so the caller that already loaded `registered_roots()`
    does not pay for a second database read. Importing roots lazily also keeps
    this module free of an import cycle — `roots` imports us, not the reverse.
    """
    try:
        target = Path(path).resolve()
    except (OSError, ValueError):
        return None

    marker = _find_marker(target)
    if marker is None:
        return None

    repo, branch = _parse_marker(marker)
    if not repo or not branch:
        return None

    if roots is None:
        from .roots import registered_roots

        roots = registered_roots()

    tree = marker.parent
    base = next((r.path for r in roots if r.project == repo), None)
    if base is None or base == tree:
        return None

    return WorktreeContext(
        tree=tree, base=base, base_project=repo, branch=branch
    )


def overlay_slug(ctx: WorktreeContext) -> str:
    """The cortex project slug an overlay for this worktree indexes under."""
    return f"{ctx.base_project}{OVERLAY_SEP}{ctx.branch}"


def parse_overlay_slug(slug: str) -> Optional[tuple[str, str]]:
    """Split `okuro@feat/x` into ("okuro", "feat/x"), or None if not one."""
    if not slug or OVERLAY_SEP not in slug:
        return None
    base, _, branch = slug.partition(OVERLAY_SEP)
    if not base or not branch:
        return None
    return base, branch


def worktrees_of(base: Path) -> Optional[dict[str, Path]]:
    """branch -> worktree path, straight from `git worktree list --porcelain`.

    Git's own list is authoritative for BOTH removal styles, which is why
    eviction keys off this and never off directory existence: a manual
    `rm -rf` leaves a stale admin entry until `git worktree prune`, while
    `git worktree remove` leaves nothing. Only git knows both.
    """
    out = _git(base, "worktree", "list", "--porcelain")
    if out is None:
        return None
    trees: dict[str, Path] = {}
    current: Optional[Path] = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):].strip())
        elif line.startswith("branch ") and current is not None:
            ref = line[len("branch "):].strip()
            trees[ref.removeprefix("refs/heads/")] = current
    return trees


def worktree_for_slug(
    slug: str,
    roots: Optional[Sequence] = None,
) -> Optional[WorktreeContext]:
    """Rebuild the context an overlay slug came from, or None if it is gone.

    None is the eviction signal: the branch has no worktree any more, or the
    base project is no longer a registered root.
    """
    parsed = parse_overlay_slug(slug)
    if parsed is None:
        return None
    base_project, branch = parsed

    if roots is None:
        from .roots import registered_roots

        roots = registered_roots()
    base = next((r.path for r in roots if r.project == base_project), None)
    if base is None:
        return None

    trees = worktrees_of(base)
    if not trees or branch not in trees:
        return None

    return WorktreeContext(
        tree=trees[branch], base=base, base_project=base_project, branch=branch
    )


def _git(cwd: Path, *args: str) -> Optional[str]:
    """Run git in `cwd`, returning stdout, or None on any failure.

    None means "could not tell" and every caller treats it as such. Guessing
    from a failed git call is how an overlay would index the wrong file set.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def base_ref(ctx: WorktreeContext) -> Optional[str]:
    """The branch the base checkout is on — what the worktree diverged FROM.

    Read from the BASE tree rather than assumed to be "main": a repo whose
    default branch is named otherwise would otherwise get an empty diff and a
    silently empty overlay.
    """
    out = _git(ctx.base, "rev-parse", "--abbrev-ref", "HEAD")
    ref = (out or "").strip()
    return ref or None


def is_merged(ctx: WorktreeContext) -> Optional[bool]:
    """True if this branch is already an ancestor of the base branch.

    None when git could not answer. A merged branch's content is in base, so
    an overlay for it is pure waste — this is eviction trigger #2.
    """
    ref = base_ref(ctx)
    if ref is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(ctx.tree), "merge-base", "--is-ancestor",
             ctx.branch, ref],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):
        return None
    return proc.returncode == 0


def _nul_paths(out: Optional[str]) -> list[str]:
    return [p for p in (out or "").split("\0") if p]


def divergent_files(ctx: WorktreeContext) -> Optional[list[Path]]:
    """Every file that differs from base — the overlay's membership.

    Three git reads, unioned:
      * `diff --name-only <base>...HEAD` — committed divergence
      * `diff --name-only HEAD`          — tracked, uncommitted
      * `ls-files --others --exclude-standard` — untracked, not ignored

    `-z` on all three so a path with a space or a non-ASCII byte survives; the
    rename arrows and shell quoting of `status --porcelain` are avoided
    entirely by not using it.

    Deleted files appear in the diff but not on disk, so the existence filter
    at the end drops them — the caller wants files it can index.

    Returns None if git could not answer, which is NOT the same as "nothing
    diverged": an empty list means the branch is clean, None means unknown.
    """
    ref = base_ref(ctx)
    if ref is None:
        return None

    committed = _git(ctx.tree, "diff", "--name-only", "-z", f"{ref}...HEAD")
    working = _git(ctx.tree, "diff", "--name-only", "-z", "HEAD")
    untracked = _git(
        ctx.tree, "ls-files", "--others", "--exclude-standard", "-z"
    )
    if committed is None or working is None or untracked is None:
        return None

    rels = set()
    for out in (committed, working, untracked):
        rels.update(_nul_paths(out))

    files = []
    for rel in sorted(rels):
        if rel == MARKER:
            continue
        p = ctx.tree / rel
        try:
            if p.is_file():
                files.append(p)
        except OSError:
            continue
    return files


def base_counterpart(ctx: WorktreeContext, path) -> Optional[Path]:
    """The path this file would have in the base checkout, or None."""
    try:
        rel = Path(path).resolve().relative_to(ctx.tree)
    except (OSError, ValueError):
        return None
    return ctx.base / rel


def divergence(ctx: WorktreeContext, path) -> str:
    """Compare one file against its base counterpart.

    Returns SAME / DIFFERS / ONLY_IN_WORKTREE / DIRECTORY / MISSING / UNKNOWN.
    A size mismatch short-circuits before any read, so the common "obviously
    edited" case never loads two files.

    MISSING comes first and matters: a path that exists in NEITHER tree was
    once reported as ONLY_IN_WORKTREE, which reads as "your branch added this
    file". Caught against the live fleet on `okuro/db.py`, which is a package
    directory, not a module — the note asserted a new branch file for a path
    that has never existed. A disclosure that invents facts is worse than none.
    """
    try:
        target = Path(path).resolve()
    except (OSError, ValueError):
        return UNKNOWN

    base_file = base_counterpart(ctx, target)
    if base_file is None:
        return UNKNOWN

    try:
        if not target.exists():
            return MISSING
        if target.is_dir():
            return DIRECTORY
        if not base_file.exists():
            return ONLY_IN_WORKTREE
        if base_file.stat().st_size != target.stat().st_size:
            return DIFFERS
        return SAME if base_file.read_bytes() == target.read_bytes() else DIFFERS
    except OSError:
        return UNKNOWN


def _verdict_line(verdict: str, base_file: Optional[Path]) -> str:
    where = str(base_file) if base_file else "the base checkout"
    if verdict == SAME:
        return f"  This file is byte-identical to the base copy at {where}."
    if verdict == DIFFERS:
        return (
            f"  This file DIFFERS from the base copy at {where} — "
            "the index still holds the base version."
        )
    if verdict == ONLY_IN_WORKTREE:
        return (
            f"  This file does NOT exist in the base checkout ({where}) — "
            "the index has no copy of it at all."
        )
    if verdict == DIRECTORY:
        return "  This is a directory; its listing came from the worktree."
    if verdict == MISSING:
        return (
            "  This path exists in NEITHER the worktree nor the base "
            "checkout — nothing was compared."
        )
    return f"  Could not compare against the base copy at {where}."


def note_for_path(path, roots: Optional[Sequence] = None) -> Optional[str]:
    """The disclosure block for a worktree path, or None if not in one.

    Loud by construction. A quiet note is the same failure as no note: the
    hazard being fixed is an agent that cannot tell which checkout answered.
    """
    if not path:
        return None

    ctx = worktree_for_path(path, roots=roots)
    if ctx is None:
        return None

    verdict = divergence(ctx, path)
    return "\n".join([
        f"WORKTREE — this path is in {ctx.tree}, a git worktree of project "
        f"'{ctx.base_project}' on branch '{ctx.branch}'.",
        "  cortex does NOT index worktrees. What you just read came from the "
        "worktree on DISK, so it is current.",
        _verdict_line(verdict, base_counterpart(ctx, path)),
        "  A PATHLESS cortex_search or cortex_route answers from the base "
        f"checkout at {ctx.base} and will NOT see this branch's changes. To "
        f"include them, pass project='{overlay_slug(ctx)}' — that builds or "
        "refreshes a diff-only overlay of just the files this branch changed, "
        "and shadows the base copy of each.",
    ])
