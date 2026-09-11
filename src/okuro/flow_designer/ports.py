# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The ONE place okuro turns a raw flow node into the canonical ports[] shape.
# index: PORTABLE_CATS | default_ports | norm_node | norm_edge
# AGENT_HEADER_END -->
"""Canonical node-port normalisation for okuro·flow.

okuro·flow nodes carry ONE ordered ``data.ports[]`` list — ``{id, label?, t?,
dir:"in"|"out", side?}`` — because handle placement is by index within a side,
which the old ``data.ins[]``/``data.outs[]`` split made un-authorable (it forced
every in above every out and crossed the edge vectors).

Every backend path that MINTS a node funnels through :func:`norm_node` here, so
the legacy shape is never emitted again. Readers stay deliberately tolerant —
``layout.iter_ports`` and the frontend's ``portsOf``/``migrateNodePorts`` still
accept ins/outs, because a hand-written agent graph or an old export may still
carry it. Emit one shape, read two.

Kept dependency-free (stdlib only): ``draw_worker`` imports it inside a
process-pool worker that must spawn cheaply and must not pull in ``okuro.web``.
"""

from __future__ import annotations

# Categories that get default wiring ports. The annotation cats — title, note,
# mdnote — are standalone labels: no ports, no edges, and norm_node leaves them
# portless rather than inventing handles nothing connects to.
PORTABLE_CATS = {"input", "process", "decision", "output"}


def default_ports() -> list[dict]:
    """The standard in/out pair every wired flow node gets.

    ``side`` is deliberately absent: it resolves from ``dir`` + the node's
    ``data.orient`` (h → in-left/out-right, v → in-top/out-bottom), so an
    omitted side lets the user flip a node's orientation without editing ports.
    The old emitters hardcoded side left/right, which silently pinned every
    generated node to horizontal."""
    return [
        {"id": "in", "label": "", "t": "flow", "dir": "in"},
        {"id": "out", "label": "", "t": "flow", "dir": "out"},
    ]


def norm_node(node: dict) -> dict:
    """Force ``node.data`` onto the canonical ports[] shape, in place.

    Three cases, in priority order:

    1. non-empty ``ports[]`` already → left alone; any stray ins/outs dropped
    2. legacy ``ins``/``outs`` → merged into ONE list, ins first then outs
       (the order the old renderer drew them in, so handles do not move), each
       port stamped with its ``dir``; the legacy keys are removed
    3. neither, and the cat is wired → the :func:`default_ports` pair, so an
       edge using sourceHandle="out"/targetHandle="in" always connects

    Port ids are untouched — "in"/"out" stay stable, which is why the edge
    contract needs no migration."""
    data = node.setdefault("data", {})
    ports = data.get("ports")
    had_ports_key = "ports" in data

    if isinstance(ports, list) and ports:
        data.pop("ins", None)
        data.pop("outs", None)
        return node

    ins = data.pop("ins", None) or []
    outs = data.pop("outs", None) or []
    if data.get("cat") in PORTABLE_CATS:
        # Per-direction fill, matching the behaviour this replaces: a node that
        # declares outs but no ins still gets its in port.
        if not ins:
            ins = [{"id": "in", "label": "", "t": "flow"}]
        if not outs:
            outs = [{"id": "out", "label": "", "t": "flow"}]

    merged = ([{**p, "dir": "in"} for p in ins if isinstance(p, dict)]
              + [{**p, "dir": "out"} for p in outs if isinstance(p, dict)])
    if merged or had_ports_key:
        data["ports"] = merged
    return node


def norm_edge(edge: dict) -> dict:
    """Force edge handles onto the standard port ids so the edge renders.

    Unchanged by the ports[] move: handles address a port BY ID, and the ids
    "in"/"out" survived the merge."""
    edge["sourceHandle"] = "out"
    edge["targetHandle"] = "in"
    edge.setdefault("type", "labeled")
    return edge
