# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Desktop notification channel — notify-send on Linux, osascript on macOS.
# index: imports | def _resolve_session_env | def _detect_display_from_compositor | class DesktopChannel
# AGENT_HEADER_END -->
"""Desktop notification channel.

Linux path: ``notify-send`` (libnotify) over the user's DBus session.
macOS path: ``osascript`` issuing a ``display notification`` AppleScript —
no entitlements, no Cocoa app required, ships on every Mac. AppleScript
notifications don't support inline actions, so the ACKNOWLEDGED return
path is Linux-only; macOS always returns SENT for delivered messages.
Windows is unsupported (see service_manager.py decision P1).
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path

from . import register_channel
from .base import Channel, DeliveryResult, DeliveryStatus, Message

log = logging.getLogger("okuro.channels.desktop")

_URGENCY_MAP = {
    1: "low",
    2: "low",
    3: "normal",
    4: "critical",
    5: "critical",
}


def _resolve_session_env() -> dict[str, str]:
    """Resolve graphical session env for headless contexts (systemd timers)."""
    env = os.environ.copy()
    uid = os.getuid()

    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        bus = Path(f"/run/user/{uid}/bus")
        if bus.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"

    if "DISPLAY" not in env and "WAYLAND_DISPLAY" not in env:
        display = _detect_display_from_compositor()
        if display:
            env["DISPLAY"] = display

    return env


def _detect_display_from_compositor() -> str | None:
    """Read DISPLAY from the running compositor/WM process environment."""
    compositors = [
        "gnome-shell", "cinnamon", "kwin_x11", "kwin_wayland",
        "xfce4-session", "sway", "plasmashell", "mutter",
    ]
    for name in compositors:
        try:
            result = subprocess.run(
                ["pgrep", "-x", name],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode != 0:
                continue
            pid = result.stdout.strip().splitlines()[0]
            environ = Path(f"/proc/{pid}/environ").read_bytes()
            for entry in environ.split(b"\0"):
                if entry.startswith(b"DISPLAY="):
                    return entry.decode().split("=", 1)[1]
                if entry.startswith(b"WAYLAND_DISPLAY="):
                    return entry.decode().split("=", 1)[1]
        except Exception:
            continue
    return None


@register_channel
class DesktopChannel(Channel):
    """Send notifications via the OS-native notification system."""

    _name = "desktop"

    def __init__(self) -> None:
        self._is_darwin = platform.system() == "Darwin"
        # On Linux we need the DBus + DISPLAY env even when the daemon is
        # running headless under systemd. On macOS osascript inherits enough
        # session context from launchd to talk to NotificationCenter, so we
        # skip the resolve to avoid the /proc and pgrep walk that doesn't
        # exist on macOS.
        self._env = {} if self._is_darwin else _resolve_session_env()

    @property
    def capabilities(self) -> set[str]:
        # macOS osascript notifications can't carry inline actions, so the
        # "interactive" capability is Linux-only.
        return {"text"} if self._is_darwin else {"text", "interactive"}

    def available(self) -> bool:
        if self._is_darwin:
            return shutil.which("osascript") is not None
        return (
            shutil.which("notify-send") is not None
            and ("DISPLAY" in self._env or "WAYLAND_DISPLAY" in self._env)
        )

    def send(self, message: Message) -> DeliveryResult:
        if not self.available():
            return DeliveryResult(
                channel=self._name,
                status=DeliveryStatus.FAILED,
                error="osascript not available" if self._is_darwin else "notify-send not available",
            )
        if self._is_darwin:
            return self._send_darwin(message)
        return self._send_linux(message)

    # ── Linux backend ────────────────────────────────────────────────

    def _send_linux(self, message: Message) -> DeliveryResult:
        urgency = _URGENCY_MAP.get(message.urgency, "normal")

        cmd = [
            "notify-send",
            "--urgency", urgency,
            "--app-name", "Okuro",
        ]

        if urgency != "critical":
            cmd.extend(["--expire-time", "10000"])

        if message.acknowledge_id:
            cmd.extend(["--action", "ack=Acknowledge"])

        cmd.extend([message.title, message.body])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                env=self._env,
            )

            if result.returncode != 0:
                return DeliveryResult(
                    channel=self._name,
                    status=DeliveryStatus.FAILED,
                    error=result.stderr.strip(),
                )

            stdout = result.stdout.strip()
            if message.acknowledge_id and stdout == "ack":
                return DeliveryResult(
                    channel=self._name,
                    status=DeliveryStatus.ACKNOWLEDGED,
                    payload={"acknowledge_id": message.acknowledge_id},
                )

            return DeliveryResult(
                channel=self._name,
                status=DeliveryStatus.SENT,
                payload={"urgency": urgency},
            )

        except subprocess.TimeoutExpired:
            return DeliveryResult(
                channel=self._name,
                status=DeliveryStatus.FAILED,
                error="notify-send timed out",
            )
        except Exception as e:
            return DeliveryResult(
                channel=self._name,
                status=DeliveryStatus.FAILED,
                error=str(e),
            )

    # ── macOS backend ────────────────────────────────────────────────

    def _send_darwin(self, message: Message) -> DeliveryResult:
        urgency = _URGENCY_MAP.get(message.urgency, "normal")

        # AppleScript string literals are quoted with " and escape \ and ".
        # We inline title + body into a `display notification` call. There's
        # no urgency dial in AppleScript notifications — critical messages
        # rely on the user's NotificationCenter alert-style preference.
        def _esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        script = (
            f'display notification "{_esc(message.body)}" '
            f'with title "Okuro" '
            f'subtitle "{_esc(message.title)}"'
        )
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return DeliveryResult(
                    channel=self._name,
                    status=DeliveryStatus.FAILED,
                    error=result.stderr.strip() or "osascript failed",
                )
            # osascript notifications are fire-and-forget — there's no
            # acknowledge channel, so even if message.acknowledge_id is set
            # we return SENT, not ACKNOWLEDGED. Callers that need ACK must
            # use a different channel on macOS.
            return DeliveryResult(
                channel=self._name,
                status=DeliveryStatus.SENT,
                payload={"urgency": urgency, "backend": "osascript"},
            )
        except subprocess.TimeoutExpired:
            return DeliveryResult(
                channel=self._name,
                status=DeliveryStatus.FAILED,
                error="osascript timed out",
            )
        except Exception as e:
            return DeliveryResult(
                channel=self._name,
                status=DeliveryStatus.FAILED,
                error=str(e),
            )
