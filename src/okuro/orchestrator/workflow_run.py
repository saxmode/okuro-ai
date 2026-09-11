# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Bind a DRAWN workflow to a task run — compile its plan instead of
#   calling the LLM decomposer, and expand its fan-out node once the runtime
#   list exists. The engine's only knowledge of drawn workflows lives here.
# index: phases_for_task | pending_fanouts | expand_fanout | describe_workflow
# AGENT_HEADER_END -->
"""Running a manually arranged workflow.

okuro plans a task one of two ways:

============  ================================================================
on the fly    ``decompose_task`` asks an LLM to plan from the prose description.
              The historical behaviour, and still the default.
drawn         ``task.workflow_id`` names a workflow someone arranged by hand.
              Its graph compiles to the plan — no LLM, deterministic, reviewable
              like code.
============  ================================================================

Everything after planning is identical: the same ``list[Phase]``, so worktrees,
the adversarial reviewer, decision gates, artifacts and the web UI do not know or
care which kind produced them.

**The fan-out is the one thing a drawn plan cannot know up front.** A node marked
``fan_out`` becomes one subtask per item, and the item count only exists once an
earlier phase has run (prism: how many topics a reader needs). So a workflow with
a fan-out runs in two passes — the initial compile withholds the fan-out node and
everything downstream of it, then ``expand_fanout`` appends the rest once the
items are known. ``state.append_phases`` extends the DAG graph too, so the engine
does not declare the task complete just because pass one finished.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("okuro.orchestrator.workflow_run")


def _workflow_id(task: Any) -> str:
    return (getattr(task, "workflow_id", "") or "").strip()


def _params(task: Any) -> dict[str, Any]:
    return dict(getattr(task, "workflow_params", None) or {})


def phases_for_task(task: Any, *, strict: bool = True) -> Optional[list]:
    """The initial phases for a drawn workflow, or ``None`` if this task has none.

    ``None`` — not an empty list — is the signal to fall through to the LLM
    decomposer, so a task without a workflow behaves exactly as it always has.

    A fan-out node and its dependents are absent from this first pass by
    construction: ``compile_flow`` withholds any node whose items are not yet
    supplied. Call ``expand_fanout`` once an earlier phase has produced them.
    """
    wf_id = _workflow_id(task)
    if not wf_id:
        return None

    from okuro.orchestrator.flow_compiler import to_phases
    from okuro.orchestrator.workflow_store import compile_workflow

    plan = compile_workflow(wf_id, params=_params(task))
    phases = to_phases(plan, strict=strict)
    logger.info("task %s: planned from drawn workflow %r — %d phase(s), %d subtask(s)",
                getattr(task, "id", "?"), wf_id, len(phases),
                sum(len(p.subtasks) for p in phases))
    return phases


def pending_fanouts(task: Any) -> dict[str, dict]:
    """Fan-out nodes of this task's workflow that have not been materialized yet.

    Empty when the task has no workflow, the workflow has no fan-out, or every
    fan-out has already been expanded into the plan.
    """
    wf_id = _workflow_id(task)
    if not wf_id:
        return {}

    from okuro.orchestrator.flow_compiler import fanout_nodes
    from okuro.orchestrator.workflow_store import get_workflow

    doc = get_workflow(wf_id)
    if doc is None:
        return {}
    specs = fanout_nodes(doc)
    if not specs:
        return {}

    # A fan-out is done once its phase exists in the plan. Phase membership is
    # the honest signal: the expansion appends whole phases, so a phase present
    # means its fan-out already ran.
    graph = doc.graph or {}
    by_node_phase = {
        str(n["id"]): (n.get("data") or {}).get("phase")
        for n in (graph.get("nodes") or [])
    }
    existing = {p.id for p in (getattr(task, "phases", None) or [])}
    return {nid: spec for nid, spec in specs.items()
            if by_node_phase.get(nid) not in existing}


def expand_fanout(task: Any, tasks_dir: Path, items_by_node: dict[str, list[dict]],
                  *, strict: bool = True) -> list:
    """Append the phases that were withheld pending a fan-out's item list.

    ``items_by_node`` maps a fan-out node id to the items it should become — one
    subtask per item, no cap. Returns the appended phases (empty if there was
    nothing left to add).

    Phases already in the task are skipped rather than duplicated, so calling
    this twice with the same items is a no-op instead of a second copy of the
    work.
    """
    wf_id = _workflow_id(task)
    if not wf_id:
        raise ValueError("task has no workflow_id — nothing to expand")
    if not items_by_node:
        raise ValueError("expand_fanout requires items for at least one node")

    from okuro.orchestrator.flow_compiler import compile_flow, to_phases
    from okuro.orchestrator.state import append_phases
    from okuro.orchestrator.workflow_store import get_workflow

    doc = get_workflow(wf_id)
    if doc is None:
        raise ValueError(f"workflow '{wf_id}' not found")

    plan = compile_flow(doc, params=_params(task), fanout=items_by_node)
    existing = {p.id for p in (getattr(task, "phases", None) or [])}
    remaining = [p for p in plan["phases"] if p["id"] not in existing]
    if not remaining:
        logger.info("task %s: fan-out expansion had nothing new to append",
                    getattr(task, "id", "?"))
        return []

    # Validate the FULL plan but convert only what we are appending: phase 2's
    # dependency on phase 1 is correct, yet looks dangling to validate_plan when
    # phase 1 is not in the slice being converted.
    phases = to_phases({"phases": remaining}, strict=strict, validate_as=plan)
    append_phases(task, phases, tasks_dir)
    logger.info("task %s: fan-out expanded — appended %d phase(s), %d subtask(s)",
                getattr(task, "id", "?"), len(phases),
                sum(len(p.subtasks) for p in phases))
    return phases


def collect_fanout_items(task: Any, tasks_dir: Path) -> dict[str, list[dict]]:
    """Items for every pending fan-out whose source artifact is on disk.

    A fan-out node declares WHERE its list comes from, so expansion needs no
    per-workflow code::

        "fan_out": {"over": "topics",
                    "items_from": "topic-map.json",   # in the task's artifacts/
                    "items_key": "topics"}            # optional key inside it

    The file must hold a JSON array, or an object containing one under
    ``items_key``. Each item must be an object — its fields become ``{item.*}``
    placeholders in the node's brief, so a bare list of strings has nothing to
    substitute and is skipped with a warning rather than silently producing
    subtasks whose prompts still contain braces.

    A fan-out with no ``items_from``, or whose file is missing or unparseable, is
    simply absent from the result: expansion waits. That is deliberate — the
    upstream subtask may not have finished writing it yet, and guessing would
    fan out over the wrong list.
    """
    import json

    pending = pending_fanouts(task)
    if not pending:
        return {}

    artifacts = Path(tasks_dir) / getattr(task, "id", "") / "artifacts"
    out: dict[str, list[dict]] = {}
    for node_id, spec in pending.items():
        src = (spec or {}).get("items_from")
        if not src:
            logger.debug("fan-out %r declares no items_from — expansion stays manual", node_id)
            continue
        path = artifacts / str(src)
        if not path.exists():
            logger.debug("fan-out %r waiting for %s", node_id, path)
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("fan-out %r: %s is not readable JSON (%s) — waiting",
                           node_id, path, exc)
            continue
        key = (spec or {}).get("items_key")
        items = data.get(key) if (key and isinstance(data, dict)) else data
        if not isinstance(items, list) or not items:
            logger.warning("fan-out %r: %s holds no usable list%s — waiting",
                           node_id, path, f" under {key!r}" if key else "")
            continue
        if not all(isinstance(i, dict) for i in items):
            logger.warning("fan-out %r: %s must be a list of OBJECTS — their fields "
                           "become {item.*} placeholders — waiting", node_id, path)
            continue
        out[node_id] = items
    return out


def auto_expand(task: Any, tasks_dir: Path, *, strict: bool = True) -> list:
    """Expand any fan-out whose items have appeared. Safe to call every loop.

    Returns the appended phases, or ``[]`` when there is nothing to do — which is
    the overwhelmingly common case, so this stays cheap: it does no work at all
    for a task with no workflow or no pending fan-out.
    """
    if not _workflow_id(task):
        return []
    items = collect_fanout_items(task, tasks_dir)
    if not items:
        return []
    return expand_fanout(task, tasks_dir, items, strict=strict)


def describe_workflow(task: Any) -> str:
    """One line for logs and the planning banner. Empty when not workflow-driven."""
    wf_id = _workflow_id(task)
    if not wf_id:
        return ""

    from okuro.orchestrator.workflow_store import get_workflow

    doc = get_workflow(wf_id)
    if doc is None:
        return f"workflow '{wf_id}' (MISSING)"
    return f"workflow '{doc.name}' ({doc.node_count} nodes)"
