# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro MEDIA MCP tools — expose the native illustration capability
#   (okuro.media.illustrate) as a first-class delivery tool any agent can call:
#   topic → on-brand vector "Architecture Noir" SVG (or photoreal ComfyUI raster),
#   on okuro's own models, indexed in the okuro asset store.
# index: imports | def get_tools | async def handle_tool
# AGENT_HEADER_END -->
"""MCP surface for okuro's illustration capability."""

import json

from mcp.types import Tool, TextContent


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="illustrate",
            description=(
                "Create an illustration for a TOPIC on-box, on okuro's own models, "
                "and index it in the okuro asset store. style='noir' (default) "
                "composes a mono 'Architecture Noir' VECTOR SVG (GPU-free, on-brand "
                "for okuro / architecture-noir) via okuro's native "
                "bridge; style='photoreal' renders a raster image via okuro's local "
                "ComfyUI (GPU + edition/licence-gated). fmt=svg|png|webm for noir "
                "(raster is png). Returns the inline svg / data-URI plus the asset id "
                "(a durable, reusable deliverable). Returns an error envelope when "
                "the requested engine is unavailable — nothing is created."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "what to illustrate."},
                    "style": {"type": "string", "enum": ["noir", "vector", "auto", "photoreal", "raster"], "description": "noir/vector (default) or photoreal (ComfyUI)."},
                    "fmt": {"type": "string", "enum": ["svg", "png", "webm"], "description": "noir output format (default svg); raster is always png."},
                    "category": {"type": "string", "description": "optional visual template hint (architecture|flow|comparison|hierarchy|network|concept|…)."},
                    "caption": {"type": "string"},
                    "persist": {"type": "boolean", "description": "index in the asset store (default true)."},
                },
                "required": ["topic"],
            },
        ),
        Tool(
            name="narrate",
            description=(
                "Turn TEXT into a spoken audio clip on-box via okuro's local TTS "
                "(Orpheus/Kokoro — no API key, no GPU) and index it in the okuro "
                "asset store as a first-class STUDIO audio asset (source=studio, "
                "kind=audio). The studio-audio parallel of `illustrate` — a podcast/"
                "narration becomes a durable, reusable media asset. Returns the "
                "audio data-URI + duration + the asset id, or an error envelope when "
                "TTS is unavailable."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "the text to speak."},
                    "voice": {"type": "string", "description": "optional voice id (default the narrator voice)."},
                    "title": {"type": "string", "description": "asset title (default: opening words)."},
                    "persist": {"type": "boolean", "description": "index in the asset store (default true)."},
                },
                "required": ["text"],
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "narrate":
        from okuro.media.audio import narrate

        text = (arguments.get("text") or "").strip()
        if not text:
            return [TextContent(type="text", text=json.dumps({"error": "text required"}))]
        block = narrate(
            text,
            voice=(arguments.get("voice") or None),
            title=(arguments.get("title") or None),
            persist=arguments.get("persist", True),
        )
        if not block:
            return [TextContent(type="text", text=json.dumps({
                "error": "TTS unavailable (no engine/voice ready).",
            }))]
        # NEVER return the base64 data-URI here — it is ~100KB+ and blows the tool
        # channel. The asset_id is the durable handle: fetch bytes via the asset
        # store / GET /api/assets/media/{id}/file.
        return [TextContent(type="text", text=json.dumps({
            "success": True,
            "kind": "audio",
            "duration": block.get("duration"),
            "voice": block.get("voice"),
            "caption": block.get("caption"),
            "asset_id": block.get("asset_id"),
            "data_bytes": len(block.get("src") or ""),
        }))]

    if name != "illustrate":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    from okuro.media.illustrate import illustrate

    topic = (arguments.get("topic") or "").strip()
    if not topic:
        return [TextContent(type="text", text=json.dumps({"error": "topic required"}))]

    block = illustrate(
        topic,
        style=(arguments.get("style") or "noir"),
        fmt=(arguments.get("fmt") or "svg"),
        category=(arguments.get("category") or None),
        caption=(arguments.get("caption") or None),
        persist=arguments.get("persist", True),
    )
    if not block:
        return [TextContent(type="text", text=json.dumps({
            "error": "illustration engine unavailable for the requested style "
                     "(photoreal needs a ready ComfyUI + edition; noir needs the bridge).",
        }))]

    # The inline svg / data-URI can be large (a raster data-URI is ~100KB+) — do
    # NOT return it on the tool channel. The asset_id is the durable handle; fetch
    # bytes via the asset store / GET /api/assets/media/{id}/file.
    return [TextContent(type="text", text=json.dumps({
        "success": True,
        "style": block.get("style"),
        "fmt": block.get("fmt"),
        "mime": block.get("mime"),
        "caption": block.get("caption"),
        "asset_id": block.get("asset_id"),
        "cache_hit": block.get("cache_hit"),
        "data_bytes": len(block.get("svg") or block.get("src") or ""),
    }))]
