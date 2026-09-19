# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Build a diagnostic message from a failed CLI probe, whichever stream it used.
# index: imports | def cli_error
# AGENT_HEADER_END -->
"""Build a diagnostic message from a failed CLI probe, whichever stream it used.

Collectors here shell out to a vendor CLI and report `proc.stderr` when it
exits non-zero. Several of those CLIs print their failure to STDOUT instead,
which yields an error string with nothing after the colon — the diagnosis is
thrown away at the point it is most needed.

Measured 2026-09-16: `nvidia-smi -q -x` under a driver/library mismatch exits
18 with an EMPTY stderr and "Failed to initialize NVML: Driver/library version
mismatch" on stdout, so `sysinfo_gpu_status` reported only "nvidia-smi failed:".
"""

import subprocess


def cli_error(label: str, proc: "subprocess.CompletedProcess[str]") -> str:
    """Return "<label> failed (exit N): <message>" using whichever stream spoke.

    stderr wins when both carry text; stdout is the fallback, not a supplement,
    so a normally-noisy stdout cannot bury a real stderr diagnosis. With both
    streams empty the exit code is still reported — never a bare colon.
    """
    detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    detail = " ".join(detail.split())[:500]
    if not detail:
        detail = "no output on stdout or stderr"
    return f"{label} failed (exit {proc.returncode}): {detail}"
