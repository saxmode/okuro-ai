# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: builds tool capabilities section for role prompts
# index: imports | def _get_tools_from_canon | def build_tools_section
# AGENT_HEADER_END -->
"""Tool Injector — builds tool sections for role prompts."""

import logging
from pathlib import Path
from okuro.orchestrator.yamlfast import yload

logger = logging.getLogger("okuro.orchestrator.tools.injector")


def _get_tools_from_canon() -> list | None:
    """Try to get tool list from okuro.canon."""
    try:
        from okuro.canon.registry import list_tools
        tools = list_tools()
        return tools if tools else None
    except Exception as e:
        logger.warning(f"Failed to get tools from okuro.canon: {e}")
    return None


def _first_sentence(text: str, hard_cap: int = 180) -> str:
    """First sentence of a canon tool description, for prompt injection.

    Canon descriptions accrete provenance and comparison prose — the
    antigravity-cli entry spent ~450 bytes on Gemini-CLI deprecation history
    and a cross-vendor model list. A subagent needs to know what the tool IS
    to decide whether to reach for it; the rest is a `canon_get_tool` call
    away and was being paid for on every spawn.

    Falls back to a hard character cap when no sentence boundary is found so a
    single run-on description cannot re-inflate the section.
    """
    text = " ".join((text or "").split())
    if not text:
        return ""
    for i, ch in enumerate(text):
        if ch == "." and i + 1 < len(text) and text[i + 1] == " ":
            return text[: i + 1]
        if ch == "." and i + 1 == len(text):
            return text
    return text if len(text) <= hard_cap else text[:hard_cap].rstrip() + "…"


def build_tools_section(role_name: str, registry_path: Path) -> str:
    """Build tool capabilities section for injection into role prompts.

    Tries okuro.canon first, falls back to local registry YAML.
    """
    canon_tools = _get_tools_from_canon()
    if canon_tools:
        lines = ["You have access to the following tools via MCP servers and CLI:", ""]
        for tool in canon_tools:
            name = tool.get("tool_id", tool.get("name", "unknown"))
            desc = _first_sentence(tool.get("description", ""))
            lines.append(f"- **{name}**: {desc}")
        lines.append("")
        lines.append("Full entry incl. capabilities: `canon_get_tool('<id>')`.")
        return "\n".join(lines)

    if not registry_path.exists():
        return ""

    try:
        with open(registry_path) as f:
            registry = yload(f)
    except Exception as e:
        logger.warning(f"Failed to read tool registry: {e}")
        return ""

    if not registry:
        return ""

    role_defaults = registry.get("role_defaults", {})
    role_tools = role_defaults.get(role_name, role_defaults.get("_default", []))
    if not role_tools:
        return ""

    all_tools = {}
    for category, tools in registry.get("tools", {}).items():
        for tool_name, tool_info in tools.items():
            if tool_info.get("status") == "active":
                all_tools[tool_name] = {**tool_info, "category": category}

    lines = ["You have access to the following tools via MCP servers and CLI:", ""]
    found_any = False
    for tool_name in role_tools:
        if tool_name in all_tools:
            tool = all_tools[tool_name]
            caps = ", ".join(tool.get("capabilities", []))
            lines.append(f"- **{tool_name}** ({tool['category']}): {tool['description']}")
            lines.append(f"  Capabilities: {caps}")
            found_any = True

    if not found_any:
        return ""

    lines.append("")
    lines.append(
        "Use these tools to accomplish your task. "
        "IMPORTANT: For codebase search, use cortex_search/cortex_route INSTEAD of Grep/Glob. "
        "For reading files, use cortex_read_header first, then cortex_read_section. "
    )
    return "\n".join(lines)
