# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Security helpers — git-tree leak detection for ~/.okuro/.
# index: imports | def detect_okuro_home_in_git_tree | def format_git_tree_warning
# AGENT_HEADER_END -->
"""Security helpers — git-tree leak detection for ~/.okuro/.

Okuro stores a SQLite DB and assorted state under ``~/.okuro/``. If that
directory lives inside a git working tree (e.g. someone keeps their
``$HOME`` in a dotfiles repo, or invokes okuro from inside a project
checked out at ``~/project/`` with ``OKURO_HOME`` redirected, or just
created a repo near it by accident), `git add .` from the repo root will
happily stage the cognitive profile and memory database. The user almost
never wants this.

We detect the situation cheaply at bootstrap and via ``okuro doctor``,
respect ``.gitignore`` so users who already excluded the path don't get
spammed, and emit a single actionable warning. We do not auto-fix.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional
from okuro.db.engine import okuro_home


def _okuro_home() -> Path:
    """Path okuro writes its state to. Mirrors the convention used elsewhere."""
    return okuro_home()


def detect_okuro_home_in_git_tree(home: Optional[Path] = None) -> Optional[Path]:
    """Return the git-root path that contains ``~/.okuro/`` if it is a leak risk.

    Walks upward from ``~/.okuro/`` looking for a ``.git`` entry (file or
    dir — submodules use a file). On the first hit, asks git whether the
    okuro path is already ignored relative to that root. Returns:

    * ``None`` — no enclosing git tree, or the path is gitignored
      (safe — staging it would require ``-f``).
    * ``Path`` — the git root that contains an un-ignored ``~/.okuro/``
      (risk — return the path so the caller can name it in the warning).

    Defensive: any error in the git probe is treated as "risk" so the
    user sees the warning and can investigate. Better a false positive
    than a silent leak.
    """
    home = home or _okuro_home()
    try:
        home_resolved = home.resolve()
    except OSError:
        return None

    current = home_resolved.parent
    while True:
        if (current / ".git").exists():
            return current if not _is_path_gitignored(current, home_resolved) else None
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _is_path_gitignored(repo_root: Path, target: Path) -> bool:
    """True if ``target`` is ignored by the git repo at ``repo_root``.

    ``git check-ignore -q`` exits 0 when the path matches a gitignore
    rule, 1 when it does not, and >=128 on internal errors. We treat
    the error case as "not ignored" so the warning fires.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "-q", str(target)],
            capture_output=True,
            timeout=2,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def format_git_tree_warning(repo_root: Path, home: Optional[Path] = None) -> str:
    """Render the bootstrap-packet warning block for a detected leak risk."""
    home = home or _okuro_home()
    return (
        "## ⚠️ Security alert — okuro state is inside a git working tree\n\n"
        f"`{home}` lives under the git repository at `{repo_root}`. "
        "Anything okuro writes (cognitive profile, memories, thoughts, work-in-progress) "
        "could be accidentally committed and leaked.\n\n"
        "**Fix one of:**\n"
        f"- Add `{home.name}/` to `{repo_root}/.gitignore`\n"
        "- Move the repo out from under your home directory\n"
        f"- Move okuro state by setting a different home (advanced)\n\n"
        "See `SECURITY.md` for the full threat model."
    )
