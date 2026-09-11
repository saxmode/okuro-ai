# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Shared PATH-resolution helper for locating AI CLI binaries.
# index: imports | _COMMON_CLI_DIRS | enhanced_path | which
# AGENT_HEADER_END -->
"""Locate AI CLI binaries with a launchd-safe PATH.

macOS apps launched from Finder / Spotlight / Launchpad inherit the
restricted LaunchServices PATH, which does NOT include `/opt/homebrew/bin`,
`/usr/local/bin`, npm-global prefixes, or per-tool install dirs like
`~/.claude/local/bin`. Plain `shutil.which('claude')` returns None in
that context even though the CLI is installed and working from a
terminal.

Every subsystem that probes for providers (bridge config, okuro.yu
detection, doctor check) needs the same augmented PATH, otherwise
"providers detected" drifts per call site. This module is the single
source of truth — callers go through ``which(binary)``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Common locations where AI CLIs install themselves or where node/brew
# put global binaries. Ordered most-specific → most-generic so a CLI's
# vendored path wins over a stale brew symlink. Non-existent entries are
# filtered out in ``enhanced_path`` so the final PATH stays clean.
_COMMON_CLI_DIRS: tuple[str, ...] = (
    # Tool-specific vendored bins (Claude Code native installer, etc.)
    "~/.claude/local/bin",
    "~/.codex/bin",
    "~/.gemini/bin",
    "~/.cursor/bin",
    # User-level language toolchains
    "~/.local/bin",
    "~/.npm-global/bin",
    "~/.volta/bin",
    "~/.asdf/shims",
    # Homebrew (ARM + Intel)
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    # System
    "/usr/bin",
    "/bin",
)


def enhanced_path() -> str:
    """Return a PATH string that includes common CLI install locations.

    Always includes the current PATH (first, so user overrides win), then
    appends each entry from ``_COMMON_CLI_DIRS`` that exists and isn't
    already present. Result is usable with ``shutil.which(path=...)`` or
    as an os.environ["PATH"] override for subprocess calls.
    """
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    seen = set(parts)

    for entry in _COMMON_CLI_DIRS:
        resolved = str(Path(entry).expanduser())
        if resolved in seen:
            continue
        if Path(resolved).is_dir():
            parts.append(resolved)
            seen.add(resolved)

    return os.pathsep.join(parts)


def which(binary: str) -> str | None:
    """``shutil.which`` over the enhanced PATH.

    Returns the absolute path to the binary if found anywhere in the
    user's PATH or in one of the common CLI install locations, else None.
    """
    return shutil.which(binary, path=enhanced_path())
