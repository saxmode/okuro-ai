# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Retrieval-tier policy — decides whether a project's files get neural
#   embeddings (advanced/pro) or stay FTS/BM25-only (air). The air floor lets
#   okuro run on CPU-only hosts with no embedding model.
# index:
#   def host_can_embed
#   def project_tier
#   def should_embed
# AGENT_HEADER_END -->
"""Where the air|advanced|pro tier is enforced.

okuro ships to heterogeneous hosts. The tier is the capability gate:

  air       code graph + BM25/FTS only — NO neural model (CPU-only safe)
  advanced  + neural embeddings (needs a runnable embedding backend)
  pro       + SCIP cross-repo (embeddings too)

should_embed() is the single decision point the cortex indexer consults. It is
conservative for backward-compat: a project with no managed_repos row (every
pre-existing okuro project) keeps embeddings. Only an explicit air-tier managed
repo — or a host that literally cannot embed — drops to the FTS-only floor.
"""

from __future__ import annotations

import os

# Global kill switch: set to force the FTS-only floor everywhere regardless of
# per-repo tier (e.g. a constrained host, or to shrink the index).
EMBED_DISABLED_ENV = "OKURO_EMBED_DISABLED"


def host_can_embed() -> bool:
    """True when this host can actually run an embedding model.

    Backed by embed.client.is_available() (sentence-transformers present). A
    CPU-only laptop that skipped the embed extras returns False, so cortex
    degrades to the air floor instead of crashing on model load.
    """
    if os.environ.get(EMBED_DISABLED_ENV, "").strip().lower() in ("1", "true", "yes"):
        return False
    try:
        from okuro.embed.client import is_available

        return is_available()
    except Exception:
        return False


def project_tier(project: str | None, db=None) -> str | None:
    """Return a managed repo's tier (air|advanced|pro), or None if unmanaged.

    Unmanaged projects (no managed_repos row) return None — callers treat that
    as 'keep existing behavior' (embed). Tolerant of a pre-081 DB with no table.
    """
    if not project:
        return None
    try:
        if db is None:
            from okuro.db import get_db

            db = get_db()
        row = db.fetchone("SELECT tier FROM managed_repos WHERE id = ?", (project,))
        return row["tier"] if row else None
    except Exception:
        return None


def should_embed(project: str | None, db=None) -> bool:
    """Decide whether ``project``'s files should be neural-embedded.

    False (FTS-only air floor) when: the host can't embed, OR the project is a
    managed repo explicitly on the air tier. True otherwise — including every
    unmanaged project, so existing okuro projects are never regressed.
    """
    if not host_can_embed():
        return False
    return project_tier(project, db) != "air"
