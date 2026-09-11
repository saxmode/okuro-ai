# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-flow MCP tools — let agents author the generic node-graph canvas
#   that renders live in okuro.web at /flow.
# index: imports | SCHEMA_DOC | def get_tools | async def handle_tool
# AGENT_HEADER_END -->
"""okuro-flow MCP tools — ``flow_designer_*``.

Agents design diagrams (architecture maps, workflows, topologies) that render in
okuro.web at ``/flow``. A save here appends to the change-feed, so an open canvas
live-updates the moment an agent writes. Graph schema is ReactFlow-shaped and
documented in ``SCHEMA_DOC`` below so authoring agents emit valid nodes/edges.
"""

import json

from mcp.types import TextContent, Tool

_ORIGIN = "agent"

# Authoring contract handed to agents so they emit a valid graph.
SCHEMA_DOC = (
    "GRAPH = {nodes: Node[], edges: Edge[], viewport?: {x,y,zoom}}. "
    "Node = {id: str, type: 'node'|'group', position?: {x: number, y: number}, "
    "data: {cat: 'process'|'input'|'output'|'decision'|'note'|'title'|'mdnote', "
    "tag: str (short chip, e.g. 'PROC'), title: str, sub?: str, "
    "body?: str (markdown, only used by 'mdnote'), color?: '#rrggbb', "
    "orient?: 'h'|'v' (default port sides: h = in-left/out-right, v = in-top/"
    "out-bottom), ports?: Port[], ins?: Port[], outs?: Port[]}}. "
    "POSITION IS OPTIONAL — omit it and the server auto-lays the node out: it "
    "estimates every node's RENDERED width from its content and places nodes "
    "left-to-right by edge depth, so a long title can never overlap its "
    "neighbour. Omitting position for the whole graph is the recommended way "
    "to author one. A position you DO supply is kept, unless the supplied "
    "positions overlap — then the overlapping nodes are laid out. "
    "Two annotation categories carry no ports and need no edges: "
    "'title' renders data.title as a large standalone heading (use for "
    "section labels / banners; data.sub is an optional subheading); "
    "'mdnote' renders data.body (falls back to data.sub) as GitHub-flavoured "
    "Markdown — bold, italic, lists, inline code, headings, links, tables. "
    "Port = {id: str (unique within node), label?: str, "
    "t?: 'flow'|'data'|'control'|'value'|'signal'|'asset', "
    "dir: 'in'|'out', side?: 'left'|'right'|'top'|'bottom'}. "
    "CANONICAL port shape = ONE ordered list in data.ports[], each carrying its "
    "own dir — the only shape okuro itself emits or stores. LEGACY input is "
    "still accepted and read (data.ins[]/data.outs[] with no dir field) and is "
    "converted to ports[] on the way in. Author ports[]. "
    "Edge = {id: str, source: nodeId, target: nodeId, sourceHandle: portId, "
    "targetHandle: portId, type: 'labeled', data?: {label?: str, "
    "pstyle?: 'default'|'smoothstep'|'step'|'straight'|'simplebezier'}}. "
    "Connect an OUT port to an IN port. Keep ids stable across updates so "
    "positions/edges survive."
)

_NODE_ITEM = {
    "type": "object",
    "description": "ReactFlow node. See the tool description for the full shape.",
    "additionalProperties": True,
}
_EDGE_ITEM = {
    "type": "object",
    "description": "ReactFlow edge connecting an out-port to an in-port.",
    "additionalProperties": True,
}


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="flow_designer_list",
            description=(
                "List all okuro-flow diagrams (summary: id, name, description, "
                "node_count, edge_count, timestamps). okuro-flow is the visual "
                "node-graph canvas at /flow."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="flow_designer_get",
            description="Get one okuro-flow diagram by id, including its full graph (nodes + edges).",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Flow id (slug)."}
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="flow_designer_save",
            description=(
                "Create or update an okuro-flow diagram. Renders live in the web "
                "canvas at /flow on save. Pass `id` to update an existing flow "
                "(omit to create; a slug is derived from `name`). " + SCHEMA_DOC
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Existing flow id to update. Omit to create a new flow.",
                    },
                    "name": {"type": "string", "description": "Human title of the diagram."},
                    "description": {"type": "string", "description": "Optional one-line description."},
                    "nodes": {
                        "type": "array",
                        "items": _NODE_ITEM,
                        "description": "Graph nodes (see schema in tool description).",
                    },
                    "edges": {
                        "type": "array",
                        "items": _EDGE_ITEM,
                        "description": "Graph edges connecting node ports.",
                    },
                    "viewport": {
                        "type": "object",
                        "description": "Optional camera {x, y, zoom}.",
                        "additionalProperties": True,
                    },
                },
                "required": ["name", "nodes"],
            },
        ),
        Tool(
            name="flow_designer_delete",
            description="Delete an okuro-flow diagram by id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Flow id (slug)."}
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="flow_designer_history",
            description=(
                "List prior-graph snapshots of an okuro-flow (newest first). Every "
                "overwrite snapshots the previous graph, so this is the recovery "
                "trail when a flow was cleared or clobbered. Pair with "
                "flow_designer_restore."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Flow id (slug)."}
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="flow_designer_restore",
            description=(
                "Restore an okuro-flow's graph from a history snapshot. Get the "
                "snapshot `seq` from flow_designer_history. The restore is itself "
                "reversible (it snapshots the current graph first)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Flow id (slug)."},
                    "seq": {
                        "type": "integer",
                        "description": "History snapshot seq to restore.",
                    },
                },
                "required": ["id", "seq"],
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    from okuro.flow_designer import storage
    from okuro.flow_designer.layout import apply_layout

    try:
        if name == "flow_designer_list":
            flows = storage.list_flows()
            result = {"flows": [f.to_summary() for f in flows], "count": len(flows)}

        elif name == "flow_designer_get":
            flow = storage.get_flow(arguments["id"])
            result = flow.to_detail() if flow else {"error": f"Flow '{arguments['id']}' not found"}

        elif name == "flow_designer_save":
            # An agent has no renderer, so it cannot know how wide a node will
            # be — position is optional and the server places what was left
            # out (and untangles what was placed on top of something else).
            edges = arguments.get("edges") or []
            graph = {
                "nodes": apply_layout(arguments.get("nodes") or [], edges, only_missing=True),
                "edges": edges,
            }
            if isinstance(arguments.get("viewport"), dict):
                graph["viewport"] = arguments["viewport"]
            saved = storage.save_flow(
                id=arguments.get("id"),
                name=arguments["name"],
                description=arguments.get("description", ""),
                graph=graph,
                origin=_ORIGIN,
            )
            result = {
                "success": True,
                "flow": saved.to_summary(),
                "url": f"/flow?id={saved.id}",
            }

        elif name == "flow_designer_delete":
            ok = storage.delete_flow(arguments["id"], origin=_ORIGIN)
            result = {"success": ok, "id": arguments["id"]}

        elif name == "flow_designer_history":
            result = {
                "id": arguments["id"],
                "history": storage.list_history(arguments["id"]),
            }

        elif name == "flow_designer_restore":
            saved = storage.restore_history(
                arguments["id"], int(arguments["seq"]), origin=_ORIGIN
            )
            result = {"success": True, "flow": saved.to_summary()}

        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as e:
        result = {"error": str(e)}

    return [TextContent(type="text", text=json.dumps(result, indent=2))]
