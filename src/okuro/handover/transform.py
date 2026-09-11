# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-handover TRANSFORM layer — the "re-express this as the target's
#   native form" step. A fast model turns a note into a real flowchart and a flow
#   into a formatted note. Used by the registry producers when the source and
#   target shapes genuinely differ; a mechanical cast is the fallback.
# index:
#   def _loads / def _invoke_fast
#   def text_to_flow_graph / def _layout
#   def flow_to_note_markdown / def _describe_graph
# AGENT_HEADER_END -->
"""okuro·handover — content transforms (fast-model).

A handover is a *translation*, not a shape cast. "How would this note look as a
flow?" / "How would this flow read as a note?" Each transform asks a fast model
(``capability="fast"``) and post-processes into the target's exact structure.
Every transform degrades gracefully: on any model / parse failure it returns the
mechanical form so a handover never hard-fails.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("okuro.handover.transform")

_TIMEOUT = 60


# ── llm plumbing ──────────────────────────────────────────────────────────────


def _invoke_fast(system: str, prompt: str, timeout: int = _TIMEOUT) -> str:
    """One fast-tier completion. okuro's bridge routes ``capability='fast'`` to the
    configured fast model — no provider hardcode here."""
    from okuro.bridge.invoke import invoke

    # tool=True → the tooling bridge: no MCP, no CLAUDE.md/bootstrap, no
    # per-machine context. A handover transform is a pure tool-function.
    res = invoke(prompt=prompt, system_prompt=system, capability="fast",
                 tool=True, timeout=timeout)
    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError((res or {}).get("error") if isinstance(res, dict) else "invoke failed")
    return res.get("output") or ""


def _loads(text: str) -> Any:
    """Lenient JSON: strip code fences, then parse the first balanced {...}/[...]."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    # extract the widest span between the first opener and last matching closer
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = t.find(open_c), t.rfind(close_c)
        if 0 <= i < j:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    raise ValueError("no JSON object found in model output")


# ── text → flow graph ─────────────────────────────────────────────────────────

_FLOW_SYSTEM = (
    "You convert a note into an okuro-flow FLOWCHART. Read the note and express "
    "its actual logic as a graph — steps, branches, inputs and outcomes. Output "
    "ONLY JSON, no prose, no code fence:\n"
    '{"nodes":[{"id":str,"cat":"input"|"process"|"decision"|"output","title":str,'
    '"sub"?:str}],"edges":[{"id":str,"source":nodeId,"target":nodeId,"label"?:str}]}\n'
    "Rules: 3-14 nodes. Start at an input, end at an output. A choice/branch is a "
    "'decision' node with ≥2 outgoing edges (label them, e.g. yes/no). ids are "
    "short slugs. title ≤6 words; sub is an optional one-line detail. Every edge "
    "connects two existing node ids. Do NOT invent facts not in the note."
)


def text_to_flow_graph(title: str, body_md: str) -> dict[str, Any]:
    """Note text → a rendered okuro-flow graph {nodes, edges}. Falls back to a
    single markdown-note node if the model output can't be used."""
    from okuro.flow_designer.draw_worker import norm_edge, norm_node

    prompt = f"# {title}\n\n{body_md}".strip()
    try:
        raw = _loads(_invoke_fast(_FLOW_SYSTEM, prompt))
        src_nodes = raw.get("nodes") or []
        src_edges = raw.get("edges") or []
        if not src_nodes:
            raise ValueError("model returned no nodes")
    except Exception as exc:  # noqa: BLE001
        logger.info("text_to_flow_graph fell back to mdnote: %s", exc)
        return _mdnote_fallback(title, body_md)

    tags = {"input": "IN", "process": "PROC", "decision": "?", "output": "OUT"}
    nodes = []
    for n in src_nodes:
        cat = n.get("cat") if n.get("cat") in tags else "process"
        nodes.append(norm_node({
            "id": str(n.get("id")),
            "type": "node",
            "position": {"x": 0, "y": 0},
            "data": {"cat": cat, "tag": tags[cat],
                     "title": (n.get("title") or "").strip() or str(n.get("id")),
                     "sub": (n.get("sub") or "").strip()},
        }))
    ids = {n["id"] for n in nodes}
    edges = []
    for i, e in enumerate(src_edges):
        s, t = str(e.get("source")), str(e.get("target"))
        if s in ids and t in ids:
            edges.append(norm_edge({
                "id": e.get("id") or f"e{i}",
                "source": s, "target": t,
                "data": {"label": (e.get("label") or "").strip()},
            }))
    _layout(nodes, edges)
    return {"nodes": nodes, "edges": edges}


def _layout(nodes: list[dict], edges: list[dict]) -> None:
    """Assign left-to-right layered positions in place: x by longest-path depth
    from the roots, y by order within a layer."""
    incoming: dict[str, int] = {n["id"]: 0 for n in nodes}
    adj: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for e in edges:
        if e["target"] in incoming:
            incoming[e["target"]] += 1
            adj[e["source"]].append(e["target"])
    depth = {n["id"]: 0 for n in nodes}
    frontier = [nid for nid, c in incoming.items() if c == 0] or [nodes[0]["id"]]
    seen = set(frontier)
    while frontier:
        nxt = []
        for nid in frontier:
            for m in adj.get(nid, []):
                depth[m] = max(depth[m], depth[nid] + 1)
                if m not in seen:
                    seen.add(m)
                    nxt.append(m)
        frontier = nxt
    per_layer: dict[int, int] = {}
    for n in sorted(nodes, key=lambda n: depth[n["id"]]):
        d = depth[n["id"]]
        row = per_layer.get(d, 0)
        per_layer[d] = row + 1
        n["position"] = {"x": d * 280, "y": row * 160}


def _mdnote_fallback(title: str, body_md: str) -> dict[str, Any]:
    return {
        "nodes": [{
            "id": "n-source", "type": "node", "position": {"x": 0, "y": 0},
            "data": {"cat": "mdnote", "tag": "NOTE", "title": title, "body": body_md},
        }],
        "edges": [],
    }


# ── flow → note markdown ──────────────────────────────────────────────────────

_NOTE_SYSTEM = (
    "You convert a flowchart into a clear, well-structured markdown NOTE. Turn "
    "the graph into readable prose + structure: a short intro, then the steps in "
    "order using headings and/or a numbered list, **bold** for each step's name, "
    "and describe every branch/decision and where each path leads. Preserve all "
    "labels. Output ONLY markdown — no code fence, no commentary."
)


def flow_to_note_markdown(title: str, graph: dict[str, Any]) -> str:
    """Flow graph → a formatted markdown note. Falls back to a plain outline of
    the nodes if the model fails."""
    desc = _describe_graph(title, graph)
    try:
        out = _invoke_fast(_NOTE_SYSTEM, desc).strip()
        return out or _describe_graph(title, graph)
    except Exception as exc:  # noqa: BLE001
        logger.info("flow_to_note_markdown fell back to outline: %s", exc)
        return desc


def _describe_graph(title: str, graph: dict[str, Any]) -> str:
    """Deterministic textual serialization of a flow — the model's input, and the
    fallback note body."""
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    by_id = {n.get("id"): n for n in nodes}
    lines = [f"# {title}", "", "## Nodes"]
    for n in nodes:
        d = n.get("data", {})
        label = d.get("title") or d.get("tag") or n.get("id")
        cat = d.get("cat", "")
        sub = d.get("sub") or d.get("body") or ""
        lines.append(f"- **{label}**" + (f" ({cat})" if cat else "")
                     + (f" — {sub}" if sub else ""))
    if edges:
        lines += ["", "## Flow"]
        for e in edges:
            s = (by_id.get(e.get("source"), {}).get("data", {}) or {}).get("title", e.get("source"))
            t = (by_id.get(e.get("target"), {}).get("data", {}) or {}).get("title", e.get("target"))
            lbl = (e.get("data", {}) or {}).get("label", "")
            lines.append(f"- {s} →" + (f" *{lbl}* →" if lbl else "") + f" {t}")
    return "\n".join(lines)
