# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: stack.components — read an installed stack's component manifest
#   from disk so any role can learn what a stack actually ships.
# index: imports | def stacks_root | def read_manifest | def list_installed
# AGENT_HEADER_END -->
"""Installed-stack component inventory.

The stack registry is declarative: `stack_profile('nextjs-shadcn')` says a
project uses Next.js and shadcn, but not which components exist, what they
are called, or when to reach for each. That knowledge lives with the
installed tree — `~/.okuro/stacks/<profile>/stack.manifest.json` — because
only the tree knows what was actually installed into it.

Why a tool and not a role field: roles cannot statically reference a stack
(no YAML key, no column, and `roles.seed` drops unmodeled keys). Even if
they could, binding a stack into a role would mean a new role per stack per
domain. One tool that any role calls at runtime scales instead — the same
call serves frontend-engineer, ui-designer and design-system-guardian, and
keeps working when a second stack lands.

Manifests are read from disk on every call rather than cached: an agent
adding a component and immediately asking what exists must see its own work.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from okuro.db.engine import okuro_home

STACKS_DIRNAME = "stacks"
MANIFEST_NAME = "stack.manifest.json"


def stacks_root() -> Path:
    """Root holding installed stacks.

    Mirrors the design-profile convention (`~/.okuro/design/profiles`):
    user-local and authoritative. Resolved per call, not at import, so tests
    and OKURO_ROOT overrides take effect — the design loader freezes its
    search path at import time and is a nuisance to test because of it.
    """
    root = os.environ.get("OKURO_ROOT")
    base = Path(root) if root else okuro_home()
    return base / STACKS_DIRNAME


def read_manifest(profile_name: str) -> dict[str, Any] | None:
    """Read one installed stack's manifest. None when not installed."""
    path = stacks_root() / profile_name / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return {"name": profile_name, "error": f"manifest unreadable: {e}"}
    data["installed_at"] = str(path.parent)
    return data


def list_installed() -> list[str]:
    """Profile names that have an installed tree with a manifest."""
    root = stacks_root()
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.iterdir()
        if (d / MANIFEST_NAME).is_file()
    )


def stack_components(profile_name: str | None = None) -> dict[str, Any]:
    """What a stack ships — or what stacks are installed.

    With no profile: the installed set, so a caller can discover before
    committing. With a profile: its full manifest.

    A profile that is registered but not installed is reported as such rather
    than as empty — "no components" and "not installed" are different answers
    and an agent must not confuse them.
    """
    installed = list_installed()
    if profile_name is None:
        return {"installed": installed}

    manifest = read_manifest(profile_name)
    if manifest is None:
        return {
            "profile": profile_name,
            "installed": False,
            "hint": (
                f"{profile_name!r} has no installed tree under {stacks_root()}. "
                f"Installed: {installed or 'none'}."
            ),
        }
    return manifest
