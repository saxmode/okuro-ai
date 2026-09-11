# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Portable path resolution for managed repos. Anchors the clone store
#   on the per-user data dir (where okuro.db lives) so it ships to any user on
#   any machine without a hardcoded path.
# index: def repos_root | def repo_path | def slugify | def repo_id
# AGENT_HEADER_END -->
"""Where cloned repositories live.

The store is ``<data-dir>/repos/<workspace>/<name>``. The data dir is resolved
via the same helper the DB uses (``default_db_path().parent`` = ``~/.okuro``),
NEVER a hardcoded machine path — okuro ships to many users, so the location must
be relative to wherever okuro runs for that user.
"""

from __future__ import annotations

import re
from pathlib import Path


def repos_root() -> Path:
    """Return ``<data-dir>/repos`` and create it on demand.

    Mirrors ``cli/db_helpers.ensure_db_dir`` — anchored on the DB's parent so
    the repo store always sits beside ``okuro.db`` under the per-user data dir.
    """
    from okuro.cli.db_helpers import default_db_path

    root = default_db_path().parent / "repos"
    root.mkdir(parents=True, exist_ok=True)
    return root


def slugify(text: str) -> str:
    """Filesystem-safe slug: lowercase, non-alnum → hyphen, trimmed."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "repo"


def repo_path(workspace: str, name: str) -> Path:
    """Resolve the clone directory for ``workspace``/``name`` (not created)."""
    return repos_root() / slugify(workspace) / slugify(name)


def repo_id(workspace: str, name: str) -> str:
    """Stable registry id for a managed repo: ``<workspace>__<name>``."""
    return f"{slugify(workspace)}__{slugify(name)}"
