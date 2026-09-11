# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Canonical registry: tool and skill definitions bundled with the okuro package.
# index:
#   imports
#   def _get_registry_path
#   def _load_yaml
#   def list_tools
#   def get_tool
#   def list_skills
#   def get_skill
#   def validate_registry
# AGENT_HEADER_END -->
"""Canonical registry: tool and skill definitions bundled with the okuro package.

Registry lives at src/okuro/canon/registry/ — shipped as package data.
No external symlinks, no ~/.okuro/canon dependency.
"""

from pathlib import Path

import yaml

# Package-local registry is the default. Override with env var for development.
_REGISTRY_DIR = Path(__file__).parent / "registry"


def _get_registry_path() -> Path:
    """Get the registry root. Package-local by default."""
    import os
    env = os.environ.get("OKURO_CANON_PATH")
    if env:
        return Path(env).expanduser() / "registry"
    return _REGISTRY_DIR


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def is_registry_entry(path: Path) -> bool:
    """True if this YAML file is a registry entry rather than an artifact.

    Every walker here globs ``*.yaml``, and the registry directories are
    inside the package tree — so cortex drops a ``.okuro-index.yaml``
    sidecar into each of them. Those sidecars were loaded as tools:
    ``canon_list_tools`` returned two ``{"id": ".okuro-index", "files":
    {...}}`` blobs ahead of the real entries, and ``canon_validate``
    reported ``valid: False`` with two "missing 'name' field" errors that
    named no real tool.

    Two rules, and the first is the general one:

    * A dotfile is never a registry entry. Catches ``.okuro-index.yaml``
      and any future sidecar convention without another edit here.
    * ``index.yaml`` is a listing, not an entry.

    ``consumers.py`` had already written this predicate inline at its own
    call site while the root cause stayed live in these four walkers —
    it now imports this instead, so the rule has one home.
    """
    return not path.name.startswith(".") and path.name != "index.yaml"


def list_tools(category: str | None = None) -> list[dict]:
    """List all registered tools."""
    tools_dir = _get_registry_path() / "tools"
    if not tools_dir.exists():
        return []

    tools = []
    for yaml_file in sorted(tools_dir.rglob("*.yaml")):
        if not is_registry_entry(yaml_file):
            continue
        data = _load_yaml(yaml_file)
        if not data:
            continue
        if category and data.get("category") != category:
            continue
        data.setdefault("id", data.get("tool_id", yaml_file.stem))
        data.setdefault("source_file", str(yaml_file))
        tools.append(data)

    return tools


def get_tool(tool_id: str) -> dict | None:
    """Get a specific tool definition."""
    tools_dir = _get_registry_path() / "tools"
    if not tools_dir.exists():
        return None

    for yaml_file in tools_dir.rglob("*.yaml"):
        if not is_registry_entry(yaml_file):
            continue
        if yaml_file.stem == tool_id:
            data = _load_yaml(yaml_file)
            data.setdefault("id", data.get("tool_id", tool_id))
            return data

        # Also match by tool_id field inside the YAML
        data = _load_yaml(yaml_file)
        if data.get("tool_id") == tool_id:
            data.setdefault("id", tool_id)
            return data

    return None


def list_skills(category: str | None = None) -> list[dict]:
    """List all registered skills."""
    skills_dir = _get_registry_path() / "skills"
    if not skills_dir.exists():
        return []

    skills = []
    for yaml_file in sorted(skills_dir.rglob("*.yaml")):
        if not is_registry_entry(yaml_file):
            continue
        data = _load_yaml(yaml_file)
        if not data:
            continue

        rel = yaml_file.relative_to(skills_dir)
        if len(rel.parts) > 1:
            data.setdefault("category", rel.parts[0])

        if category and data.get("category") != category:
            continue

        data.setdefault("id", yaml_file.stem)
        data.setdefault("source_file", str(yaml_file))
        skills.append(data)

    return skills


def get_skill(skill_id: str) -> dict | None:
    """Get a specific skill definition."""
    skills_dir = _get_registry_path() / "skills"
    if not skills_dir.exists():
        return None

    for yaml_file in skills_dir.rglob("*.yaml"):
        if not is_registry_entry(yaml_file):
            continue
        if yaml_file.stem == skill_id:
            data = _load_yaml(yaml_file)
            data.setdefault("id", skill_id)
            return data

    return None


def validate_registry() -> dict:
    """Validate registry consistency."""
    registry = _get_registry_path()
    errors = []
    warnings = []

    if not registry.exists():
        return {
            "valid": False,
            "errors": [f"Registry not found: {registry}"],
            "warnings": [],
            "stats": {},
        }

    tools = list_tools()
    skills = list_skills()

    for tool in tools:
        if not tool.get("name"):
            errors.append(f"Tool '{tool.get('id')}' missing 'name' field")

    for skill in skills:
        if not skill.get("name"):
            errors.append(f"Skill '{skill.get('id')}' missing 'name' field")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "tools": len(tools),
            "skills": len(skills),
        },
    }
