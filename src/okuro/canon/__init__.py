# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.canon (規範) — tool registry.
# index: none
# AGENT_HEADER_END -->
"""okuro.canon (規範) — tool registry."""

from .registry import list_tools, get_tool, list_skills, get_skill, validate_registry
from .consumers import (
    ConsumerRecord,
    list_consumers,
    get_consumer,
    consumer_paths,
    provider_ids,
    cli_binaries,
)

__all__ = [
    "list_tools",
    "get_tool",
    "list_skills",
    "get_skill",
    "validate_registry",
    "ConsumerRecord",
    "list_consumers",
    "get_consumer",
    "consumer_paths",
    "provider_ids",
    "cli_binaries",
]
