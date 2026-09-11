# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Install and authenticate inference CLIs from canon entries via spawned OS terminal.
# index:
#   imports
#   def list_inference_clis
#   def detect_state
#   def _spawn_terminal
#   def install_cli
#   def authenticate_cli
# AGENT_HEADER_END -->
"""Install and authenticate inference CLIs via a spawned OS-native terminal.

Reads install metadata from canon (``tools/clis/*.yaml``) so this module
doesn't hardcode any package names, binary paths, or auth commands — the
YAML is the source of truth.

The onboarding UI calls :func:`list_inference_clis` to render its step-1
grid, then :func:`install_cli` / :func:`authenticate_cli` to trigger the
user-facing actions. Both spawn a terminal owned by the user's desktop
session (gnome-terminal / Terminal.app / ...) running the command, then
return immediately — the frontend polls :func:`detect_state` for
completion.

Security: every command string comes from the canon YAML, not user input.
We still pass argv as a ``bash -c`` string because the canon authors are
okuro maintainers, not arbitrary users.
"""

from __future__ import annotations

import json
import logging
import platform
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("okuro.system.cli_installer")


# ── Public API ──────────────────────────────────────────────────────


def _resolve_user_binary(binary: str) -> Optional[str]:
    """Find ``binary`` against the orchestrator's PATH AND the user's
    runtime version-manager / Homebrew / package-manager layout.

    Re-evaluated at every call so a CLI installed AFTER the orchestrator
    started (e.g. user runs `npm install -g @anthropic-ai/claude-code`
    while the dashboard is open) shows up on the next refresh — no
    service reinstall, no orchestrator restart.

    Layered lookup:

      1. ``shutil.which(binary)`` — orchestrator's current PATH (fast,
         covers everything the plist baked in at install time).
      2. ``shutil.which(binary, path=":".join(_user_path_dirs()))`` —
         dynamic probe of nvm-newest, fnm/asdf shims, Homebrew, user
         local bins, etc. Picks up tools installed since the plist
         was rendered.

    Returns the absolute path of the first hit, or None if no probe
    found the binary.
    """
    p = shutil.which(binary)
    if p:
        return p
    # Lazy import — avoids a heavyweight import on every cli_installer
    # access when service_manager isn't otherwise needed.
    from okuro.system.service_manager import _user_path_dirs

    extra = ":".join(_user_path_dirs())
    if extra:
        p = shutil.which(binary, path=extra)
        if p:
            return p
    return None


def list_inference_clis() -> list[dict]:
    """Return canon entries with ``type=cli_tool`` and ``category=inference_cli``,
    each annotated with current ``installed`` / ``authenticated`` state.
    """
    from okuro.canon.registry import list_tools

    home = Path.home()
    out = []
    for tool in list_tools():
        if tool.get("type") != "cli_tool":
            continue
        if tool.get("category") != "inference_cli":
            continue
        tid = tool.get("tool_id") or tool.get("id")
        binary = tool.get("binary") or tid
        path = _resolve_user_binary(binary)
        installed = path is not None
        authenticated = _check_auth(tid, home) if installed else None
        out.append({
            "tool_id": tid,
            "name": tool.get("name", tid),
            "description": tool.get("description", ""),
            "provider_id": tool.get("provider_id", tid),
            "recommended": bool(tool.get("recommended", False)),
            "docs_url": tool.get("docs_url"),
            "binary": binary,
            "installed": installed,
            "path": path,
            "authenticated": authenticated,
            # Expose OS-specific install block so the frontend can show a
            # preview of what will run in the terminal.
            "install_preview": _install_preview(tool),
            "authenticate": tool.get("authenticate", {}),
        })
    # Recommended first, then alphabetical.
    out.sort(key=lambda t: (not t["recommended"], t["tool_id"]))
    return out


def detect_state(tool_id: str) -> dict:
    """Lightweight single-tool state check used by the polling endpoint.

    Uses the same dynamic resolver as ``list_inference_clis`` so the
    polling endpoint sees a freshly-installed CLI without waiting for a
    service reinstall.
    """
    from okuro.canon.registry import get_tool

    tool = get_tool(tool_id)
    if not tool:
        return {"tool_id": tool_id, "error": "unknown tool"}
    binary = tool.get("binary") or tool_id
    path = _resolve_user_binary(binary)
    installed = path is not None
    return {
        "tool_id": tool_id,
        "installed": installed,
        "path": path,
        "authenticated": _check_auth(tool_id, Path.home()) if installed else None,
    }


def install_cli(tool_id: str) -> dict:
    """Open a terminal running the canon install command for ``tool_id``.

    Returns ``{status, command, terminal}``:
      - status="spawned" + terminal=<name>    terminal opened, user is running install
      - status="no_terminal" + command=str    no supported terminal found; caller
                                              should render a copy-to-clipboard UI
      - status="missing_prereq" + prereq=...  install command needs something the
                                              machine doesn't have (e.g. Homebrew
                                              on Mac, Node on Linux)
      - status="unsupported_os" | "unknown_tool"
    """
    from okuro.canon.registry import get_tool

    tool = get_tool(tool_id)
    if not tool:
        return {"status": "unknown_tool", "tool_id": tool_id}

    os_key = _os_key()
    if os_key not in ("linux", "darwin"):
        return {"status": "unsupported_os", "os": platform.system()}

    install_block = (tool.get("install") or {}).get(os_key) or {}
    command = install_block.get("command")
    alt_command = install_block.get("alt_command")
    if not command:
        return {"status": "no_command_for_os", "os": os_key}

    # Pre-flight: on Mac the default install often uses Homebrew (`brew install
    # --cask codex`). If the user doesn't have Homebrew, fall back to the
    # alt_command (usually the npm path) if present, else surface an
    # actionable error the UI can show. Never silently "spawn a terminal that
    # will immediately fail with `brew: command not found`".
    command = _select_command(command, alt_command)
    if command is None:
        # Both primary and fallback require something missing.
        hint = _missing_prereq_hint(install_block, os_key)
        return {
            "status": "missing_prereq",
            "tool_id": tool_id,
            "prereq": hint["prereq"],
            "message": hint["message"],
            "install_command_hint": install_block.get("command"),
            "alt_command_hint": alt_command,
        }

    # Combine install + verification, leave the terminal open so the user sees
    # any errors.
    check = install_block.get("post_install_check")
    full_cmd = command + (f" && {check}" if check else "") + "; echo; echo '[ Done — you can close this window ]'; read -r _"
    return _spawn_terminal(full_cmd, title=f"okuro — install {tool_id}")


# Known install-command prefixes that REQUIRE a specific tool on PATH. If the
# tool isn't there we either fall back to alt_command or surface a clear error.
# Deliberately small and explicit — add rows here, not regex cleverness.
_COMMAND_PREREQS = [
    ("brew ",    "brew",    "Homebrew (https://brew.sh)"),
    ("npm ",     "npm",     "Node.js with npm (https://nodejs.org)"),
    ("pnpm ",    "pnpm",    "pnpm (https://pnpm.io)"),
    ("yarn ",    "yarn",    "Yarn (https://yarnpkg.com)"),
    ("pipx ",    "pipx",    "pipx (https://pipx.pypa.io)"),
    ("curl ",    "curl",    "curl (install via your package manager)"),
]


def _command_missing_prereq(cmd: str) -> Optional[tuple[str, str]]:
    """If ``cmd`` starts with a known tool that isn't on PATH, return
    ``(tool_name, human_description)``. Otherwise None.
    """
    for prefix, tool, desc in _COMMAND_PREREQS:
        if cmd.lstrip().startswith(prefix) and not shutil.which(tool):
            return tool, desc
    return None


def _select_command(command: str, alt_command: Optional[str]) -> Optional[str]:
    """Pick the best install command given what's on the machine.

    Returns the primary command if its prereq is installed (or it has none).
    Otherwise returns ``alt_command`` if its prereq is installed. Returns
    None if neither is runnable — the caller surfaces a prereq error.
    """
    if _command_missing_prereq(command) is None:
        return command
    if alt_command and _command_missing_prereq(alt_command) is None:
        return alt_command
    return None


def _missing_prereq_hint(install_block: dict, os_key: str) -> dict:
    """Build the human-readable error for the missing_prereq status."""
    primary = install_block.get("command", "")
    alt = install_block.get("alt_command")
    primary_missing = _command_missing_prereq(primary)
    alt_missing = _command_missing_prereq(alt) if alt else None

    if primary_missing and alt_missing:
        tool, desc = primary_missing
        message = (
            f"This install needs {desc} (primary) or "
            f"{alt_missing[1]} (fallback). Install one and try again."
        )
        return {"prereq": tool, "message": message}

    if primary_missing:
        tool, desc = primary_missing
        install_hint = _platform_install_hint(tool, os_key)
        message = f"This install needs {desc}. {install_hint}"
        return {"prereq": tool, "message": message}

    return {"prereq": "unknown", "message": "Install prerequisites missing."}


def _platform_install_hint(tool: str, os_key: str) -> str:
    """Per-OS quick hint for installing a missing prereq. Copy-paste-able."""
    if tool == "brew" and os_key == "darwin":
        return 'Run `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`.'
    if tool == "npm":
        if os_key == "darwin":
            return "Run `brew install node` (needs Homebrew) or install from https://nodejs.org."
        return "Ubuntu/Debian: `sudo apt install nodejs npm`. Fedora: `sudo dnf install nodejs`."
    return "Install it, then click the Install button again."


def authenticate_cli(tool_id: str) -> dict:
    """Open a terminal running the canon authenticate command."""
    from okuro.canon.registry import get_tool

    tool = get_tool(tool_id)
    if not tool:
        return {"status": "unknown_tool", "tool_id": tool_id}

    auth = tool.get("authenticate") or {}
    command = auth.get("command")
    if not command:
        return {"status": "no_auth_command", "tool_id": tool_id}

    full_cmd = command + "; echo; echo '[ Auth step finished — you can close this window ]'; read -r _"
    return _spawn_terminal(full_cmd, title=f"okuro — authenticate {tool_id}")


# ── Internals ───────────────────────────────────────────────────────


def _os_key() -> str:
    s = platform.system().lower()
    return "darwin" if s == "darwin" else ("linux" if s == "linux" else s)


def _install_preview(tool: dict) -> dict[str, Optional[str]]:
    """Expose command strings the frontend can show before the user clicks Install."""
    out: dict[str, Optional[str]] = {}
    for os_key in ("linux", "darwin"):
        block = (tool.get("install") or {}).get(os_key) or {}
        out[os_key] = block.get("command")
    return out


# macOS-specific Keychain service names for CLIs that don't write a file.
# Sourced from observed `security find-generic-password` output on a freshly
# authenticated Claude Code install (Mac install report 2026-04-28). Anthropic
# owns the service name; promote to canon's `tools/clis/*.yaml` if/when a
# second Keychain-backed CLI lands.
_DARWIN_KEYCHAIN_SERVICES: dict[str, str] = {
    "claude-code": "Claude Code-credentials",
    "claude": "Claude Code-credentials",
}


def _check_darwin_keychain_item(service: str) -> bool:
    """Return True iff a generic-password Keychain item with this service exists.

    Uses ``security find-generic-password -s <service>`` which prints item
    METADATA (not the secret) when present and exits 0. Reading metadata
    does not require unlock or trigger the ACL prompt — only reading the
    secret value would. Returns False on any failure (no security binary,
    timeout, item missing) so a broken probe never produces a false GREEN.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _check_auth(tool_id: str, home: Path) -> Optional[bool]:
    """Per-CLI auth probe — delegates to the canonical ``cli_probe`` module.

    The previous implementation was filesystem-only and accepted the mere
    existence of a credentials file (or macOS keychain entry) as proof of
    authentication. Two failure modes:

      1. Stale credentials (expired tokens, leftover keychain entries from
         prior installs / different accounts) returned True even though the
         CLI itself rejected them at invoke time → wizard rendered green,
         user hit "Not logged in" on first task.

      2. Claude Code's keychain entry on Darwin survives `claude /login` /
         `claude /logout` cycles in some Tahoe versions. File / keychain
         existence ≠ valid session.

    ``cli_probe`` asks the CLI itself (``claude auth status --json``,
    ``codex login status``) where a status verb exists, and falls back to
    JWT-exp / oauth-expiry parsing where it doesn't. This is the same
    probe the wizard, dashboard, bridge, and doctor consume — all four
    surfaces now agree by construction.

    The ``home`` argument is accepted for API compatibility but ignored;
    cli_probe always reads from ``Path.home()``. Returns True/False/None
    matching the pre-existing contract.
    """
    # Translate the canon's tool_id → cli_probe canonical name. Canon uses
    # "claude-code" for the Anthropic CLI; cli_probe uses "claude" because
    # that's the binary name. Other ids match.
    name = {"claude-code": "claude"}.get(tool_id, tool_id)
    try:
        from okuro.system.cli_probe import detect as _detect
    except Exception:
        return None
    try:
        state = _detect(name)
    except ValueError:
        # tool_id not in cli_probe's registry — no probe defined.
        return None
    if state.auth.state == "ok":
        return True
    # "ineligible" is a DEFINITE negative, not ignorance: the credentials are
    # valid and the CLI still refuses (retired tier). Letting it fall through to
    # None would render as "unknown" in the UI for a state we have proof of.
    # NOTE: this bool cannot say "signed in but not entitled" — the label some
    # callers pair with False ("not signed in") is inaccurate for this case.
    # auth.detail carries the real reason; a richer status type is the fix.
    if state.auth.state in ("missing", "expired", "ineligible"):
        return False
    # "unknown" / "error" — we genuinely don't know.
    return None


# Ordered list of desktop terminals to try on Linux. First match wins.
# Order is "most likely on this user's box" — we lead with GNOME/KDE/Cinnamon
# defaults, then modern keyboard-driven terminals (kitty/alacritty/wezterm/
# ghostty/foot), then DE-bundled fallbacks (xfce4/tilix), then xterm as the
# last-resort guarantee.
_LINUX_TERMINALS: list[tuple[str, list[str]]] = [
    # (binary, argv template — {cmd} placeholder gets replaced with shell-escaped command)
    ("gnome-terminal", ["gnome-terminal", "--title={title}", "--", "bash", "-lc", "{cmd}"]),
    ("konsole",        ["konsole", "-p", "tabtitle={title}", "-e", "bash", "-lc", "{cmd}"]),
    ("kitty",          ["kitty", "--title", "{title}", "bash", "-lc", "{cmd}"]),
    ("alacritty",      ["alacritty", "-t", "{title}", "-e", "bash", "-lc", "{cmd}"]),
    # WezTerm: --new-tab-cmd takes the program; "start" creates a new window.
    ("wezterm",        ["wezterm", "start", "--always-new-process", "--", "bash", "-lc", "{cmd}"]),
    # Ghostty (GTK4): -e takes the command directly. Wayland-first so this
    # may be the only working terminal on Fedora Silverblue / minimal Sway.
    ("ghostty",        ["ghostty", "-e", "bash -lc {cmd_q}"]),
    # foot (Wayland-only on most distros): the canonical Sway/Hyprland choice.
    ("foot",           ["foot", "--title={title}", "bash", "-lc", "{cmd}"]),
    ("tilix",          ["tilix", "-t", "{title}", "-e", "bash -lc {cmd_q}"]),
    ("xfce4-terminal", ["xfce4-terminal", "--title={title}", "--command=bash -lc {cmd_q}"]),
    ("terminator",     ["terminator", "-T", "{title}", "-x", "bash", "-lc", "{cmd}"]),
    ("xterm",          ["xterm", "-T", "{title}", "-e", "bash", "-lc", "{cmd}"]),
]


# macOS terminal candidates. Terminal.app is guaranteed; iTerm2/Ghostty/
# WezTerm are user-installed. We probe via `osascript -e 'application "X"'`
# existence rather than `shutil.which` because GUI apps live in /Applications
# and may not have a CLI shim on PATH. iTerm2 + Terminal.app share the
# AppleScript "do script" verb; Ghostty/WezTerm prefer their own CLI binary
# (`ghostty`, `wezterm`).
_DARWIN_GUI_TERMINALS: list[tuple[str, str]] = [
    # (display_name, applescript_app_name)
    ("iTerm",   "iTerm"),    # iTerm2 — most common power-user choice
    ("Terminal", "Terminal"),
]


def _spawn_terminal(cmd: str, title: str) -> dict:
    """Spawn an OS-native terminal running ``cmd``. Detached, non-blocking."""
    os_key = _os_key()

    if os_key == "darwin":
        # Try CLI-shim terminals first (Ghostty, WezTerm, kitty if installed
        # via brew) — they spawn faster and don't require AppleScript. Fall
        # back to AppleScript-driven GUI terminals (iTerm, Terminal.app).
        for binary, argv in (
            ("ghostty",  ["ghostty", "-e", f"bash -lc {shlex.quote(cmd)}"]),
            ("wezterm",  ["wezterm", "start", "--always-new-process", "--", "bash", "-lc", cmd]),
            ("kitty",    ["kitty", "--title", title, "bash", "-lc", cmd]),
        ):
            if not shutil.which(binary):
                continue
            try:
                subprocess.Popen(
                    argv,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return {"status": "spawned", "terminal": binary, "command": cmd}
            except Exception as exc:
                logger.warning("%s spawn failed: %s", binary, exc)
                continue

        # AppleScript: iTerm first (more common in power-user setups), then
        # Terminal.app as the always-present fallback. We escape backslash
        # and double-quote so the user's command survives the embedded
        # AppleScript string.
        escaped = cmd.replace("\\", "\\\\").replace('"', '\\"')
        for display_name, app_name in _DARWIN_GUI_TERMINALS:
            # `tell application "iTerm"` in AppleScript is non-fatal even
            # when iTerm isn't installed — it offers to install. To avoid
            # that prompt, we probe via `osascript -e 'id of app "X"'` first
            # which fails fast if the app isn't on the system.
            probe = subprocess.run(
                ["osascript", "-e", f'id of application "{app_name}"'],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if probe.returncode != 0:
                continue
            if app_name == "iTerm":
                # iTerm's "do script" creates a new window; older iTerm2
                # versions also accept it (legacy alias from Terminal).
                script = (
                    f'tell application "iTerm"\n'
                    f'  create window with default profile command "{escaped}"\n'
                    f'end tell'
                )
            else:
                script = f'tell application "{app_name}" to do script "{escaped}"'
            try:
                subprocess.Popen(
                    ["osascript", "-e", script],
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return {"status": "spawned", "terminal": display_name, "command": cmd}
            except Exception as exc:
                logger.warning("%s spawn failed: %s", display_name, exc)
                continue

        return {"status": "no_terminal", "command": cmd}

    # Linux — walk the candidate list.
    for binary, template in _LINUX_TERMINALS:
        if not shutil.which(binary):
            continue
        argv = [
            arg.format(title=title, cmd=cmd, cmd_q=shlex.quote(cmd))
            for arg in template
        ]
        try:
            subprocess.Popen(
                argv,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            logger.warning("%s spawn failed: %s", binary, exc)
            continue
        return {"status": "spawned", "terminal": binary, "command": cmd}

    return {"status": "no_terminal", "command": cmd}
