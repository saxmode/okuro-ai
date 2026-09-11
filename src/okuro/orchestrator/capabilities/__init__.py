# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Capability registry — tracks reusable building blocks from completed tasks.
# index: none
# AGENT_HEADER_END -->
"""Capability registry — tracks reusable building blocks from completed tasks."""
from .models import Capability
from .registry import CapabilityRegistry

__all__ = ["Capability", "CapabilityRegistry"]
