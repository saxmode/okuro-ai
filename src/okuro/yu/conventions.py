# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: System conventions — static rules for host, GPUs, ports, directories.
# index: imports | def get_conventions | def get_convention | def format_conventions_markdown | def reload_conventions
# AGENT_HEADER_END -->
"""System conventions — static rules for host, GPUs, ports, directories.

Loaded from ~/.okuro/config.yaml (under the 'conventions' key) or embedded defaults.
Runtime state (what's actually running) comes from okuro.system, never from here.
"""

import os
import yaml
from pathlib import Path
from functools import lru_cache

_CONFIG_PATH = Path(os.environ.get("OKURO_CONFIG", "~/.okuro/config.yaml")).expanduser()


@lru_cache(maxsize=1)
def get_conventions() -> dict:
    """Load conventions from config file or return defaults."""
    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH) as f:
                data = yaml.safe_load(f) or {}
            return data.get("conventions", data if "host" in data else _DEFAULTS)
        except Exception:
            return _DEFAULTS
    return _DEFAULTS


def get_convention(key: str, default=None):
    """Get a single convention value by dot-separated path.

    Examples:
        get_convention("host.domain")     -> "myhost.example"
        get_convention("gpus.0.name")     -> "GPU0"
        get_convention("work_rules.venv") -> "~/.venv/bin/python"
    """
    conv = get_conventions()
    parts = key.split(".")
    current = conv
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (IndexError, ValueError):
                return default
        else:
            return default
        if current is None:
            return default
    return current


def format_conventions_markdown() -> str:
    """Format conventions as markdown for bootstrap packet."""
    data = get_conventions()
    lines = ["## Conventions"]

    if "host" in data:
        host = data["host"]
        lines.append(f"### Host: {host.get('name', '')} ({host.get('domain', '')})")

    if "gpus" in data:
        lines.append("### GPUs")
        for gpu in data["gpus"]:
            lines.append(
                f"- **{gpu.get('name', '')}** (GPU {gpu.get('id', '')}): "
                f"{gpu.get('hardware', '')} -- {gpu.get('role', '')}"
            )

    if "ports" in data:
        lines.append("### Ports")
        ports = data["ports"]
        for entry in ports.get("ranges", []):
            lines.append(
                f"- **{entry.get('range', '')}**: "
                f"{entry.get('owner', '')} ({entry.get('examples', '')})"
            )
        for entry in ports.get("fixed", []):
            lines.append(f"- **{entry.get('port', '')}**: {entry.get('owner', '')}")

    if "directories" in data:
        lines.append("### Directories")
        dirs = data["directories"]
        if isinstance(dirs, dict):
            for path, purpose in dirs.items():
                lines.append(f"- `{path}`: {purpose}")
        elif isinstance(dirs, list):
            for d in dirs:
                lines.append(f"- `{d.get('path', '')}`: {d.get('purpose', '')}")

    if "work_rules" in data:
        lines.append("### Work Rules")
        for key, val in data["work_rules"].items():
            lines.append(f"- **{key}**: {val}")

    return "\n".join(lines)


def reload_conventions():
    """Clear cache and reload from disk."""
    get_conventions.cache_clear()


# --- Defaults ---
# Minimal defaults for a fresh install. Override via ~/.okuro/config.yaml.

_DEFAULTS = {
    "host": {
        "name": "",
        "domain": "",
        "lan_only": True,
    },
    "gpus": [],
    "ports": {"ranges": [], "fixed": []},
    "directories": {},
    "work_rules": {
        "secrets": "okuro keyring (~/.okuro/keyring/), never .env files",
        "modules": "subprocess (never import cross-project)",
    },
}
