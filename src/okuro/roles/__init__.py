# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.roles (叡智) — role framework.
# index: none
# AGENT_HEADER_END -->
"""okuro.roles (叡智) — role framework."""

from .registry import list_roles, get_role, get_role_info, get_domains

__all__ = ["list_roles", "get_role", "get_role_info", "get_domains"]
