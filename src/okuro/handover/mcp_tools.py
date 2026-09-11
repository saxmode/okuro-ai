# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: MCP surface for okuro-handover — handover_targets (discover reachable
#   tools + their intake contract for a selection) and handover_send (produce the
#   destination doc). Lets an agent move content between notes / flow / prism the
#   same way the UI does.
# index: imports | def get_tools | async def handle_tool
# AGENT_HEADER_END -->
import json

from mcp.types import TextContent, Tool

_ORIGIN = "mcp"

_IR_SCHEMA = {
    "type": "object",
    "description": (
        "Content IR — the normalized selection. `kind` is text|subgraph|facet. "
        "`body_md` is always used; `structured` carries {nodes,edges} for a "
        "subgraph or {facets} for a facet."
    ),
    "properties": {
        "kind": {"type": "string", "enum": ["text", "subgraph", "facet"]},
        "title": {"type": "string"},
        "body_md": {"type": "string"},
        "structured": {"type": "object", "additionalProperties": True},
        "project": {"type": "string"},
        "source": {
            "type": "object",
            "description": "Provenance {tool, id, label} for the backlink.",
            "additionalProperties": True,
        },
    },
    "required": ["kind"],
}


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="handover_targets",
            description=(
                "Given a content selection (Content IR), list the okuro tools it "
                "can be handed to and the exact fields each target needs. Read "
                "this FIRST — it tells you what to collect before handover_send "
                "(e.g. prism needs a recipient: a person or a target group)."
            ),
            inputSchema={
                "type": "object",
                "properties": {"content": _IR_SCHEMA},
                "required": ["content"],
            },
        ),
        Tool(
            name="handover_send",
            description=(
                "Hand a content selection to a target tool. Produces a NEW doc in "
                "that tool and returns its url. `inputs` must satisfy the target's "
                "`ask` fields from handover_targets. Recipient shape: "
                '{"recipient": {"kind": "person"|"group", "id": "..."}}.'
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": _IR_SCHEMA,
                    "target": {
                        "type": "string",
                        "description": "Target id: notes | flow | prism | slides | delivery.",
                    },
                    "inputs": {
                        "type": "object",
                        "description": "Resolved intake fields (title, name, recipient…).",
                        "additionalProperties": True,
                    },
                },
                "required": ["content", "target"],
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    from okuro.handover import ContentIR, HandoverError, handover, targets_for

    try:
        if name == "handover_targets":
            ir = ContentIR.from_dict(arguments["content"])
            result = {"kind": ir.kind, "targets": targets_for(ir)}

        elif name == "handover_send":
            ir = ContentIR.from_dict(arguments["content"])
            result = handover(ir, arguments["target"], arguments.get("inputs") or {})

        else:
            result = {"error": f"unknown tool '{name}'"}

    except HandoverError as exc:
        result = {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        result = {"error": f"{type(exc).__name__}: {exc}"}

    return [TextContent(type="text", text=json.dumps(result, default=str))]
