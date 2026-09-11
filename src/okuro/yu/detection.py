# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: System auto-detection for `okuro init`.
# index:
#   imports
#   def detect_system
#   def detect_os
#   def detect_shell
#   def detect_timezone
#   def detect_python
#   def detect_gpus
#   def detect_providers
#   def detect_tools
# AGENT_HEADER_END -->
"""System auto-detection for `okuro init`.

Detects: timezone, shell, OS, GPUs, providers (Claude/Codex/Cursor/Antigravity),
available tools, Python version. Results populate the profile and conventions.
"""

import os
import platform
import shutil
import subprocess
from pathlib import Path


def detect_system() -> dict:
    """Run all detectors and return a combined result dict."""
    return {
        "os": detect_os(),
        "shell": detect_shell(),
        "timezone": detect_timezone(),
        "python": detect_python(),
        "gpus": detect_gpus(),
        "providers": detect_providers(),
        "tools": detect_tools(),
    }


def detect_os() -> dict:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
    }


def detect_shell() -> str:
    return os.environ.get("SHELL", "unknown")


def detect_timezone() -> str:
    """Detect the system timezone as an IANA/Olson name (e.g. 'Europe/Zurich').

    Resolution order (cross-platform):
      1. /etc/timezone — many Linux distros
      2. /etc/localtime symlink — Linux + macOS point at /usr/share/zoneinfo/<Region>/<City>
      3. timedatectl — systemd Linux only
      4. $TZ env var
      5. 'UTC' fallback
    """
    # 1. /etc/timezone (plain text on Debian/Ubuntu and others)
    tz_file = Path("/etc/timezone")
    if tz_file.exists():
        try:
            tz = tz_file.read_text().strip()
            if tz:
                return tz
        except Exception:
            pass

    # 2. /etc/localtime symlink (Linux + macOS)
    localtime = Path("/etc/localtime")
    try:
        if localtime.is_symlink() or localtime.exists():
            target = localtime.resolve()
            parts = target.parts
            if "zoneinfo" in parts:
                idx = parts.index("zoneinfo")
                name = "/".join(parts[idx + 1 :])
                if name:
                    return name
    except Exception:
        pass

    # 3. systemd timedatectl (Linux only, silently skipped elsewhere)
    if shutil.which("timedatectl"):
        try:
            result = subprocess.run(
                ["timedatectl", "show", "--property=Timezone", "--value"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass

    # 4 + 5. TZ env var or UTC fallback
    return os.environ.get("TZ", "UTC")


def detect_python() -> dict:
    return {
        "version": platform.python_version(),
        "executable": os.path.realpath(os.sys.executable),
    }


def detect_gpus() -> list[dict]:
    """Detect NVIDIA GPUs via nvidia-smi."""
    if not shutil.which("nvidia-smi"):
        return []
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                gpus.append({
                    "id": int(parts[0]),
                    "hardware": parts[1],
                    "vram_mb": int(float(parts[2])),
                })
        return gpus
    except Exception:
        return []


def detect_providers() -> dict[str, bool]:
    """Check which AI provider CLIs/configs are present.

    Dir-existence is the strongest signal (an authenticated CLI leaves a
    config dir); binary-on-PATH is the fallback. Uses the enhanced PATH
    from okuro.system.provider_path so macOS apps launched from Finder
    still see brew + npm-global bins.
    """
    from okuro.system.provider_path import which as provider_which

    home = Path.home()
    return {
        "claude": (home / ".claude").is_dir() or provider_which("claude") is not None,
        "codex": (home / ".codex").is_dir() or provider_which("codex") is not None,
        "cursor": (home / ".cursor").is_dir() or provider_which("cursor") is not None,
        # `gemini` was retired 2026-07-18. antigravity replaces it, but it
        # SHARES ~/.gemini as its config dir — so dir-existence would report
        # true for a leftover gemini install with no agy binary. Binary on
        # PATH is the only unambiguous signal here.
        "antigravity": provider_which("agy") is not None,
    }


def detect_tools() -> dict[str, bool]:
    """Check which system tools are available."""
    tools = ["git", "docker", "nvidia-smi", "ss", "sqlite3", "npm", "node"]
    return {t: shutil.which(t) is not None for t in tools}
