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


def draft_role(
    name: str,
    description: str,
    role_content: dict | None = None,
    seed_knowledge: list[str] | None = None,
    domain: str = "engineering",
) -> dict:
    """Create a draft role immediately for current use.

    Args:
        name: Role identifier (e.g. "supabase-specialist")
        description: What the role does
        role_content: Optional dict with "micro" / "lean" / "full" keys
        seed_knowledge: Optional list of initial knowledge entries
        domain: Domain for the role
    """
    # Guard: reject artifact references
    if role_content:
        for grade in ("full", "lean", "micro"):
            val = role_content.get(grade, "")
            if val and len(val) < 200 and any(
                val.strip().lower().startswith(prefix)
                for prefix in ("see artifact", "see file", "see task", "refer to")
            ):
                return {
                    "error": f"role_content['{grade}'] contains an artifact reference "
                    f"instead of actual content ({len(val)} chars). "
                    f"Pass the full role text, not a pointer."
                }

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

    # Store in DB
    embedding = embed_one(description)
    emb_bytes = to_bytes(embedding)
    db = get_db()

    with db.transaction():
        db.execute(
            "INSERT INTO roles (role_id, domain, description, maturity, "
            "micro_prompt, lean_prompt, prompt, tier, model, origin) "
            "VALUES (?, ?, ?, 'draft', ?, ?, ?, 'standard', 'sonnet', 'user') "
            "ON CONFLICT(role_id) DO UPDATE SET "
            "description = excluded.description, "
            "micro_prompt = excluded.micro_prompt, "
            "lean_prompt = excluded.lean_prompt, "
            "prompt = excluded.prompt",
            (name, domain, description, micro_content, lean_content, full_content),
        )

        # Upsert vec_roles
        db.execute("DELETE FROM vec_roles WHERE id = ?", (name,))
        db.execute(
            "INSERT INTO vec_roles (id, embedding) VALUES (?, ?)",
            (name, emb_bytes),
        )

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
    """Actually promote a draft to active. Called after user approval."""
    db = get_db()
    row = db.fetchone(
        "SELECT role_id FROM roles WHERE role_id = ?", (role_id,)
    )
    if not row:
        return {"error": f"Role '{role_id}' not found"}

    db.execute(
        "UPDATE roles SET maturity = 'active' WHERE role_id = ?",
        (role_id,),
    )
    return {"id": role_id, "maturity": "active", "status": "promoted"}
