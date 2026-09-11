# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.bridge (橋) — unified LLM bridge.
# index: none
# AGENT_HEADER_END -->
"""okuro.bridge (橋) — unified LLM bridge."""

from .invoke import invoke
from .providers import resolve_provider, list_providers, get_routing_table

__all__ = ["invoke", "resolve_provider", "list_providers", "get_routing_table"]
