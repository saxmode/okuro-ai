# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Load-probe qualification — the MEASURED half of "runnable". Actually
#          start a bundle's engine at a target context on a target GPU, confirm
#          it serves, measure peak VRAM, tear down, and stamp the result onto
#          bundle.qualification (okuro.qualification/v1). Estimate proposes; the
#          probe promises.
# index:
#   def read_gpu_used_mb
#   def probe_bundle
# AGENT_HEADER_END -->
"""Load-probe: turn a VRAM *estimate* into a VRAM *measurement*.

llama.cpp preallocates the KV cache for ``--ctx-size`` at load, so measuring
GPU memory right after the engine reports healthy already captures weights +
full KV at the target context — the exact quantity the analytic estimate can
only predict. A model is ``serving_ready`` only once it has actually loaded and
answered here; nothing downstream may call it runnable on an estimate alone.

The engine runner and the VRAM reader are injectable so the control flow is
unit-testable without spawning a real server or touching a GPU.
"""

from __future__ import annotations

import subprocess
import time
from typing import Callable, Optional

QUALIFICATION_SCHEMA = "okuro.qualification/v1"


def read_gpu_used_mb(gpu_index: int) -> float:
    """Used VRAM (MB) on one GPU via nvidia-smi. 0.0 if unreadable."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used",
             "--format=csv,noheader,nounits", "-i", str(gpu_index)],
            text=True, timeout=10,
        )
        return float(out.strip().splitlines()[0])
    except Exception:
        return 0.0


def probe_bundle(
    bundle,
    *,
    gpu_index: int = 0,
    context_length: Optional[int] = None,
    prompt: str = "Reply with the single word: OK.",
    runner=None,
    vram_reader: Optional[Callable[[int], float]] = None,
    caller: str = "probe",
) -> dict:
    """Start ``bundle`` at ``context_length``, verify it serves, measure VRAM.

    Writes and returns an okuro.qualification/v1 dict:
        serving_ready         did it load + answer without error/OOM
        measured_vram_gb      peak GPU used − baseline (weights + KV at ctx)
        max_context_verified  the context the engine was allocated at
        latency_s             first-completion latency
        engine / error

    Always tears the engine down (releases the broker lease) on every path.
    """
    vram_reader = vram_reader or read_gpu_used_mb
    if runner is None:  # real infra: broker-leased engine runner
        from .broker import Broker
        from .engine import EngineRunner

        runner = EngineRunner(Broker())

    if context_length is not None:
        bundle.context_length = context_length
    target_ctx = bundle.context_length

    result: dict = {
        "schema": QUALIFICATION_SCHEMA,
        "serving_ready": False,
        "measured_vram_gb": None,
        "max_context_verified": target_ctx,
        "latency_s": None,
        "engine": bundle.engine,
        "error": None,
    }

    baseline = vram_reader(gpu_index)
    eng = None
    try:
        eng = runner.start(bundle, gpu_index=gpu_index, caller=caller)
        from okuro.bridge.local import invoke_local

        t0 = time.time()
        r = invoke_local(eng.endpoint, bundle.id, prompt, timeout=60)
        result["latency_s"] = round(time.time() - t0, 2)

        peak = vram_reader(gpu_index)
        result["serving_ready"] = bool(r.get("success"))
        result["measured_vram_gb"] = round(max(0.0, peak - baseline) / 1024, 2)
        if not r.get("success"):
            result["error"] = r.get("error")
    except Exception as exc:  # OOM, spawn failure, broker rejection, …
        result["error"] = str(exc)
    finally:
        if eng is not None:
            try:
                runner.stop(bundle.id)
            except Exception:
                pass

    bundle.qualification = result
    return result
