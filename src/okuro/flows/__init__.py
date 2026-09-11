# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.flows — role team templates (flow composition).
# index: none
# AGENT_HEADER_END -->
"""okuro.flows — reusable role-team templates used to seed required_roles on task creation."""

from .storage import (
    DEFAULT_ORCHESTRATOR_X,
    DEFAULT_ORCHESTRATOR_Y,
    Flow,
    OrchestratorPlacement,
    RolePlacement,
    delete_flow,
    get_flow,
    list_flows,
    save_flow,
    slugify,
)

__all__ = [
    "DEFAULT_ORCHESTRATOR_X",
    "DEFAULT_ORCHESTRATOR_Y",
    "Flow",
    "OrchestratorPlacement",
    "RolePlacement",
    "delete_flow",
    "get_flow",
    "list_flows",
    "save_flow",
    "slugify",
]
