# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Maintenance mandate generation for role knowledge freshness.
# index: imports | def get_maintenance_mandate | def _build_mandate | def update_maintained
# AGENT_HEADER_END -->
"""Maintenance mandate generation for role knowledge freshness."""

import re

from .registry import get_role
from .knowledge import is_stale, get_knowledge_stats
from okuro.db import get_db


# Default search queries by domain
DOMAIN_SEARCHES = {
    "engineering": [
        "{role} best practices 2026",
        "{role} breaking changes",
        "{role} security advisories",
    ],
    "quality": [
        "testing best practices 2026",
        "security vulnerability trends",
        "{role} new patterns",
    ],
    "design": [
        "design system trends 2026",
        "accessibility standards update",
        "{role} best practices",
    ],
    "research": [
        "AI research trends 2026",
        "new ML techniques",
        "{role} latest developments",
    ],
    "marketing": [
        "digital marketing trends 2026",
        "platform algorithm changes",
        "{role} strategies",
    ],
    "documentation": [
        "technical writing best practices",
        "documentation tools 2026",
    ],
    "c-level": ["leadership trends 2026", "technology strategy"],
    "freaks": ["{role} latest discoveries", "{role} academic papers 2026"],
    "system": [
        "infrastructure best practices 2026",
        "devops trends",
        "{role} updates",
    ],
    "planning": ["software architecture trends 2026", "{role} patterns"],
}


def get_maintenance_mandate(role_id: str | None = None) -> dict | list[dict]:
    """Return maintenance mandate(s) for stale roles."""
    if role_id:
        return _build_mandate(role_id)

    # All stale roles
    db = get_db()
    rows = db.fetchall(
        "SELECT role_id, domain, maturity FROM roles WHERE maturity != 'draft'"
    )
    mandates = []
    for r in rows:
        if is_stale(r["role_id"]):
            mandates.append(_build_mandate(r["role_id"]))
    return mandates


def _build_mandate(role_id: str) -> dict:
    """Build a maintenance mandate for a single role."""
    db = get_db()
    role_row = db.fetchone(
        "SELECT domain, maintenance_schedule, last_maintained FROM roles "
        "WHERE role_id = ?",
        (role_id,),
    )
    domain = role_row["domain"] if role_row else "engineering"
    schedule = role_row["maintenance_schedule"] if role_row else "monthly"
    last_maintained = role_row["last_maintained"] if role_row else None

    stats = get_knowledge_stats(role_id)

    # Build search queries
    search_templates = DOMAIN_SEARCHES.get(
        domain, ["{role} best practices 2026"]
    )
    searches = [
        t.replace("{role}", role_id.replace("-", " ")) for t in search_templates
    ]

    # Extract sources from role definition
    role_data = get_role(role_id, level="full")
    sources = []
    if role_data and role_data.get("content"):
        urls = re.findall(r"https?://\S+", role_data["content"])
        sources = urls[:5]

    by_type = stats.get("by_type", {})
    knowledge_summary = (
        f"{stats['total_entries']} entries "
        f"({', '.join(f'{k}: {v}' for k, v in by_type.items())})"
        if by_type
        else "No knowledge entries"
    )

    return {
        "role": role_id,
        "domain": domain,
        "stale": is_stale(role_id),
        "stale_since": last_maintained,
        "schedule": schedule,
        "research_mandate": {
            "searches": searches,
            "sources_to_check": sources,
            "current_knowledge_summary": knowledge_summary,
        },
        "instructions": (
            f"Research these topics. "
            f"For each finding, call roles_learn('{role_id}', learning, type). "
            f"When done, the last_maintained timestamp updates automatically."
        ),
    }


def update_maintained(role_id: str):
    """Update last_maintained timestamp after maintenance research."""
    db = get_db()
    db.execute(
        "UPDATE roles SET last_maintained = datetime('now') WHERE role_id = ?",
        (role_id,),
    )
