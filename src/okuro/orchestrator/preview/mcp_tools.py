# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: MCP tools for the preview launcher — preview_status / start / stop / logs.
# index: imports | def _text | def _resolve_task_dir | def get_tools | async def handle_tool
# AGENT_HEADER_END -->
"""MCP tools for the preview launcher.

Mounted into the unified ``okuro`` MCP server via
:mod:`okuro.mcp._registry` (see ``_MODULES``). The handlers delegate to
:mod:`okuro.orchestrator.preview.launcher` — same code path as the REST
adapter.
"""

from __future__ import annotations

import json
import logging
import os
import re as _re
from pathlib import Path

from mcp.types import Tool, TextContent

from okuro.orchestrator.preview import (
    LauncherError,
    RecipeError,
    logs_tail as launcher_logs_tail,
    start as launcher_start,
    status as launcher_status,
    stop as launcher_stop,
)
from okuro.db.engine import okuro_home

logger = logging.getLogger(__name__)

OKURO_ROOT = Path(os.environ.get("OKURO_ROOT", okuro_home() / "orchestrator"))
TASKS_DIR = OKURO_ROOT / "tasks"

_TASK_ID_RE = _re.compile(r"^task(?:-[a-z]+)?-\d{8}-\d{6}(?:-[A-Za-z0-9_-]+)?$")


def _text(data) -> list[TextContent]:
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _resolve_task_dir(task_id: str) -> Path:
    if not _TASK_ID_RE.match(task_id):
        raise ValueError(f"Invalid task_id format: {task_id}")
    task_dir = (TASKS_DIR / task_id).resolve()
    if not str(task_dir).startswith(str(TASKS_DIR.resolve())):
        raise ValueError("Invalid task_id: path traversal detected")
    if not task_dir.exists():
        raise ValueError(f"Task not found: {task_id}")
    return task_dir


def get_tools() -> list[Tool]:
    common_task_id = {
        "task_id": {
            "type": "string",
            "description": "Orchestration task id, e.g. 'task-20260425-091203' or 'task-rec-20260425-050000-role-refresh'.",
        },
    }
    return [
        Tool(
            name="preview_status",
            description=(
                "Report current preview state for a task: idle | building | ready | failed | stopped. "
                "Includes URL, port, and recipe presence. No side effects."
            ),
            inputSchema={
                "type": "object",
                "properties": dict(common_task_id),
                "required": ["task_id"],
            },
        ),
        Tool(
            name="preview_start",
            description=(
                "Build (if needed) and serve the task's preview. Idempotent — "
                "if already running, returns the cached URL. Blocks until the "
                "ready check passes or times out. Requires preview.yaml in the task dir."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **common_task_id,
                    "force_rebuild": {
                        "type": "boolean",
                        "default": False,
                        "description": "Bypass build cache and rerun the build cmd.",
                    },
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="preview_stop",
            description="Stop the preview unit if running. Idempotent.",
            inputSchema={
                "type": "object",
                "properties": dict(common_task_id),
                "required": ["task_id"],
            },
        ),
        Tool(
            name="preview_logs",
            description=(
                "One-shot tail of the preview unit's journal. Returns the last "
                "N lines (default 200). MCP is not a streaming surface; use the "
                "REST endpoint /api/preview/{task_id}/logs?stream=true for live tailing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **common_task_id,
                    "lines": {
                        "type": "integer",
                        "default": 200,
                        "description": "Number of trailing lines to return.",
                    },
                },
                "required": ["task_id"],
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    task_id = arguments.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return _text({"error": "task_id (string) is required"})

    try:
        task_dir = _resolve_task_dir(task_id)
    except ValueError as exc:
        return _text({"error": str(exc)})

    try:
        if name == "preview_status":
            return _text(launcher_status(task_dir, slug_default=task_id).to_dict())

        if name == "preview_start":
            force = bool(arguments.get("force_rebuild") or False)
            return _text(launcher_start(task_dir, slug_default=task_id, force_rebuild=force).to_dict())

        if name == "preview_stop":
            return _text(launcher_stop(task_dir, slug_default=task_id).to_dict())

        if name == "preview_logs":
            lines = int(arguments.get("lines") or 200)
            return _text({
                "task_id": task_id,
                "lines": launcher_logs_tail(task_dir, slug_default=task_id, lines=lines),
            })

    except RecipeError as exc:
        if getattr(exc, "proposer_miss", False):
            return _text({
                "error": "auto_detect_miss",
                "detail": str(exc),
                "scanned": getattr(exc, "scanned", []),
                "known_templates": getattr(exc, "known_templates", []),
            })
        return _text({"error": "recipe_error", "detail": str(exc)})
    except LauncherError as exc:
        return _text({"error": "launcher_error", "detail": str(exc)})
    except Exception as exc:  # noqa: BLE001
        logger.exception("preview tool %s failed", name)
        return _text({"error": "internal_error", "detail": str(exc)})

    return _text({"error": f"Unknown tool: {name}"})
