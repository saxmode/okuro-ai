# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Role registry: list, get, search roles from okuro.db.
# index:
#   imports
#   def list_roles
#   def get_role
#   def get_role_info
#   def update_role_info
#   def get_domains
# AGENT_HEADER_END -->
"""Role registry: list, get, search roles from okuro.db."""

import json

from okuro.db import get_db


def list_roles(domain: str | None = None) -> list[dict]:
    """List available roles, optionally filtered by domain."""
    db = get_db()
    if domain:
        rows = db.fetchall(
            "SELECT role_id, domain, maturity, sessions, learnings, "
            "maintenance_schedule, last_maintained, tier, model, description "
            "FROM roles WHERE domain = ? ORDER BY role_id",
            (domain,),
        )
    else:
        rows = db.fetchall(
            "SELECT role_id, domain, maturity, sessions, learnings, "
            "maintenance_schedule, last_maintained, tier, model, description "
            "FROM roles ORDER BY domain, role_id",
        )
    return [
        {
            "id": r["role_id"],
            "domain": r["domain"],
            "maturity": r["maturity"] or "active",
            "sessions": r["sessions"] or 0,
            "learnings": r["learnings"] or 0,
            "maintenance_schedule": r["maintenance_schedule"],
            "last_maintained": r["last_maintained"],
            "tier": r["tier"] or "standard",
            "model": r["model"] or "sonnet",
            "description": r["description"] or "",
        }
        for r in rows
    ]


def get_role(role_id: str, level: str = "micro") -> dict | None:
    """Get role content at requested fidelity level.

    level: "micro" | "lean" | "full"
    Returns dict with role content and metadata, or None.
    """
    db = get_db()
    row = db.fetchone(
        "SELECT role_id, domain, maturity, tier, model, description, "
        "prompt, lean_prompt, micro_prompt "
        "FROM roles WHERE role_id = ?",
        (role_id,),
    )
    if not row:
        return None

    # Map level to column
    level_map = {"micro": "micro_prompt", "lean": "lean_prompt", "full": "prompt"}
    col = level_map.get(level, "micro_prompt")
    content = row[col]

    # Fallback chain: requested → lean → micro → full
    actual_level = level
    if not content:
        for fallback_level, fallback_col in [
            ("lean", "lean_prompt"),
            ("micro", "micro_prompt"),
            ("full", "prompt"),
        ]:
            if row[fallback_col]:
                content = row[fallback_col]
                actual_level = fallback_level
                break

    if content is None:
        return None

    return {
        "id": row["role_id"],
        "domain": row["domain"],
        "level": actual_level,
        "maturity": row["maturity"] or "active",
        "tier": row["tier"] or "standard",
        "model": row["model"] or "sonnet",
        "description": row["description"] or "",
        # Legacy callers (CLI, bootstrap) read `content` at the resolved level.
        "content": content,
        # Raw columns for UIs with a tabbed viewer. Without these the
        # frontend role-viewer rendered "(no X prompt)" on every tab.
        "prompt": row["prompt"] or "",
        "lean_prompt": row["lean_prompt"] or "",
        "micro_prompt": row["micro_prompt"] or "",
    }


def get_role_info(role_id: str) -> dict | None:
    """Get role metadata without loading prompt content."""
    db = get_db()
    row = db.fetchone(
        "SELECT role_id, domain, maturity, sessions, learnings, tier, model, "
        "description, maintenance_schedule, last_maintained, tools "
        "FROM roles WHERE role_id = ?",
        (role_id,),
    )
    if not row:
        return None

    tools = row["tools"]
    if isinstance(tools, str):
        try:
            tools = json.loads(tools)
        except (json.JSONDecodeError, TypeError):
            tools = []

    return {
        "id": row["role_id"],
        "domain": row["domain"],
        "maturity": row["maturity"] or "active",
        "sessions": row["sessions"] or 0,
        "learnings": row["learnings"] or 0,
        "tier": row["tier"] or "standard",
        "model": row["model"] or "sonnet",
        "description": row["description"] or "",
        "maintenance_schedule": row["maintenance_schedule"],
        "last_maintained": row["last_maintained"],
        "tools": tools,
    }


def update_role_info(role_id: str, updates: dict):
    """Update role metadata."""
    if not updates:
        return
    db = get_db()
    set_clauses = []
    values = []
    for key, val in updates.items():
        if key == "tools":
            set_clauses.append("tools = ?")
            values.append(json.dumps(val) if not isinstance(val, str) else val)
        else:
            set_clauses.append(f"{key} = ?")
            values.append(val)
    values.append(role_id)
    db.execute(
        f"UPDATE roles SET {', '.join(set_clauses)} WHERE role_id = ?",
        tuple(values),
    )


def get_domains() -> list[str]:
    """Get list of all domains."""
    db = get_db()
    rows = db.fetchall("SELECT DISTINCT domain FROM roles ORDER BY domain")
    return [r["domain"] for r in rows]
