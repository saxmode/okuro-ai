# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Host CPU + RAM metrics collector (cross-platform via psutil).
# index: imports | def get_host_status
# AGENT_HEADER_END -->
"""Host CPU + RAM metrics collector (cross-platform via psutil)."""

from datetime import datetime, timezone

import psutil


def get_host_status() -> dict:
    """Return cross-platform CPU + RAM snapshot.

    CPU: total count, per-core utilization, frequency when available.
    RAM: total/used/free in MB + percent.
    """
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()

    try:
        freq = psutil.cpu_freq()
        freq_mhz = freq.current if freq else None
    except Exception:
        freq_mhz = None

    return {
        "cpu": {
            "count_logical": psutil.cpu_count(logical=True),
            "count_physical": psutil.cpu_count(logical=False),
            "utilization_percent": psutil.cpu_percent(interval=0.1),
            "per_core_percent": psutil.cpu_percent(interval=0.1, percpu=True),
            "frequency_mhz": freq_mhz,
        },
        "memory": {
            "total_mb": round(vm.total / (1024 * 1024)),
            "used_mb": round(vm.used / (1024 * 1024)),
            "available_mb": round(vm.available / (1024 * 1024)),
            # psutil's vm.percent on macOS is (total - available) / total,
            # which counts inactive/cached pages as "used". That makes the
            # bar read 75% while the "8.9 / 24 GB" label below reads 37% —
            # same data, two different denominators. Recompute from used_mb
            # so the bar and the text label always agree.
            "utilization_percent": (
                round(vm.used / vm.total * 100, 1) if vm.total else 0.0
            ),
        },
        "swap": {
            "total_mb": round(swap.total / (1024 * 1024)),
            "used_mb": round(swap.used / (1024 * 1024)),
            "utilization_percent": (
                round(swap.used / swap.total * 100, 1) if swap.total else 0.0
            ),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
