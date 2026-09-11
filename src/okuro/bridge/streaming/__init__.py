# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: CLI streaming adapters for inline web sessions (claude + codex + antigravity).
# index: imports | public re-exports
# AGENT_HEADER_END -->
"""CLI streaming adapters for inline web sessions.

Each subscription-CLI adapter exposes the same public protocol
(``start`` / ``send_input`` / ``cancel`` / ``stream`` /
``poll_events``). The registry picks the right adapter from the
``provider`` string at session-create time.

The gemini adapter was removed on 2026-07-18 when the provider was
retired (its CLI stopped serving individual accounts on 2026-06-18);
antigravity (`agy`) replaced it as the Google path the same day.

Public surface:

- :class:`ClaudeStreamAdapter` — long-lived subprocess + token-level deltas.
- :class:`CodexStreamAdapter` — spawn-per-turn, codex-assigned thread_id.
- :class:`AntigravityStreamAdapter` — spawn-per-turn, prose output, private
  ``$HOME`` per session (agy's only MCP-scoping seam).
- :class:`StreamRegistry` — in-memory session map (one instance per
  process via :func:`get_registry`).
- :data:`UNIFIED_EVENT_TYPES` — names the contract doc §2.1 pins.
- ``translate_<provider>_event`` — pure functions, one per provider.

Everything here speaks the unified-event protocol in
``docs/research/cli-streaming-contract.md``.
"""

from .claude import (
    UNIFIED_EVENT_TYPES,
    ClaudeStreamAdapter,
    translate_claude_event,
)
from .antigravity import (
    AntigravityStreamAdapter,
    translate_antigravity_line,
)
from .codex import (
    CodexStreamAdapter,
    translate_codex_event,
)
from .registry import (
    AdapterNotImplemented,
    SessionAlreadyTerminal,
    SessionEventBus,
    SessionNotFound,
    StreamRegistry,
    get_registry,
    reset_registry_for_tests,
)

__all__ = [
    "AdapterNotImplemented",
    "AntigravityStreamAdapter",
    "ClaudeStreamAdapter",
    "CodexStreamAdapter",
    "SessionAlreadyTerminal",
    "SessionEventBus",
    "SessionNotFound",
    "StreamRegistry",
    "UNIFIED_EVENT_TYPES",
    "get_registry",
    "reset_registry_for_tests",
    "translate_claude_event",
    "translate_antigravity_line",
    "translate_codex_event",
]
