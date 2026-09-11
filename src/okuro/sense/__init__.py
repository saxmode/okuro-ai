# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.sense (知) — awareness: brain, memory, thoughts, sessions, bootstrap.
# index: imports
# AGENT_HEADER_END -->
"""okuro.sense (知) — awareness: brain, memory, thoughts, sessions, bootstrap.

Central awareness layer. Manages persistent memory, thought capture,
project tracking, progress logging, and task briefing.
"""

from typing import TYPE_CHECKING

from okuro._lazy import install as _install

if TYPE_CHECKING:  # static analysers still see the full surface
    from .advisor import advise
    from .bootstrap import assemble as bootstrap
    from .memory import read_memory, write_memory
    from .principles import get_principles
    from .phases import project_phases_set
    from .progress import get_progress, log_progress
    from .projects import (
        get_project,
        list_projects,
        scan_projects,
        update_project,
        update_project_agent,
    )
    from .status import project_status
    from .thoughts import capture_thought, daily_digest, search_thoughts, update_thought
    from .todos import (
        todo_add,
        todo_delete,
        todo_done,
        todo_get,
        todo_list,
        todo_update,
    )

# Deferred: this package is the parent of okuro.sense.mcp_tools, so eagerly
# re-exporting here loaded 18 files into every process that merely registered
# the MCP tool surface. See okuro._lazy.
_EXPORTS = {
    "write_memory": ("okuro.sense.memory", "write_memory"),
    "read_memory": ("okuro.sense.memory", "read_memory"),
    **{n: ("okuro.sense.thoughts", n) for n in (
        "capture_thought", "search_thoughts", "update_thought", "daily_digest")},
    **{n: ("okuro.sense.todos", n) for n in (
        "todo_add", "todo_list", "todo_get", "todo_update", "todo_done",
        "todo_delete")},
    **{n: ("okuro.sense.progress", n) for n in ("log_progress", "get_progress")},
    "project_status": ("okuro.sense.status", "project_status"),
    "project_phases_set": ("okuro.sense.phases", "project_phases_set"),
    **{n: ("okuro.sense.projects", n) for n in (
        "list_projects", "get_project", "update_project",
        "update_project_agent", "scan_projects")},
    "get_principles": ("okuro.sense.principles", "get_principles"),
    "advise": ("okuro.sense.advisor", "advise"),
    "bootstrap": ("okuro.sense.bootstrap", "assemble"),
}

__getattr__, __dir__ = _install(globals(), _EXPORTS)

__all__ = [
    # Memory
    "write_memory",
    "read_memory",
    # Thoughts
    "capture_thought",
    "search_thoughts",
    "update_thought",
    "daily_digest",
    # Todos
    "todo_add",
    "todo_list",
    "todo_get",
    "todo_update",
    "todo_done",
    "todo_delete",
    # Progress
    "log_progress",
    "get_progress",
    # Project status
    "project_status",
    "project_phases_set",
    # Projects
    "list_projects",
    "get_project",
    "update_project",
    "update_project_agent",
    "scan_projects",
    # Principles
    "get_principles",
    # Advisor
    "advise",
    # Bootstrap
    "bootstrap",
]
