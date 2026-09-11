# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Managed-repository lifecycle — clone remote repos under the per-user
#   data dir, register them as projects (→ cortex roots), code-graph ingest.
# index: from paths | from lifecycle
# AGENT_HEADER_END -->
"""Managed-repository lifecycle package.

Public surface re-exported for MCP tools + API router:
  repos_root, repo_path      — portable path resolution (paths.py)
  add_repo, sync_repo,        — lifecycle operations (lifecycle.py)
  update_repo, update_credential
  remove_repo, list_repos, get_repo
"""

from okuro.repos.paths import repos_root, repo_path
from okuro.repos.lifecycle import (
    add_repo,
    inherit_credential,
    sync_repo,
    push_repo,
    update_repo,
    update_credential,
    probe_credentials,
    remove_repo,
    list_repos,
    get_repo,
)

__all__ = [
    "repos_root",
    "repo_path",
    "add_repo",
    "inherit_credential",
    "sync_repo",
    "push_repo",
    "update_repo",
    "update_credential",
    "probe_credentials",
    "remove_repo",
    "list_repos",
    "get_repo",
]
