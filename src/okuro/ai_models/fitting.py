# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: VRAM requirement calculation + GPU fitting.
# index: def estimate_vram | def fits_gpu | def recommend_gpu
# AGENT_HEADER_END -->
"""VRAM requirement calculation + GPU fitting."""

import re
from typing import Optional


# Approximate VRAM requirements by quantization (per billion parameters)
QUANT_GB_PER_B = {
    "Q2_K": 0.4,
    "Q3_K_S": 0.5,
    "Q3_K_M": 0.55,
    "Q4_0": 0.6,
    "Q4_K_S": 0.6,
    "Q4_K_M": 0.65,
    "Q5_0": 0.7,
    "Q5_K_S": 0.72,
    "Q5_K_M": 0.75,
    "Q6_K": 0.85,
    "Q8_0": 1.1,
    "F16": 2.0,
    "F32": 4.0,
}

# Context overhead (approximate, for 8K context)
CONTEXT_OVERHEAD_GB = 1.0


def estimate_vram(
    parameters: str | None,
    quantization: str | None,
    context_length: int = 8192,
) -> float:
    """Estimate VRAM requirement in GB.

    Args:
        parameters: Parameter count string (e.g. "7B", "32B")
        quantization: Quantization string (e.g. "Q4_K_M", "Q8_0")
        context_length: Context length in tokens

    Returns:
        Estimated VRAM in GB
    """
    if not parameters:
        return 0.0

    # Parse parameter count
    m = re.match(r"(\d+(?:\.\d+)?)", parameters)
    if not m:
        return 0.0
    param_b = float(m.group(1))

    # Get per-B VRAM for quantization
    quant_upper = (quantization or "Q4_K_M").upper()
    gb_per_b = QUANT_GB_PER_B.get(quant_upper, 0.65)

    # Base VRAM = params * per-B rate
    base_vram = param_b * gb_per_b

    # Context overhead scales roughly with context length
    ctx_factor = context_length / 8192
    overhead = CONTEXT_OVERHEAD_GB * ctx_factor

    return round(base_vram + overhead, 1)


# Bytes per KV-cache element. llama.cpp defaults the KV cache to f16 (2 bytes);
# f16 is the conservative assumption — quantized KV only ever uses less.
_KV_BYTES_F16 = 2


def kv_cache_gb(
    n_layers: int,
    n_kv_heads: int,
    head_dim: int,
    context_length: int,
    bytes_per_elem: int = _KV_BYTES_F16,
) -> float:
    """Exact KV-cache size in GB for a transformer at a given context.

    kv = 2 (K and V) × n_layers × n_kv_heads × head_dim × context × bytes.
    ``n_kv_heads`` is the GROUPED-query head count (``head_count_kv`` in GGUF) —
    the whole point: GQA/MQA models cache far fewer heads than they attend with,
    which the old ``1GB × ctx/8K`` fudge completely ignored. Returns 0 when any
    dimension is missing (caller falls back to the coarse estimate).
    """
    if not all((n_layers, n_kv_heads, head_dim, context_length)):
        return 0.0
    total_bytes = 2 * n_layers * n_kv_heads * head_dim * context_length * bytes_per_elem
    return total_bytes / (1024 ** 3)


def estimate_vram_detailed(
    param_b: float | None,
    quantization: str | None,
    *,
    n_layers: int | None = None,
    n_kv_heads: int | None = None,
    head_dim: int | None = None,
    context_length: int = 8192,
    kv_bytes: int = _KV_BYTES_F16,
) -> dict:
    """VRAM requirement with a transparent breakdown — the rock-solid estimate.

    total = weights + kv_cache + compute/framework overhead, × fragmentation
    headroom. When the attention dims are supplied (from GGUF metadata) the KV
    term is EXACT and GQA-aware; without them it degrades to the coarse
    ``1GB × ctx/8K`` heuristic and flags ``exact=False`` so callers know the
    figure is a guess, not a guarantee.

    Returns {weights, kv_cache, overhead, total, exact}.
    """
    pb = float(param_b or 0)
    gb_per_b = QUANT_GB_PER_B.get((quantization or "Q4_K_M").upper(), 0.65)
    weights = pb * gb_per_b

    kv = kv_cache_gb(n_layers or 0, n_kv_heads or 0, head_dim or 0, context_length, kv_bytes)
    exact = kv > 0
    if not exact:
        kv = CONTEXT_OVERHEAD_GB * (context_length / 8192)

    # Compute buffers + framework/runtime overhead: a floor plus a small slice
    # of the weight footprint (grows with model size).
    overhead = max(0.6, 0.05 * weights)
    # Fragmentation / allocator headroom — the difference between "loaded" and
    # "loaded without a late-context OOM".
    total = (weights + kv + overhead) * 1.1

    return {
        "weights": round(weights, 2),
        "kv_cache": round(kv, 2),
        "overhead": round(overhead, 2),
        "total": round(total, 1),
        "exact": exact,
    }


def fits_gpu(
    vram_required_gb: float,
    gpu_vram_mb: int,
    safety_margin: float = 0.9,
) -> bool:
    """Check if a model fits in GPU VRAM.

    Args:
        vram_required_gb: Estimated VRAM requirement
        gpu_vram_mb: Available GPU VRAM in MB
        safety_margin: Fraction of VRAM to consider usable (default 90%)
    """
    available_gb = (gpu_vram_mb * safety_margin) / 1024
    return vram_required_gb <= available_gb


def recommend_gpu(
    vram_required_gb: float,
    gpus: list[dict],
) -> dict | None:
    """Recommend the best GPU for a model.

    Args:
        vram_required_gb: Estimated VRAM requirement
        gpus: List of GPU dicts with 'name', 'vram.free_mb'

    Returns:
        Best GPU dict, or None if none fit
    """
    candidates = []
    for gpu in gpus:
        free_mb = gpu.get("vram", {}).get("free_mb", 0)
        if fits_gpu(vram_required_gb, free_mb):
            candidates.append((gpu, free_mb))

    if not candidates:
        return None

    # Pick the GPU with least free VRAM that still fits (most efficient)
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0]
