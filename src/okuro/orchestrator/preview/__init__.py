# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Preview module — launch the visible result of an orchestration task.
# index: none
# AGENT_HEADER_END -->
"""Preview module.

Launches the visible result of an orchestration task as a tracked
``systemd-user`` transient unit. Same launcher backs both the REST/SSE
surface (``okuro.orchestrator.api.preview``) and the MCP tools
(``okuro.orchestrator.preview.mcp_tools``) — one core, two adapters.
"""

from okuro.orchestrator.preview.recipe import (
    Recipe,
    RecipeError,
    load_recipe,
    resolve_recipe_save_path,
)
from okuro.orchestrator.preview.launcher import (
    BUILD_LOG_FILE,
    LauncherError,
    PreviewState,
    logs_tail,
    propose_recipe,
    save_recipe,
    start,
    start_background,
    status,
    stop,
    suggest_project_paths,
)
from okuro.orchestrator.preview.proposer import (
    KNOWN_TEMPLATES,
    Proposal,
    ProposerMiss,
)

__all__ = [
    "BUILD_LOG_FILE",
    "KNOWN_TEMPLATES",
    "LauncherError",
    "PreviewState",
    "Proposal",
    "ProposerMiss",
    "Recipe",
    "RecipeError",
    "load_recipe",
    "logs_tail",
    "propose_recipe",
    "resolve_recipe_save_path",
    "save_recipe",
    "start",
    "start_background",
    "status",
    "stop",
    "suggest_project_paths",
]
