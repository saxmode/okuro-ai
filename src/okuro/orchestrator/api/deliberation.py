# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Deliberation API — REST endpoints for DAG-based deliberation in okuro orchestrator.
# index:
#   imports
#   class PanelRole
#   class PanelConfirmRequest
#   class PanelAddRequest
#   class PanelRemoveRequest
#   class AssignmentRequest
#   class DiscussionAssignRequest
#   class StatementRequest
#   class RecouncilRequest
#   class PositionSummary
#   class DiscussionState
#   class AuthorityEntry
#   class GraphResponse
#   def _load_task
#   def _require_deliberation
#   def _extract_claim
# AGENT_HEADER_END -->
"""Deliberation API — REST endpoints for DAG-based deliberation in okuro orchestrator.

CLI-testable before the web UI ships. All endpoints work with curl/httpie.
"""

import logging
import os
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from okuro.db.engine import okuro_home

logger = logging.getLogger("okuro.orchestrator.api.deliberation")

# Resolve paths — must match main.py (~/.okuro/orchestrator). The previous
# fallback resolved to the source tree (src/okuro/orchestrator/) which
# doesn't contain a tasks/ dir, so every deliberation endpoint returned
# "Task not found" for tasks that actually existed on disk.
OKURO_ROOT = Path(os.environ.get("OKURO_ROOT", okuro_home() / "orchestrator"))
TASKS_DIR = OKURO_ROOT / "tasks"

# Lazy imports to avoid circular deps — deliberation.py imports state.py
# which the API also uses. We import at call time.


# ── Pydantic Models ────────────────────────────────────────────────────────


class PanelRole(BaseModel):
    role_id: str
    domain: str = ""
    description: str = ""
    why: str = ""
    similarity: float = 0.0
    # match_type: "matched" if similarity >= threshold, else "fallback".
    # The UI uses this to flag a capability gap (all-fallback panel) and
    # surface the gap-acceptance card instead of dispatching the panel.
    match_type: str = "fallback"


class CapabilityGapResponse(BaseModel):
    task_id: str
    kind: str
    summary: str
    creator_roles: List[str]
    slot_descriptions: List[str]
    payload: dict
    phase0: List[dict]
    status: str
    created_at: str


class CapabilityGapAcceptRequest(BaseModel):
    # Optional override — let the user trim creator_roles before accepting
    # (e.g. drop role-designer if they only want research).
    creator_roles: Optional[List[str]] = None


class PanelConfirmRequest(BaseModel):
    roles: List[str]
    strategy: str = Field(default="parallel", pattern="^(parallel|sequential|debate)$")


class PanelAddRequest(BaseModel):
    role: str


class PanelRemoveRequest(BaseModel):
    role: str


class AssignmentRequest(BaseModel):
    role: str
    action: str = Field(pattern="^(assign|acknowledge|dismiss)$")
    leads: str = ""
    reasoning: str = ""

    @field_validator("leads")
    @classmethod
    def _leads_required_when_assign(cls, v: str, info):
        action = info.data.get("action")
        if action == "assign" and not (v or "").strip():
            raise ValueError(
                "leads must be non-empty when action='assign'. "
                "Set leads to the domain this role leads, "
                "or use action='acknowledge' instead."
            )
        return v


class DiscussionAssignRequest(BaseModel):
    assignments: List[AssignmentRequest]


class StatementRequest(BaseModel):
    statement: str


class RecouncilRequest(BaseModel):
    roles: Optional[List[str]] = None  # None = reuse previous panel


class PositionSummary(BaseModel):
    node_id: str
    role: str
    status: str
    source: str
    round: int
    artifact: str = ""
    claim: str = ""           # CLAIM section, full
    reasoning: str = ""       # REASONING / KEY POINTS section (capped)
    risks: str = ""           # RISKS section (capped)
    recommendation: str = ""  # RECOMMENDATION / NEXT STEPS section (capped)


class DiscussionState(BaseModel):
    node_id: str
    status: str
    round: int
    positions: List[PositionSummary]
    assignments: List[dict] = []
    user_statement: str = ""
    inherits: str = ""


class AuthorityEntry(BaseModel):
    role: str
    action: str
    leads: str = ""
    reasoning: str = ""
    round: int = 1


class GraphResponse(BaseModel):
    nodes: List[dict]
    edges: List[dict]


# ── Router ─────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/tasks", tags=["deliberation"])


def _load_task(task_id: str):
    """Load task with validation."""
    from okuro.orchestrator.state import load_task
    task_path = TASKS_DIR / task_id
    if not task_path.exists():
        raise HTTPException(404, f"Task not found: {task_id}")
    return load_task(task_id, TASKS_DIR)


def _require_deliberation(task):
    """Ensure task is in deliberation mode."""
    if task.mode != "deliberate":
        raise HTTPException(400, f"Task {task.id} is in '{task.mode}' mode, not 'deliberate'")
    if not task.graph:
        raise HTTPException(400, f"Task {task.id} has no DAG graph")


def _ensure_engine_running(task_id: str) -> Optional[int]:
    """Spawn the orchestrator engine for ``task_id`` if it isn't already.

    Deliberation-mode tasks exit the engine process whenever the loop
    has no work to do (e.g. waiting for panel confirmation or for a
    capability gap to be accepted). After the user mutates state via
    an API endpoint, nothing else respawns the engine — so every
    state-changing endpoint MUST call this helper to pick up where the
    loop left off. Idempotent: returns ``None`` when an engine is
    already alive for the task.

    Returns the new pid on spawn, ``None`` when no spawn was needed.
    """
    import sys
    from okuro.orchestrator.api.main import (
        _is_orchestrator_running,
        spawn_orchestrator,
    )
    if _is_orchestrator_running(task_id):
        return None
    cmd = [
        sys.executable,
        "-m", "okuro.orchestrator.engine",
        "--resume", task_id,
    ]
    pid = spawn_orchestrator(cmd, task_id)
    if not pid:
        raise HTTPException(500, "Failed to respawn task orchestrator")
    return pid


def _extract_claim(task_id: str, artifact_path: str) -> str:
    """Backwards-compatible: returns the CLAIM section only (used by some callers).
    Prefer _extract_sections for richer surfacing.
    """
    return _extract_sections(task_id, artifact_path).get("claim", "")


# Section title → field name mapping. Aliases accommodate role-specific
# variations in the position-template the agents follow.
_SECTION_ALIASES = {
    "claim": ("CLAIM",),
    "reasoning": ("REASONING", "KEY POINTS", "ANALYSIS", "FINDINGS"),
    "risks": ("RISKS", "TRADE-OFFS", "TRADEOFFS", "CONSTRAINTS"),
    "recommendation": ("RECOMMENDATION", "RECOMMENDATIONS", "NEXT STEPS", "DECISION"),
}

_FIELD_CAPS = {"claim": 1200, "reasoning": 1400, "risks": 1000, "recommendation": 1000}


def _extract_sections(task_id: str, artifact_path: str) -> dict:
    """Parse a position artifact and pull out structured sections so the
    dashboard can surface key points per role inline (no click-through).

    Returns a dict with keys: claim, reasoning, risks, recommendation. Each
    value is capped per _FIELD_CAPS to keep API payload bounded.
    """
    result = {k: "" for k in _SECTION_ALIASES}
    if not artifact_path:
        return result
    full_path = TASKS_DIR / task_id / artifact_path
    if not full_path.exists():
        return result
    try:
        text = full_path.read_text()
    except Exception:
        return result

    # First pass: collect raw section contents keyed by their normalized title.
    sections: dict[str, list[str]] = {}
    current_title: str | None = None
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            current_title = stripped.removeprefix("## ").strip().upper()
            sections.setdefault(current_title, [])
            continue
        if current_title is not None and stripped:
            sections[current_title].append(line.rstrip())

    # Second pass: map aliases → field, capped.
    for field, aliases in _SECTION_ALIASES.items():
        for alias in aliases:
            if alias in sections:
                blob = "\n".join(sections[alias]).strip()
                result[field] = blob[: _FIELD_CAPS[field]]
                break

    return result


# ── Panel Endpoints ────────────────────────────────────────────────────────


@router.get("/{task_id}/proposed-panel", response_model=List[PanelRole])
async def get_proposed_panel(task_id: str):
    """Get orchestrator's suggested roles for deliberation."""
    task = _load_task(task_id)
    if task.mode != "deliberate":
        raise HTTPException(400, "Task is not in deliberation mode")

    from okuro.orchestrator.deliberation import propose_panel
    suggestions = propose_panel(task.description)

    # Force-include roles the user EXPLICITLY demanded. The deliberate flow
    # always confirms a panel before gathering positions, so a demanded role
    # must appear here (pre-matched) or it never enters the process — the
    # exact failure the named-role gap was built to fix. We surface them even
    # if semantic match would rank them sub-threshold (or skip a fresh draft),
    # because the user's demand is the authority, not the embedding score.
    demanded = _demanded_role_ids(task_id)
    if demanded:
        from okuro.roles.registry import get_role
        present = {s.get("role_id") for s in suggestions}
        forced = []
        for rid in demanded:
            if rid in present or get_role(rid) is None:
                continue
            info = get_role(rid) or {}
            forced.append({
                "role_id": rid,
                "domain": info.get("domain", "") if isinstance(info, dict) else "",
                "description": info.get("description", "") if isinstance(info, dict) else "",
                "why": "explicitly demanded in the task prompt",
                "similarity": 1.0,
                "match_type": "matched",
            })
        suggestions = forced + suggestions
    return [PanelRole(**s) for s in suggestions]


def _demanded_role_ids(task_id: str) -> list[str]:
    """Role ids the user explicitly demanded, from a named-role capability gap.

    Returns [] unless ``capability_gap.json`` exists with
    ``payload.trigger == "named_role_demand"``. Order-preserving, deduped.
    """
    import json
    gap_file = TASKS_DIR / task_id / "capability_gap.json"
    if not gap_file.exists():
        return []
    try:
        gap = json.loads(gap_file.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if (gap.get("payload") or {}).get("trigger") != "named_role_demand":
        return []
    out: list[str] = []
    for name in gap.get("slot_descriptions") or []:
        if name and name not in out:
            out.append(name)
    return out


@router.get("/{task_id}/capability-gap", response_model=CapabilityGapResponse)
async def get_capability_gap(task_id: str):
    """Return the capability_gap descriptor written by the engine, if any.

    The engine writes ``capability_gap.json`` into the task directory when
    the panel proposer cannot find any matched role. The frontend reads
    this endpoint to render an approval card showing the missing-role
    summary and the Phase 0 plan that will be executed on accept.
    """
    import json
    _load_task(task_id)  # validates existence + permissions
    gap_file = TASKS_DIR / task_id / "capability_gap.json"
    if not gap_file.exists():
        raise HTTPException(404, f"No capability gap for task {task_id}")
    try:
        payload = json.loads(gap_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"Failed to read capability_gap.json: {exc}")
    return CapabilityGapResponse(**payload)


@router.post("/{task_id}/capability-gap/accept")
async def accept_capability_gap(
    task_id: str, body: CapabilityGapAcceptRequest | None = None,
):
    """Accept the gap plan and inject Phase 0 subtasks into the DAG.

    Creates a synthetic resolved-discussion node so the existing
    discussion→execution invariants of the engine still hold, then
    attaches Phase 0 execution nodes (role-researcher → role-designer)
    underneath it. The engine's deliberation loop picks them up on its
    next tick.
    """
    import json
    from okuro.orchestrator.state import (
        DAGGraph, Deliberation, save_plan, save_task_meta,
    )

    task = _load_task(task_id)
    if task.mode != "deliberate":
        raise HTTPException(400, "Task is not in deliberation mode")

    gap_file = TASKS_DIR / task_id / "capability_gap.json"
    if not gap_file.exists():
        raise HTTPException(404, "No capability gap to accept")
    try:
        gap = json.loads(gap_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"Failed to read capability_gap.json: {exc}")

    if gap.get("status") == "accepted":
        raise HTTPException(409, "Capability gap already accepted")

    phase0 = gap.get("phase0") or []
    if body and body.creator_roles:
        allowed = set(body.creator_roles)
        phase0 = [p for p in phase0 if p["role"] in allowed]
        if not phase0:
            raise HTTPException(
                400,
                "creator_roles override rejected every Step 0 part",
            )

    if not task.graph:
        task.graph = DAGGraph()
    if not task.deliberation:
        task.deliberation = Deliberation()

    # Synthetic resolved-discussion parent so the engine's
    # discussion→execution invariants still hold. status='resolved' keeps
    # the engine's auto-decompose branch (engine.py around line 1152)
    # from re-running decompose_task on this node. Shared with the in-process
    # autopilot accept (DP10 — one mechanism).
    from okuro.orchestrator.deliberation import attach_capability_gap_phase0
    discuss_id = attach_capability_gap_phase0(
        task.graph, gap.get("kind", "unknown"), phase0,
    )

    gap["status"] = "accepted"
    gap_file.write_text(json.dumps(gap, indent=2))

    from okuro.orchestrator.api.main import _clear_awaiting_resumable
    _clear_awaiting_resumable(task, TASKS_DIR, new_status="deliberating")
    save_plan(task, TASKS_DIR)

    pid = _ensure_engine_running(task_id)

    return {
        "status": "gap_accepted",
        "task_id": task_id,
        "phase0_node_ids": [p["id"] for p in phase0],
        "discussion_id": discuss_id,
        "engine_pid": pid,
    }


@router.post("/{task_id}/panel")
async def confirm_panel(task_id: str, body: PanelConfirmRequest):
    """Confirm the role panel and start position gathering.

    Creates position nodes + discussion node in the DAG.
    Sets task status to 'deliberating'.
    """
    from okuro.orchestrator.state import save_plan, save_task_meta
    from okuro.orchestrator.deliberation import create_position_nodes

    task = _load_task(task_id)
    if task.mode != "deliberate":
        raise HTTPException(400, "Task is not in deliberation mode")

    if not task.graph:
        from okuro.orchestrator.state import DAGGraph, Deliberation
        task.graph = DAGGraph()
        task.deliberation = Deliberation()

    task.deliberation.strategy = body.strategy
    task.deliberation.panel = body.roles

    # Re-confirming an in-flight panel (decomposer's draft was wrong, user
    # corrects via UI) must wipe the previous round-1 nodes — otherwise stale
    # roles linger and corrupt the discussion. Idempotent: harmless when the
    # graph is empty on first confirm.
    stale_ids = [
        n.id for n in list(task.graph.nodes)
        if int(getattr(n, "round", 1) or 1) == 1
        and n.type in ("position", "discussion")
        and n.status in ("pending", "proposed")
    ]
    for nid in stale_ids:
        task.graph.remove_node(nid)

    position_nodes, discuss_id = create_position_nodes(
        task.graph, roles=body.roles, round_num=1,
    )

    from okuro.orchestrator.api.main import _clear_awaiting_resumable
    _clear_awaiting_resumable(task, TASKS_DIR, new_status="deliberating")
    save_plan(task, TASKS_DIR)

    pid = _ensure_engine_running(task_id)

    return {
        "status": "panel_confirmed",
        "task_id": task_id,
        "discussion_node": discuss_id,
        "position_nodes": [n.id for n in position_nodes],
        "strategy": body.strategy,
        "engine_pid": pid,
    }


@router.post("/{task_id}/panel/add")
async def add_panel_role(task_id: str, body: PanelAddRequest):
    """Add a role mid-deliberation."""
    from okuro.orchestrator.deliberation import add_role_to_discussion

    task = _load_task(task_id)
    _require_deliberation(task)

    # Find the current (non-superseded) discussion node
    discussions = [n for n in task.graph.nodes
                   if n.type == "discussion" and n.status not in ("superseded",)]
    if not discussions:
        raise HTTPException(400, "No active discussion node found")

    discuss = discussions[-1]
    position = add_role_to_discussion(task, discuss.id, body.role, TASKS_DIR)
    pid = _ensure_engine_running(task_id)

    return {
        "status": "role_added",
        "discussion_node": discuss.id,
        "position_node": position.id,
        "role": body.role,
        "engine_pid": pid,
    }


@router.post("/{task_id}/panel/remove")
async def remove_panel_role(task_id: str, body: PanelRemoveRequest):
    """Remove/dismiss a role from the current discussion."""
    from okuro.orchestrator.deliberation import remove_role_from_discussion

    task = _load_task(task_id)
    _require_deliberation(task)

    discussions = [n for n in task.graph.nodes
                   if n.type == "discussion" and n.status not in ("superseded",)]
    if not discussions:
        raise HTTPException(400, "No active discussion node found")

    discuss = discussions[-1]
    dismissed_id = remove_role_from_discussion(task, discuss.id, body.role, TASKS_DIR)

    if not dismissed_id:
        raise HTTPException(404, f"Role '{body.role}' not found in discussion {discuss.id}")

    pid = _ensure_engine_running(task_id)

    return {
        "status": "role_dismissed",
        "discussion_node": discuss.id,
        "dismissed_node": dismissed_id,
        "role": body.role,
        "engine_pid": pid,
    }


# ── Position Endpoints ─────────────────────────────────────────────────────


@router.get("/{task_id}/positions", response_model=List[PositionSummary])
async def get_positions(task_id: str, round: Optional[int] = None):
    """Get all position artifacts for the current (or specified) round."""
    task = _load_task(task_id)
    _require_deliberation(task)

    target_round = round or (task.deliberation.current_round if task.deliberation else 1)

    positions = []
    for node in task.graph.nodes:
        if node.type != "position":
            continue
        if node.round != target_round:
            continue
        positions.append(PositionSummary(
            node_id=node.id,
            role=node.role,
            status=node.status,
            source=node.source,
            round=node.round,
            artifact=node.artifact,
            **_extract_sections(task_id, node.artifact),
        ))

    return positions


# ── Discussion Endpoints ───────────────────────────────────────────────────


@router.get("/{task_id}/discussion/{node_id}", response_model=DiscussionState)
async def get_discussion(task_id: str, node_id: str):
    """Get discussion node state with all connected positions."""
    task = _load_task(task_id)
    _require_deliberation(task)

    node = task.graph.get_node(node_id)
    if not node or node.type != "discussion":
        raise HTTPException(404, f"Discussion node {node_id} not found")

    # Gather positions feeding into this discussion
    positions = []
    for pred_id in task.graph.get_predecessors(node_id):
        pred = task.graph.get_node(pred_id)
        if pred and pred.type == "position":
            positions.append(PositionSummary(
                node_id=pred.id,
                role=pred.role,
                status=pred.status,
                source=pred.source,
                round=pred.round,
                artifact=pred.artifact,
                **_extract_sections(task_id, pred.artifact),
            ))

    return DiscussionState(
        node_id=node.id,
        status=node.status,
        round=node.round,
        positions=positions,
        assignments=[
            {"role": a.role, "action": a.action, "leads": a.leads, "reasoning": a.reasoning}
            for a in node.assignments
        ],
        user_statement=node.user_statement,
        inherits=node.inherits,
    )


@router.post("/{task_id}/discussion/{node_id}/assign")
async def submit_assignments(task_id: str, node_id: str, body: DiscussionAssignRequest):
    """Submit authority assignments for a discussion node (without resolving yet)."""
    from okuro.orchestrator.state import save_plan, Assignment

    task = _load_task(task_id)
    _require_deliberation(task)

    node = task.graph.get_node(node_id)
    if not node or node.type != "discussion":
        raise HTTPException(404, f"Discussion node {node_id} not found")

    node.assignments = [
        Assignment(
            role=a.role, action=a.action,
            leads=a.leads, reasoning=a.reasoning,
        )
        for a in body.assignments
    ]
    save_plan(task, TASKS_DIR)

    return {"status": "assignments_saved", "node": node_id, "count": len(body.assignments)}


@router.post("/{task_id}/discussion/{node_id}/statement")
async def submit_statement(task_id: str, node_id: str, body: StatementRequest):
    """Submit user's summary statement for a discussion node."""
    from okuro.orchestrator.state import save_plan

    task = _load_task(task_id)
    _require_deliberation(task)

    node = task.graph.get_node(node_id)
    if not node or node.type != "discussion":
        raise HTTPException(404, f"Discussion node {node_id} not found")

    node.user_statement = body.statement
    save_plan(task, TASKS_DIR)

    return {"status": "statement_saved", "node": node_id}


@router.post("/{task_id}/discussion/{node_id}/proceed")
async def proceed_discussion(task_id: str, node_id: str):
    """Resolve the discussion and allow execution to continue.

    Requires assignments + user statement to be set first.
    """
    from okuro.orchestrator.deliberation import resolve_discussion

    task = _load_task(task_id)
    _require_deliberation(task)

    node = task.graph.get_node(node_id)
    if not node or node.type != "discussion":
        raise HTTPException(404, f"Discussion node {node_id} not found")

    # P-COMM-2 — idempotent. A repeat /proceed (UI double-click, client retry)
    # on an already-resolved node previously reached resolve_discussion and
    # raised ValueError → HTTP 500. The engine already advanced; ACK cleanly.
    if node.status == "resolved":
        return {
            "status": "resolved",
            "node": node_id,
            "assignments": len(node.assignments),
            "already_resolved": True,
        }

    # Reject /proceed when any input position is still pending/running.
    # Without this, a premature dashboard click resolves the discussion
    # with zero authority signal and the engine decomposes blind. The DAG's
    # advance_discussion_state only flips to awaiting_user when every
    # position is terminal — that's the gate we enforce here. Done-position
    # roles are collected in the same pass for the assignment default below.
    terminal = frozenset({"done", "skipped", "dismissed"})
    pending_positions: list[str] = []
    done_roles: list[str] = []
    for pred_id in task.graph.get_predecessors(node_id):
        pred = task.graph.get_node(pred_id)
        if pred is None or pred.type != "position":
            continue
        if pred.status not in terminal:
            pending_positions.append(f"{pred.id} ({pred.status})")
        elif pred.status == "done" and pred.role:
            done_roles.append(pred.role)
    if pending_positions:
        raise HTTPException(
            409,
            "Cannot proceed — positions still running: "
            + ", ".join(pending_positions),
        )

    # Default assignments when none were set. The "Continue with deliberation"
    # banner (BlockerCard → GenericBlocker) POSTs an empty body — it has no
    # assignment UI — so without this it dead-ended on 400 "No assignments set"
    # while the rich DiscussionPanel (which auto-defaults every done position
    # to "acknowledge") worked. A bare Continue now means the same thing the
    # panel's default means: proceed with every position as CONTEXT, no role
    # leading a domain. Empty done_roles (no terminal positions at all) is the
    # only genuine no-authority case and still rejects.
    if not node.assignments:
        from okuro.orchestrator.state import Assignment, save_plan
        if not done_roles:
            raise HTTPException(
                400,
                "No assignments set and no completed positions to default from.",
            )
        node.assignments = [
            Assignment(role=r, action="acknowledge", leads="", reasoning="")
            for r in done_roles
        ]
        save_plan(task, TASKS_DIR)

    # user_statement is OPTIONAL — assignments alone are enough authority for
    # the executor to proceed. build_execution_brief handles empty statements.

    assignments_raw = [
        {"role": a.role, "action": a.action, "leads": a.leads, "reasoning": a.reasoning}
        for a in node.assignments
    ]

    resolve_discussion(task, node_id, assignments_raw, node.user_statement, TASKS_DIR)
    from okuro.orchestrator.api.main import _clear_awaiting_resumable
    # Post-deliberation: decompose + execute kicks in. Status flips to
    # 'active' so the UI badge stops showing DELIBERATING through execution.
    _clear_awaiting_resumable(task, TASKS_DIR, new_status="active")
    pid = _ensure_engine_running(task_id)

    return {
        "status": "resolved",
        "node": node_id,
        "assignments": len(assignments_raw),
        "engine_pid": pid,
    }


@router.post("/{task_id}/discussion/{node_id}/recouncil")
async def recouncil(task_id: str, node_id: str, body: Optional[RecouncilRequest] = None):
    """Start a new council round from a resolved discussion."""
    from okuro.orchestrator.deliberation import start_recouncil

    task = _load_task(task_id)
    _require_deliberation(task)

    node = task.graph.get_node(node_id)
    if not node or node.type != "discussion":
        raise HTTPException(404, f"Discussion node {node_id} not found")

    if node.status not in ("resolved", "awaiting_user"):
        raise HTTPException(400, f"Cannot re-council from status '{node.status}'")

    # Determine roles for new round
    if body and body.roles:
        roles = body.roles
    elif task.deliberation and task.deliberation.panel:
        roles = task.deliberation.panel
    else:
        raise HTTPException(400, "No roles specified and no previous panel found")

    position_nodes, new_discuss_id = start_recouncil(
        task, node_id, roles, TASKS_DIR,
    )
    pid = _ensure_engine_running(task_id)

    return {
        "status": "recouncil_started",
        "old_discussion": node_id,
        "new_discussion": new_discuss_id,
        "round": task.deliberation.current_round if task.deliberation else 2,
        "position_nodes": [n.id for n in position_nodes],
        "engine_pid": pid,
    }


# ── Authority + Graph Endpoints ────────────────────────────────────────────


@router.get("/{task_id}/authority-map", response_model=List[AuthorityEntry])
async def get_authority_map_endpoint(task_id: str):
    """Get the current domain→role authority map."""
    from okuro.orchestrator.deliberation import get_authority_map

    task = _load_task(task_id)
    _require_deliberation(task)

    authority = get_authority_map(task.graph)
    return [AuthorityEntry(**a) for a in authority]


@router.get("/{task_id}/graph", response_model=GraphResponse)
async def get_graph(task_id: str):
    """Get the full DAG state, annotated with blocked_by_gate per exec node.

    Without the annotation, exec nodes whose phase has a pending decision
    gate ahead show as "pending" in the UI — identical to "queued for an
    open slot". The user can't tell the difference between "waiting for
    capacity" and "waiting for my ADR lock". Surfacing the gate-block
    here removes that ambiguity at no engine cost.
    """
    task = _load_task(task_id)
    _require_deliberation(task)

    from okuro.orchestrator.state import _serialize_graph, pending_decision_gates
    data = _serialize_graph(task.graph)

    pending_gate_phase_ids = {pid for pid, _gate in pending_decision_gates(task)}
    if pending_gate_phase_ids:
        # Build subtask_id → blocking_gate map. A node is blocked when its
        # subtask sits in a phase whose id ≥ the smallest pending-gate phase.
        # "Lock gate N first" semantics — gates ahead of the work block it.
        first_blocking_phase = min(pending_gate_phase_ids)
        blocked_subtask_ids: set[str] = set()
        for phase in task.phases:
            if phase.id < first_blocking_phase:
                continue
            for st in phase.subtasks:
                blocked_subtask_ids.add(st.id)
        for n in data["nodes"]:
            if n.get("type") == "execution" and n.get("id") in blocked_subtask_ids:
                n["blocked_by_gate"] = True
                n["blocking_gate_phase"] = first_blocking_phase

    return GraphResponse(nodes=data["nodes"], edges=data["edges"])
