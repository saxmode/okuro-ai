# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.peer (仲) — communication partner profiles.
# index: none
# AGENT_HEADER_END -->
"""okuro.peer (仲) — communication partner profiles."""

from .persons import (
    person_add,
    person_get,
    person_list,
    person_update,
    person_lens,
    person_match,
    person_merge,
)
from .crm import (
    company_add,
    company_get,
    company_list,
    company_set_brand,
    affiliation_add,
    affiliation_list,
    affiliation_set_primary,
    affiliation_remove,
    connection_add,
    connection_list,
    connection_remove,
    engagement_add,
    engagement_list,
    engagement_resolve,
)
from .target_groups import (
    target_group_add,
    target_group_get,
    target_group_list,
    target_group_add_member,
    target_group_remove_member,
    seed_standard_groups,
    resolve_audience,
    group_embed_text,
    reembed_group,
    reembed_all_groups,
    match_groups,
)

__all__ = [
    "person_add",
    "person_get",
    "person_list",
    "person_update",
    "person_lens",
    "person_match",
    "person_merge",
    "company_add",
    "company_get",
    "company_list",
    "company_set_brand",
    "affiliation_add",
    "affiliation_list",
    "affiliation_set_primary",
    "affiliation_remove",
    "connection_add",
    "connection_list",
    "connection_remove",
    "engagement_add",
    "engagement_list",
    "engagement_resolve",
    "target_group_add",
    "target_group_get",
    "target_group_list",
    "target_group_add_member",
    "target_group_remove_member",
    "seed_standard_groups",
    "resolve_audience",
    "group_embed_text",
    "reembed_group",
    "reembed_all_groups",
    "match_groups",
]
