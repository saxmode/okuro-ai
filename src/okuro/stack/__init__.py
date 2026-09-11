# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.stack — tech stack registry public API (including brands).
# index: public exports
# AGENT_HEADER_END -->
"""okuro.stack — tech stack registry.

Sibling to okuro.design but for tech-stack decisions (frontend framework,
backend, API philosophy, MCP readiness, …). Single source of truth for
"what do we build with?" — queryable from MCP tools, the web UI, and
bootstrap.

Entry lifecycle: trial → approved → deprecated → banned.
Profiles are opinionated compositions; projects bind to a profile **or**
a brand. Brands compose design + fe stack + be stack + principle_set via
a configurable slot table.
"""

from .registry import (
    list_layers,
    get_layer,
    list_entries,
    get_entry,
    match_entries,
    list_profiles,
    get_profile,
    resolve_profile,
    active_profile_for,
    seed_registry,
    list_slot_kinds,
    list_brands,
    get_brand,
    resolve_brand,
    active_brand_for,
)
from .writer import (
    upsert_entry,
    set_entry_status,
    assign_project_profile,
    upsert_profile,
    propose,
    decide_proposal,
    list_proposals,
    upsert_brand,
    set_brand_slot,
    delete_brand_slot,
    assign_project_brand,
    delete_brand,
)
from .validator import validate_registry, lint_profile, lint_brand

__all__ = [
    "list_layers",
    "get_layer",
    "list_entries",
    "get_entry",
    "match_entries",
    "list_profiles",
    "get_profile",
    "resolve_profile",
    "active_profile_for",
    "seed_registry",
    "upsert_entry",
    "set_entry_status",
    "assign_project_profile",
    "upsert_profile",
    "propose",
    "decide_proposal",
    "list_proposals",
    "validate_registry",
    "lint_profile",
    # Brands
    "list_slot_kinds",
    "list_brands",
    "get_brand",
    "resolve_brand",
    "active_brand_for",
    "upsert_brand",
    "set_brand_slot",
    "delete_brand_slot",
    "assign_project_brand",
    "delete_brand",
    "lint_brand",
]
