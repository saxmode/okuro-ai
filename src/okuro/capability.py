# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Canonical hardware capability probe — GPU/CPU/RAM + display GPU + tier.
# index:
#   imports
#   def _detect_gpus_nvml
#   def _drm_card_bdf
#   def _has_connected_connector
#   def _nvidia_pci_bdf_to_index
#   def _display_gpu_indices_linux
#   def _detect_display_gpu_index_linux
#   def _detect_display_gpu_index_macos
#   def is_display_gpu
#   def capabilities
#   def recommended_tier
#   def recommended_device
# AGENT_HEADER_END -->
"""Canonical hardware capability probe — GPU/CPU/RAM + display GPU + tier.

Promoted from ``okuro.embed.detect`` (P0 of the local-inference plan) so the
single hardware/capability authority is shared, not embed-specific. Consumed
by embeddings today; the inference broker, model-catalog ranking, and tier
resolution are the next consumers — all read one probe, no per-subsystem
nvidia-smi calls.

``capabilities()`` returns a JSON-friendly host summary. The single most
important rule lives in ``recommended_device``: **the display GPU is NEVER
auto-selected** for any okuro GPU service. Auto-selecting the display GPU is
what caused months of Chrome GPU-process freezes on the reference host through
2026-05-10. The onboarding picker may override it (with a warning); auto
resolution may not.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("okuro.capability")


def _detect_gpus_nvml() -> list[dict[str, Any]]:
    """Detect NVIDIA GPUs via nvidia-smi --query-gpu (CSV output).

    Avoids importing pynvml so detection works without a GPU build of the
    venv (LOW-tier laptops). Returns ``[]`` when nvidia-smi is missing or
    the call fails.
    """
    if shutil.which("nvidia-smi") is None:
        return []
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("nvidia-smi failed: %s", exc)
        return []

    gpus: list[dict[str, Any]] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            idx = int(parts[0])
            mib = int(parts[2])
        except ValueError:
            continue
        gpus.append({
            "index": idx,
            "name": parts[1],
            "vram_gb": round(mib / 1024, 1),
            "is_display": False,  # filled in by caller
        })
    return gpus


def gpu_memory_free() -> dict[int, float]:
    """Live free VRAM per CUDA index in GB via nvidia-smi (``{}`` if absent).

    The broker's single-authority admission reads this — not just okuro's own
    lease ledger — so VRAM claimed by non-okuro users (ComfyUI, raw
    tm-inference, Ollama) is respected and never double-booked.
    """
    if shutil.which("nvidia-smi") is None:
        return {}
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("nvidia-smi memory.free failed: %s", exc)
        return {}

    free: dict[int, float] = {}
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            free[int(parts[0])] = round(int(parts[1]) / 1024, 2)
        except ValueError:
            continue
    return free


# Known VRAM tenants → cmdline/name substrings. Ordered: first match wins.
# Lets the broker name WHAT holds a GPU (ComfyUI, Ollama, a personal service),
# not just how much — nvidia-smi free already covers the "how much".
# Personal GPU services register theirs via the ``gpu.tenant_patterns``
# convention (list of [name, [substrings...]]); those match FIRST.
def _tenant_patterns() -> list[tuple[str, tuple[str, ...]]]:
    from okuro.yu.conventions import get_convention

    user = [
        (str(name), tuple(str(s) for s in subs))
        for name, subs in (get_convention("gpu.tenant_patterns", []) or [])
    ]
    return user + [
        ("comfyui", ("comfyui", "comfy")),
        ("ollama", ("ollama",)),
        ("okuro-engine", ("llama_cpp.server", "vllm.entrypoints", "llama-server", "okuro")),
    ]


_TENANT_PATTERNS = _tenant_patterns()


def _classify_tenant(cmdline: str, name: str = "") -> str:
    """Map a process cmdline/name to a known tenant label (or the raw name)."""
    hay = f"{cmdline} {name}".lower()
    for tenant, needles in _TENANT_PATTERNS:
        if any(n in hay for n in needles):
            return tenant
    return name or "unknown"


def _read_cmdline(pid: int) -> str:
    """Full command line for a pid (/proc), '' if unreadable."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _parse_compute_apps(uuid_index: dict[str, int], compute_csv: str,
                        cmdline_fn=_read_cmdline) -> dict[int, list[dict]]:
    """Pure parse of nvidia-smi compute-apps CSV → per-index tenant list."""
    result: dict[int, list[dict]] = {}
    for line in compute_csv.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        gpu_uuid, pid_s, name, used_s = parts[0], parts[1], parts[2], parts[3]
        idx = uuid_index.get(gpu_uuid)
        if idx is None:
            continue
        try:
            pid, used_mb = int(pid_s), int(used_s)
        except ValueError:
            continue
        result.setdefault(idx, []).append({
            "pid": pid, "name": name, "used_mb": used_mb,
            "tenant": _classify_tenant(cmdline_fn(pid), name),
        })
    return result


def gpu_processes() -> dict[int, list[dict]]:
    """Per-CUDA-index compute processes holding VRAM, each tenant-classified.

    ``{index: [{pid, name, tenant, used_mb}, ...]}``. Names non-okuro tenants
    (user-registered services / comfyui / ollama) so the broker can report WHAT
    holds a GPU, not only the aggregate free VRAM. ``{}`` if nvidia-smi absent.
    """
    if shutil.which("nvidia-smi") is None:
        return {}
    try:
        idx_out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        apps_out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("nvidia-smi compute-apps failed: %s", exc)
        return {}

    uuid_index: dict[str, int] = {}
    for line in idx_out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            try:
                uuid_index[parts[1]] = int(parts[0])
            except ValueError:
                continue
    return _parse_compute_apps(uuid_index, apps_out)


_BDF_RE = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]")


def _drm_card_bdf(card_dir: Path) -> Optional[str]:
    """Return the *device's own* PCI BDF, not its parent bridge.

    /sys/class/drm/cardN/device resolves through any number of PCIe
    bridges, e.g.
    ``/sys/devices/pci0000:00/0000:00:02.1/0000:03:00.0/0000:04:00.0/0000:05:00.0``.
    The card's own BDF is the LAST element in that chain. Earlier
    iterations of this code took the first match and silently mapped
    every NVIDIA GPU on the reference host to the same upstream bridge — which
    no nvidia-smi BDF would ever match.
    """
    device_link = card_dir / "device"
    if not device_link.is_symlink():
        return None
    try:
        real = os.path.realpath(str(device_link))
    except OSError:
        return None
    matches = _BDF_RE.findall(real)
    return matches[-1].lower() if matches else None


def _has_connected_connector(card_dir: Path) -> bool:
    """True iff any of the card's DP/HDMI/eDP subnodes reports ``connected``.

    /sys/class/drm exposes connector siblings as ``cardN-<TYPE>-<id>``
    (e.g. ``card1-DP-4``). A monitor plugged in registers as
    ``status=connected``; the other connectors stay ``disconnected``.
    This is the single reliable signal for "is this GPU driving a
    display right now" on a headless-capable Linux box.
    """
    parent = card_dir.parent
    name = card_dir.name
    for connector in parent.glob(f"{name}-*"):
        status_file = connector / "status"
        if not status_file.exists():
            continue
        try:
            if status_file.read_text().strip() == "connected":
                return True
        except OSError:
            continue
    return False


def _nvidia_pci_bdf_to_index() -> dict[str, int]:
    """Map normalised PCI BDF (``0000:01:00.0``) → CUDA index."""
    if shutil.which("nvidia-smi") is None:
        return {}
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,pci.bus_id",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("nvidia-smi pci query failed: %s", exc)
        return {}

    mapping: dict[str, int] = {}
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        bdf = parts[1].lower()
        # nvidia-smi reports BDFs as "00000000:01:00.0"; trim the
        # leading domain pad to match /sys's "0000:01:00.0".
        if bdf.startswith("00000000:"):
            bdf = "0000:" + bdf.split(":", 1)[1]
        mapping[bdf] = idx
    return mapping


def _display_gpu_indices_linux() -> set[int]:
    """CUDA indices currently driving an actively-connected monitor."""
    drm_root = Path("/sys/class/drm")
    if not drm_root.exists():
        return set()
    bdf_to_idx = _nvidia_pci_bdf_to_index()
    if not bdf_to_idx:
        return set()
    display_indices: set[int] = set()
    for card in drm_root.glob("card[0-9]*"):
        # Skip the connector subnodes (cardN-DP-...); only consider top-level cards.
        if "-" in card.name:
            continue
        bdf = _drm_card_bdf(card)
        if bdf is None or bdf not in bdf_to_idx:
            continue
        if _has_connected_connector(card):
            display_indices.add(bdf_to_idx[bdf])
    return display_indices


def _detect_display_gpu_index_linux() -> Optional[int]:
    """Lowest CUDA index that's currently driving a display (or None)."""
    indices = _display_gpu_indices_linux()
    return min(indices) if indices else None


def _detect_display_gpu_index_macos() -> Optional[int]:
    """On macOS, the Apple-Silicon iGPU is always the display.

    okuro doesn't run on Intel Macs in any meaningful way (no CUDA path),
    so the simple rule "if there's a GPU, GPU 0 is display" holds.
    """
    return 0


def is_display_gpu(idx: int, *, system: Optional[str] = None) -> bool:
    """True if CUDA index ``idx`` currently drives the user's display."""
    sys_ = system or platform.system()
    if sys_ == "Darwin":
        display = _detect_display_gpu_index_macos()
    else:
        display = _detect_display_gpu_index_linux()
    return display is not None and display == idx


def _cpu_cores() -> int:
    return os.cpu_count() or 1


def _ram_gb() -> float:
    try:
        import psutil
        return round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        # /proc/meminfo fallback (Linux); return 0 if unavailable.
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        kib = int(line.split()[1])
                        return round(kib / (1024 ** 2), 1)
        except OSError:
            pass
        return 0.0


def capabilities() -> dict[str, Any]:
    """Inspect the host and return a JSON-friendly summary.

    Shape (mirrors plan v2 schema):

        {
          "system": "Linux" | "Darwin" | "Windows",
          "display_gpu_index": int | None,
          "gpus": [
              {"index": int, "name": str, "vram_gb": float, "is_display": bool},
              ...
          ],
          "cpu_cores": int,
          "ram_gb": float,
        }
    """
    sys_ = platform.system()
    gpus = _detect_gpus_nvml()
    display_indices: set[int]
    if sys_ == "Darwin":
        macos_idx = _detect_display_gpu_index_macos()
        display_indices = {macos_idx} if (macos_idx is not None and gpus) else set()
    else:
        display_indices = _display_gpu_indices_linux() if gpus else set()
    for g in gpus:
        g["is_display"] = g["index"] in display_indices
    display_idx = min(display_indices) if display_indices else None
    return {
        "system": sys_,
        "display_gpu_index": display_idx,
        "display_gpu_indices": sorted(display_indices),
        "gpus": gpus,
        "cpu_cores": _cpu_cores(),
        "ram_gb": _ram_gb(),
    }


_HIGH_TIER_VRAM_THRESHOLD_GB = 8.0


def recommended_tier(detection: dict[str, Any]) -> str:
    """``high`` iff there's a non-display GPU with >=8 GB VRAM, else ``low``."""
    for g in detection.get("gpus", []):
        if g.get("is_display"):
            continue
        if float(g.get("vram_gb", 0)) >= _HIGH_TIER_VRAM_THRESHOLD_GB:
            return "high"
    return "low"


def _visible_cuda_ordinal(global_index: int) -> Optional[int]:
    """Map a global NVML index → this process's local CUDA ordinal.

    nvidia-smi (the detection source) reports GLOBAL indices and ignores
    ``CUDA_VISIBLE_DEVICES``. But CUDA runtimes (torch, CTranslate2) index
    devices RELATIVE to that env var: with ``CUDA_VISIBLE_DEVICES=1`` the sole
    visible GPU is local ordinal ``0``, not ``1``. Returning the global index
    unchanged makes ``cuda:1`` raise "invalid device ordinal" inside a pinned
    process. Translate here so the two namespaces reconcile.

    Returns ``None`` when the chosen GPU is not exposed to this process (or the
    var lists UUIDs we can't resolve) — callers should then keep scanning / fall
    back to CPU rather than emit an ordinal the runtime will reject.
    """
    cvd = os.environ.get("CUDA_VISIBLE_DEVICES")
    if cvd is None or cvd.strip() == "":
        return global_index  # unset/empty-unset → global == local
    entries = [e.strip() for e in cvd.split(",") if e.strip() != ""]
    try:
        return entries.index(str(global_index))
    except ValueError:
        # global_index not in the visible set, or entries are UUIDs, not ints.
        return None


def recommended_device(detection: dict[str, Any]) -> str:
    """First non-display GPU with >=8 GB VRAM; else ``cpu``.

    The display GPU is NEVER auto-selected. This is a hard rule: the
    onboarding picker is allowed to override it (with a warning), but
    auto-resolution may not.

    The emitted ordinal is process-LOCAL — it honours ``CUDA_VISIBLE_DEVICES``
    so a service pinned to one GPU (e.g. the orchestrator's ``=1``) gets the
    ordinal its CUDA runtime actually sees, not the global NVML index.
    """
    for g in detection.get("gpus", []):
        if g.get("is_display"):
            continue
        if float(g.get("vram_gb", 0)) >= _HIGH_TIER_VRAM_THRESHOLD_GB:
            local = _visible_cuda_ordinal(int(g["index"]))
            if local is not None:
                return f"cuda:{local}"
    if platform.system() == "Darwin":
        return "mps"
    return "cpu"
