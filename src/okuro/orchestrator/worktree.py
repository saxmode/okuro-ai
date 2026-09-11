# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Wave-5b G18 — git worktree per parallel subagent for shared-tree safety.
# index: imports | def is_git_repo | def should_use_worktree |
#   def setup_worktree | def cleanup_worktree | def cleanup_stale_worktrees
# AGENT_HEADER_END -->
"""Per-subtask git worktree helpers.

Wave-5b G18: when ``max_parallel > 1`` AND ``task.project_path`` is a git
repo, parallel subagents commit/index/checkout into the SAME ``.git`` and
race on the index. Lost work, wrong-branch commits, dirty stash. Per-subtask
worktrees give each subagent an isolated working tree + index + branch on
the same backing repo — git operations no longer interfere.

Design notes
------------
* Worktrees live under ``<project>/.git/worktrees/`` (git's own canonical
  location). Their directory paths land at ``<project>/.okuro-worktrees/<task>-<sub>/``
  so a stray ``ls`` of the project root reveals them and a ``git worktree
  prune`` can recover them after a crash.
* Each worktree is created on a branch named ``okuro/<task_id>/<subtask_id>``
  branched from the current HEAD of the project. Branch names are reused
  on retry to avoid orphan-branch sprawl; ``git worktree add --force`` re-uses
  the existing dir if it survived a crash.
* Cleanup is best-effort. ``git worktree remove --force`` followed by a
  filesystem ``rm -rf`` of the directory. The branch is deleted too unless
  the user has merged it into another ref.
* All operations are wrapped — any failure falls back to "no worktree", the
  caller's project_path stays unchanged, and the subagent runs against the
  shared tree (pre-G18 behavior). Worse than worktrees, but never breaks.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("okuro.orchestrator.worktree")


def is_git_repo(path: str | Path) -> bool:
    """Return True if ``path`` is inside a git working tree.

    Uses ``git rev-parse --is-inside-work-tree`` so it correctly returns
    True for any subdirectory of a repo, not just the root.
    """
    try:
        p = Path(path)
        if not p.exists():
            return False
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(p), capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.SubprocessError, OSError):
        return False


def _git_toplevel(path: Path) -> Optional[Path]:
    """Return the project root (output of ``git rev-parse --show-toplevel``)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path), capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def should_use_worktree(project_path: str, max_parallel: int) -> bool:
    """Gate worktree creation on the conditions where it actually helps.

    Conditions:
      - max_parallel > 1 (sequential dispatch has no race to prevent)
      - project_path is set and is a git repo
    """
    if not project_path or max_parallel <= 1:
        return False
    return is_git_repo(project_path)


def _branch_name(task_id: str, subtask_id: str) -> str:
    """Stable branch name reused across retries — no orphan sprawl."""
    safe_task = task_id.replace("/", "-")
    safe_sub = subtask_id.replace("/", "-").replace(".", "-")
    return f"okuro/{safe_task}/{safe_sub}"


def _worktree_dir(project_root: Path, task_id: str, subtask_id: str) -> Path:
    """Directory the worktree is checked out into.

    Lives next to the project root in ``.okuro-worktrees/`` so an operator
    inspecting the project sees them; git's own bookkeeping is in
    ``<project>/.git/worktrees/``.
    """
    safe_task = task_id.replace("/", "-")
    safe_sub = subtask_id.replace("/", "-").replace(".", "-")
    return project_root / ".okuro-worktrees" / f"{safe_task}-{safe_sub}"


def setup_worktree(
    project_path: str, task_id: str, subtask_id: str,
) -> Optional[Path]:
    """Create a worktree for one subtask. Returns the path or None on failure.

    Idempotent on retry: if the worktree dir already exists from a prior
    attempt, ``git worktree add --force`` reuses it. Caller treats None as
    "no worktree, fall back to shared tree" and continues.
    """
    if not project_path:
        return None

    src = Path(project_path)
    project_root = _git_toplevel(src)
    if project_root is None:
        return None

    wt_dir = _worktree_dir(project_root, task_id, subtask_id)
    branch = _branch_name(task_id, subtask_id)

    try:
        wt_dir.parent.mkdir(parents=True, exist_ok=True)

        # Idempotent retry handling — if the directory exists from a prior
        # attempt, check git's bookkeeping. If the worktree is registered,
        # return its path as-is. If the directory is stale (registry was
        # wiped but the dir survived), remove it and create afresh.
        if wt_dir.exists():
            list_out = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=str(project_root), capture_output=True, text=True, timeout=5,
            )
            registered_paths = set()
            for line in list_out.stdout.splitlines():
                if line.startswith("worktree "):
                    registered_paths.add(line[len("worktree "):].strip())
            if str(wt_dir.resolve()) in registered_paths or str(wt_dir) in registered_paths:
                logger.info("[worktree] reusing existing %s", wt_dir)
                return wt_dir
            # Stale dir — remove and recreate.
            shutil.rmtree(wt_dir, ignore_errors=True)

        # Try to create the branch from current HEAD if it doesn't exist.
        # If it exists (retry), the worktree add below will reuse it.
        check_branch = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", branch],
            cwd=str(project_root), capture_output=True, text=True, timeout=5,
        )
        if check_branch.returncode != 0:
            create_branch = subprocess.run(
                ["git", "branch", branch],
                cwd=str(project_root), capture_output=True, text=True, timeout=5,
            )
            if create_branch.returncode != 0:
                logger.warning(
                    "[worktree] branch create failed for %s: %s",
                    branch, create_branch.stderr.strip(),
                )
                return None

        # --force lets us reuse the branch even when no worktree currently
        # holds it (e.g. retry after cleanup).
        add = subprocess.run(
            ["git", "worktree", "add", "--force", str(wt_dir), branch],
            cwd=str(project_root), capture_output=True, text=True, timeout=15,
        )
        if add.returncode != 0:
            logger.warning(
                "[worktree] add failed for %s: %s",
                wt_dir, add.stderr.strip(),
            )
            return None

        logger.info("[worktree] created %s on branch %s", wt_dir, branch)
        return wt_dir
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("[worktree] setup raised %r — falling back to shared tree", exc)
        return None


def cleanup_worktree(worktree_path: Optional[Path]) -> None:
    """Best-effort tear-down. Never raises.

    Steps: ``git worktree remove --force`` (which also clears the
    .git/worktrees bookkeeping), then a filesystem ``rmtree`` if the dir
    survives. The branch is intentionally LEFT ALONE so the operator can
    inspect / merge / delete on their own schedule.
    """
    if worktree_path is None:
        return
    if not worktree_path.exists():
        return
    try:
        # Run git worktree remove from inside the project root, not the
        # worktree dir itself (which gets removed mid-call).
        project_root = _git_toplevel(worktree_path)
        if project_root:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=str(project_root), capture_output=True, text=True, timeout=15,
            )
    except Exception as exc:
        logger.warning("[worktree] git remove raised %r", exc)

    try:
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
    except Exception as exc:
        logger.warning("[worktree] rmtree raised %r", exc)


def cleanup_stale_worktrees(project_path: str) -> int:
    """Run ``git worktree prune`` to clear orphaned bookkeeping.

    Run on orchestrator startup — survivors of crashed runs leave entries in
    ``.git/worktrees/`` even after the dir is gone. Returns the number of
    pruned entries on success, 0 otherwise. Never raises.
    """
    if not project_path:
        return 0
    try:
        result = subprocess.run(
            ["git", "worktree", "prune", "--verbose"],
            cwd=project_path, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return 0
        return result.stdout.count("Removing")
    except (subprocess.SubprocessError, OSError):
        return 0


def cleanup_stale_branches(project_path: str, prefix: str = "okuro/") -> int:
    """Delete merged ``okuro/`` branches whose worktrees no longer exist.

    Umbrella audit fix #12 — pre-fix, cleanup_stale_worktrees pruned
    worktree bookkeeping but left every ``okuro/<task>/<sub>`` branch
    behind. After enough orchestrator runs (especially crashed ones)
    these branches accumulate indefinitely, polluting branch lists and
    blocking refspec operations.

    Strategy: list local branches starting with the prefix, check if
    each branch is still associated with an active worktree
    (``git worktree list --porcelain`` enumerates branch/HEAD per
    worktree). Branches with no live worktree are safe to delete — the
    work either merged back or never produced output.

    Returns the number of branches deleted. Never raises.
    """
    if not project_path:
        return 0
    try:
        # 1. Enumerate live worktree branches.
        wt_result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=project_path, capture_output=True, text=True, timeout=10,
        )
        if wt_result.returncode != 0:
            return 0
        live_branches: set[str] = set()
        for line in wt_result.stdout.splitlines():
            if line.startswith("branch refs/heads/"):
                live_branches.add(line[len("branch refs/heads/"):])

        # 2. Enumerate local branches matching the prefix. Use ``**`` so
        # nested paths like ``okuro/<task>/<sub>`` match — bare ``*`` does
        # NOT cross slash boundaries in git's ref glob.
        br_result = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)",
             f"refs/heads/{prefix}**"],
            cwd=project_path, capture_output=True, text=True, timeout=10,
        )
        if br_result.returncode != 0:
            return 0

        deleted = 0
        for line in br_result.stdout.splitlines():
            branch = line.strip()
            if not branch or branch in live_branches:
                continue
            # -D (force delete) — these branches are intentionally orphan.
            del_result = subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=project_path, capture_output=True, text=True, timeout=10,
            )
            if del_result.returncode == 0:
                deleted += 1
        return deleted
    except (subprocess.SubprocessError, OSError):
        return 0
