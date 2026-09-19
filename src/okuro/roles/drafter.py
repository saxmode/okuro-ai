# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Draft role creation: instant role creation for immediate use.
# index: imports | def draft_role | def promote_role | def confirm_promote
# AGENT_HEADER_END -->
"""Draft role creation: instant role creation for immediate use."""

import json
import uuid
from datetime import datetime, timezone

import yaml

from okuro.db import get_db
from okuro.sense.retrieval import vec_write
from okuro.embed import embed_one
from okuro.embed.client import to_bytes
from okuro.roles.designer import artifact_reference_error
from okuro.roles.write import promote_role as _write_promote, upsert_role


def draft_role(
    name: str,
    description: str,
    role_content: dict | None = None,
    seed_knowledge: list[str] | None = None,
    domain: str = "engineering",
    tier: str = "standard",
    model: str = "sonnet",
) -> dict:
    """Create a draft role immediately for current use.

    Args:
        name: Role identifier (e.g. "supabase-specialist")
        description: What the role does
        role_content: Optional dict with "micro" / "lean" / "full" keys
        seed_knowledge: Optional list of initial knowledge entries
        domain: Domain for the role
        tier: canonical dispatch tier, checked against CANONICAL_TIERS by the
            write helper. Used to be the literal 'standard' in this function's
            INSERT. The default preserves the old behaviour.
        model: provider model name. Was the literal 'sonnet' for the same
            reason.
    """
    # Pointer guard, through the shared gate rather than a second copy of it.
    # Only the pointer half applies here: draft_role SYNTHESISES the lean and
    # micro grades when the caller omits them, so a structure verdict on what
    # the caller sent would judge text this function is about to write itself.
    if role_content:
        pointer = artifact_reference_error(role_content)
        if pointer:
            return {"error": pointer}

    # Generate or use provided content
    if role_content and "micro" in role_content:
        micro_content = role_content["micro"]
    else:
        micro_content = yaml.dump(
            {
                "id": name,
                "purpose": description,
                "expertise": [],
                "constraints": [],
                "protocol": ["analyze", "implement", "verify"],
            },
            default_flow_style=False,
        )

    if role_content and "lean" in role_content:
        lean_content = role_content["lean"]
    else:
        lean_content = (
            f"# {name.upper()}\n\n## CORE\n\n**Purpose:** {description}\n\n"
            f"## PROTOCOL\n\n1. Analyze requirements\n2. Implement solution\n"
            f"3. Verify results\n"
        )

    full_content = role_content.get("full") if role_content else None

    # The row, the tags, the vector and updated_at go through the one write
    # path. The hand-written INSERT this replaces wrote vec_roles directly
    # without setting `description_embedded`, so the staleness backfill could
    # never tell this role's vector from a stale one; and `tier`/`model` were
    # literals, so every drafted role came out 'standard'/'sonnet' whatever it
    # was drafted for.
    written = upsert_role(
        {
            "role_id": name,
            "domain": domain,
            "description": description,
            "tier": tier,
            "model": model,
            "micro_prompt": micro_content,
            "lean_prompt": lean_content,
            "prompt": full_content,
            "maturity": "draft",
        },
        actor="roles.drafter:draft_role",
    )

    db = get_db()
    # The `UPDATE roles SET origin = 'user'` that stood here is gone with the
    # column (migration 162, E3). It marked a drafted role as the user's so a
    # re-seeder would not overwrite it; migration 155 deleted that seeder, and
    # "does a migration ship this role" is now computed by
    # `repair_plan.migration_carried_ids` from the migration files themselves.

    with db.transaction():
        # Seed knowledge
        if seed_knowledge:
            for learning in seed_knowledge:
                kid = str(uuid.uuid4())
                emb = embed_one(learning)
                db.execute(
                    "INSERT INTO role_knowledge (id, role_id, content, type) "
                    "VALUES (?, ?, ?, 'research')",
                    (kid, name, learning),
                )
                vec_write(
                    db, "vec_knowledge", kid, to_bytes(emb),
                    partition=("role_id", name),
                )

    return {
        "id": name,
        "domain": domain,
        "maturity": "draft",
        "seed_knowledge_count": len(seed_knowledge) if seed_knowledge else 0,
        "embedded": written["embedded"],
    }


def promote_role(role_id: str) -> dict:
    """Return draft summary + recommendation for promotion."""
    db = get_db()
    role = db.fetchone(
        "SELECT role_id, domain, maturity, sessions, learnings FROM roles "
        "WHERE role_id = ?",
        (role_id,),
    )
    if not role:
        return {"error": f"Role '{role_id}' not found"}
    if role["maturity"] != "draft":
        return {
            "error": f"Role '{role_id}' is not a draft (maturity: {role['maturity']})"
        }

    knowledge_row = db.fetchone(
        "SELECT COUNT(*) as cnt FROM role_knowledge WHERE role_id = ?",
        (role_id,),
    )
    knowledge_count = knowledge_row["cnt"] if knowledge_row else 0
    sessions = role["sessions"] or 0
    learnings = role["learnings"] or 0

    if sessions >= 3 and learnings >= 5:
        recommendation = "promote"
        reason = "Draft has been used enough to prove its value"
    elif sessions >= 1 and learnings >= 1:
        recommendation = "promote-with-note"
        reason = "Limited usage but has accumulated some knowledge"
    else:
        recommendation = "needs-work"
        reason = "Not enough usage to evaluate"

    return {
        "id": role_id,
        "domain": role["domain"],
        "maturity": "draft",
        "sessions": sessions,
        "learnings": learnings,
        "knowledge_in_db": knowledge_count,
        "recommendation": recommendation,
        "reason": reason,
    }


def confirm_promote(role_id: str) -> dict:
    """Actually promote a draft to active. Called after user approval.

    The bare UPDATE this replaces was the same defect as the HTTP promote
    endpoint and was missed by the audit because it is spelled differently:
    a draft is invisible to ``roles_match``, so promotion is the first moment
    the role's vector and tags have to be right, and neither was written here.

    Raises ``RolePromotionRefused`` when the stored body does not pass the
    canonical structure audit. Deliberately NOT converted to an ``{"error":
    ...}`` dict like the two lookups above: those report a bad ARGUMENT, which
    a caller can be trusted to read, while this reports that a role was about
    to go live incomplete. A caller that ignores the return value would
    promote it anyway, and every caller ignores a return value eventually.
    """
    if not get_db().fetchone(
        "SELECT role_id FROM roles WHERE role_id = ?", (role_id,)
    ):
        return {"error": f"Role '{role_id}' not found"}

    result = _write_promote(role_id, actor="roles.drafter:confirm_promote")
    return {
        "id": role_id,
        "maturity": "active",
        "status": "promoted",
        "embedded": result["embedded"],
    }
