# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: MCP surface for the prism decision-helper libraries — components /
#   archetypes / layouts. The reasoning menu a deck-building workflow queries to
#   CHOOSE how to carry information (which component, archetype vs free
#   composition, which layout) instead of a frozen fit-table.
# index: imports | _load | _guides | get_tools | handle_tool
# AGENT_HEADER_END -->
"""MCP tool for the prism component/archetype/layout libraries.

One tool, ``prism_library``:
    no kind         -> overview: per-library counts + entry ids + every decision
                       guide (selection guides + archetype-vs-component +
                       components->layout). The "how do I choose" entry point.
    kind only       -> all entries of that library + its selection guide(s)
    kind + id       -> one entry
    kind + query    -> entries whose id/name/role/when-to-use/carries match

Static JSON (src/okuro/prism/library/*.json), cached on first read. A
missing/invalid file is reported as readable text, never raised, so tool
discovery degrades gracefully.
"""
from __future__ import annotations

import json
from pathlib import Path

from mcp.types import TextContent, Tool

_LIB = Path(__file__).resolve().parent
_FILES = {
    "components": "components.json",
    "archetypes": "archetypes.json",
    "layouts": "layouts.json",
}
_cache: dict[str, dict] = {}


def _load(kind: str) -> dict:
    if kind not in _cache:
        _cache[kind] = json.loads((_LIB / _FILES[kind]).read_text())
    return _cache[kind]


def _guides(lib: dict) -> dict:
    """Every top-level decision helper on a library (selection_guide and any
    *_guide key — archetype_vs_component_guide, layout_selection_guide, …)."""
    return {k: v for k, v in lib.items()
            if k == "selection_guide" or k.endswith("_guide")}


def _text(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="prism_library",
            description=(
                "Query the prism deck-building decision libraries: COMPONENTS (37 "
                "kit modules), ARCHETYPES (12 fixed slide templates), LAYOUTS (grid "
                "families + arrangements). Each entry carries when-to-use / avoid-when "
                "/ info-depths (L1-L4) / claim-shapes carried / capacity / pairs-with, "
                "plus decision helpers (how to pick a component, archetype-vs-free-"
                "composition, and which layout for N components of types X). Call with "
                "NO kind for the overview + all decision guides (start here when "
                "composing a slide); kind=components|archetypes|layouts for entries; "
                "add id for one entry or query to filter."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["components", "archetypes", "layouts"],
                        "description": "Which library. Omit for the cross-library overview + all decision guides.",
                    },
                    "id": {"type": "string", "description": "Return one entry by id (requires kind)."},
                    "query": {"type": "string", "description": "Filter entries by id/name/when-to-use/carries substring (requires kind)."},
                },
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "prism_library":
        return _text({"error": f"unknown tool: {name}"})
    try:
        kind = (arguments.get("kind") or "").strip()
        if not kind:
            overview = {}
            for k in _FILES:
                lib = _load(k)
                ents = lib.get("entries", [])
                overview[k] = {
                    "count": len(ents),
                    "ids": [e.get("id") for e in ents],
                    **_guides(lib),
                }
            return _text({
                "libraries": overview,
                "hint": "Call again with kind=components|archetypes|layouts (+ optional id or query) for full entries.",
            })
        if kind not in _FILES:
            return _text({"error": f"unknown kind: {kind!r}", "valid": list(_FILES)})
        lib = _load(kind)
        ents = lib.get("entries", [])
        eid = (arguments.get("id") or "").strip()
        if eid:
            hit = next((e for e in ents if e.get("id") == eid), None)
            return _text(hit or {"error": f"no {kind} entry id={eid!r}",
                                 "ids": [e.get("id") for e in ents]})
        q = (arguments.get("query") or "").strip().lower()
        if q:
            def _match(e: dict) -> bool:
                hay = " ".join(str(e.get(f, "")) for f in
                               ("id", "name", "one_liner", "when_to_use", "avoid_when"))
                carries = " ".join(str(c) for c in (e.get("carries") or []))
                return q in hay.lower() or q in carries.lower()
            ents = [e for e in ents if _match(e)]
        return _text({"kind": kind, **_guides(lib), "count": len(ents), "entries": ents})
    except FileNotFoundError as e:
        return _text({"error": f"prism library file missing: {e}"})
    except (ValueError, KeyError) as e:
        return _text({"error": f"prism library read failed: {e}"})
