# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: /api/target-groups router — JSON list of target groups for the browser
#   (the handover recipient picker's "Group" mode). target_groups was MCP-only;
#   this is the thin HTTP surface over okuro.peer.target_groups.list_groups.
# index: imports | router | list
# AGENT_HEADER_END -->
"""okuro·target-groups API — structured group list for the frontend.

The peer ``target_groups`` module rendered markdown (for agents); the handover
recipient picker needs JSON. One read endpoint:

  GET /api/target-groups → {groups: [{id, name, kind, company_id, role_class, member_count}]}
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/target-groups", tags=["target-groups"])


@router.get("")
def list_target_groups() -> dict:
    from okuro.peer.target_groups import list_groups

    return {"groups": list_groups()}
