# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Generate MCP server configs for AI providers.
# index:
#   imports
#   def _load_servers
#   def _provider_paths
#   def _build_server_entry
#   def _resolve_env
#   def generate_mcp_entries
#   def _remove_legacy_servers
#   def _clean_claude_settings_local
#   def _merge_mcp_servers
#   def _read_json
#   def _write_json
#   def write_claude_config
#   def write_cursor_config
#   def write_codex_config
#   def write_claude_desktop_config
#   def write_chatgpt_desktop_config
#   def write_all_configs
# AGENT_HEADER_END -->
"""Generate MCP server configs for AI providers.

Supports writing configs into:
- the real user home (so the user's own CLI sessions see the servers), and
- an optional scoped agent home (e.g. ``~/.okuro-agent-home/``) so subagents
  spawned by the orchestrator have an isolated tool surface.

Every writer accepts ``base_home: Path | None``; when omitted it defaults to
the real user home. Callers pass the scoped home path to populate agent-only
configs without touching the user's real CLI settings.
"""

import copy
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

from okuro.keyring import real_user_home


# ---------------------------------------------------------------------------
# Transport selection
# ---------------------------------------------------------------------------
#
# The default is still "stdio" — every client spawns its own subprocess. Pass
# ``transport="http"`` (or set ``OKURO_MCP_TRANSPORT=http``) to point the
# configs at the HTTP MCP surface hosted inside ``okuro-daemon`` instead
# (subagent #15 folded the standalone ``okuro-mcpd`` in). ``"auto"`` probes
# the daemon's /health endpoint and picks http when reachable, stdio otherwise.

_VALID_TRANSPORTS = {"stdio", "http", "auto"}


def _resolve_transport(transport: str | None) -> str:
    choice = transport or os.environ.get("OKURO_MCP_TRANSPORT") or "stdio"
    if choice not in _VALID_TRANSPORTS:
        raise ValueError(
            f"invalid transport {choice!r}; expected one of {_VALID_TRANSPORTS}"
        )
    if choice == "auto":
        return "http" if _http_daemon_reachable() else "stdio"
    return choice


def _http_daemon_reachable() -> bool:
    """Is the HTTP MCP daemon up? Read-only — a probe must not have side effects.

    ``configured_port``, not ``resolve_port``: asking "is the daemon running?"
    used to ALLOCATE a port and write ~/.okuro/config.yaml when none was set,
    so merely resolving ``transport="auto"`` mutated the user's config on a
    machine that had never booted the daemon. No configured port means nobody
    has ever listened, which is exactly the answer the probe should give.
    """
    try:
        import urllib.request

        from okuro.mcp.http_server import configured_port
        port = configured_port()
        if port is None:
            return False
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=0.5
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _read_http_token() -> str:
    """Read the bearer token written by the HTTP MCP surface on first boot."""
    from okuro.mcp.http_server import TOKEN_PATH
    if not TOKEN_PATH.is_file():
        raise RuntimeError(
            f"token file missing at {TOKEN_PATH} — start okuro-daemon once to create it"
        )
    return TOKEN_PATH.read_text().strip()


def _http_url() -> str:
    """URL of the running daemon's MCP surface.

    ``configured_port``, not ``resolve_port``: the latter ALLOCATES a port and
    writes it to ~/.okuro/config.yaml when unset, which turned generating a
    config blob into a mutation of the user's real home — even under a scoped
    base_home, since these paths are machine-global. An unset port means the
    daemon has never booted, which is the same "there is nothing to point at"
    condition ``_read_http_token`` already refuses on, so refuse the same way.
    """
    from okuro.mcp.http_server import MCP_MOUNT, configured_port
    port = configured_port()
    if port is None:
        raise RuntimeError(
            "no mcp.http.port in ~/.okuro/config.yaml — start okuro-daemon "
            "once to allocate one, then re-run"
        )
    return f"http://127.0.0.1:{port}{MCP_MOUNT}"

# MCP server definitions — derived from canon registry
def _load_servers() -> dict:
    """Load server definitions from okuro.canon registry."""
    try:
        from okuro.canon.registry import list_tools
        servers = {}
        for tool in list_tools():
            if tool.get("type") != "mcp_server":
                continue
            tid = tool.get("tool_id", tool.get("id"))
            cfg = tool.get("config", {})
            entry = {"module": cfg["args"][-1]} if cfg.get("args") else {}
            env_keys = list((cfg.get("env") or {}).keys())
            if env_keys:
                entry["env_keys"] = env_keys
            servers[tid] = entry
        if servers:
            return servers
    except Exception:
        pass
    # Fallback if canon unavailable
    return {"okuro": {"module": "okuro.mcp.server"}}


SERVERS = _load_servers()

# Legacy server names to remove during migration (both tm-* and old okuro-* split servers)
_LEGACY_SERVERS = [
    # Old tm-* names — okuro's OWN pre-unification split servers, all dead.
    # NOT "tm-mobile": that is a LIVE, independent mobile-bridge MCP server the
    # user runs (it is in the active tool list), not an okuro server. It was
    # swept in by the tm-* pattern, and once write_claude_config targets
    # ~/.claude.json — where tm-mobile is registered — this list would DELETE
    # it. okuro must not remove servers it does not own. (Caught 2026-07-17 by
    # the real-.claude.json preservation proof.)
    "tm-cortex", "tm-launcher", "tm-keyring", "tm-sysinfo",
    "tm-eichi", "tm-hashi", "tm-canon",
    "tm-telemetry", "tm-embedding", "tm-tui", "tm-spine",
    # Old okuro-* split server names (pre-unification)
    "okuro-sense", "okuro-cortex", "okuro-keyring", "okuro-roles",
    "okuro-bridge", "okuro-system", "okuro-canon",
]


def _claude_desktop_path(base: Path) -> Path | None:
    """OS-specific path to Claude Desktop's MCP config file.

    Linux uses the lowercase ``~/.config/claude/config.json`` location used by
    community Linux builds (Anthropic ships no official Linux package).
    Returns None on unknown platforms.
    """
    if sys.platform == "darwin":
        return base / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "win32":
        return base / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("linux"):
        # Claude Desktop for Linux (community .deb from aaddrick) reads
        # ~/.config/Claude/claude_desktop_config.json — same filename as
        # macOS, just under XDG config dir with capitalized "Claude".
        return base / ".config" / "Claude" / "claude_desktop_config.json"
    return None


def _chatgpt_desktop_path(base: Path) -> Path | None:
    """OS-specific path to ChatGPT Desktop's MCP config file.

    Experimental: ChatGPT Desktop MCP config schema is not formally
    documented; path is best-effort. Writer no-ops when the parent dir
    doesn't exist (app not installed).
    """
    if sys.platform == "darwin":
        return base / "Library" / "Application Support" / "ChatGPT" / "mcp.json"
    if sys.platform == "win32":
        return base / "AppData" / "Roaming" / "ChatGPT" / "mcp.json"
    return None


def _provider_paths(base: Path) -> dict[str, Path | None]:
    """Return absolute config paths for each supported provider under ``base``.

    Phase B (2026-07-17): DERIVED from the canon consumer registry — the
    ``mcp.path`` in each ``registry/tools/clis/*.yaml`` / ``registry/consumers/
    *.yaml`` is now the single source of these paths, replacing the hardcoded
    table this used to be. (That is where the ``claude → ~/.claude.json``
    user-scope decision and the per-OS desktop paths, incl. None-on-Linux, now
    live.) Proven identical to the former table by
    ``tests/canon/test_consumers.py``. Desktop-app entries still return ``None``
    where the app has no install location.
    """
    from okuro.canon.consumers import consumer_paths
    return consumer_paths(base)


def _okuro_python() -> str:
    """The interpreter to register as the stdio MCP command.

    NOT ``sys.executable``: that is the CURRENTLY RUNNING process's binary,
    which differs by caller — a bare CLI invocation reports
    ``<venv>/bin/python`` while the orchestrator and daemon run renamed copies
    (``<venv>/libexec/okuro-orchestrator``). Registering sys.executable made the
    written command depend on WHICH okuro process wrote it, so:
      * registration_status computed inside the orchestrator saw the existing
        ``bin/python`` entry as different from what it "would write" and
        reported every JSON-config consumer STALE (a false positive in the
        Settings UI — 2026-07-17);
      * a canon deploy triggered from the web UI would have written the
        orchestrator's own libexec binary as Claude Code's spawn command.

    All okuro processes share one venv, so ``sys.prefix`` is stable across them;
    ``<sys.prefix>/bin/python`` is the canonical, process-independent
    interpreter. Fall back to sys.executable only if that path is absent.
    """
    bindir = "Scripts" if sys.platform == "win32" else "bin"
    exe = "python.exe" if sys.platform == "win32" else "python"
    cand = Path(sys.prefix) / bindir / exe
    return str(cand) if cand.exists() else sys.executable


# Consumers whose stdio MCP entry omits the `type` field (they infer stdio from
# `command`, and reject unknown keys). Keyed by provider_id.
_NO_TYPE_STDIO_PROVIDERS = {"antigravity-cli"}


def _build_server_entry(
    module: str,
    env: dict | None = None,
    provider_id: str | None = None,
) -> dict:
    """Stdio MCP entry. When ``provider_id`` is set, inject ``OKURO_PROVIDER``
    into env so the spawned subprocess can self-identify in telemetry without
    relying on /tmp marker freshness across concurrent agent CLIs.
    """
    merged_env: dict = dict(env) if env else {}
    if provider_id:
        merged_env.setdefault("OKURO_PROVIDER", provider_id)
    entry = {
        "type": "stdio",
        "command": _okuro_python(),
        "args": ["-m", module],
    }
    # Antigravity infers stdio from the presence of `command` and its config
    # schema is strict (additionalProperties: false, per the IDE's
    # mcp_config.schema.json), so a `type` key risks rejection. Its own docs
    # show stdio servers as {command, args, env} with no `type`. Every other
    # consumer wants the explicit `type`.
    if provider_id in _NO_TYPE_STDIO_PROVIDERS:
        del entry["type"]
    if merged_env:
        entry["env"] = merged_env
    return entry


def _build_http_server_entry() -> dict:
    """Config blob pointing at the HTTP MCP surface inside ``okuro-daemon``.

    Every supported client accepts this shape for HTTP MCP servers
    (``type: http`` + ``url`` + ``headers``). Codex's TOML format is
    translated in ``write_codex_config``.
    """
    return {
        "type": "http",
        "url": _http_url(),
        "headers": {"Authorization": f"Bearer {_read_http_token()}"},
    }


def _resolve_env(server_def: dict) -> dict | None:
    """Resolve env vars for a server from current environment."""
    env_keys = server_def.get("env_keys", [])
    if not env_keys:
        return None
    env = {}
    for key in env_keys:
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env if env else None


def generate_mcp_entries(
    transport: str | None = None,
    provider_id: str | None = None,
) -> dict:
    """Generate MCP server entries for all okuro servers.

    ``transport`` controls whether the entry points at a per-client stdio
    subprocess or the shared HTTP MCP surface hosted inside okuro-daemon.
    Defaults to stdio.

    ``provider_id`` is the okuro provider name (e.g. ``"claude-code"``,
    ``"antigravity"``, ``"codex"``, ``"cursor"``). When supplied for stdio
    entries, it's injected as ``OKURO_PROVIDER`` so the spawned MCP
    subprocess attributes its telemetry to the correct CLI even when
    other agent CLIs are running concurrently. Irrelevant for HTTP
    transport because every client shares one daemon process.
    """
    resolved = _resolve_transport(transport)

    if resolved == "http":
        # Under HTTP, every "server" collapses into one daemon. Emit a
        # single entry per okuro server name so existing tool-lookup
        # dispatch (tool.name -> server) keeps working for tooling that
        # iterates mcpServers keys.
        #
        # deepcopy, not dict(): a shallow copy left every emitted server
        # sharing ONE headers dict, so setting one server's Authorization
        # rewrote all of them. Harmless while SERVERS holds a single entry —
        # which is exactly why it would have shipped unnoticed the moment a
        # second one appeared.
        http_entry = _build_http_server_entry()
        return {name: copy.deepcopy(http_entry) for name in SERVERS}

    entries = {}
    for name, server_def in SERVERS.items():
        env = _resolve_env(server_def)
        entries[name] = _build_server_entry(
            server_def["module"], env, provider_id=provider_id
        )
    return entries


def _remove_legacy_servers(config: dict) -> int:
    """Remove legacy tm-* server entries from config. Returns count removed."""
    servers = config.get("mcpServers", {})
    removed = 0
    for name in _LEGACY_SERVERS:
        if name in servers:
            del servers[name]
            removed += 1
    return removed


def _clean_claude_settings_local(base: Path) -> int:
    """Remove legacy tm-* entries from Claude Code's enabledMcpjsonServers."""
    settings_path = base / ".claude" / "settings.local.json"
    if not settings_path.exists():
        return 0
    try:
        settings = json.loads(settings_path.read_text())
    except Exception:
        return 0
    enabled = settings.get("enabledMcpjsonServers", [])
    original_len = len(enabled)
    cleaned = [s for s in enabled if s not in _LEGACY_SERVERS]
    if len(cleaned) < original_len:
        settings["enabledMcpjsonServers"] = cleaned
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        return original_len - len(cleaned)
    return 0


def _merge_mcp_servers(config: dict, entries: dict) -> tuple[int, int]:
    """In-place merge okuro entries into ``config['mcpServers']`` dict.

    Returns (added, updated) counts.
    """
    if "mcpServers" not in config:
        config["mcpServers"] = {}

    added = 0
    updated = 0
    for name, entry in entries.items():
        if name in config["mcpServers"]:
            if config["mcpServers"][name] != entry:
                config["mcpServers"][name] = entry
                updated += 1
        else:
            config["mcpServers"][name] = entry
            added += 1
    return added, updated


class ConfigUnreadable(Exception):
    """An existing provider config exists but cannot be parsed.

    Raised instead of silently starting from an empty document. These files
    belong to the user and their other tools — ~/.codex/config.toml is the
    entire Codex CLI config, ~/.mcp.json holds every server the user
    registered. A stray comma is a reason to stop, not a licence to replace
    the file with okuro's entry alone.
    """


def _read_json(path: Path) -> dict:
    """Parse a provider's JSON config.

    A file that exists but does not parse raises: returning ``{}`` here made
    every caller serialize that empty dict straight back over the user's file,
    so one hand-edited trailing comma silently deleted every other registered
    server. ``_clean_claude_settings_local`` already refuses on this condition.

    An EMPTY file (0 bytes / whitespace only) is NOT malformed — it holds
    nothing to preserve, so it is treated as "no config yet" and returns ``{}``.
    Tools legitimately touch a config path into existence before writing it
    (agy creates ~/.gemini/config/mcp_config.json empty on first boot);
    refusing that would block first-time registration for nothing.
    """
    if path.exists():
        text = path.read_text()
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except Exception as exc:
            raise ConfigUnreadable(
                f"{path} exists but is not valid JSON ({exc}). Refusing to "
                f"overwrite it — okuro would drop every other server in it. "
                f"Fix or remove the file, then re-run."
            ) from exc
    return {}


def _write_json(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")


def write_claude_config(
    base_home: Path | None = None, transport: str | None = None
) -> tuple[int, int]:
    """Write okuro MCP servers to Claude USER scope (``~/.claude.json``).

    Writes the top-level ``mcpServers`` block — user scope, which loads in
    every project. Also removes legacy tm-* entries, strips the old
    project-scope ``~/.mcp.json`` shadow that caused the every-session
    disconnect, and (for the real home) cleans Claude Code's
    ``settings.local.json`` enabledMcpjsonServers list.
    """
    base = base_home or real_user_home()
    config_path = _provider_paths(base)["claude"]
    config = _read_json(config_path)

    _remove_legacy_servers(config)
    added, updated = _merge_mcp_servers(
        config, generate_mcp_entries(transport, provider_id="claude-code")
    )

    _write_json(config_path, config)

    # Only clean Claude Code's internal settings when writing to the real home —
    # scoped agent home has no claude-code session state of its own.
    if base_home is None:
        _clean_claude_settings_local(base)

    # Remove the project-scope shadow. okuro used to be written to ~/.mcp.json;
    # left there, that entry outranks the user-scope one we just wrote and
    # starts disconnected every session. Strip ONLY okuro (and legacy names)
    # from .mcp.json, preserving the user's other servers — a no-op once
    # migrated.
    _remove_claude_project_shadow(base)

    return added, updated


def _remove_claude_project_shadow(base: Path) -> int:
    """Strip okuro/legacy keys from ``base/.mcp.json`` (Claude PROJECT scope).

    Canon now owns the USER-scope entry in .claude.json; a duplicate in
    .mcp.json is the shadow that shipped the every-session disconnect. Uses the
    same key-only, other-servers-preserving removal as unregister.
    """
    shadow = base / ".mcp.json"
    try:
        return _remove_okuro_from_json_config(shadow)
    except ConfigUnreadable:
        # Never destroy a malformed user file to clean a shadow — leave it.
        return 0


def write_cursor_config(
    base_home: Path | None = None, transport: str | None = None
) -> tuple[int, int]:
    """Write okuro MCP servers to Cursor config."""
    base = base_home or real_user_home()
    config_path = _provider_paths(base)["cursor"]
    config = _read_json(config_path)

    _remove_legacy_servers(config)
    added, updated = _merge_mcp_servers(
        config, generate_mcp_entries(transport, provider_id="cursor")
    )

    _write_json(config_path, config)
    return added, updated


def _antigravity_ide_path(base: Path) -> Path:
    """Config the Antigravity IDE reads — SEPARATE from the CLI's.

    The IDE builds its path as ``.gemini/<ideName>`` with ideName="antigravity",
    while `agy` reads ``.gemini/config/``. Same product family, two files.
    """
    return base / ".gemini" / "antigravity" / "mcp_config.json"


def write_antigravity_config(
    base_home: Path | None = None, transport: str | None = None
) -> tuple[int, int]:
    """Write okuro MCP servers to the Antigravity config(s).

    Writes BOTH the CLI config `agy` reads (~/.gemini/config/mcp_config.json)
    and the IDE config (~/.gemini/antigravity/mcp_config.json) — two files, one
    okuro registration. Root key ``mcpServers``; the entry omits the ``type``
    field (antigravity infers stdio from ``command`` and its schema rejects
    unknown keys) via provider_id="antigravity-cli". Antigravity has no
    Streamable HTTP transport (stdio/SSE only), so an http request is coerced to
    stdio rather than writing a URL agy cannot use.

    Returns the (added, updated) counts for the CLI config — the surface that is
    end-to-end proven (`agy -p` lists okuro's tools). The IDE write is
    best-effort and verified only at the file level: driving the GUI to confirm
    it LOADS the entry is not possible headless, so a failure there is logged,
    not raised, and never blocks the CLI registration.
    """
    base = base_home or real_user_home()

    resolved = _resolve_transport(transport)
    if resolved == "http":
        resolved = "stdio"  # antigravity has no Streamable HTTP transport

    entries = generate_mcp_entries(resolved, provider_id="antigravity-cli")

    # CLI config — the authoritative, proven surface.
    cli_path = _provider_paths(base)["antigravity"]
    config = _read_json(cli_path)
    _remove_legacy_servers(config)
    added, updated = _merge_mcp_servers(config, entries)
    _write_json(cli_path, config)

    # IDE config — same entry, separate file. Best-effort.
    ide_path = _antigravity_ide_path(base)
    try:
        ide_config = _read_json(ide_path)
        _remove_legacy_servers(ide_config)
        _merge_mcp_servers(ide_config, entries)
        _write_json(ide_path, ide_config)
    except ConfigUnreadable as exc:
        log.warning("antigravity IDE config not written (%s)", exc)
    except Exception:  # noqa: BLE001
        log.exception("antigravity IDE config write failed")

    return added, updated


def write_codex_config(
    base_home: Path | None = None, transport: str | None = None
) -> tuple[int, int]:
    """Write okuro MCP servers to Codex config (``~/.codex/config.toml``).

    Uses tomlkit for comment-preserving round-trip. Codex stores MCP servers
    under ``[mcp_servers.<name>]`` with ``command``, ``args``, optional ``env``
    and per-tool ``[mcp_servers.<name>.tools.<tool>.approval_mode]`` tables.
    """
    import tomlkit

    base = base_home or real_user_home()
    config_path = base / ".codex" / "config.toml"
    resolved_transport = _resolve_transport(transport)

    if config_path.exists():
        try:
            doc = tomlkit.parse(config_path.read_text())
        except Exception as exc:
            # Starting from an empty document here wrote okuro's table back as
            # the WHOLE file: one broken line and the user lost their model,
            # approval_policy, trust settings and every other MCP server in
            # config.toml. _remove_okuro_from_codex_toml already refuses on
            # exactly this condition.
            raise ConfigUnreadable(
                f"{config_path} exists but is not valid TOML ({exc}). Refusing "
                f"to overwrite it — this is the whole Codex CLI config, not "
                f"okuro's file. Fix or remove it, then re-run."
            ) from exc
    else:
        doc = tomlkit.document()

    if "mcp_servers" not in doc:
        doc["mcp_servers"] = tomlkit.table(is_super_table=True)

    servers = doc["mcp_servers"]

    # Remove legacy tm-* entries
    for name in _LEGACY_SERVERS:
        if name in servers:
            del servers[name]

    # Add/update okuro entries
    added = 0
    updated = 0
    entries = generate_mcp_entries(resolved_transport, provider_id="codex")

    for name, entry in entries.items():
        server_table = tomlkit.table()

        if resolved_transport == "http":
            server_table["type"] = "http"
            server_table["url"] = entry["url"]
            headers_table = tomlkit.table()
            for k, v in entry.get("headers", {}).items():
                headers_table[k] = v
            server_table["headers"] = headers_table
            # Codex doesn't need command/args for HTTP; skip them.
        else:
            server_table["command"] = entry["command"]
            server_table["args"] = entry["args"]
            if entry.get("env"):
                env_table = tomlkit.table()
                for k, v in entry["env"].items():
                    env_table[k] = v
                server_table["env"] = env_table

        if name in servers:
            servers[name] = server_table
            updated += 1
        else:
            servers[name] = server_table
            added += 1

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(tomlkit.dumps(doc))
    return added, updated


def write_claude_desktop_config(
    base_home: Path | None = None, transport: str | None = None
) -> tuple[int, int]:
    """Write okuro MCP servers to Claude Desktop's config file.

    No-ops (returns ``(0, 0)``) when:
    - called with a scoped agent home — desktop apps live under the real home;
    - the OS has no canonical Claude Desktop location (Linux);
    - the Claude Desktop config directory doesn't exist (app not installed).
    """
    if base_home is not None:
        return (0, 0)
    base = real_user_home()
    config_path = _claude_desktop_path(base)
    if config_path is None or not config_path.parent.exists():
        return (0, 0)
    config = _read_json(config_path)
    _remove_legacy_servers(config)
    added, updated = _merge_mcp_servers(
        config, generate_mcp_entries(transport, provider_id="claude-desktop")
    )
    _write_json(config_path, config)
    return added, updated


def write_chatgpt_desktop_config(
    base_home: Path | None = None, transport: str | None = None
) -> tuple[int, int]:
    """Write okuro MCP servers to ChatGPT Desktop's config file.

    Experimental — ChatGPT Desktop MCP config schema is not formally
    documented. Same skip rules as :func:`write_claude_desktop_config`.
    """
    if base_home is not None:
        return (0, 0)
    base = real_user_home()
    config_path = _chatgpt_desktop_path(base)
    if config_path is None or not config_path.parent.exists():
        return (0, 0)
    config = _read_json(config_path)
    _remove_legacy_servers(config)
    added, updated = _merge_mcp_servers(
        config, generate_mcp_entries(transport, provider_id="chatgpt-desktop")
    )
    _write_json(config_path, config)
    return added, updated


def _remove_okuro_from_json_config(path: Path) -> int:
    """Remove every SERVERS key from a JSON mcp config file. Returns count removed."""
    if not path.exists():
        return 0
    config = _read_json(path)
    servers = config.get("mcpServers") or {}
    removed = 0
    for name in list(SERVERS.keys()):
        if name in servers:
            del servers[name]
            removed += 1
    # Also scrub legacy names so a round-trip install→uninstall is tidy.
    for legacy in _LEGACY_SERVERS:
        if legacy in servers:
            del servers[legacy]
            removed += 1
    if removed:
        if servers:
            config["mcpServers"] = servers
        else:
            # Leave an empty dict rather than a dangling key if it existed.
            config["mcpServers"] = {}
        _write_json(path, config)
    return removed


def _remove_okuro_from_codex_toml(path: Path) -> int:
    """Strip okuro entries from Codex's config.toml. Preserves other tables."""
    if not path.exists():
        return 0
    try:
        import tomlkit
        doc = tomlkit.parse(path.read_text())
    except Exception:
        return 0
    servers = doc.get("mcp_servers") if "mcp_servers" in doc else doc.get("mcpServers")
    if not servers:
        return 0
    removed = 0
    for name in list(SERVERS.keys()):
        if name in servers:
            del servers[name]
            removed += 1
    for legacy in _LEGACY_SERVERS:
        if legacy in servers:
            del servers[legacy]
            removed += 1
    if removed:
        path.write_text(tomlkit.dumps(doc))
    return removed


# Provider -> okuro provider id, mirroring what each writer passes to
# generate_mcp_entries. Kept beside the writers so a new provider cannot be
# added to one without the other going stale.
# Phase B (2026-07-17): DERIVED from the canon consumer registry
# (short_name → tool_id), replacing the hardcoded table. Pure data — unlike
# cli_probe._KNOWN_CLIS, this has no behavioural coupling, so it can be
# single-sourced outright. Computed once at import (the registry read is cached
# and mcp_config already reads it for SERVERS). tests/canon/test_consumers.py
# proves it equals what the table held.
def _derive_provider_ids() -> dict[str, str]:
    from okuro.canon.consumers import provider_ids
    return provider_ids()


_PROVIDER_IDS = _derive_provider_ids()


def _entry_transport(entry: dict) -> str | None:
    """Classify a config entry as the transport it points at."""
    if not isinstance(entry, dict):
        return None
    declared = entry.get("type")
    if declared in ("stdio", "http"):
        return declared
    # Codex TOML and older writers omit "type": a url means http, a command
    # means a spawned subprocess.
    if entry.get("url"):
        return "http"
    if entry.get("command"):
        return "stdio"
    return None


def _read_codex_servers(path: Path) -> dict:
    """okuro's ``[mcp_servers.*]`` tables from a Codex config.toml."""
    import tomlkit

    try:
        doc = tomlkit.parse(path.read_text())
    except Exception:
        return {}
    servers = doc.get("mcp_servers", {})
    return {k: dict(v) for k, v in servers.items() if isinstance(v, dict)}


def registration_status(
    base_home: Path | None = None, transport: str | None = None
) -> dict:
    """Report how okuro is registered with each provider. Reads only.

    The status board for MCP registration, in the same spirit as
    ``cli_probe.detect_all`` — one source of truth so the settings UI, the
    canon CLI, and ``okuro doctor`` cannot disagree about whether okuro is
    actually wired up.

    Per provider: whether its config exists, whether okuro is in it, which
    transport that entry points at, whether it matches what canon would write
    for ``transport`` right now, and any legacy ``tm-*`` / ``okuro-*`` names
    still lying around. ``stale=True`` means registered but NOT matching — the
    case a redeploy fixes and the one a plain "registered: yes" light hides.

    ``unmanaged`` lists okuro entries found in files canon does NOT write. It
    exists because Claude Code reads user-scoped servers from ``~/.claude.json``
    while canon writes ``~/.mcp.json`` (its project-scoped file, effective only
    when the CLI runs from that directory). An entry can therefore be live in a
    file canon will never update — reporting it beats pretending canon owns it.
    """
    resolved = _resolve_transport(transport)
    base = base_home or real_user_home()
    paths = _provider_paths(base)

    providers: dict[str, dict] = {}
    for name, path in paths.items():
        info: dict = {
            "path": str(path) if path else None,
            "supported": path is not None,
            "config_exists": bool(path and path.exists()),
            "registered": False,
            "transport": None,
            "matches_expected": False,
            "stale": False,
            "legacy_found": [],
        }
        if not path or not path.exists():
            providers[name] = info
            continue

        try:
            if name == "codex":
                servers = _read_codex_servers(path)
            else:
                servers = _read_json(path).get("mcpServers", {}) or {}
        except Exception as exc:  # noqa: BLE001
            info["error"] = str(exc)
            providers[name] = info
            continue

        info["legacy_found"] = [n for n in _LEGACY_SERVERS if n in servers]

        # Antigravity has no Streamable HTTP transport (stdio/SSE only), and its
        # writer coerces http→stdio, so its registration is ALWAYS stdio.
        # Comparing it against an http-expected entry would report false stale
        # whenever the global transport is http; pin it to stdio.
        name_transport = "stdio" if name == "antigravity" else resolved
        expected = generate_mcp_entries(
            name_transport, provider_id=_PROVIDER_IDS.get(name)
        )
        present = [n for n in SERVERS if n in servers]
        info["registered"] = bool(present)
        if present:
            entry = servers[present[0]]
            info["transport"] = _entry_transport(entry)
            # Codex stores a translated shape, so compare transport intent
            # rather than the literal blob it round-trips through tomlkit.
            if name == "codex":
                info["matches_expected"] = info["transport"] == resolved
            else:
                info["matches_expected"] = all(
                    servers.get(n) == e for n, e in expected.items()
                )
            info["stale"] = not info["matches_expected"]
        providers[name] = info

    return {
        "transport_expected": resolved,
        "base_home": str(base),
        "providers": providers,
        "unmanaged": _unmanaged_registrations(base),
    }


def _unmanaged_registrations(base: Path) -> list[dict]:
    """okuro entries Claude Code sees that canon does NOT manage.

    Canon now writes the USER-scope entry in ~/.claude.json, so that is no
    longer surfaced here. What IS surfaced is anything that can SHADOW or
    DISABLE it:
      * a project-scope okuro in ~/.mcp.json — outranks user scope and starts
        disconnected (the every-session-reconnect bug); canon strips it on
        deploy, so seeing it means a deploy hasn't run since migration.
      * okuro in a project's disabledMcpServers — tools silently won't load.
    """
    out: list[dict] = []

    # Project-scope shadow in ~/.mcp.json (outranks the managed user entry).
    shadow = base / ".mcp.json"
    if shadow.exists():
        try:
            servers = (_read_json(shadow).get("mcpServers") or {})
        except ConfigUnreadable:
            servers = {}
        for name in SERVERS:
            if name in servers:
                out.append({
                    "path": str(shadow),
                    "scope": "project",
                    "server": name,
                    "transport": _entry_transport(servers[name]),
                    "note": ("project-scope shadow outranks the managed user "
                             "entry and starts disconnected — run `okuro canon "
                             "deploy` to strip it"),
                })

    claude_json = base / ".claude.json"
    if not claude_json.exists():
        return out
    try:
        cfg = _read_json(claude_json)
    except ConfigUnreadable:
        return out

    for proj_path, proj in (cfg.get("projects") or {}).items():
        disabled = proj.get("disabledMcpServers") or []
        for name in SERVERS:
            if name in disabled:
                out.append({
                    "path": str(claude_json),
                    "scope": f"project:{proj_path}",
                    "server": name,
                    "transport": None,
                    "note": "explicitly DISABLED for this project — tools will not load",
                })
    return out


def unregister_all_configs(base_home: Path | None = None) -> dict[str, int]:
    """Reverse of :func:`write_all_configs` — removes okuro's entries from every
    provider's MCP config file. Returns ``{provider: removed_count}``.

    Safe on any machine: if a provider config doesn't exist or was already
    clean, that provider reports 0 removed. Non-okuro mcpServers entries are
    preserved. The config file itself stays; only okuro keys are stripped.
    """
    base = base_home or real_user_home()
    paths = _provider_paths(base)
    out: dict[str, int] = {}
    for name, path in paths.items():
        if path is None or not path.parent.exists():
            out[name] = 0
            continue
        try:
            if name == "codex":
                out[name] = _remove_okuro_from_codex_toml(path)
            else:
                out[name] = _remove_okuro_from_json_config(path)
        except Exception as exc:  # noqa: BLE001
            out[name] = -1  # signal error; caller can warn
            out[f"_{name}_error"] = str(exc)  # type: ignore[assignment]
    return out


def write_all_configs(
    base_home: Path | None = None, transport: str | None = None
) -> dict[str, tuple[int, int]]:
    """Write MCP configs for all supported providers.

    Args:
        base_home: Optional alternate base directory. When set, writes the
            configs under that path (e.g. a scoped agent HOME). When omitted,
            writes to the user's real home.
        transport: "stdio" (default), "http", or "auto". See module docstring.

    A writer that raises is logged and reported as ``(0, 0)`` so one broken
    provider cannot cost the others their registration.

    KNOWN GAP (unchanged here, pinned by
    tests/cli/test_mcp_config.py::test_write_all_configs_swallows_a_writer_
    exception_as_zeros_CHARACTERIZATION_possible_bug): ``(0, 0)`` is also what
    a legitimate no-op returns — claude_desktop on a machine without the app —
    so the counts alone cannot distinguish a crash from "nothing to do", and
    the return type has no room to say which. The logging below is the only
    signal. Fixing that properly means changing this signature, which is the
    consumer-registry work's job, not a drive-by here.
    """
    results: dict[str, tuple[int, int]] = {}

    for name, writer in [
        ("claude", write_claude_config),
        ("cursor", write_cursor_config),
        ("codex", write_codex_config),
        ("antigravity", write_antigravity_config),
        ("claude_desktop", write_claude_desktop_config),
        ("chatgpt_desktop", write_chatgpt_desktop_config),
    ]:
        try:
            results[name] = writer(base_home, transport)
        except ConfigUnreadable as exc:
            # The user's own file is broken and we refused to clobber it —
            # actionable, so say it plainly rather than as a traceback.
            log.error("mcp config [%s]: %s", name, exc)
            results[name] = (0, 0)
        except Exception:  # noqa: BLE001
            # Was swallowed silently: a crashed writer returned the same (0, 0)
            # as a clean no-op, so a deploy printed a tidy "up to date" for a
            # provider whose registration had just blown up.
            log.exception("mcp config [%s] failed", name)
            results[name] = (0, 0)

    return results


# ---------------------------------------------------------------------------
# CLI — one command to flip every client's transport
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m okuro.cli.mcp_config",
        description=(
            "Write okuro MCP server entries into every supported client "
            "config. Choose the transport with --transport: 'stdio' (one "
            "subprocess per client, the historical default), 'http' (all "
            "clients connect to the shared HTTP MCP surface hosted in "
            "okuro-daemon), or 'auto' "
            "(picks http when the daemon's /health responds, else stdio)."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=sorted(_VALID_TRANSPORTS),
        default="stdio",
        help="Transport to configure. Default: stdio.",
    )
    parser.add_argument(
        "--client",
        choices=[
            "all",
            "claude",
            "cursor",
            "codex",
            "claude_desktop",
            "chatgpt_desktop",
        ],
        default="all",
        help="Which client config to touch. Default: all supported.",
    )
    parser.add_argument(
        "--base-home",
        type=Path,
        default=None,
        help="Write under this directory instead of $HOME (scoped agent home).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without touching any file.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    resolved = _resolve_transport(args.transport)
    if args.dry_run:
        entries = generate_mcp_entries(resolved)
        print(f"transport={resolved}")
        print(json.dumps(entries, indent=2))
        return 0

    if args.client == "all":
        results = write_all_configs(args.base_home, resolved)
    else:
        writer = {
            "claude": write_claude_config,
            "cursor": write_cursor_config,
            "codex": write_codex_config,
            "claude_desktop": write_claude_desktop_config,
            "chatgpt_desktop": write_chatgpt_desktop_config,
        }[args.client]
        results = {args.client: writer(args.base_home, resolved)}

    print(f"transport={resolved}")
    for name, (added, updated) in results.items():
        print(f"  {name}: +{added} ~{updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
