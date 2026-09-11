# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: stage an export — materialize the manifest surface of one committed sha.
# index:
#   imports
#   class ExportError / class ExportResult
#   def _git
#   def resolve_sha
#   def dirty_manifest_paths
#   def tree_paths
#   def stage_export
# AGENT_HEADER_END -->
"""Stage the release surface of ONE committed sha into a directory.

The export reads the git OBJECT database (``git archive``), never the
working tree — but it still refuses a tree that is dirty on the manifest
surface. Not because dirt could leak (it cannot; archive reads objects),
but because an export that silently differs from what the dev machine is
RUNNING is how "works here, broken there" ships: 22 untracked source
files inside the shipped package were finding #1 of the v2 critique.

The staged tree is a plain directory, not a git repo. publish.py turns it
into the release commit; gates.py must pass first.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import MANIFEST_DIRS, README_BANNER, selects


class ExportError(RuntimeError):
    """A precondition failed — nothing was staged."""


@dataclass
class ExportResult:
    source_sha: str
    dest: Path
    files: list[str] = field(default_factory=list)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0:
        raise ExportError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def resolve_sha(repo: Path, ref: str = "HEAD") -> str:
    """The full sha of ``ref``, verified to be a commit in ``repo``."""
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def dirty_manifest_paths(repo: Path) -> list[str]:
    """Working-tree paths on the manifest surface that differ from the index
    or are untracked — each one makes the export lie about what dev runs.

    Porcelain lines are "XY path"; split on the status FIELD, never a fixed
    column (an unstaged edit renders " M path" — slicing mangles it).
    """
    out = _git(repo, "status", "--porcelain")
    dirty: list[str] = []
    for line in out.splitlines():
        parts = line[2:].strip() if len(line) > 3 else ""
        # renames render "R  old -> new"; the new path is what would ship
        path = parts.split(" -> ", 1)[-1].strip('"')
        if not path:
            continue
        # an untracked directory collapses to one porcelain entry — treat
        # the prefix as on-surface if any manifest dir owns it
        candidate = path.rstrip("/")
        if selects(candidate) or any(
            candidate == d or candidate.startswith(d + "/") for d in MANIFEST_DIRS
        ):
            dirty.append(path)
    return dirty


def tree_paths(repo: Path, sha: str) -> list[str]:
    """Every file path tracked at ``sha``."""
    out = _git(repo, "ls-tree", "-r", "--name-only", "-z", sha)
    return [p for p in out.split("\0") if p]


def stage_export(repo: Path, dest: Path, ref: str = "HEAD") -> ExportResult:
    """Materialize the manifest surface of ``ref`` into ``dest``.

    ``dest`` must not exist or be empty. Raises ExportError on a dirty
    manifest surface, a bad ref, or an empty selection.
    """
    repo = Path(repo)
    dest = Path(dest)
    sha = resolve_sha(repo, ref)

    dirty = dirty_manifest_paths(repo)
    if dirty:
        listing = "\n  ".join(dirty[:20])
        more = f"\n  … and {len(dirty) - 20} more" if len(dirty) > 20 else ""
        raise ExportError(
            f"working tree is dirty on the manifest surface — an export from "
            f"{sha[:12]} would not match what this machine runs. Commit or "
            f"stash first:\n  {listing}{more}"
        )

    if dest.exists() and any(dest.iterdir()):
        raise ExportError(f"destination {dest} exists and is not empty")

    selected = sorted(p for p in tree_paths(repo, sha) if selects(p))
    if not selected:
        raise ExportError(f"manifest selects nothing at {sha[:12]}")

    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
        subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", "-o", tmp.name, sha],
            check=True,
        )
        with tarfile.open(tmp.name) as tar:
            members = [m for m in tar.getmembers() if m.isfile() and m.name in set(selected)]
            tar.extractall(dest, members=members, filter="data")

    staged = sorted(
        str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()
    )
    if staged != selected:
        missing = sorted(set(selected) - set(staged))
        raise ExportError(f"archive did not yield the selection; missing: {missing[:10]}")

    readme = dest / "README.md"
    if readme.exists():
        readme.write_text(
            README_BANNER + readme.read_text(encoding="utf-8"), encoding="utf-8"
        )

    return ExportResult(source_sha=sha, dest=dest, files=staged)


def clean_dest(dest: Path) -> None:
    """Remove a staged export directory (and nothing else)."""
    shutil.rmtree(dest, ignore_errors=True)
