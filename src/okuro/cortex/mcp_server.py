# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-cortex MCP server — codebase navigation, search, routing.
# index: imports | def _py_grep | def _text
# AGENT_HEADER_END -->
"""okuro-cortex MCP server — codebase navigation, search, routing."""

import json
import logging
import os
import re as _re
import shutil
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)
server = Server("okuro-cortex")

_root: Path = Path(".")

# Detect ripgrep at import time. Falls back to a Python walker when missing.
_HAS_RG = shutil.which("rg") is not None
if not _HAS_RG:
    logger.warning(
        "ripgrep (rg) not found on PATH — cortex_search_code will use a Python fallback "
        "(slower, smaller result cap). Install ripgrep for full performance."
    )

_FALLBACK_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", ".next", ".turbo", ".pnpm", "build",
}
_FALLBACK_MAX_FILE_BYTES = 1_000_000  # 1 MB — skip large files in Python walker


def _py_grep(query: str, root: Path, max_matches: int) -> list[dict]:
    """Python fallback for `rg --json`.

    Walks files under `root`, greps each line with a compiled regex, and
    returns up to `max_matches` results in the same shape as the rg branch.
    Not as fast as ripgrep — sized to still be useful for small codebases.
    """
    try:
        pattern = _re.compile(query)
    except _re.error:
        pattern = _re.compile(_re.escape(query))

    matches: list[dict] = []

    def _iter_files(base: Path):
        if base.is_file():
            yield base
            return
        for p in base.rglob("*"):
            if any(part in _FALLBACK_SKIP_DIRS for part in p.parts):
                continue
            if p.is_file():
                yield p

    for fp in _iter_files(root):
        if len(matches) >= max_matches:
            break
        try:
            if fp.stat().st_size > _FALLBACK_MAX_FILE_BYTES:
                continue
            with open(fp, "r", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if pattern.search(line):
                        matches.append({
                            "path": str(fp),
                            "line": i,
                            "text": line.rstrip("\n"),
                        })
                        if len(matches) >= max_matches:
                            break
        except (OSError, UnicodeDecodeError):
            continue

    return matches


def _text(data) -> list[TextContent]:
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="cortex_route",
            description="Find best navigation path to a concept using routers + semantic search",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What you're looking for (natural language)"}},
                "required": ["query"],
            },
        ),
        Tool(
            name="cortex_search",
            description="Semantic search across all indexed content using vector similarity",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language search query"},
                    "n": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                    "role": {"type": "string", "enum": ["code", "doc", "config", "data", "template", "test"]},
                    "project": {"type": "string", "description": "Filter by project/path prefix"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="cortex_read_header",
            description="Read only the AGENT_HEADER block from a file. Returns purpose, role, and section index.",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path relative to project root"}},
                "required": ["path"],
            },
        ),
        Tool(
            name="cortex_read_section",
            description="Read specific lines from a file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to project root"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path", "start_line", "end_line"],
            },
        ),
        Tool(
            name="cortex_read_file",
            description="Read an entire file.",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        Tool(
            name="cortex_navigate",
            description="Navigate up/down/siblings in the documentation hierarchy",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "direction": {"type": "string", "enum": ["up", "down", "siblings"]},
                },
                "required": ["path", "direction"],
            },
        ),
        Tool(
            name="cortex_search_code",
            description="Search for code patterns using ripgrep-style matching",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Code pattern to search for"},
                    "path": {"type": "string", "description": "Restrict to path prefix"},
                    "n": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="cortex_context",
            description="Get full context for a file (header + related files + search)",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "cortex_route":
            from okuro.cortex.vectorstore import VectorStore
            vs = VectorStore()
            results = vs.search(arguments["query"], n_results=5)
            return _text({
                "query": arguments["query"],
                "results": [{"path": str(r.file_path), "score": r.relevance, "snippet": r.snippet} for r in results],
            })

        if name == "cortex_search":
            from okuro.cortex.vectorstore import VectorStore
            vs = VectorStore()
            results = vs.search(
                arguments["query"],
                n_results=arguments.get("n", 5),
            )
            return _text({
                "results": [{"path": str(r.file_path), "score": r.relevance, "snippet": r.snippet} for r in results],
            })

        if name == "cortex_read_header":
            from okuro.cortex.core import parse_header, resolve_index_lines
            path = _root / arguments["path"]
            if not path.is_file():
                return _text(f"File not found: {path}")
            content = path.read_text(errors="replace")
            header = parse_header(content, file_path=path)
            if header is None:
                return _text(f"No AGENT_HEADER found in {arguments['path']}")
            header.index = resolve_index_lines(header.index, content)
            return _text(header)

        if name == "cortex_read_section":
            path = _root / arguments["path"]
            start = arguments["start_line"]
            end = arguments["end_line"]
            lines = path.read_text().splitlines()
            section = "\n".join(lines[start - 1 : end])
            return _text({"path": arguments["path"], "start_line": start, "end_line": end, "content": section})

        if name == "cortex_read_file":
            path = _root / arguments["path"]
            return _text(path.read_text())

        if name == "cortex_navigate":
            path = _root / arguments["path"]
            direction = arguments["direction"]
            if direction == "up":
                parent = path.parent
                entries = [f.name for f in parent.iterdir() if not f.name.startswith(".")]
                return _text({"parent": str(parent.relative_to(_root)), "entries": sorted(entries)})
            elif direction == "down":
                if path.is_dir():
                    entries = [f.name for f in path.iterdir() if not f.name.startswith(".")]
                    return _text({"path": arguments["path"], "entries": sorted(entries)})
                return _text({"path": arguments["path"], "entries": []})
            else:  # siblings
                entries = [f.name for f in path.parent.iterdir() if not f.name.startswith(".")]
                return _text({"path": arguments["path"], "siblings": sorted(entries)})

        if name == "cortex_search_code":
            n = int(arguments.get("n", 10))
            query = arguments["query"]
            base = _root / arguments["path"] if arguments.get("path") else _root

            if _HAS_RG:
                import subprocess
                cmd = ["rg", "--json", "-m", str(n), query, str(base)]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                matches = []
                for line in proc.stdout.splitlines():
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "match":
                            d = obj["data"]
                            matches.append({
                                "path": d["path"]["text"],
                                "line": d["line_number"],
                                "text": d["lines"]["text"].strip(),
                            })
                    except (json.JSONDecodeError, KeyError):
                        pass
                return _text({"matches": matches})

            # Fallback: Python walker (slower, but works without ripgrep)
            return _text({"matches": _py_grep(query, base, n), "backend": "python"})

        if name == "cortex_context":
            from okuro.cortex.core import parse_header
            path = _root / arguments["path"]
            header = parse_header(path)
            content = path.read_text()[:2000]
            return _text({"header": header, "preview": content})

        return _text(f"Unknown tool: {name}")
    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return [TextContent(type="text", text=f"Error: {e}")]


async def main(root: str | None = None):
    global _root
    _root = Path(root or os.environ.get("OKURO_ROOT", ".")).resolve()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
