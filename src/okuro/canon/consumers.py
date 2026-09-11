# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Consumer read-model — one place that knows okuro's MCP consumers.
# index:
#   imports
#   class ConsumerRecord
#   def _platform_key
#   def _resolve_mcp_path
#   def _raw_consumer_dicts
#   def list_consumers
#   def get_consumer
#   def consumer_paths
#   def provider_ids
#   def cli_binaries
# AGENT_HEADER_END -->
"""Consumer read-model (P2 Phase A).

A single, data-driven view of okuro's MCP *consumers* — the CLIs and desktop
apps okuro registers itself into. Built entirely from canon YAML, so the
next phase can collapse the 13 scattered enumerations (``_provider_paths``,
``_PROVIDER_IDS``, ``_KNOWN_CLIS``, …) onto one source of truth.

**This module is ADDITIVE and wired to nothing yet.** It reads the registry
and reproduces the current enumerations for equivalence proofs; it does not
replace or feed any writer, probe, or path function. Deleting the old tables
is a later phase, gated on the proof in ``tests/canon/test_consumers.py``.

Two YAML sources, unified here:

* ``registry/tools/clis/*.yaml`` that carry a ``capabilities`` block —
  consumers that are *also* okuro inference backends (claude-code, codex,
  gemini, antigravity-cli). Left in place so ``list_tools()`` /
  ``list_inference_clis()`` / the role-prompt injector are byte-for-byte
  unchanged.
* ``registry/consumers/*.yaml`` — *pure* MCP consumers okuro registers into
  but never invokes as inference (cursor, claude-desktop, chatgpt-desktop).
  Kept out of the ``tools/`` scan path on purpose: nothing runtime lists
  them, so adding them changes no existing behavior.

Three identifiers travel with every consumer — keep them straight:

* ``tool_id``      — canon id / filename stem (e.g. ``claude-code``). This is
  the *value* held by ``mcp_config._PROVIDER_IDS``.
* ``short_name``   — the key used by ``_provider_paths`` / ``_PROVIDER_IDS`` /
  ``_KNOWN_CLIS`` (e.g. ``claude``, ``claude_desktop``).
* ``provider_id``  — the bridge/runtime provider the YAML already declared
  (e.g. ``claude``). A *third* thing; do not conflate with the two above.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .registry import (
    _get_registry_path,
    _load_yaml,
    is_registry_entry,
    list_tools,
)


@dataclass(frozen=True)
class ConsumerRecord:
    """One MCP consumer, as declared in canon."""

    tool_id: str
    short_name: str
    provider_id: str
    binary: str | None
    kind: str            # cli | desktop | ide | extension
    mcp: dict            # path, format, root_key, remote_field, transports, preferred_transport
    instructions: dict | None
    hooks: dict
    probe: str           # cli | none
    enabled: bool
    source_file: str | None
    raw: dict

    @property
    def is_cli(self) -> bool:
        """A consumer okuro can auth-probe as a CLI (kind cli + probe cli).

        This is the set that must line up with ``cli_probe._KNOWN_CLIS``.
        """
        return self.kind == "cli" and self.probe == "cli"

    def mcp_path(self, base_home: Path) -> Path | None:
        """Resolve this consumer's MCP config path under ``base_home``.

        Returns ``None`` when the consumer has no config path on the current
        platform (e.g. ChatGPT Desktop on Linux) — mirroring
        ``mcp_config._provider_paths`` returning ``None`` for such entries.
        """
        return _resolve_mcp_path(self.mcp, base_home)


# --- platform / path resolution -------------------------------------------
#
# Reproduces mcp_config._claude_desktop_path / _chatgpt_desktop_path exactly:
# darwin / win32 / linux* are the only known platforms; anything else (and,
# for a per-OS map with no key for the current platform) resolves to None.


def _platform_key() -> str | None:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "win32"
    if sys.platform.startswith("linux"):
        return "linux"
    return None


def _resolve_mcp_path(mcp: dict, base_home: Path) -> Path | None:
    raw = (mcp or {}).get("path")
    if raw is None:
        return None
    if isinstance(raw, dict):
        # Per-OS map (desktop apps). Missing key or explicit null -> None.
        key = _platform_key()
        if key is None:
            return None
        val = raw.get(key)
        if val is None:
            return None
        return base_home / val
    return base_home / raw


# --- loading ---------------------------------------------------------------


def _raw_consumer_dicts() -> list[dict]:
    """Collect raw consumer dicts from both YAML sources.

    Honors ``OKURO_CANON_PATH`` via ``_get_registry_path`` so tests can point
    at a scratch registry.
    """
    out: list[dict] = []
    seen: set[str] = set()

    # (1) inference CLIs that are also consumers — tools/clis/*.yaml carrying
    #     a capabilities block. Sourced through list_tools() so OKURO_CANON_PATH
    #     and the same dedup/id defaults apply.
    for tool in list_tools():
        if isinstance(tool.get("capabilities"), dict):
            tid = tool.get("tool_id") or tool.get("id")
            if tid in seen:
                continue
            seen.add(tid)
            out.append(tool)

    # (2) pure MCP consumers — registry/consumers/*.yaml. A consumer is
    #     defined by carrying a capabilities block, so index artifacts
    #     (index.yaml, .okuro-index.yaml, …) are ignored structurally.
    cdir = _get_registry_path() / "consumers"
    if cdir.exists():
        for yaml_file in sorted(cdir.glob("*.yaml")):
            if not is_registry_entry(yaml_file):
                continue
            data = _load_yaml(yaml_file)
            if not isinstance(data, dict) or not isinstance(
                data.get("capabilities"), dict
            ):
                continue
            tid = data.get("tool_id") or yaml_file.stem
            if tid in seen:
                continue
            seen.add(tid)
            data.setdefault("id", tid)
            data.setdefault("source_file", str(yaml_file))
            out.append(data)

    return out


def _to_record(data: dict) -> ConsumerRecord:
    caps = data.get("capabilities") or {}
    tid = data.get("tool_id") or data.get("id") or ""
    return ConsumerRecord(
        tool_id=tid,
        short_name=data.get("short_name") or tid,
        provider_id=data.get("provider_id") or tid,
        binary=data.get("binary"),
        kind=caps.get("kind", ""),
        mcp=caps.get("mcp") or {},
        instructions=caps.get("instructions"),
        hooks=caps.get("hooks") or {},
        probe=caps.get("probe", "none"),
        enabled=bool(data.get("enabled", True)),
        source_file=data.get("source_file"),
        raw=data,
    )


# Cache keyed on the resolved registry path. okuro's consumer YAMLs are shipped
# files that do not change during a process run (an okuro update restarts the
# process), so a long-lived daemon/orchestrator can hold this. Now that
# _provider_paths / _PROVIDER_IDS / _KNOWN_CLIS derive from here, an uncached
# read (~17ms of YAML rglob) would land on every registration_status and write.
# Keyed on the path so a test pointing OKURO_CANON_PATH at a fresh registry
# gets its own entry; reset_consumer_cache() clears it for same-path mutations.
_CONSUMER_CACHE: dict[str, list["ConsumerRecord"]] = {}


def reset_consumer_cache() -> None:
    """Drop the cached consumer list. For tests that mutate a registry in place."""
    _CONSUMER_CACHE.clear()


def list_consumers() -> list[ConsumerRecord]:
    """All MCP consumers okuro knows, sorted by ``tool_id`` (cached per registry)."""
    key = str(_get_registry_path())
    cached = _CONSUMER_CACHE.get(key)
    if cached is None:
        cached = [_to_record(d) for d in _raw_consumer_dicts()]
        cached.sort(key=lambda r: r.tool_id)
        _CONSUMER_CACHE[key] = cached
    return cached


def get_consumer(key: str) -> ConsumerRecord | None:
    """Look up a consumer by ``tool_id`` or ``short_name``."""
    for rec in list_consumers():
        if rec.tool_id == key or rec.short_name == key:
            return rec
    return None


# --- enumeration reproductions (for equivalence + future single-sourcing) --


def consumer_paths(base_home: Path) -> dict[str, Path | None]:
    """``short_name -> resolved MCP config path`` for every consumer.

    Superset of ``mcp_config._provider_paths(base_home)``: it additionally
    covers consumers that have no writer yet (e.g. antigravity). On the keys
    the two share, the values match — proven in ``test_consumers.py``.
    """
    return {rec.short_name: rec.mcp_path(base_home) for rec in list_consumers()}


def provider_ids() -> dict[str, str]:
    """``short_name -> tool_id`` — reproduces ``mcp_config._PROVIDER_IDS``."""
    return {rec.short_name: rec.tool_id for rec in list_consumers()}


def cli_binaries() -> dict[str, str | None]:
    """``short_name -> binary`` for CLI consumers — reproduces ``_KNOWN_CLIS``."""
    return {rec.short_name: rec.binary for rec in list_consumers() if rec.is_cli}
