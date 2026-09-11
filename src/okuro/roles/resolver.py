# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Role resolution: match tasks to roles via embedding similarity.
# index: imports | def seed_role_embeddings | def match_roles | def match_role
# AGENT_HEADER_END -->
"""Role resolution: match tasks to roles via embedding similarity."""

import json

import yaml

from okuro.db import get_db
from okuro.embed import embed_one
from okuro.embed.client import embed_query, to_bytes
from .registry import list_roles, get_role

# General-purpose fallback role (hardcoded)
GENERAL_ROLE = {
    "id": "general",
    "domain": "system",
    "level": "micro",
    "maturity": "active",
    "content": yaml.dump(
        {
            "id": "general",
            "purpose": "Versatile engineering agent with full system access",
            "expertise": ["full-stack", "infrastructure", "documentation", "research"],
            "constraints": [
                "Follow okuro conventions (keyring for secrets, cortex for search)",
                "Test changes before declaring done",
                "Log progress at meaningful milestones",
            ],
            "tools": [
                "okuro.cortex (codebase navigation)",
                "okuro.system (live system state)",
                "okuro.keyring (secrets)",
                "okuro.sense (memory, thoughts, progress)",
                "okuro.roles (adopt a role if the task becomes specialized)",
            ],
            "protocol": [
                "understand context",
                "research domain",
                "implement",
                "verify",
                "document",
            ],
        },
        default_flow_style=False,
    ),
}

# Cosine threshold on true cosine (see match_roles). With the Qwen3
# instruction-wrapped query, the right role lands ~0.55-0.65 and unrelated
# roles fall below ~0.4. (The old 0.1 was a bge-small-era value compared
# against a broken `1 - L2` score — it silently degraded after the tier
# switch to Qwen3.) The value is unchanged by the 2026-07-15 metric migration:
# it was already calibrated against true cosine, only the derivation moved
# from an L2 reconstruction to the natively-declared metric.
SIMILARITY_THRESHOLD = 0.40

# The maturity/panel filters below run AFTER the vector cut, so a filtered
# candidate would otherwise consume a result slot and silently shrink the
# answer (measured: top_k=5 returned 1 row). Overfetch, filter, then truncate.
_CANDIDATE_OVERFETCH = 4


def seed_role_embeddings():
    """Generate and store embeddings for all registered roles.

    Reads each role's micro description and stores embedding in vec_roles.
    """
    roles = list_roles()
    db = get_db()
    count = 0

    for role in roles:
        role_id = role["id"]
        role_data = get_role(role_id, level="micro")
        if not role_data:
            continue

        content = role_data["content"]
        if "AGENT_HEADER_END" in content:
            content = content.split("AGENT_HEADER_END -->")[-1].strip()

        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError:
            parsed = {}

        purpose = parsed.get("purpose", "") if isinstance(parsed, dict) else ""
        expertise = parsed.get("expertise", []) if isinstance(parsed, dict) else []
        if isinstance(expertise, list):
            expertise = ", ".join(expertise)

        description = f"{purpose}. Expertise: {expertise}" if expertise else purpose
        if not description:
            continue

        embedding = embed_one(description)
        emb_bytes = to_bytes(embedding)

        # Update description in roles table
        db.execute(
            "UPDATE roles SET description = ? WHERE role_id = ?",
            (description, role_id),
        )

        # Upsert into vec_roles
        db.execute("DELETE FROM vec_roles WHERE id = ?", (role_id,))
        db.execute(
            "INSERT INTO vec_roles (id, embedding) VALUES (?, ?)",
            (role_id, emb_bytes),
        )
        count += 1

    return count


def match_roles(
    task_description: str,
    *,
    top_k: int = 3,
    threshold: float = SIMILARITY_THRESHOLD,
    panel_only: bool = False,
) -> list[dict]:
    """Match a task to roles by embedding similarity (canonical API).

    Returns up to ``top_k`` matches sorted by similarity descending. Each
    item: ``{id, domain, description, similarity, match_type}`` where
    ``match_type`` is ``"matched"`` if ``similarity >= threshold`` else
    ``"fallback"``.

    ``panel_only`` filters out roles flagged ``panel_eligible = 0``. It is
    OFF by default: that flag marks roles that do not belong on a
    DELIBERATION panel, and applying it to every caller made the two meta
    roles (role-designer, role-researcher) unreachable by any match at all.
    Only the panel proposer passes it.

    Returns ``[]`` when the ``vec_roles`` index is empty, every candidate is
    draft, or THIS HOST CANNOT EMBED. Callers (orchestrator panel proposer,
    MCP tools, CLI) decide what to do with an empty result — never re-wrap
    silently.
    """
    # Role matching is dense-only: there is no BM25 arm for vec_roles, so an
    # air host has nothing to fall back TO. Degrade to "no role matched" —
    # which every caller already handles as match_type "fallback" — instead of
    # raising EmbeddingsUnavailable out of a routing helper. Before 2026-09-06
    # this call was unguarded and an air install could not run roles_match at
    # all. The gate is the same one the cortex indexer obeys.
    from okuro.cortex.tier_policy import host_can_embed

    if not host_can_embed():
        return []

    # QUERY side: wrap with the instruction prefix for instruction-tuned tiers
    # (Qwen3). Role descriptions in vec_roles are embedded RAW (see
    # seed_role_embeddings) — the asymmetric pairing the model expects.
    embedding = embed_query(task_description)
    emb_bytes = to_bytes(embedding)

    db = get_db()
    raw = db.vec_search(
        "vec_roles", emb_bytes, limit=max(top_k, top_k * _CANDIDATE_OVERFETCH)
    )
    if not raw:
        return []

    results: list[dict] = []
    for m in raw:
        role_id = m["id"]
        # vec_roles declares distance_metric=cosine, so sqlite-vec returns
        # cosine distance (1 - cos) and cosine is `1 - d`. This previously
        # reconstructed cosine from L2 (1 - d^2/2) because the table carried
        # vec0's default metric; okuro.embed.repair now declares it in the
        # schema. Clamp at 0 — cosine distance runs to 2, so `1 - d` is
        # negative for opposed vectors.
        similarity = round(max(0.0, 1.0 - m["distance"]), 4)
        info = db.fetchone(
            "SELECT role_id, domain, description, maturity, panel_eligible "
            "FROM roles WHERE role_id = ?",
            (role_id,),
        )
        if not info or info["maturity"] == "draft":
            continue
        # panel_eligible marks meta roles (role-designer/researcher/
        # maintainer) and builder/writer roles whose primary output is a
        # deliverable artifact: they belong in execution subtasks, not on
        # deliberation panels. That is a PANEL statement, so only a panel
        # caller filters on it — filtering here for every caller removed
        # those roles from execution matching, the CLI, bootstrap and MCP
        # roles_match too.
        if (
            panel_only
            and info["panel_eligible"] is not None
            and int(info["panel_eligible"]) == 0
        ):
            continue
        results.append({
            "id": info["role_id"],
            "domain": info["domain"],
            "description": info["description"] or "",
            "similarity": similarity,
            "match_type": "matched" if similarity >= threshold else "fallback",
        })
        if len(results) >= top_k:
            break
    return results


def match_role(task_description: str) -> dict:
    """Legacy single-result wrapper around :func:`match_roles`.

    Preserves the older return shape: a single dict with ``alternatives``
    when the best match crosses threshold, or a general fallback with a
    ``closest`` hint pointing at the highest sub-threshold candidate.
    New code should call :func:`match_roles` directly.
    """
    matches = match_roles(task_description, top_k=3)
    if matches and matches[0]["match_type"] == "matched":
        primary = matches[0]
        return {
            "id": primary["id"],
            "domain": primary["domain"],
            "description": primary["description"],
            "similarity": primary["similarity"],
            "match_type": "matched",
            "alternatives": [
                {"id": m["id"], "domain": m["domain"], "similarity": m["similarity"]}
                for m in matches[1:]
                if m["match_type"] == "matched"
            ],
        }

    closest = None
    if matches:
        closest = {"id": matches[0]["id"], "similarity": matches[0]["similarity"]}
    return {
        "id": "general",
        "domain": "system",
        "description": "Versatile engineering agent with full system access",
        "similarity": matches[0]["similarity"] if matches else 0.0,
        "match_type": "fallback",
        "closest": closest,
    }
