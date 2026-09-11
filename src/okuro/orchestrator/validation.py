# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: validates loaded task state for impossible/corrupt conditions
# index: def validate_task | def _validate_graph | def _has_cycle
# AGENT_HEADER_END -->
"""
Okuro Orchestrator State Validation

Detects impossible states, missing fields, and inconsistencies.
Returns advisory warnings (never blocks).
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from okuro.orchestrator.state import Task

logger = logging.getLogger("okuro.orchestrator.validation")

VALID_TASK_STATUSES = frozenset({
    "pending", "planning", "active", "done", "failed", "halted", "blocked",
    "deliberating", "awaiting_decision", "waiting_user",
    # Ran to the end of what was reachable, but something failed permanently
    # or was stranded behind a failure. Terminal, and deliberately NOT `done`.
    "completed_partial",
})

# Quality-review outcome — orthogonal to VALID_SUBTASK_STATUSES. A review
# verdict writes ONLY this; it must never touch subtask.status. See the
# review_state docstring on state.Subtask for why the two were separated.
VALID_REVIEW_STATES = frozenset({
    "not_reviewed", "passed", "failed", "overridden",
})
VALID_SUBTASK_STATUSES = frozenset({
    "pending", "running", "done", "failed", "skipped", "approved", "waiting_approval",
    "superseded", "rejected",
})
# Gate 2 §C6 — dead enum values removed. "active" was declared but no engine
# code path ever assigns it to phase.status (phases flip pending → done or
# pending → blocked_review only). "skipped" was declared on position nodes
# but only ever written to subtasks; position nodes terminate via
# "dismissed" (deliberation.py:270).
VALID_PHASE_STATUSES = frozenset({"pending", "done", "blocked_review"})
VALID_NODE_TYPES = frozenset({"position", "discussion", "execution"})
VALID_POSITION_STATUSES = frozenset({"pending", "running", "done", "failed", "dismissed"})
VALID_DISCUSSION_STATUSES = frozenset({"proposed", "positions_running", "awaiting_user", "resolved", "superseded"})
# Gate 2 §C3 — execution-node statuses are derived from the SoT subtask.
# "superseded" is mirrored from subtask.status when apply_supersedes fires
# (graph view of an obsoleted subtask), and tracked by get_ready_nodes'
# terminal set so successors observe the same exclusion.
VALID_EXECUTION_STATUSES = frozenset({"pending", "running", "done", "failed", "skipped", "superseded"})
VALID_ASSIGNMENT_ACTIONS = frozenset({"assign", "acknowledge", "dismiss"})


# Gate 2 §C6 — authoritative write-boundary guards.
#
# The validator above is advisory (returns warnings only on a full Task
# reload). update_subtask / update_node / save_plan / save_task_meta call
# these focused guards BEFORE the atomic write — invalid status values
# raise ValueError instead of silently landing on disk and producing a
# logger.warning at the next reload.
#
# This is the "promote validator to authoritative" half of C6. The other
# half — removing declared-but-unset values — is the enum trims above.


class InvalidStatusError(ValueError):
    """Raised when a writer is asked to persist a status outside the
    closed enum for its entity kind.

    Distinct from the bare ValueError that update_subtask raises for
    "subtask not found" so callers can disambiguate by exception type if
    they need to.
    """


def _validate_status_write(kind: str, value: str) -> None:
    """Reject writes that carry an invalid status enum value.

    Called from every state writer (update_subtask, update_node, save_plan,
    save_task_meta) BEFORE the lock-protected atomic write. The check is
    cheap (frozenset membership) and runs only when ``status`` is in the
    updates payload — writers that don't touch status pay nothing.

    Parameters
    ----------
    kind:
        One of ``task``, ``subtask``, ``phase``, ``position``, ``discussion``,
        ``execution``. The taxonomy matches the enum frozensets above so
        callers don't have to know which frozenset to pick.
    value:
        The proposed status string. Empty string is accepted as a no-op
        (writers occasionally pass partial dicts with no status key).
    """
    if not value:
        return
    enum_map: dict[str, frozenset[str]] = {
        "task": VALID_TASK_STATUSES,
        "subtask": VALID_SUBTASK_STATUSES,
        "phase": VALID_PHASE_STATUSES,
        "position": VALID_POSITION_STATUSES,
        "discussion": VALID_DISCUSSION_STATUSES,
        "execution": VALID_EXECUTION_STATUSES,
        "review_state": VALID_REVIEW_STATES,
    }
    valid = enum_map.get(kind)
    if valid is None:
        raise InvalidStatusError(f"Unknown status kind: {kind!r}")
    if value not in valid:
        raise InvalidStatusError(
            f"Invalid {kind} status: {value!r} (allowed: {sorted(valid)})"
        )


def validate_task(task: "Task") -> list[str]:
    """Validate a loaded task for impossible states. Returns warning list."""
    errors: list[str] = []

    if not task.id:
        errors.append("Task has no ID")
    if not task.description:
        errors.append("Task has no description")
    if not task.created_at:
        errors.append("Task has no created_at timestamp")
    if task.status not in VALID_TASK_STATUSES:
        errors.append(f"Invalid task status: '{task.status}'")

    if task.status == "active" and not task.phases:
        errors.append("Task is 'active' but has no phases")

    all_subtask_ids: set[str] = set()
    for phase in task.phases:
        for st in phase.subtasks:
            all_subtask_ids.add(st.id)

    for phase in task.phases:
        if phase.status not in VALID_PHASE_STATUSES:
            errors.append(f"Phase {phase.id}: invalid status '{phase.status}'")
        if not phase.subtasks:
            errors.append(f"Phase {phase.id} has no subtasks")

        for st in phase.subtasks:
            if st.status not in VALID_SUBTASK_STATUSES:
                errors.append(f"Subtask {st.id}: invalid status '{st.status}'")
            if st.status == "running" and not st.started_at:
                errors.append(f"Subtask {st.id} is 'running' but has no started_at")
            if st.status == "done" and st.duration <= 0:
                errors.append(f"Subtask {st.id} is 'done' but has no duration")
            for dep in st.dependencies:
                if dep not in all_subtask_ids:
                    errors.append(f"Subtask {st.id} depends on non-existent '{dep}'")

    if task.status == "done":
        not_done = [s.id for p in task.phases for s in p.subtasks if s.status not in ("done", "skipped")]
        if not_done:
            errors.append(f"Task is 'done' but subtasks not complete: {not_done}")

    if task.status == "failed":
        still_running = [s.id for p in task.phases for s in p.subtasks if s.status == "running"]
        if still_running:
            errors.append(f"Task is 'failed' but subtasks still 'running': {still_running}")

    for phase in task.phases:
        if phase.status == "done":
            not_done = [s.id for s in phase.subtasks if s.status not in ("done", "skipped")]
            if not_done:
                errors.append(f"Phase {phase.id} is 'done' but subtasks not complete: {not_done}")

    if hasattr(task, "graph") and task.graph and task.graph.nodes:
        errors.extend(_validate_graph(task))

    return errors


def _validate_graph(task: "Task") -> list[str]:
    errors: list[str] = []
    graph = task.graph
    node_ids = {n.id for n in graph.nodes}

    for node in graph.nodes:
        if node.type not in VALID_NODE_TYPES:
            errors.append(f"Node {node.id}: invalid type '{node.type}'")

        if node.type == "position":
            if node.status not in VALID_POSITION_STATUSES:
                errors.append(f"Node {node.id} (position): invalid status '{node.status}'")
            if not node.role:
                errors.append(f"Node {node.id} (position): missing role")
        elif node.type == "discussion":
            if node.status not in VALID_DISCUSSION_STATUSES:
                errors.append(f"Node {node.id} (discussion): invalid status '{node.status}'")
            for a in node.assignments:
                if a.action not in VALID_ASSIGNMENT_ACTIONS:
                    errors.append(f"Node {node.id}: assignment for {a.role} has invalid action '{a.action}'")
                if a.action == "assign" and not a.leads:
                    errors.append(f"Node {node.id}: assignment for {a.role} is 'assign' but has no 'leads' domain")
        elif node.type == "execution":
            if node.status not in VALID_EXECUTION_STATUSES:
                errors.append(f"Node {node.id} (execution): invalid status '{node.status}'")

    for edge in graph.edges:
        if edge.source not in node_ids:
            errors.append(f"Edge from '{edge.source}' references non-existent node")
        if edge.target not in node_ids:
            errors.append(f"Edge to '{edge.target}' references non-existent node")

    if _has_cycle(graph):
        errors.append("DAG graph contains a cycle")

    return errors


def _has_cycle(graph) -> bool:
    adj: dict[str, list[str]] = {n.id: [] for n in graph.nodes}
    for e in graph.edges:
        if e.source in adj:
            adj[e.source].append(e.target)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in adj}

    def dfs(u: str) -> bool:
        color[u] = GRAY
        for v in adj.get(u, []):
            if color.get(v) == GRAY:
                return True
            if color.get(v) == WHITE and dfs(v):
                return True
        color[u] = BLACK
        return False

    return any(color[nid] == WHITE and dfs(nid) for nid in adj)
