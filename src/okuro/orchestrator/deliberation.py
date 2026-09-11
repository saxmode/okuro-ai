# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: DAG-based deliberation engine — position → discussion → execution lifecycle
# index:
#   imports
#   def get_ready_nodes
#   def is_graph_complete
#   def is_graph_blocked
#   def advance_discussion_state
#   def create_position_nodes
#   def create_discussion_node
#   def create_execution_nodes
#   def resolve_discussion
#   def start_recouncil
#   def add_role_to_discussion
#   def remove_role_from_discussion
#   def build_execution_brief
#   def get_authority_map
#   def propose_panel
# AGENT_HEADER_END -->
"""Okuro Orchestrator Deliberation Engine — DAG-based deliberation system."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from okuro.orchestrator.state import (
    DAGGraph, DAGNode, DAGEdge, Assignment, Deliberation, Phase, Subtask,
    Task, update_node, save_plan, save_task_meta, append_log,
)

logger = logging.getLogger("okuro.orchestrator.deliberation")


# ── DAG Walker ─────────────────────────────────────────────────────────────


# Gate 2 §C3 / §C5 — node statuses, split on the same two axes as the
# plan-side sets in state.py (SATISFIED_STATUSES / FINISHED_STATUSES). The two
# readers mirror each other by contract, so the split must land on both or
# graph-side and plan-side dispatch diverge on a failed node.
#
# SATISFIED — predecessor satisfaction. `failed` absent: a successor must not
# run on a failed input.
# FINISHED — graph completion. `failed` present: a failed node is no longer
# being worked on.
_SATISFIED_NODE_STATUSES = frozenset(
    {"done", "skipped", "dismissed", "resolved", "superseded"}
)
_FINISHED_NODE_STATUSES = _SATISFIED_NODE_STATUSES | frozenset({"failed"})
_HALT_TASK_STATUSES = frozenset({"halted", "failed", "blocked", "done"})
_BLOCKED_PHASE_STATUSES = frozenset({"blocked_review"})
# Every awaiting kind currently emitted by the engine halts *all*
# sibling dispatch — the user has work to do before progress resumes.
# Keep this frozen here (not in state.py) so the dispatch guard owns
# the policy and FE projections that consume `is_dispatchable` agree
# bit-for-bit with the work loop.
_HALT_AWAITING_KINDS = frozenset(
    {"blocked_review", "panel_confirmation", "capability_gap",
     "discussion_proceed", "decision_gate", "timeout_cap",
     "subtask_approval"}
)
# Default retries cap when the caller has no config in scope. Re-exported
# from the canonical source (config.DEFAULT_MAX_RETRIES) so library/test
# callers without config in scope match a configured run bit-for-bit.
# state.get_ready_subtasks imports this symbol — keep the name stable.
from okuro.orchestrator.config import DEFAULT_MAX_RETRIES as _DEFAULT_MAX_RETRIES


def _task_halt_reason(task: Optional[Task]) -> Optional[str]:
    """Class-level halts that block EVERY sibling dispatch.

    Returns a short stable token (used for logging / telemetry) when
    the task is in a halt state, or ``None`` when dispatch may proceed
    on a per-node basis. Shared by ``is_dispatchable`` (graph-side) and
    ``get_ready_subtasks`` (plan-side) so the two readers cannot drift
    apart on the awaiting / halted axes.
    """
    if task is None:
        return None
    if task.status in _HALT_TASK_STATUSES:
        return f"task_status:{task.status}"
    awaiting = getattr(task, "awaiting", None)
    if awaiting is not None and getattr(awaiting, "kind", "") in _HALT_AWAITING_KINDS:
        return f"task_awaiting:{awaiting.kind}"
    return None


def is_dispatchable(
    task: Optional[Task], node: DAGNode, *, max_retries: Optional[int] = None,
) -> tuple[bool, str]:
    """Composite dispatch guard — single source of truth.

    Gate 2 §C3 / §C5 — every dispatch site (auto-execute + deliberate
    work loop, plus the FE's ready-projection) consumes this predicate.
    Returns ``(True, "")`` when the node is dispatchable, or
    ``(False, <reason>)`` when not. ``reason`` is a short stable token
    suitable for logging and telemetry.

    The predicate enforces the full halt set in one place:
      * node-local: ``node.status == "pending"``
      * task-level: ``task.status`` not in halt set; ``task.awaiting``
        kind not in the halt-awaiting set
      * execution-node: parent phase not in ``blocked_review``; subtask
        retries strictly below the cap

    ``max_retries`` defaults to ``_DEFAULT_MAX_RETRIES``
    (config.DEFAULT_MAX_RETRIES) when the
    caller has no config — matches the reviewer/pipeline fallback so the
    cap is enforced even in library / test callers that hold only a
    graph + task.
    """
    # Node-local checks: type must be dispatchable, status must be
    # pending. Position / discussion nodes share the same pending →
    # dispatched flow.
    if node.status != "pending":
        return False, f"node_status:{node.status}"

    # Task-level halts: caller may not have a task (legacy paths) —
    # only enforce when present.
    if task is not None:
        halt = _task_halt_reason(task)
        if halt is not None:
            return False, halt

        # Execution nodes inherit their parent phase's gating. The SoT
        # is the subtask row; the graph node's status was derived from
        # it at the last save (see state._apply_execution_node_derivation).
        if node.type == "execution":
            owner_phase: Optional[Phase] = None
            owner_subtask: Optional[Subtask] = None
            for phase in task.phases:
                for st in phase.subtasks:
                    if st.id == node.id:
                        owner_phase = phase
                        owner_subtask = st
                        break
                if owner_phase is not None:
                    break

            if owner_phase is not None and owner_phase.status in _BLOCKED_PHASE_STATUSES:
                return False, f"phase_status:{owner_phase.status}"

            # Explicit dispatch lock. A subtask whose review could not settle
            # must not be re-dispatched until the human resolves it — but it is
            # `done` on the work axis, so this guard is what holds it, NOT a
            # forged `failed` status. `overridden` deliberately does not lock:
            # the human already resolved it and the run continues.
            if (
                owner_subtask is not None
                and getattr(owner_subtask, "review_state", "") == "failed"
            ):
                return False, "review_state:failed"

            # Retry invariant — mirror state.get_ready_subtasks exactly:
            # dispatch the final correction attempt at retries == cap; the
            # cap branch in handle_failure escalates on its FAIL. Refuse
            # only ABOVE the cap (defensive backstop). See the long note
            # in state.get_ready_subtasks for the full lifecycle.
            cap = int(max_retries) if max_retries is not None else _DEFAULT_MAX_RETRIES
            if (
                owner_subtask is not None
                and int(getattr(owner_subtask, "retries", 0) or 0) > cap
            ):
                return False, "retries_cap"

    return True, ""


def get_ready_nodes(
    graph: DAGGraph, task: Optional[Task] = None, *, max_retries: Optional[int] = None,
) -> list[DAGNode]:
    """Find all nodes whose dependencies are satisfied.

    Gate 2 §C3 enforcement: this function reads ``node.status`` — which
    is now the derived view written by state._apply_execution_node_derivation
    — so a blocked_review phase already excludes its successors from
    predecessor satisfaction by construction.

    Gate 2 §C5 enforcement: when ``task`` is provided the function applies
    the full ``is_dispatchable`` composite guard so awaiting kinds /
    task.status / retries cap participate at the same site. A task-level
    halt short-circuits the whole loop — no sibling dispatch is permitted.
    Legacy callers that pass ``graph`` only retain today's behavior on
    the node-local + predecessor axes.
    """
    # Task-level halts short-circuit the entire ready set. A halted task
    # or active awaiting (any halt-set kind) means dispatch is refused
    # for every node — checking node-local predicates would be wasted
    # work and could mask the structural halt in test output.
    if task is not None and _task_halt_reason(task) is not None:
        return []

    status_map = {n.id: n.status for n in graph.nodes}

    ready = []
    for node in graph.nodes:
        if node.status != "pending":
            continue
        predecessors = graph.get_predecessors(node.id)
        if not all(status_map.get(p) in _SATISFIED_NODE_STATUSES for p in predecessors):
            continue
        if task is not None:
            ok, _ = is_dispatchable(task, node, max_retries=max_retries)
            if not ok:
                continue
        ready.append(node)
    return ready


def is_graph_complete(graph: DAGGraph) -> bool:
    return all(n.status in _FINISHED_NODE_STATUSES for n in graph.nodes)


def is_graph_blocked(graph: DAGGraph) -> bool:
    if is_graph_complete(graph):
        return False
    if get_ready_nodes(graph):
        return False
    waiting_states = frozenset({"awaiting_user", "positions_running", "running"})
    return not any(n.status in waiting_states for n in graph.nodes)


# ── Discussion Node State Machine ──────────────────────────────────────────


def advance_discussion_state(graph: DAGGraph, discussion_id: str) -> Optional[str]:
    """Advance a discussion node's state based on its input positions."""
    node = graph.get_node(discussion_id)
    if not node or node.type != "discussion":
        return None

    input_nodes = [graph.get_node(nid) for nid in graph.get_predecessors(discussion_id)]
    input_nodes = [n for n in input_nodes if n is not None]
    if not input_nodes:
        return None

    terminal = frozenset({"done", "skipped", "dismissed"})

    if node.status in ("proposed", "positions_running"):
        if all(n.status in terminal for n in input_nodes):
            node.status = "awaiting_user"
            return "awaiting_user"

    if node.status == "proposed":
        if any(n.status in ("running", "done") for n in input_nodes):
            node.status = "positions_running"
            return "positions_running"

    return None


# ── Graph Construction ─────────────────────────────────────────────────────


def create_position_nodes(graph: DAGGraph, roles: list[str], round_num: int = 1,
                          context_from: str = "", sources: Optional[dict[str, str]] = None,
                          directive: str = "",
                          ) -> tuple[list[DAGNode], str]:
    """Create position nodes for a set of roles + a discussion node.

    ``directive`` carries a round-specific subject for the panel. Left empty
    the panel deliberates ``task.description`` (initial council). Set to the
    continuation instructions it deliberates THAT new work instead — this is
    how the deliberation-on-continuation toggle re-runs the council for a
    follow-up intervention. Stored on each node's ``description`` (round-trips
    through the graph serializer) and surfaced by ``build_position_prompt``.
    """
    sources = sources or {}
    prefix = f"d{round_num}"
    position_nodes = []

    for i, role in enumerate(roles, 1):
        node_id = f"{prefix}.{i}"
        node = DAGNode(
            id=node_id, type="position", role=role,
            source=sources.get(role, "proposed"),
            round=round_num, context_from=context_from,
            description=directive,
        )
        graph.add_node(node)
        position_nodes.append(node)

    discuss_id = f"{prefix}.discuss"
    discuss_node = DAGNode(
        id=discuss_id, type="discussion", status="proposed",
        round=round_num, inputs=[n.id for n in position_nodes],
        inherits=context_from, description=directive,
    )
    graph.add_node(discuss_node)

    for pn in position_nodes:
        graph.add_edge(pn.id, discuss_id)

    return position_nodes, discuss_id


def create_discussion_node(graph: DAGGraph, input_ids: list[str], round_num: int = 1) -> DAGNode:
    discuss_id = f"d{round_num}.discuss"
    node = DAGNode(id=discuss_id, type="discussion", status="proposed",
                   round=round_num, inputs=input_ids)
    graph.add_node(node)
    for inp in input_ids:
        graph.add_edge(inp, discuss_id)
    return node


def create_execution_nodes(graph: DAGGraph, discussion_id: str,
                           subtasks: list[dict]) -> list[DAGNode]:
    """Create execution nodes downstream of a resolved discussion."""
    nodes = []
    for st in subtasks:
        node = DAGNode(
            id=st["id"], type="execution", role=st.get("role", ""),
            description=st.get("description", ""),
            authority_from=discussion_id,
            risk=st.get("risk", "LOW"), complexity=st.get("complexity", "standard"),
            artifact_name=st.get("artifact_name", ""),
        )
        graph.add_node(node)
        graph.add_edge(discussion_id, node.id)
        nodes.append(node)

    for st in subtasks:
        for dep_id in st.get("dependencies", []):
            graph.add_edge(dep_id, st["id"])

    return nodes


def attach_capability_gap_phase0(
    graph: DAGGraph, gap_kind: str, phase0: list[dict],
) -> str:
    """Attach Phase-0 creator subtasks under a synthetic resolved-discussion
    node so the engine's discussion→execution invariants still hold.

    Shared by BOTH the ``/capability-gap/accept`` REST endpoint and the
    in-process autopilot accept (DP10 — one mechanism, no HTTP-to-self). The
    synthetic parent uses ``status='resolved'`` so the engine's auto-decompose
    branch does not re-run decompose_task on it. Idempotent on the discussion
    node id. Returns that id.
    """
    from okuro.orchestrator.state import DAGNode

    discuss_id = f"capability-gap-{gap_kind or 'unknown'}-discuss"
    if graph.get_node(discuss_id) is None:
        graph.add_node(
            DAGNode(id=discuss_id, type="discussion", status="resolved",
                    round=0, inputs=[])
        )
    create_execution_nodes(graph, discuss_id, phase0)
    return discuss_id


# ── Discussion Resolution ──────────────────────────────────────────────────


def resolve_discussion(task: Task, discussion_id: str, assignments: list[dict],
                       user_statement: str, tasks_dir: Path) -> DAGNode:
    if not task.graph:
        raise ValueError("Task has no DAG graph")
    node = task.graph.get_node(discussion_id)
    if not node:
        raise ValueError(f"Discussion node {discussion_id} not found")
    if node.type != "discussion":
        raise ValueError(f"Node {discussion_id} is not a discussion node")
    if node.status not in ("awaiting_user", "positions_running"):
        raise ValueError(f"Discussion {discussion_id} is '{node.status}', expected 'awaiting_user'")

    node.assignments = [
        Assignment(role=a["role"], action=a["action"],
                   leads=a.get("leads", ""), reasoning=a.get("reasoning", ""))
        for a in assignments
    ]
    node.user_statement = user_statement
    node.status = "resolved"

    save_plan(task, tasks_dir)
    append_log(task.id, {
        "type": "discussion_resolved", "node": discussion_id,
        "assignments": len(assignments),
    }, tasks_dir)
    return node


# ── Re-Council ─────────────────────────────────────────────────────────────


def start_recouncil(task: Task, discussion_id: str, roles: list[str],
                    tasks_dir: Path, sources: Optional[dict[str, str]] = None,
                    ) -> tuple[list[DAGNode], str]:
    if not task.graph:
        raise ValueError("Task has no DAG graph")
    old_node = task.graph.get_node(discussion_id)
    if not old_node:
        raise ValueError(f"Discussion node {discussion_id} not found")

    old_node.status = "superseded"
    new_round = old_node.round + 1
    if task.deliberation:
        task.deliberation.current_round = new_round

    position_nodes, new_discuss_id = create_position_nodes(
        task.graph, roles=roles, round_num=new_round,
        context_from=discussion_id, sources=sources,
    )
    save_plan(task, tasks_dir)
    save_task_meta(task, tasks_dir)
    append_log(task.id, {
        "type": "recouncil_started", "old_discussion": discussion_id,
        "new_discussion": new_discuss_id, "round": new_round, "roles": roles,
    }, tasks_dir)
    return position_nodes, new_discuss_id


# ── Mid-Deliberation Role Changes ─────────────────────────────────────────


def add_role_to_discussion(task: Task, discussion_id: str, role: str,
                           tasks_dir: Path) -> DAGNode:
    if not task.graph:
        raise ValueError("Task has no DAG graph")
    discuss = task.graph.get_node(discussion_id)
    if not discuss or discuss.type != "discussion":
        raise ValueError(f"Discussion node {discussion_id} not found")

    existing = [n for n in task.graph.nodes
                if n.type == "position" and n.id.startswith(f"d{discuss.round}.")]
    next_idx = len(existing) + 1
    node_id = f"d{discuss.round}.{next_idx}"

    position = DAGNode(
        id=node_id, type="position", role=role, source="user-added",
        round=discuss.round, context_from=discuss.inherits,
    )
    task.graph.add_node(position)
    task.graph.add_edge(node_id, discussion_id)
    discuss.inputs.append(node_id)
    discuss.status = "positions_running"

    save_plan(task, tasks_dir)
    append_log(task.id, {
        "type": "role_added_to_discussion", "discussion": discussion_id,
        "role": role, "node": node_id,
    }, tasks_dir)
    return position


def remove_role_from_discussion(task: Task, discussion_id: str, role: str,
                                tasks_dir: Path) -> Optional[str]:
    if not task.graph:
        raise ValueError("Task has no DAG graph")
    discuss = task.graph.get_node(discussion_id)
    if not discuss or discuss.type != "discussion":
        raise ValueError(f"Discussion node {discussion_id} not found")

    for nid in discuss.inputs:
        node = task.graph.get_node(nid)
        if node and node.type == "position" and node.role == role:
            node.status = "dismissed"
            save_plan(task, tasks_dir)
            append_log(task.id, {
                "type": "role_removed_from_discussion", "discussion": discussion_id,
                "role": role, "node": nid,
            }, tasks_dir)
            return nid
    return None


# ── Execution Brief ────────────────────────────────────────────────────────


def build_execution_brief(graph: DAGGraph, discussion_id: str) -> str:
    node = graph.get_node(discussion_id)
    if not node or node.type != "discussion":
        return ""

    lines = ["## Deliberation Brief\n"]
    for a in node.assignments:
        if a.action == "assign":
            lines.append(f"**AUTHORITY:** {a.role} leads {a.leads}")
            lines.append(f"  Reasoning: {a.reasoning}")
            for pn in (graph.get_node(nid) for nid in node.inputs):
                if pn and pn.role == a.role and pn.artifact:
                    lines.append(f"  Position artifact: {pn.artifact}")
            lines.append("")
        elif a.action == "acknowledge":
            lines.append(f"**CONTEXT:** {a.role} — {a.reasoning}\n")

    if node.user_statement:
        lines.append(f"**USER DIRECTION:** {node.user_statement}\n")

    if node.inherits:
        prev = graph.get_node(node.inherits)
        if prev and prev.user_statement:
            lines.append(f"**PREVIOUS ROUND ({prev.round}) DIRECTION:** {prev.user_statement}\n")

    return "\n".join(lines)


def get_authority_map(graph: DAGGraph) -> list[dict]:
    discussions = [n for n in graph.nodes if n.type == "discussion" and n.status == "resolved"]
    if not discussions:
        return []
    latest = max(discussions, key=lambda n: n.round)
    return [
        {"role": a.role, "action": a.action, "leads": a.leads,
         "reasoning": a.reasoning, "round": latest.round}
        for a in latest.assignments
    ]


# ── Panel Proposal ─────────────────────────────────────────────────────────


def propose_panel(task_description: str, max_roles: int = 5) -> list[dict]:
    """Propose a panel of roles for deliberation.

    Returns the resolver's ranked matches passed through with the UI
    aliases existing consumers expect (``role_id``, ``why``). Items
    below the similarity threshold are returned tagged
    ``match_type="fallback"`` so the engine can detect a capability gap
    and escalate to role creation instead of stalling on an empty panel.

    Resolver failures are NOT swallowed — a broken role index must
    surface loudly. Returning ``[]`` here means the index is genuinely
    empty (every candidate filtered out or vec_roles unseeded), which is
    itself a capability gap the engine must handle.
    """
    from okuro.roles.resolver import match_roles
    # panel_only: this IS the deliberation panel — the one caller the
    # panel_eligible flag was written for (resolver.match_roles).
    matches = match_roles(task_description, top_k=max_roles, panel_only=True)
    return [
        {
            "role_id": m["id"],
            "domain": m.get("domain", ""),
            "description": m.get("description", ""),
            "similarity": m.get("similarity", 0.0),
            "match_type": m.get("match_type", "fallback"),
            "why": m.get("description", ""),
        }
        for m in matches
    ]
