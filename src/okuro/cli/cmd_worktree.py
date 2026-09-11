# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro wt — create and inspect branch-bound git worktrees.
# index:
#   imports
#   def _git
#   def _repo_root
#   def _main_root
#   def _worktrees
#   def wt
#   def wt_add
#   def wt_list
# AGENT_HEADER_END -->
"""okuro wt — create and inspect branch-bound git worktrees.

Concurrent agent sessions that share one working tree race on the git
index: they flip each other's branch mid-session, and `git add` on a
file with mixed edits sweeps another agent's uncommitted work into the
wrong commit (ORCH-PARALLEL-TREE).

The remedy is one worktree per topic. ``okuro wt add`` makes that the
cheap path and — critically — writes the ``.okuro-worktree`` binding
file that the pre-commit BIND guard reads. Without a binding, BIND has
nothing to compare HEAD against and silently passes.

Retiring merged worktrees is deliberately not implemented here yet.
"""

from pathlib import Path
import subprocess

import click

from .output import console, ok, warn, fail, heading, info, data_table


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return stripped stdout."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo_root(start: Path) -> Path:
    """Toplevel of the worktree containing ``start``."""
    return Path(_git(start, "rev-parse", "--show-toplevel"))


def _main_root(repo: Path) -> Path:
    """Toplevel of the MAIN worktree, regardless of which tree we are in.

    The common dir lives at ``<main-root>/.git`` (or is ``<main-root>/.git``
    itself for a bare-ish layout), so its parent is the main root.
    """
    common = Path(
        _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    )
    return common.parent.resolve()


def _worktrees(repo: Path) -> list[tuple[Path, str]]:
    """``(root, branch)`` for every worktree; detached HEADs are skipped."""
    out = _git(repo, "worktree", "list", "--porcelain")
    results: list[tuple[Path, str]] = []
    current: Path | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):])
        elif line.startswith("branch ") and current is not None:
            results.append((current, line[len("branch "):].removeprefix("refs/heads/")))
            current = None
    return results


@click.group("wt")
def wt():
    """Create and inspect branch-bound git worktrees."""


@wt.command("add")
@click.argument("topic")
@click.option(
    "--branch", "branch_arg", default=None,
    help="Branch name. Defaults to feat/<topic>.",
)
@click.option(
    "--from", "base", default="main",
    help="Base commit-ish for a new branch. Default: main.",
)
@click.option(
    "--repo", "repo_arg", default=None,
    help="Repo path. Defaults to the current directory.",
)
def wt_add(topic, branch_arg, base, repo_arg):
    """Create a worktree for TOPIC, bound to its own branch.

    Creates ``<repo>-wt-<topic>`` beside the repo, checks out (or
    creates) the branch, and writes the ``.okuro-worktree`` binding the
    pre-commit BIND guard enforces.

    Reuses the tree if it already exists and is already correctly bound.
    """
    start = Path(repo_arg or ".").resolve()
    try:
        repo = _repo_root(start)
        main_root = _main_root(repo)
    except subprocess.CalledProcessError:
        fail(f"Not a git repo: {start}")
        raise SystemExit(1)

    branch = branch_arg or f"feat/{topic}"
    # Inside the repo, under a gitignored `worktrees/`, not as a sibling.
    #
    # Siblings put one directory in $HOME per topic — 50 of them at the peak,
    # and ten of those outlived their registration as orphans that `git status`
    # could not even report on (their `.git` file points at a pruned admin dir,
    # so the command fails to stderr and returns empty stdout, which a census
    # reads as "clean").
    #
    # Nesting is safe because cortex applies gitignore semantics: measured
    # 2026-09-09, the five nested `.claude/worktrees/` trees and the sibling
    # gridlab tree each contributed 0 of okuro's 47,080 indexed documents. The
    # indexer was the only real argument for keeping them outside.
    dest = main_root / "worktrees" / topic

    existing = {r.resolve(): b for r, b in _worktrees(repo)}
    if dest.resolve() in existing:
        bound = existing[dest.resolve()]
        if bound != branch:
            fail(f"{dest} already exists and is on '{bound}', not '{branch}'.")
            info("Pick another topic, or work in that tree on its own branch.")
            raise SystemExit(1)
        info(f"Worktree already exists: {dest}")
    else:
        if dest.exists():
            fail(f"Path already exists and is not a worktree: {dest}")
            raise SystemExit(1)
        branch_exists = (
            subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                 f"refs/heads/{branch}"],
                capture_output=True, text=True,
            ).returncode == 0
        )
        cmd = ["worktree", "add"]
        cmd += [str(dest), branch] if branch_exists else [str(dest), "-b", branch, base]
        try:
            _git(repo, *cmd)
        except subprocess.CalledProcessError as exc:
            fail(f"git worktree add failed: {exc.stderr.strip() or exc}")
            raise SystemExit(1)
        ok(f"Worktree created: {dest}")
        info(f"Branch: {branch}" + ("" if branch_exists else f" (new, from {base})"))

    marker = dest / ".okuro-worktree"
    marker.write_text(
        "# okuro worktree binding — written by `okuro wt add`.\n"
        "# The pre-commit BIND guard refuses commits when HEAD != branch.\n"
        f"repo={main_root.name}\n"
        f"branch={branch}\n"
    )
    ok(f"Bound to '{branch}' via {marker}")
    console.print()
    info(f"Work here:  cd {dest}")


@wt.command("list")
@click.option(
    "--repo", "repo_arg", default=None,
    help="Repo path. Defaults to the current directory.",
)
def wt_list(repo_arg):
    """List worktrees with their binding, merge state and dirt."""
    start = Path(repo_arg or ".").resolve()
    try:
        repo = _repo_root(start)
        main_root = _main_root(repo)
    except subprocess.CalledProcessError:
        fail(f"Not a git repo: {start}")
        raise SystemExit(1)

    heading(f"Worktrees of {main_root.name}")

    rows: list[list[str]] = []
    unbound = 0
    for root, branch in _worktrees(repo):
        is_main = root.resolve() == main_root.resolve()

        if is_main:
            bound = "(main tree)"
        else:
            marker = root / ".okuro-worktree"
            if marker.exists():
                found = [
                    ln[len("branch="):].strip()
                    for ln in marker.read_text(errors="replace").splitlines()
                    if ln.startswith("branch=")
                ]
                bound = found[0] if found else "—"
            else:
                bound = "UNBOUND"
                unbound += 1

        drift = "" if bound in ("(main tree)", "UNBOUND", branch) else "  ⚠ DRIFT"

        merged = (
            subprocess.run(
                ["git", "-C", str(repo), "merge-base", "--is-ancestor", branch, "main"],
                capture_output=True,
            ).returncode == 0
        )
        ahead = _git(repo, "rev-list", "--count", f"main..{branch}") if not is_main else "0"
        dirty = len(
            subprocess.run(
                ["git", "-C", str(root), "status", "--short"],
                capture_output=True, text=True,
            ).stdout.splitlines()
        )

        rows.append([
            root.name,
            branch + drift,
            bound,
            "merged" if merged else f"+{ahead}",
            str(dirty) if dirty else "",
        ])

    console.print(data_table(
        ["worktree", "HEAD", "bound to", "vs main", "dirty"], rows
    ))

    if unbound:
        warn(f"{unbound} worktree(s) UNBOUND — the BIND guard cannot protect them.")
        info("Fix with: okuro setup-git-guards   (backfills bindings)")
