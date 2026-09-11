# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-slides MCP tools — let agents/roles build, read, and edit decks
#   that render live at /slides.
# index: imports | OPS_DOC | def get_tools | async def handle_tool
# AGENT_HEADER_END -->
"""okuro-slides MCP tools — ``slides_*``.

The canonical edit surface for autonomous agents/roles (review-edit, build,
restyle). A save appends to the change-feed, so an open /slides canvas
live-updates. Mirrors flow_designer's MCP shape.
"""

import json

from mcp.types import TextContent, Tool

_ORIGIN = "agent"

OPS_DOC = (
    "OPS = list of: "
    '{"op":"set_meta","title?":str,"background?":"#rrggbb","font?":str,"arrangement?":"horizontal|vertical","transitionMode?":"morph|push","brandId?":str} | '
    '{"op":"add_slide","after?":int,"slide?":{"elements":[Element]}} | '
    '{"op":"duplicate_slide","index":int} | {"op":"remove_slide","index":int} | '
    '{"op":"move_slide","from":int,"to":int} | {"op":"set_notes","slide":int,"notes":str} | '
    '{"op":"add_element","slide":int,"element":Element} | '
    '{"op":"update_element","slide":int,"id":str,"patch":Element} | '
    '{"op":"remove_element","slide":int,"id":str}. '
    "Element = {id?, kind:'text'|'box'|'image'|'frame'|'video'|'flow', x,y,w,h, text?, src?, "
    "flowId?, fontSize?, fontWeight?, color?, bg?, align?, radius?, z?, "
    "children?:[Element...], layout?:{flow:'none'|'row'|'col', gap, padX, padY, "
    "align:'start'|'center'|'end'}}. A 'frame' auto-lays-out its children "
    "(layout.flow 'col'/'row' + gap/pad) and HUGS the content, reflowing on wrap — "
    "wrap stacked text in a frame so siblings never overlap; children use coords "
    "relative to the frame. Canvas 1280x720. "
    "Reuse an element id across slides so it persists/morphs (smart-animate)."
)


#: okuro·slides and okuro·prism are separate products that happen to share a verb
#: set. An agent asked for "a deck" reached for the nearest matching verb once and
#: landed the work in the wrong engine; every slides_* description therefore says
#: which product it is and where prism work belongs. This is disambiguation, NOT a
#: seal — slides_* is live and legitimately driven by roles/catalog/film-director.
_DISAMBIG = (
    " — okuro·slides: standalone free-canvas HTML decks at /slides. For a "
    "recipient-tailored, depth-laddered prism deck use the prism_deck_* workflow "
    "(prism_deck_open -> … -> prism_deck_assemble), which renders at /prism/deck."
)


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="slides_list",
            description="List okuro·slides decks (id, title, arrangement, slide_count, timestamps)." + _DISAMBIG,
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="slides_get",
            description="Get one deck by id, including its full scene IR (slides + elements)." + _DISAMBIG,
            inputSchema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        ),
        Tool(
            name="slides_generate",
            description=(
                "Generate a NEW recipient-tailored, brand-styled deck from a topic. "
                "Renders live at /slides on save." + _DISAMBIG
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "person_id": {"type": "string", "description": "okuro person to tailor flight-level + wording to."},
                    "brand_id": {"type": "string", "description": "brand whose design system styles it."},
                },
                "required": ["topic"],
            },
        ),
        Tool(
            name="slides_edit",
            description=(
                "Edit or review an existing deck with a natural-language instruction "
                "(add/remove/rewrite slides or elements, restyle, improve). The agent "
                "translates it to ops internally. Use for review-edit passes." + _DISAMBIG
            ),
            inputSchema={
                "type": "object",
                "properties": {"deck_id": {"type": "string"}, "instruction": {"type": "string"}},
                "required": ["deck_id", "instruction"],
            },
        ),
        Tool(
            name="slides_apply_ops",
            description=("Apply STRUCTURED ops directly to a deck (when you already know the exact edits)."
                         + _DISAMBIG + " " + OPS_DOC),
            inputSchema={
                "type": "object",
                "properties": {
                    "deck_id": {"type": "string"},
                    "ops": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                },
                "required": ["deck_id", "ops"],
            },
        ),
        Tool(
            name="slides_retailor",
            description=(
                "Re-tailor an existing deck to a new RECIPIENT and/or explicit "
                "communication axes — WITHOUT regenerating it. A wording/translation "
                "pass over existing elements (text-only update_element ops; geometry "
                "and element ids stay stable). Axes: density (concise|balanced|detailed), "
                "jargon (plain|balanced|technical), language (BCP-47, e.g. 'de', 'de-CH', "
                "'fr' — translates ALL slide text). With as_variant=true (default) the "
                "result is saved as a NEW deck linked to the source (variantOf), so you "
                "keep switchable audience variants; with as_variant=false it edits in "
                "place. Renders live at /slides on save." + _DISAMBIG
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "deck_id": {"type": "string"},
                    "person_id": {"type": "string", "description": "okuro person to tailor wording + flight-level to."},
                    "density": {"type": "string", "enum": ["concise", "balanced", "detailed"]},
                    "jargon": {"type": "string", "enum": ["plain", "balanced", "technical"]},
                    "language": {"type": "string", "description": "BCP-47 target language, e.g. 'en', 'de', 'de-CH', 'fr'."},
                    "as_variant": {"type": "boolean", "description": "true (default) → new linked variant; false → edit in place."},
                },
                "required": ["deck_id"],
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    from okuro.slides import get_deck, list_decks, save_deck

    try:
        if name == "slides_list":
            decks = list_decks()
            result = {"decks": [d.to_summary() for d in decks], "count": len(decks)}

        elif name == "slides_get":
            doc = get_deck(arguments["id"])
            result = doc.to_detail() if doc else {"error": f"deck '{arguments['id']}' not found"}

        elif name == "slides_generate":
            from okuro.slides.generate import generate_deck
            result = generate_deck(
                arguments["topic"],
                person_id=(arguments.get("person_id") or None),
                brand_id=(arguments.get("brand_id") or "okuro"),
            )
            result = {"success": True, "deck": result, "url": f"/slides"}

        elif name == "slides_edit":
            from okuro.slides.generate import edit_deck
            result = {"success": True, "deck": edit_deck(arguments["deck_id"], arguments["instruction"])}

        elif name == "slides_apply_ops":
            from okuro.slides.ops import apply_ops
            doc = get_deck(arguments["deck_id"])
            if doc is None:
                result = {"error": f"deck '{arguments['deck_id']}' not found"}
            else:
                new_deck, errors = apply_ops(doc.deck, arguments.get("ops") or [])
                saved = save_deck(id=doc.id, title=new_deck.get("title") or doc.title, deck=new_deck, origin=_ORIGIN, allow_empty=True)
                result = {"success": True, "deck": saved.to_summary(), "op_errors": errors, "url": f"/slides"}

        elif name == "slides_retailor":
            from okuro.slides.retailor import retailor_deck
            detail = retailor_deck(
                arguments["deck_id"],
                person_id=(arguments.get("person_id") or None),
                density=(arguments.get("density") or None),
                jargon=(arguments.get("jargon") or None),
                language=(arguments.get("language") or None),
                as_variant=arguments.get("as_variant", True),
            )
            result = {"success": True, "deck": detail, "url": f"/slides"}

        else:
            result = {"error": f"unknown tool '{name}'"}

    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)}

    return [TextContent(type="text", text=json.dumps(result, default=str))]
