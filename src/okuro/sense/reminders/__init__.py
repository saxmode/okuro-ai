# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Profile-driven reminder engine.
# index: none
# AGENT_HEADER_END -->
"""Profile-driven reminder engine.

Provides CRUD operations (called by MCP tools) and an eval loop (called by timer).
"""

from .engine import (
    create_reminder,
    list_reminders,
    snooze_reminder,
    dismiss_reminder,
    acknowledge_reminder,
    evaluate_due,
)
from .suggestions import (
    generate_suggestions,
    accept_suggestion,
    reject_suggestion,
)

__all__ = [
    "create_reminder",
    "list_reminders",
    "snooze_reminder",
    "dismiss_reminder",
    "acknowledge_reminder",
    "evaluate_due",
    "generate_suggestions",
    "accept_suggestion",
    "reject_suggestion",
]
