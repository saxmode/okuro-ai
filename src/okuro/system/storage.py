# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Storage metrics collector (cross-platform via psutil).
# index: imports | def get_storage_status | def _collect_mounts | def _get_raid_status
# AGENT_HEADER_END -->
"""Storage metrics collector (cross-platform via psutil)."""

import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psutil


def _important_mounts() -> set[str]:
    """Mounts always reported even when quiet — extend per machine via the
    ``host.important_mounts`` convention (e.g. a model store or RAID)."""
    from okuro.yu.conventions import get_convention

    return {"/", "/home"} | set(get_convention("host.important_mounts", []) or [])


_IMPORTANT_MOUNTS = _important_mounts()
_SKIP_MOUNT_PREFIXES = ("/snap", "/boot/efi", "/System/Volumes")
_SKIP_FSTYPES = {"tmpfs", "devtmpfs", "devfs", "autofs", "overlay", "squashfs"}


def get_storage_status(
    mount: Optional[str] = None,
    alert_threshold: int = 80,
) -> dict:
    """Get storage status including disk usage and (Linux) RAID health."""
    try:
        mounts = _collect_mounts()
        if mount:
            mounts = [m for m in mounts if m["path"] == mount]

        alerts = []
        for m in mounts:
            if m["usage_percent"] > alert_threshold:
                m["alert"] = True
                severity = "critical" if m["usage_percent"] > 95 else "warning"
                alerts.append({
                    "mount": m["path"],
                    "severity": severity,
                    "message": f"Storage at {m['usage_percent']}% — {m['free_gb']:.1f}GB free",
                })
            else:
                m["alert"] = False

        result = {
            "mounts": mounts,
            "alerts": alerts,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        raid = _get_raid_status()
        if raid:
            result["raid"] = raid

        return result

    except Exception as e:
        return {"error": str(e), "timestamp": datetime.now(timezone.utc).isoformat()}


def _collect_mounts() -> list:
    mounts = []
    for part in psutil.disk_partitions(all=False):
        if part.fstype in _SKIP_FSTYPES:
            continue
        if part.mountpoint.startswith(_SKIP_MOUNT_PREFIXES):
            continue
        # Keep the "important" subset or any real block device
        if (
            part.mountpoint not in _IMPORTANT_MOUNTS
            and not part.device.startswith("/dev/")
        ):
            continue

        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue

        mounts.append({
            "path": part.mountpoint,
            "device": part.device,
            "size_gb": round(usage.total / (1024 ** 3), 1),
            "used_gb": round(usage.used / (1024 ** 3), 1),
            "free_gb": round(usage.free / (1024 ** 3), 1),
            "usage_percent": int(round(usage.percent)),
        })

    return mounts


def _get_raid_status() -> Optional[dict]:
    """Read Linux software-RAID state from /proc/mdstat. Silently skipped on macOS."""
    if platform.system() != "Linux":
        return None
    mdstat = Path("/proc/mdstat")
    if not mdstat.exists():
        return None
    try:
        content = mdstat.read_text()
        m = re.search(r"(md\d+)\s*:\s*(\w+)\s+(raid\d+)\s+(.*?)$", content, re.MULTILINE)
        if not m:
            return None

        state_match = re.search(r"\[([U_]+)\]", content)
        state = state_match.group(0) if state_match else "[??]"
        active = state.count("U")
        total = len(state) - 2

        return {
            "device": m.group(1),
            "status": "healthy" if m.group(2) == "active" and active == total else "degraded",
            "type": m.group(3),
            "state": state,
            "active_drives": active,
            "total_drives": total,
        }
    except Exception:
        return None
