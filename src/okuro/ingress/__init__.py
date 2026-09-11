# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.ingress — adapters that pull messages from external channels
#   (telegram, slack, …) into the okuro action surface (thoughts/todos/
#   reminders/orchestrator). Each adapter implements IngressAdapter and is
#   supervised by IngressSupervisor inside okuro-daemon.
# index: exports
# AGENT_HEADER_END -->
"""okuro.ingress — external-channel adapters."""

from .adapter import IngressAdapter, IngressMessage

__all__ = ["IngressAdapter", "IngressMessage"]
