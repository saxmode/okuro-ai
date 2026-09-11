# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Compile an okuro-flow graph into an orchestrator plan — the bridge that
#   turns a DRAWN workflow into an executable DAG. Gives okuro a second kind of
#   workflow beside the LLM-decomposed one: manually arranged, with a role, prompt
#   and acceptance criteria per node. No LLM anywhere in this path.
# index: NODE_KIND | FlowCompileError | compile_flow | expand_fanout | to_phases
# AGENT_HEADER_END -->
"""okuro-flow graph -> orchestrator plan.

**okuro-flow is NOT the workflow store, and must not become one.** ``/flow``
(``okuro.flow_designer``) is a standalone visualizer for complex information; its
documents stay free-form and must never acquire orchestration semantics. The
workflow designer is a SEPARATE surface inside okuro-orchestrator that reuses the
same ReactFlow *components* (``flow-canvas.tsx``, ``nodes.tsx``, ``graph.ts``) —
shared component, separate store.

This module therefore takes a plain ``{"nodes": [...], "edges": [...]}`` mapping
and imports nothing from ``flow_designer``. It will compile any graph-shaped
input, whatever persists it.

**Two kinds of workflow, one engine.** The decomposer plans on the fly from a
prose task; this compiles a plan that a human (or an agent) arranged by hand.
Both end up as ``list[Phase]`` through the same pure helpers
(``validate_plan`` -> ``plan_to_phases``), so everything downstream — worktrees,
the adversarial reviewer, decision gates, artifacts, the web UI — is identical.

**Node contract** (``node["data"]``, ignored by every other okuro-flow consumer)::

    {
      "kind": "subtask",              # REQUIRED — nodes without it are decoration
      "phase": 1,                     # REQUIRED — explicit, not derived from depth
      "role": "researcher",           # REQUIRED — must exist in the role catalogue
      "prompt": "…",                  # the subagent brief; {param} placeholders filled
      "acceptance_criteria": ["…"],
      "risk": "LOW|MED|HIGH",         # default MED
      "complexity": "fast|standard|strategic",   # default standard
      "artifact_name": "…", "outputs": ["…"],
      "serialize": true,              # phase-level; any node in the phase sets it
      "fan_out": {"over": "topics"}   # this node becomes ONE SUBTASK PER ITEM
    }

Edges are dependencies: ``source -> target`` means target waits for source.

**Ordering is visual.** Subtasks are numbered by canvas position (top-to-bottom,
then left-to-right), so the ids a user reads in the DAG match the order they see
on the canvas. Node id is the final tie-break so compilation stays deterministic.

**The dynamic axis.** A ``fan_out`` node expands into N subtasks once N is known —
compile phase 1, run it, then compile the rest with the items it produced. That
is the one thing a static plan cannot know up front, and it is expressed as data
rather than as a special case per workflow.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

# ROCK-SOLID v5 D-A — the default phase NAME is rendered copy.
from okuro.orchestrator.gate_messages import step_label

logger = logging.getLogger(__name__)

NODE_KIND = "subtask"

_RISKS = ("LOW", "MED", "HIGH")
_COMPLEXITIES = ("fast", "standard", "strategic")


class FlowCompileError(ValueError):
    """A drawn workflow that cannot become a valid plan.

    Raised rather than repaired: a hand-arranged workflow is authored
    deliberately, so a missing role or a cycle is a mistake to show the author,
    not fuzz to silently patch the way an LLM plan would be.
    """


def _graph(flow: Any) -> dict[str, Any]:
    if isinstance(flow, dict):
        return flow.get("graph", flow) or {}
    return getattr(flow, "graph", None) or {}


def _fmt(text: str, params: dict[str, Any]) -> str:
    """Fill {placeholders} from params, leaving unknown braces untouched.

    A prompt is prose written by a human; an unmatched brace is far more likely
    to be JSON they pasted in than a parameter they meant, so it must not raise.
    """
    out = text or ""
    for k, v in (params or {}).items():
        out = out.replace("{" + str(k) + "}", str(v))
    return out


def _subtask_nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = graph.get("nodes") or []
    return [n for n in nodes if ((n.get("data") or {}).get("kind") == NODE_KIND)]


def _sort_key(node: dict[str, Any]) -> tuple:
    pos = node.get("position") or {}
    return (float(pos.get("y", 0) or 0), float(pos.get("x", 0) or 0), str(node.get("id", "")))


def _validate_node(node: dict[str, Any]) -> dict[str, Any]:
    d = node.get("data") or {}
    nid = node.get("id")
    if not nid:
        raise FlowCompileError("a subtask node has no id")
    if not d.get("role"):
        raise FlowCompileError(f"node {nid!r} has no role — every subtask needs one")
    phase = d.get("phase")
    if phase is None:
        raise FlowCompileError(f"node {nid!r} has no phase — set data.phase (1, 2, 3, …)")
    try:
        phase = int(phase)
    except (TypeError, ValueError):
        raise FlowCompileError(f"node {nid!r} has a non-numeric phase {phase!r}") from None
    if phase < 1:
        raise FlowCompileError(f"node {nid!r} has phase {phase} — phases start at 1")
    risk = str(d.get("risk") or "MED").upper()
    if risk not in _RISKS:
        raise FlowCompileError(f"node {nid!r} has risk {risk!r}; expected one of {_RISKS}")
    cx = str(d.get("complexity") or "standard").lower()
    if cx not in _COMPLEXITIES:
        raise FlowCompileError(
            f"node {nid!r} has complexity {cx!r}; expected one of {_COMPLEXITIES}")
    return {**d, "phase": phase, "risk": risk, "complexity": cx}


def _detect_cycle(node_ids: list[str], deps: dict[str, list[str]]) -> Optional[list[str]]:
    """3-colour DFS. Returns the cycle path, or None. The orchestrator's own
    validate_plan only WARNS on a cycle; a drawn workflow should not get that far."""
    WHITE, GRAY, BLACK = 0, 1, 2
    colour = {n: WHITE for n in node_ids}
    stack: list[str] = []

    def visit(n: str) -> Optional[list[str]]:
        colour[n] = GRAY
        stack.append(n)
        for m in deps.get(n, []):
            if colour.get(m) == GRAY:
                return stack[stack.index(m):] + [m]
            if colour.get(m, BLACK) == WHITE:
                found = visit(m)
                if found:
                    return found
        stack.pop()
        colour[n] = BLACK
        return None

    for n in node_ids:
        if colour[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return None


def compile_flow(
    flow: Any,
    *,
    params: Optional[dict[str, Any]] = None,
    phases: Optional[Iterable[int]] = None,
    fanout: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Compile an okuro-flow graph into an orchestrator plan dict.

    ``params``   fills ``{placeholders}`` in prompts, names and outputs.
    ``phases``   restricts compilation to those phase numbers — this is how a
                 workflow with a fan-out runs in two passes (compile phase 1,
                 run it, then compile the rest once N is known).
    ``fanout``   ``{node_id: [item, …]}`` materializes a fan-out node into one
                 subtask per item. A fan-out node with no items supplied is
                 OMITTED, and anything depending on it is dropped with it —
                 loudly, via a warning, never silently.
    """
    params = dict(params or {})
    fanout = fanout or {}
    graph = _graph(flow)
    nodes = _subtask_nodes(graph)
    if not nodes:
        raise FlowCompileError(
            "flow has no subtask nodes — set data.kind='subtask' on the nodes that "
            "should run")

    wanted = set(int(p) for p in phases) if phases is not None else None

    # node id -> validated data, in canvas order
    ordered = sorted(nodes, key=_sort_key)
    data_by_id: dict[str, dict[str, Any]] = {}
    for n in ordered:
        data_by_id[str(n["id"])] = _validate_node(n)

    # edges -> dependency map (target depends on sources)
    deps: dict[str, list[str]] = {nid: [] for nid in data_by_id}
    for e in graph.get("edges") or []:
        src, tgt = str(e.get("source") or ""), str(e.get("target") or "")
        if not src or not tgt:
            continue
        if src not in data_by_id or tgt not in data_by_id:
            # An edge to a decoration node is fine on a canvas; it just carries
            # no dependency. An edge to a MISSING node is a broken graph.
            known = set(str(n.get("id")) for n in (graph.get("nodes") or []))
            if src not in known or tgt not in known:
                raise FlowCompileError(
                    f"edge {src!r}->{tgt!r} references a node that is not in the graph")
            continue
        deps[tgt].append(src)

    cycle = _detect_cycle(list(data_by_id), deps)
    if cycle:
        raise FlowCompileError(f"workflow has a dependency cycle: {' -> '.join(cycle)}")

    # Assign subtask ids: <phase>.<n>, numbered in canvas order within the phase.
    # A fan-out node claims one id per item.
    ids_by_node: dict[str, list[str]] = {}
    counters: dict[int, int] = {}
    for n in ordered:
        nid = str(n["id"])
        d = data_by_id[nid]
        ph = d["phase"]
        fan = d.get("fan_out")
        items = fanout.get(nid) if fan else None
        n_ids = len(items) if items is not None else (0 if fan else 1)
        assigned = []
        for _ in range(n_ids):
            counters[ph] = counters.get(ph, 0) + 1
            assigned.append(f"{ph}.{counters[ph]}")
        ids_by_node[nid] = assigned

    unresolved = {nid for nid, got in ids_by_node.items() if not got}
    if unresolved:
        logger.warning("flow compile: %d fan-out node(s) not materialized (%s) — "
                       "they and their dependents are omitted from this pass",
                       len(unresolved), sorted(unresolved))

    # Drop anything that (transitively) depends on an unresolved fan-out.
    dropped = set(unresolved)
    changed = True
    while changed:
        changed = False
        for nid, srcs in deps.items():
            if nid not in dropped and any(s in dropped for s in srcs):
                dropped.add(nid)
                changed = True

    by_phase: dict[int, list[dict[str, Any]]] = {}
    serialize_by_phase: dict[int, bool] = {}
    for n in ordered:
        nid = str(n["id"])
        d = data_by_id[nid]
        ph = d["phase"]
        if nid in dropped or (wanted is not None and ph not in wanted):
            continue
        # Dependencies keep their ids even when the source phase is not in THIS
        # pass: a two-pass compile appends to a DAG whose earlier subtasks already
        # ran, and the orchestrator's continuation contract allows referencing
        # completed subtask ids.
        dep_ids = sorted({i for s in deps[nid] for i in ids_by_node.get(s, [])})

        if d.get("serialize"):
            serialize_by_phase[ph] = True
        serialize_by_phase.setdefault(ph, False)

        items = fanout.get(nid) if d.get("fan_out") else None
        targets = items if items is not None else [None]
        for st_id, item in zip(ids_by_node[nid], targets):
            by_phase.setdefault(ph, []).append(
                _subtask(st_id, d, dep_ids, params, item, node_id=nid))

    if not by_phase:
        raise FlowCompileError(
            "nothing to compile — every subtask node was filtered out by "
            f"phases={sorted(wanted) if wanted else None} or an unmaterialized fan-out")

    return {
        "phases": [
            {
                "id": ph,
                "name": _fmt(str(_phase_name(ordered, data_by_id, ph)), params),
                "serialize": serialize_by_phase.get(ph, False),
                "subtasks": by_phase[ph],
            }
            for ph in sorted(by_phase)
        ],
    }


def _phase_name(ordered: list[dict], data_by_id: dict[str, dict], phase: int) -> str:
    for n in ordered:
        d = data_by_id[str(n["id"])]
        if d["phase"] == phase and d.get("phase_name"):
            return str(d["phase_name"])
    return step_label(phase)


def _subtask(st_id: str, d: dict[str, Any], deps: list[str], params: dict[str, Any],
             item: Optional[dict[str, Any]], *, node_id: str) -> dict[str, Any]:
    """One plan subtask. A fan-out item's fields become extra ``{placeholders}``
    prefixed ``item.``, so one drawn node writes N briefs without N prompts."""
    p = dict(params)
    if item is not None:
        for k, v in item.items():
            p[f"item.{k}"] = v
    st: dict[str, Any] = {
        "id": st_id,
        "role": d["role"],
        "description": _fmt(d.get("prompt") or d.get("description") or "", p),
        "risk": d["risk"],
        "complexity": d["complexity"],
    }
    if deps:
        st["dependencies"] = deps
    if d.get("artifact_name"):
        st["artifact_name"] = _fmt(str(d["artifact_name"]), p)
    if d.get("outputs"):
        st["outputs"] = [_fmt(str(o), p) for o in d["outputs"]]
    if d.get("acceptance_criteria"):
        st["acceptance_criteria"] = [_fmt(str(a), p) for a in d["acceptance_criteria"]]
    st["_node_id"] = node_id      # provenance: which drawn node produced this
    return st


def _strip_provenance(plan: dict[str, Any]) -> dict[str, Any]:
    """A copy without ``_node_id``, FOR VALIDATION ONLY.

    ``validate_plan`` checks a plan against the shape an LLM planner emits, and
    an unknown key there reads as a malformed subtask. Conversion keeps the
    provenance — see ``to_phases``.
    """
    return {"phases": [
        {**ph, "subtasks": [{k: v for k, v in st.items() if k != "_node_id"}
                            for st in ph["subtasks"]]}
        for ph in plan["phases"]
    ]}


def to_phases(plan: dict[str, Any], *, role_index: Optional[dict] = None,
              strict: bool = True, validate_as: Optional[dict[str, Any]] = None) -> list:
    """Plan dict -> ``list[Phase]`` via the decomposer's PURE helpers. No LLM.

    ``strict`` (default ON, unlike an LLM plan) turns validation warnings into an
    error: a warning on a HAND-ARRANGED workflow is an authoring mistake to show
    its author, not planner fuzz to repair.

    ``validate_as`` validates a DIFFERENT (superset) plan while converting
    ``plan``. A fan-out expansion compiles only the phases it is appending, so a
    dependency on an already-completed subtask looks dangling to ``validate_plan``
    even though it is correct. Passing the full plan validates what is true
    without weakening strictness for the phases actually being added.
    """
    from okuro.orchestrator.decomposer import ADVISORY_PREFIX, plan_to_phases, validate_plan

    # Stripped for the VALIDATOR only. The conversion below keeps `_node_id`, so
    # each Subtask remembers the drawn node it came from.
    to_validate = _strip_provenance(validate_as if validate_as is not None else plan)

    if role_index is None:
        try:
            from okuro.orchestrator.config import load_role_index
            role_index = load_role_index()
        except Exception as exc:  # noqa: BLE001 — validation guards, it does not gate
            logger.warning("flow compile: role index unavailable (%s) — skipping validation", exc)
            role_index = None

    if role_index is not None:
        warnings = validate_plan(to_validate, role_index)
        for w in warnings:
            logger.warning("flow compile: %s", w)
        # P5.7 — advisories are logged but never fatal. A drawn workflow whose
        # writer node declares no `outputs` is worth a line in the log and is
        # not a broken workflow; strict mode exists to catch plans that cannot
        # RUN. Filtering here rather than at the producer keeps the advice
        # visible to the person compiling, which is the only reason to emit it.
        blocking = [w for w in warnings if not w.startswith(ADVISORY_PREFIX)]
        if blocking and strict:
            raise FlowCompileError(
                f"drawn workflow produced {len(blocking)} validation warning(s): {blocking}")
    # The UNSTRIPPED plan: plan_to_phases reads `_node_id` onto Subtask.node_id.
    return plan_to_phases(plan)


def fanout_nodes(flow: Any) -> dict[str, dict[str, Any]]:
    """``{node_id: fan_out spec}`` — what the caller must supply items for."""
    return {str(n["id"]): (n.get("data") or {}).get("fan_out")
            for n in _subtask_nodes(_graph(flow))
            if (n.get("data") or {}).get("fan_out")}
