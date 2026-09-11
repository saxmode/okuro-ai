# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Provider adapter registry — configures each agent provider.
# index: def register | def get_provider | def list_providers | def generate_all | def regen_all_provider_instructions | def _ensure_loaded
# AGENT_HEADER_END -->
"""Provider adapter registry — configures each agent provider."""

import logging

log = logging.getLogger(__name__)

_REGISTRY = {}


def register(cls):
    """Class decorator that registers a provider adapter."""
    _REGISTRY[cls.name] = cls
    return cls


def get_provider(name: str):
    """Get a provider adapter instance by name."""
    _ensure_loaded()
    if name not in _REGISTRY:
        raise KeyError(f"Unknown provider: {name}. Available: {list(_REGISTRY.keys())}")
    return _REGISTRY[name]()


def list_providers():
    """Return instances of all registered provider adapters."""
    _ensure_loaded()
    return [cls() for cls in _REGISTRY.values()]


def generate_all() -> list[str]:
    """Generate instruction files, hooks, and TOOL-PROTOCOL.md for all detected providers.

    Each adapter writes to its canonical path (e.g., ~/.claude/, ~/.codex/).
    TOOL-PROTOCOL.md goes to ~/.okuro/. No output_dir needed.
    """
    generated = []

    # Provider-specific files (CLAUDE.md, hooks, etc.)
    for adapter in list_providers():
        if adapter.detect():
            generated.extend(adapter.generate_instructions())
            generated.extend(adapter.install_hooks())

    # Standalone TOOL-PROTOCOL.md (provider-independent, read by all agents + subagents)
    try:
        from okuro.sense.bootstrap.sections import generate_tool_protocol
        proto_path = generate_tool_protocol()
        generated.append(proto_path)
    except Exception:
        pass

    return generated


def regen_all_provider_instructions() -> list[str]:
    """Regenerate instruction files for every detected provider.

    Iterates the registered adapters, calls ``generate_instructions()`` on
    each one whose ``detect()`` returns True. Per-adapter exceptions are
    swallowed and logged at WARNING level so a single broken adapter does
    not prevent the others from being regenerated.

    Also refreshes the shared profile caches consumed by Claude's
    UserPromptSubmit + Stop hooks and Gemini's BeforeAgent + AfterAgent
    hooks (``~/.okuro/profile-hook-rules.json`` and
    ``~/.okuro/profile-turn-context.txt``). Without this refresh, those
    on-disk caches drifted from the live profile until the next
    ``okuro install`` — every settings-page edit would update CLAUDE.md
    but leave the per-turn injection / compliance detectors stale.

    This is the post-edit hook used by ``yu.profile.update_profile`` to
    keep provider instruction files (``~/.claude/CLAUDE.md``,
    ``~/.codex/AGENTS.md``, ``~/.gemini/AGENTS.md``, etc.) AND the hook
    caches in sync with the live profile in okuro.db.

    Returns:
        List of absolute paths of files written, across all adapters
        plus the two profile caches.
    """
    paths: list[str] = []
    for adapter in list_providers():
        try:
            if not adapter.detect():
                continue
            paths.extend(adapter.generate_instructions())
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "regen_all_provider_instructions: adapter %r failed: %s",
                getattr(adapter, "name", adapter),
                exc,
            )

    # Profile-driven caches consumed by hook scripts. Single refresh
    # covers all providers because the cache content is provider-agnostic.
    from ._profile_compliance import (
        write_profile_hook_rules,
        write_profile_turn_context,
    )

    try:
        paths.append(write_profile_hook_rules())
    except Exception as exc:  # noqa: BLE001
        log.warning("profile-hook-rules.json refresh failed: %s", exc)

    try:
        paths.append(write_profile_turn_context())
    except Exception as exc:  # noqa: BLE001
        log.warning("profile-turn-context.txt refresh failed: %s", exc)

    return paths


_loaded = False


def _ensure_loaded():
    global _loaded
    if not _loaded:
        from . import antigravity, claude, codex, cursor  # noqa: F401
        _loaded = True
