# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Bootstrap section safety wrapper — log + record failures instead of swallowing them.
# index: imports | FAILED_SECTIONS | _safe | get_failed_sections | clear_failed_sections | format_degraded_block
# AGENT_HEADER_END -->
"""Bootstrap section safety wrapper.

Audit 2026-04-26 (R1) found ~30 `except Exception: pass` sites in the
bootstrap pipeline. Each silently dropped a section/sub-block from the
packet — DB lookups, profile reads, design-profile resolvers, project
queries — leaving the user with a clean-looking packet that was
actually missing data. Zero degraded-mode signal.

`_safe(name, fn)` calls `fn()` and:
- returns `fn()`'s result on success
- on exception: logs a warning AND records `(name, error)` into a
  module-level list so the assembler can surface the degradation in
  the packet header
- returns a sentinel (default ``None``; callers pass ``default=`` for
  cases where they need a different empty value, e.g. ``""``)

Convention: each call site picks a unique ``name`` (e.g.
``"sections.build_design_profile.project_lookup"``) so a degraded-mode
report can pinpoint which sub-block failed without grepping the source.

The list is process-global, not thread-local — bootstrap runs in a
single request and is not entered concurrently by the same process.
``clear_failed_sections()`` MUST be called at the start of each
``assemble()`` to avoid cross-bootstrap leakage.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar

log = logging.getLogger("okuro.sense.bootstrap")

# Global accumulator for the current bootstrap. Reset by
# ``clear_failed_sections()`` at the top of ``assemble()``.
FAILED_SECTIONS: list[tuple[str, str]] = []

T = TypeVar("T")


def _safe(name: str, fn: Callable[[], T], *, default: Any = None) -> T | Any:
    """Run ``fn()`` and capture any exception.

    Args:
        name: Stable identifier for the call site (e.g.
            ``"sections.build_project.db_query"``). Surfaces in the
            log line and in the degraded-mode block.
        fn: Zero-arg callable to invoke. Wrap any args via lambda
            (e.g. ``_safe("...", lambda: build_project(slug))``).
        default: Sentinel returned on exception. ``None`` is correct
            for most builders that already short-circuit on
            ``None``. Pass ``""`` when the caller appends to a string,
            ``[]`` when it iterates, etc.

    Returns:
        ``fn()`` on success, ``default`` on exception.
    """
    try:
        return fn()
    except Exception as exc:
        log.warning("bootstrap section %r failed: %s", name, exc)
        FAILED_SECTIONS.append((name, str(exc)))
        return default


def get_failed_sections() -> list[tuple[str, str]]:
    """Return a copy of the failure list for the current bootstrap."""
    return list(FAILED_SECTIONS)


def clear_failed_sections() -> None:
    """Reset the failure list. Called at the top of ``assemble()``
    so consecutive bootstraps don't accumulate cross-run failures."""
    FAILED_SECTIONS.clear()


def format_degraded_block(failures: list[tuple[str, str]]) -> str:
    """Render the degraded-mode summary block for the packet.

    Inserted by the assembler AFTER the banner directive but BEFORE the
    rest of the packet, so the user can't miss it. Keeps the banner UX
    intact while making degradation impossible to overlook.
    """
    if not failures:
        return ""
    lines = [
        "## ⚠ Bootstrap degraded",
        "The following sections failed to load and are missing from this packet:",
    ]
    for name, err in failures:
        # Keep the rendered error short — full traceback is in the log.
        err_short = err.splitlines()[0][:200] if err else "<no message>"
        lines.append(f"  - {name}: {err_short}")
    lines.append("Run `okuro doctor` for the full picture.")
    return "\n".join(lines)
