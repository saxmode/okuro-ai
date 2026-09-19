# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: VRAM requirement calculation + the PLACEMENT-SPACE hardware fit —
#          where on this host's GPUs and RAM a model could actually run.
# index:
#   def estimate_vram | def kv_cache_gb | def estimate_vram_detailed
#   def fits_gpu | def recommend_gpu          (the pre-P3 surface, kept)
#   def detect_hardware | class Subject | class Placement
#   def fit | def fit_unit | def fit_release  (the placement space, ruling 6)
#   def persist_fits | def fit_task
# AGENT_HEADER_END -->
"""VRAM requirement calculation + GPU fitting."""

import logging
import os
import re
from typing import Optional

log = logging.getLogger("okuro.ai_models.fitting")


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
    """Estimate VRAM requirement in GB, for one GPU holding the whole model.

    ROUTED THROUGH THE PLACEMENT ARITHMETIC. The interpretation report's gap
    (g) was three systems computing fit three incompatible ways, and this
    function was okuro's outlier. It now uses the same
    weights + KV + runtime + safety sum as :func:`fit`, which is what makes the
    inventory, the discovery gate and the Models page agree.

    TWO ERRORS CANCELLED IN THE OLD VERSION, and that is what makes this worth
    doing even though the totals move only a little. The old GB-per-billion
    table over-stated the WEIGHTS — it charged 0.65 GB/B for Q4_K_M, about 5.6
    bits per weight against the real 4.85 — while charging nothing at all for
    the CUDA context, the compute buffer or allocator fragmentation. Measured
    against this host's own files: a 70B Q4_K_M is 39.60 GB on disk, the old
    table said 45.5 GB of weights and the bits-per-weight figure says 39.53.
    So the weights term is now right to within a tenth of a percent and the
    overhead is counted separately instead of hiding inside a wrong weight.

    Args:
        parameters: Parameter count string (e.g. "7B", "32B")
        quantization: Quantization string (e.g. "Q4_K_M", "Q8_0")
        context_length: Context length in tokens

    Returns:
        Estimated VRAM in GB
    """
    if not parameters:
        return 0.0

    m = re.match(r"(\d+(?:\.\d+)?)", parameters)
    if not m:
        return 0.0
    param_b = float(m.group(1))

    weights = _weights_from_params(param_b, quantization)
    kv = CONTEXT_OVERHEAD_GB * (context_length / 8192)
    return round(_need(weights, kv), 1)


def _weights_from_params(param_b: float, quantization: str | None) -> float:
    """Weight bytes from a param count — only when no file size is available.

    Bits-per-weight rather than the older GB-per-billion table: the two say the
    same thing, but bits/weight is the number quantization is actually
    specified in, so a new quant can be added to one table and be right in
    both places.
    """
    try:
        from okuro.ai_models.lineage import BITS_PER_WEIGHT, _DEFAULT_BPW
        key = (quantization or "").upper().split("-")[-1]
        bpw = BITS_PER_WEIGHT.get(key)
        if bpw is None:
            bpw = _DEFAULT_BPW
    except Exception:  # lineage must never be load-bearing for a fallback
        bpw = 8.0 * QUANT_GB_PER_B.get((quantization or "Q4_K_M").upper(), 0.65)
    return param_b * 1e9 * bpw / 8 / (1024 ** 3)


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


# ============================================================================
# PLACEMENT-SPACE FIT  (P3 of okuro-model-manager, ruling 6)
# ============================================================================
#
# Ruling 6 rejected "does it fit a GPU" as too simple. The question the user
# actually asks is "can I run this on my box", and on a box with two unequal
# cards and 124 GB of system RAM that has five different answers, only one of
# which is "it fits card 0":
#
#   single-gpu           the whole model on one card
#   gpu-split            tensor-split across both cards (llama.cpp --tensor-split)
#   gpu-ram-offload      some layers on the GPUs, the rest in system RAM
#   moe-expert-offload   attention resident, cold experts in RAM (--n-cpu-moe)
#   cpu-only             no GPU at all
#
# So `fit()` evaluates the SPACE and returns every feasible placement ranked
# fast -> slow, with `best` being the first one that works when the GPUs are
# idle. "Runnable on my hardware" is the OR over that set, never one card's
# answer.
#
# Gap (g) from the interpretation report — three systems computing fit three
# incompatible ways — closes here for okuro's own answers: `estimate_vram`,
# the discovery gate and the inventory now all reach the same arithmetic.
# tm-inference is NOT edited; where it disagrees, the disagreement is the KV
# cache and the runtime headroom its file-size rule does not model.

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional as _Opt

_GiB = 1024 ** 3

# --- the constants, and why each one is the number it is --------------------

#: Context the fit is computed AT. Not the model's maximum — a model that
#: advertises 262144 tokens does not need that cache to be useful, and sizing
#: every unit at its ceiling would report almost nothing as runnable. 8k is the
#: working default; override per host with the `ai_models.fit_context`
#: convention, or per call.
DEFAULT_CONTEXT = 8192
CONTEXT_CONVENTION_KEY = "ai_models.fit_context"

#: CUDA context, cuBLAS workspace and the driver's own allocation. Present on
#: every GPU that holds any part of a model, which is why a split pays it
#: TWICE — the commonest way a split estimate comes out too optimistic.
RUNTIME_OVERHEAD_FLOOR_GB = 0.6

#: Compute-graph buffer, which grows with the model. Added to the floor rather
#: than replacing it.
RUNTIME_OVERHEAD_FRAC = 0.05

#: Allocator fragmentation between the mmap'd weight blocks and the KV arena —
#: the difference between "loaded" and "loaded without an OOM once the context
#: fills". Additive and explicit, so a `why` line can name it, rather than the
#: multiplicative safety factor it replaces: a factor applied on top of an
#: overhead term counts the same slack twice and nobody can say by how much.
VRAM_SAFETY_GB = 1.0

#: Above this share of the model living in system RAM, decode is bound by host
#: memory bandwidth rather than by the GPU. This host's DDR5 dual channel runs
#: about 90 GB/s against roughly 960 GB/s on the larger card — call it a
#: tenfold gap — so at 30% offloaded the RAM-resident share alone already costs
#: several times the whole GPU-resident pass, and the curve only steepens.
#: Below the line the placement is `usable`; above it, `slow`.
OFFLOAD_USABLE_MAX_PCT = 30.0

#: MoE floor: the share of weights that stays on the GPU no matter how few
#: experts are active. `--n-cpu-moe` moves EXPERT FFN tensors to the host; the
#: attention projections, embeddings, output head and any shared expert are not
#: expert-FFN and never leave. The plan's arithmetic is
#: params_active/params_total x weights, which for an 80B-A3B model is 3.75% —
#: far below what those resident tensors alone occupy. This floor keeps the
#: brief's formula and stops it returning a number that is physically
#: impossible. It is an ESTIMATE, labelled as one in every `why` it appears in.
MOE_MIN_RESIDENT_FRAC = 0.15

#: Diffusion activation headroom as a share of weights — latents, attention
#: maps and the VAE decode buffer at a typical working resolution. Media fit is
#: an ESTIMATE by construction (see `media-estimate` below) and this constant
#: is the largest reason why.
MEDIA_ACTIVATION_FRAC = 0.35

#: Share of a media unit's bytes that is text encoders / CLIP rather than the
#: diffusion transformer — what moves to the second card in the
#: `encoders-on-second-gpu` layout. A directory-level split would measure this
#: per unit; P3 does not, so it is declared and labelled.
MEDIA_ENCODER_FRAC = 0.30

#: Share of the diffusion transformer that block-swap keeps in system RAM.
MEDIA_BLOCK_SWAP_FRAC = 0.50

SPEED_FAST, SPEED_USABLE, SPEED_SLOW = "fast", "usable", "slow"
_SPEED_ORDER = {SPEED_FAST: 0, SPEED_USABLE: 1, SPEED_SLOW: 2}

BASIS_GGUF = "gguf-header"
BASIS_HEURISTIC = "param-heuristic"
BASIS_MEDIA = "media-estimate"

MODE_SINGLE = "single-gpu"
MODE_SPLIT = "gpu-split"
MODE_OFFLOAD = "gpu-ram-offload"
MODE_MOE = "moe-expert-offload"
MODE_CPU = "cpu-only"
MODE_MEDIA_SINGLE = "media-single-gpu"
MODE_MEDIA_ENCODERS = "media-encoders-on-second-gpu"
MODE_MEDIA_SWAP = "media-block-swap-to-ram"
MODE_MEDIA_FP8 = "media-fp8-cast"

#: Modalities whose placements are media estimates rather than the text
#: precision path. Ruling 10 put text first and this is where that shows.
MEDIA_MODALITIES = frozenset({"image", "video", "audio", "3d"})


def fit_context() -> int:
    """The context length fits are computed at on this host."""
    try:
        from okuro.yu.conventions import get_convention
        v = get_convention(CONTEXT_CONVENTION_KEY, None)
        if v:
            return max(512, int(v))
    except Exception:  # a bad convention must never break a fit
        pass
    return DEFAULT_CONTEXT


# --- hardware ---------------------------------------------------------------


@dataclass(frozen=True)
class GPU:
    """One GPU as the fit sees it: a name, a size, and what is free right now."""

    index: int
    name: str
    total_gb: float
    free_now_gb: _Opt[float] = None

    def to_dict(self) -> dict:
        return {"index": self.index, "name": self.name,
                "total_gb": round(self.total_gb, 2),
                "free_now_gb": (round(self.free_now_gb, 2)
                                if self.free_now_gb is not None else None)}


@dataclass(frozen=True)
class Hardware:
    """What this host can hold. Measured, never declared in code."""

    gpus: tuple[GPU, ...] = ()
    ram_total_gb: float = 0.0
    ram_available_gb: float = 0.0
    cpu_threads: int = 0
    now_known: bool = False

    @property
    def vram_total_gb(self) -> float:
        return sum(g.total_gb for g in self.gpus)

    def to_dict(self) -> dict:
        return {"gpus": [g.to_dict() for g in self.gpus],
                "ram_total_gb": round(self.ram_total_gb, 1),
                "ram_available_gb": round(self.ram_available_gb, 1),
                "cpu_threads": self.cpu_threads,
                "now_known": self.now_known}


def _ram_meminfo() -> tuple[float, float]:
    """``(total_gb, available_gb)`` from /proc/meminfo, psutil as the fallback.

    MemAvailable is the kernel's own estimate of what a new allocation can get
    without swapping — which is the question a RAM-offload placement asks. It
    is not MemFree and must not be replaced by it.
    """
    total = avail = 0.0
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) / (1024 ** 2)
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) / (1024 ** 2)
                if total and avail:
                    break
    except OSError:
        pass
    if not total:
        try:
            import psutil
            vm = psutil.virtual_memory()
            total, avail = vm.total / _GiB, vm.available / _GiB
        except Exception:
            pass
    return round(total, 2), round(avail, 2)


def _gpu_names() -> dict[int, str]:
    """Index -> the name this host calls that card.

    Read from the `conventions.gpus` block, which already exists on this host.
    The names are host data and are never written into this repo; a card with
    no declared name falls back to ``gpu<index>``.
    """
    out: dict[int, str] = {}
    try:
        from okuro.yu.conventions import get_convention
        for entry in get_convention("gpus", []) or []:
            if isinstance(entry, dict) and entry.get("name") is not None:
                try:
                    out[int(entry.get("id"))] = str(entry["name"])
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass
    return out


def detect_hardware(*, now: bool = False) -> Hardware:
    """This host's GPUs and RAM, measured.

    Totals come from ``okuro.capability`` (nvidia-smi), names from the
    ``gpus`` convention, RAM from /proc/meminfo. With ``now=True`` each GPU
    also carries what is actually free, taken from the okuro broker's
    single-authority figure — ``min(ledger_free, nvidia-smi free) - headroom``
    — so VRAM held by a non-okuro tenant is respected rather than double-booked.
    """
    gpus: list[GPU] = []
    names = _gpu_names()
    try:
        from okuro.capability import capabilities
        detected = capabilities().get("gpus", []) or []
    except Exception as exc:
        log.debug("gpu detection unavailable: %s", exc)
        detected = []

    free_by_index: dict[int, float] = {}
    now_known = False
    if now and detected:
        try:
            from okuro.inference.broker import Broker
            broker = Broker()
            for g in detected:
                rep = broker.free_report(int(g["index"]))
                adm = rep.get("admissible_gb")
                if adm is not None:
                    free_by_index[int(g["index"])] = float(adm)
            now_known = bool(free_by_index)
        except Exception as exc:
            log.debug("live free-VRAM unavailable: %s", exc)

    for g in detected:
        idx = int(g.get("index", 0))
        gpus.append(GPU(
            index=idx,
            name=names.get(idx, f"gpu{idx}"),
            total_gb=float(g.get("vram_gb") or 0.0),
            free_now_gb=free_by_index.get(idx),
        ))
    gpus.sort(key=lambda g: (-g.total_gb, g.index))

    total, avail = _ram_meminfo()
    threads = 0
    try:
        threads = os.cpu_count() or 0
    except Exception:
        pass
    return Hardware(tuple(gpus), total, avail, threads, now_known)


# --- subject ----------------------------------------------------------------


@dataclass
class Subject:
    """The thing being placed — an installed unit or an undownloaded release.

    One shape for both, because ruling 6 asks the same question of each and two
    shapes would be two answers. Everything here is cheap: a size already in
    the inventory, a name already parsed, and at most one GGUF metadata header.
    Nothing is ever loaded.
    """

    kind: str                       # 'unit' | 'release'
    subject_id: str
    name: str = ""
    size_gb: float = 0.0            # file size (unit) or card size (release)
    quant: _Opt[str] = None
    params_total_b: _Opt[float] = None
    params_active_b: _Opt[float] = None
    is_moe: bool = False
    modality: str = "text"
    context: int = DEFAULT_CONTEXT
    gguf_path: _Opt[str] = None
    status: str = "ok"
    note: _Opt[str] = None
    _dims: _Opt[dict] = field(default=None, repr=False)

    @property
    def is_media(self) -> bool:
        return self.modality in MEDIA_MODALITIES

    def dims(self) -> _Opt[dict]:
        """Attention dims from the GGUF header, read at most once."""
        if self._dims is None and self.gguf_path:
            from okuro.ai_models.gguf_header import attention_dims
            self._dims = attention_dims(self.gguf_path) or {}
        return self._dims or None

    def to_dict(self) -> dict:
        return {"kind": self.kind, "subject_id": self.subject_id,
                "name": self.name, "size_gb": round(self.size_gb, 2),
                "quant": self.quant, "params_total_b": self.params_total_b,
                "params_active_b": self.params_active_b, "is_moe": self.is_moe,
                "modality": self.modality, "context": self.context,
                "status": self.status}


@dataclass(frozen=True)
class Placement:
    """One way this subject could run here."""

    mode: str
    gpu: str
    est_vram_gb: float
    vram_detail: dict
    est_ram_gb: float
    offloaded_pct: float
    speed_class: str
    fits_idle: bool
    fits_now: bool
    basis: str
    why: str

    def to_dict(self) -> dict:
        return {"mode": self.mode, "gpu": self.gpu,
                "est_vram_gb": round(self.est_vram_gb, 2),
                "vram_detail": {k: round(v, 2) for k, v in self.vram_detail.items()},
                "est_ram_gb": round(self.est_ram_gb, 2),
                "offloaded_pct": round(self.offloaded_pct, 1),
                "speed_class": self.speed_class,
                "fits_idle": self.fits_idle, "fits_now": self.fits_now,
                "basis": self.basis, "why": self.why}


# --- the shared arithmetic --------------------------------------------------


def _kv_for(subject: Subject) -> tuple[float, str, str]:
    """``(kv_gb, basis, how)`` for this subject at its context.

    The GGUF header is read when there is one: that makes the KV term exact and
    GQA-aware, which matters enormously — a model with 8 KV heads caches eight
    times less than the head count suggests. Without a header it degrades to
    the param-count heuristic that was already here, and says so, because a
    number whose basis is unknown is a number nobody can check.
    """
    dims = subject.dims()
    if dims and dims.get("n_layers") and dims.get("n_kv_heads") and dims.get("head_dim"):
        kv = kv_cache_gb(dims["n_layers"], dims["n_kv_heads"], dims["head_dim"],
                         subject.context)
        if kv > 0:
            return kv, BASIS_GGUF, (
                f"KV exact from the GGUF header "
                f"({dims['n_layers']}L x {dims['n_kv_heads']} kv-heads x "
                f"{dims['head_dim']} head-dim at {subject.context} ctx)")
    kv = CONTEXT_OVERHEAD_GB * (subject.context / 8192)
    return kv, BASIS_HEURISTIC, (
        f"KV from the param-count heuristic ({CONTEXT_OVERHEAD_GB} GB per 8k) "
        f"— no GGUF header available")


def _overhead(weights_gb: float) -> float:
    return max(RUNTIME_OVERHEAD_FLOOR_GB, RUNTIME_OVERHEAD_FRAC * weights_gb)


def _need(weights_gb: float, kv_gb: float) -> float:
    """VRAM one GPU must have to hold this much of a model."""
    return weights_gb + kv_gb + _overhead(weights_gb) + VRAM_SAFETY_GB


def _weights_gb(subject: Subject) -> tuple[float, str]:
    """The weight footprint, and where the figure came from.

    A measured file size wins every time — it is the bytes that have to land in
    memory, with no quant table in between. Only an undownloaded release with
    no card size falls back to params x bits-per-weight.
    """
    if subject.size_gb and subject.size_gb > 0:
        return subject.size_gb, "measured file size"
    if subject.params_total_b:
        from okuro.ai_models.lineage import BITS_PER_WEIGHT
        bpw = BITS_PER_WEIGHT.get((subject.quant or "").split("-")[-1].upper(), 4.85)
        gb = subject.params_total_b * 1e9 * bpw / 8 / _GiB
        return gb, f"{subject.params_total_b:g}B params at {bpw} bits/weight"
    return 0.0, "size unknown"


def _fits_now(need_by_gpu: dict[str, float], hw: Hardware) -> Optional[bool]:
    """Does this placement fit what is free RIGHT NOW? None when unknown."""
    if not hw.now_known:
        return None
    by_name = {g.name: g for g in hw.gpus}
    for name, need in need_by_gpu.items():
        g = by_name.get(name)
        if g is None or g.free_now_gb is None:
            return None
        if need > g.free_now_gb:
            return False
    return True


def _place(mode, gpu, need_by_gpu, ram_gb, offloaded_pct, speed, fits_idle,
           basis, why, hw, *, now_by_gpu: Optional[dict] = None) -> Placement:
    """Build a Placement, deciding ``fits_now`` from what is free.

    ``now_by_gpu`` lets a placement be evaluated against a DIFFERENT per-GPU
    requirement when the cards are busy — see the split, whose ratio is
    re-proportioned to free VRAM rather than to total VRAM.
    """
    now = _fits_now(now_by_gpu or need_by_gpu, hw) if fits_idle else False
    return Placement(
        mode=mode, gpu=gpu,
        est_vram_gb=sum(need_by_gpu.values()),
        vram_detail=dict(need_by_gpu),
        est_ram_gb=ram_gb, offloaded_pct=offloaded_pct,
        speed_class=speed, fits_idle=fits_idle,
        fits_now=bool(now) if now is not None else fits_idle,
        basis=basis, why=why)


# --- the placement evaluators ----------------------------------------------


def _eval_single(s: Subject, hw: Hardware, w: float, kv: float, basis: str,
                 kv_how: str) -> list[Placement]:
    """One card holds the whole model. The fastest placement there is."""
    out = []
    need = _need(w, kv)
    for g in hw.gpus:
        fits = need <= g.total_gb
        out.append(_place(
            MODE_SINGLE, g.name, {g.name: need}, 0.0, 0.0, SPEED_FAST, fits,
            basis,
            f"{w:.1f} GB weights + {kv:.1f} GB KV + {_overhead(w):.1f} GB runtime "
            f"+ {VRAM_SAFETY_GB:.1f} GB safety = {need:.1f} GB against "
            f"{g.total_gb:.1f} GB. {kv_how}.",
            hw))
    return out


def _eval_split(s: Subject, hw: Hardware, w: float, kv: float, basis: str,
                kv_how: str) -> list[Placement]:
    """Tensor-split across every card, in proportion to VRAM.

    llama.cpp's ``--tensor-split`` semantics, which is what tm-inference's
    SPLIT target already drives. The trap this models and a naive "add the
    cards together" does not: the runtime overhead and the safety margin are
    paid ONCE PER CARD, because each has its own CUDA context and compute
    buffer.
    """
    if len(hw.gpus) < 2 or hw.vram_total_gb <= 0:
        return []

    def shares(weight_by_gpu: dict[str, float], denom: float) -> dict[str, float]:
        return {name: _need(w * (v / denom), kv * (v / denom))
                for name, v in weight_by_gpu.items()}

    totals = {g.name: g.total_gb for g in hw.gpus}
    need_by = shares(totals, hw.vram_total_gb)
    fits = all(need_by[g.name] <= g.total_gb for g in hw.gpus)

    # `--tensor-split` is a RATIO the caller picks, not a fixed property of the
    # cards. When one card is busy the same model can be re-proportioned onto
    # what is actually free, so `fits_now` is evaluated against a split sized
    # to free VRAM. Splitting by total VRAM there would report a layout as
    # impossible when only the default ratio was.
    now_by = None
    frees = {g.name: g.free_now_gb for g in hw.gpus}
    if hw.now_known and all(v is not None for v in frees.values()):
        free_total = sum(frees.values())  # type: ignore[arg-type]
        if free_total > 0:
            now_by = shares(frees, free_total)  # type: ignore[arg-type]

    name = "+".join(g.name for g in hw.gpus)
    ratio = ":".join(f"{g.total_gb:.0f}" for g in hw.gpus)
    extra = ""
    if s.is_moe:
        extra = (" Expert offload does not balance itself across unequal "
                 "cards, so this layout needs an explicit tensor-split.")
    if now_by:
        extra += (" When a card is busy the ratio is re-proportioned to free "
                  "VRAM, which is what the tensor-split flag is for.")
    return [_place(
        MODE_SPLIT, name, need_by, 0.0, 0.0, SPEED_FAST, fits, basis,
        f"weights spread {ratio} by VRAM: "
        + ", ".join(f"{k} {v:.1f} GB" for k, v in need_by.items())
        + f". Runtime overhead and safety are paid per card. {kv_how}.{extra}",
        hw, now_by_gpu=now_by)]


def _eval_offload(s: Subject, hw: Hardware, w: float, kv: float, basis: str,
                  kv_how: str) -> list[Placement]:
    """GPU layers plus system RAM for the rest."""
    if not hw.gpus:
        return []
    per_gpu_cost = sum(_overhead(w * (g.total_gb / hw.vram_total_gb))
                       + VRAM_SAFETY_GB for g in hw.gpus)
    gpu_budget = max(0.0, hw.vram_total_gb - per_gpu_cost)
    model = w + kv
    if model <= 0:
        return []
    on_gpu = min(gpu_budget, model)
    offloaded = model - on_gpu
    pct = 100.0 * offloaded / model
    if offloaded <= 0:
        # Nothing is actually offloaded, so this is not a distinct placement —
        # it is the split, reported a second time under a slower name. Emitting
        # it would put an "offload" row that offloads nothing on the card.
        return []
    fits = offloaded <= hw.ram_available_gb
    speed = SPEED_USABLE if pct <= OFFLOAD_USABLE_MAX_PCT else SPEED_SLOW
    need_by = {g.name: min(g.total_gb, on_gpu * (g.total_gb / hw.vram_total_gb)
                           + _overhead(w * (g.total_gb / hw.vram_total_gb))
                           + VRAM_SAFETY_GB)
               for g in hw.gpus}
    name = "+".join(g.name for g in hw.gpus)
    return [_place(
        MODE_OFFLOAD, name, need_by, offloaded, pct, speed, fits, basis,
        f"{on_gpu:.1f} of {model:.1f} GB on the GPUs, {offloaded:.1f} GB "
        f"({pct:.0f}%) in system RAM against {hw.ram_available_gb:.1f} GB "
        f"available. {speed} because {pct:.0f}% offloaded is "
        f"{'at or under' if pct <= OFFLOAD_USABLE_MAX_PCT else 'over'} the "
        f"{OFFLOAD_USABLE_MAX_PCT:.0f}% host-bandwidth threshold. {kv_how}.",
        hw)]


def _eval_moe(s: Subject, hw: Hardware, w: float, kv: float, basis: str,
              kv_how: str) -> list[Placement]:
    """Attention resident, cold experts in RAM — llama.cpp ``--n-cpu-moe``.

    MoE decode reads only the ACTIVE experts per token, so the cold ones can
    live in host RAM at a cost proportional to how rarely they are touched.
    That is what puts a 120B model on a 48 GB card at all.
    """
    if not hw.gpus or not s.is_moe:
        return []
    total_b, active_b = s.params_total_b, s.params_active_b
    if not total_b or not active_b or total_b <= 0:
        return []
    active_frac = active_b / total_b
    frac = max(active_frac, MOE_MIN_RESIDENT_FRAC)
    floored = frac > active_frac
    gpu_w = w * frac
    ram = w - gpu_w
    need = _need(gpu_w, kv)
    out = []
    for g in hw.gpus:
        fits = need <= g.total_gb and ram <= hw.ram_available_gb
        out.append(_place(
            MODE_MOE, g.name, {g.name: need}, ram,
            100.0 * (1 - frac), SPEED_USABLE, fits, basis,
            f"{active_b:g}B of {total_b:g}B params active "
            f"({active_frac * 100:.1f}%)"
            + (f", floored to {MOE_MIN_RESIDENT_FRAC * 100:.0f}% for the "
               f"attention and embedding tensors that never leave the GPU"
               if floored else "")
            + f" -> {gpu_w:.1f} GB resident + {kv:.1f} GB KV = {need:.1f} GB on "
              f"{g.name} ({g.total_gb:.1f} GB), {ram:.1f} GB of cold experts in "
              f"RAM against {hw.ram_available_gb:.1f} GB. {kv_how}.",
            hw))
    return out


def _eval_cpu(s: Subject, hw: Hardware, w: float, kv: float, basis: str,
              kv_how: str) -> list[Placement]:
    """No GPU at all."""
    model = w + kv
    fits = model <= hw.ram_available_gb
    return [_place(
        MODE_CPU, "", {}, model, 100.0, SPEED_SLOW, fits, basis,
        f"{model:.1f} GB entirely in system RAM against "
        f"{hw.ram_available_gb:.1f} GB available, on {hw.cpu_threads} CPU "
        f"threads. {kv_how}.",
        hw)]


def _eval_media(s: Subject, hw: Hardware, w: float, basis: str) -> list[Placement]:
    """The diffusion layouts, computed with the SAME size arithmetic.

    Ruling 10 put text first and this is where that shows: these four are
    labelled ``media-estimate`` and they are estimates. The weight term is
    measured like any other, but the activation headroom, the encoder share and
    the block-swap fraction are declared constants rather than measurements,
    because deriving them needs a per-unit breakdown of a diffusion pipeline
    that P3 does not do. The numbers are honest about their own basis instead
    of being dressed up as the text path.
    """
    if not hw.gpus:
        return []
    out: list[Placement] = []
    act = w * MEDIA_ACTIVATION_FRAC
    est = "media-estimate: activation headroom is a declared fraction, not a measurement"

    need = _need(w, act)
    for g in hw.gpus:
        out.append(_place(
            MODE_MEDIA_SINGLE, g.name, {g.name: need}, 0.0, 0.0, SPEED_FAST,
            need <= g.total_gb, BASIS_MEDIA,
            f"{w:.1f} GB weights + {act:.1f} GB activations "
            f"({MEDIA_ACTIVATION_FRAC:.0%} of weights) = {need:.1f} GB against "
            f"{g.total_gb:.1f} GB. {est}.", hw))

    if len(hw.gpus) >= 2:
        prime, second = hw.gpus[0], hw.gpus[1]
        dit = w * (1 - MEDIA_ENCODER_FRAC)
        enc = w * MEDIA_ENCODER_FRAC
        need_by = {prime.name: _need(dit, dit * MEDIA_ACTIVATION_FRAC),
                   second.name: _need(enc, 0.0)}
        out.append(_place(
            MODE_MEDIA_ENCODERS, f"{prime.name}+{second.name}", need_by, 0.0,
            0.0, SPEED_FAST,
            need_by[prime.name] <= prime.total_gb
            and need_by[second.name] <= second.total_gb,
            BASIS_MEDIA,
            f"transformer {dit:.1f} GB on {prime.name}, text encoders "
            f"{enc:.1f} GB on {second.name} "
            f"({MEDIA_ENCODER_FRAC:.0%} of bytes assumed to be encoders). {est}.",
            hw))

    g0 = hw.gpus[0]
    swap = w * MEDIA_BLOCK_SWAP_FRAC
    resident = w - swap
    need_sw = _need(resident, resident * MEDIA_ACTIVATION_FRAC)
    out.append(_place(
        MODE_MEDIA_SWAP, g0.name, {g0.name: need_sw}, swap,
        MEDIA_BLOCK_SWAP_FRAC * 100, SPEED_USABLE,
        need_sw <= g0.total_gb and swap <= hw.ram_available_gb, BASIS_MEDIA,
        f"{MEDIA_BLOCK_SWAP_FRAC:.0%} of the transformer blocks swapped to RAM: "
        f"{need_sw:.1f} GB on {g0.name}, {swap:.1f} GB in RAM against "
        f"{hw.ram_available_gb:.1f} GB. {est}.", hw))

    quant = (s.quant or "").upper()
    if quant in ("BF16", "FP16", "F16", "FP32", "F32", ""):
        cast = w * (8.0 / (32.0 if "32" in quant else 16.0))
        need_fp8 = _need(cast, cast * MEDIA_ACTIVATION_FRAC)
        out.append(_place(
            MODE_MEDIA_FP8, g0.name, {g0.name: need_fp8}, 0.0, 0.0, SPEED_FAST,
            need_fp8 <= g0.total_gb, BASIS_MEDIA,
            f"cast to fp8 halves the {quant or 'assumed 16-bit'} weights to "
            f"{cast:.1f} GB -> {need_fp8:.1f} GB on {g0.name}. {est}.", hw))
    return out


# --- the one entry point ----------------------------------------------------


def fit(subject: Subject, hardware: Optional[Hardware] = None, *,
        now: bool = False) -> dict:
    """Every way ``subject`` could run on ``hardware``, ranked fast to slow.

    The ONE fit function ruling 6 asked for: a ModelUnit and a Release go
    through the same arithmetic, so the inventory page and the Discover page
    can never disagree about whether something runs here.

    Returns ``{subject, hardware, placements, best, runnable_idle,
    runnable_now}``. ``best`` is the first feasible placement when the GPUs are
    idle. ``runnable_idle`` is the OR over the space — "can this box run it at
    all" — and is deliberately NOT one card's answer.
    """
    hw = hardware if hardware is not None else detect_hardware(now=now)
    w, w_how = _weights_gb(subject)
    kv, basis, kv_how = _kv_for(subject)

    placements: list[Placement] = []
    if w <= 0:
        return {"subject": subject.to_dict(), "hardware": hw.to_dict(),
                "placements": [], "best": None,
                "runnable_idle": False, "runnable_now": False,
                "reason": f"no size for this subject ({w_how}) — nothing to place"}

    if subject.is_media:
        placements.extend(_eval_media(subject, hw, w, BASIS_MEDIA))
    else:
        placements.extend(_eval_single(subject, hw, w, kv, basis, kv_how))
        placements.extend(_eval_split(subject, hw, w, kv, basis, kv_how))
        placements.extend(_eval_offload(subject, hw, w, kv, basis, kv_how))
        placements.extend(_eval_moe(subject, hw, w, kv, basis, kv_how))
    placements.extend(_eval_cpu(subject, hw, w, kv,
                                BASIS_MEDIA if subject.is_media else basis,
                                kv_how))

    # Feasible first, then fast before slow, then the smallest footprint. A
    # placement that fits NOW outranks one that only fits idle, because the
    # user asking `--now` is asking what they can start this minute.
    placements.sort(key=lambda p: (
        not p.fits_idle,
        not p.fits_now,
        _SPEED_ORDER.get(p.speed_class, 9),
        p.est_vram_gb + p.est_ram_gb,
    ))
    feasible_idle = [p for p in placements if p.fits_idle]
    feasible_now = [p for p in placements if p.fits_idle and p.fits_now]

    return {
        "subject": subject.to_dict(),
        "hardware": hw.to_dict(),
        "weights_gb": round(w, 2),
        "weights_basis": w_how,
        "kv_gb": round(kv, 2),
        "basis": BASIS_MEDIA if subject.is_media else basis,
        "placements": [p.to_dict() for p in placements],
        "best": feasible_idle[0].to_dict() if feasible_idle else None,
        "best_now": feasible_now[0].to_dict() if feasible_now else None,
        "runnable_idle": bool(feasible_idle),
        "runnable_now": bool(feasible_now) if hw.now_known else None,
    }


# --- building a subject from what okuro already has -------------------------

#: Category directory -> modality. A store's own top-level layout already sorts
#: models by what they are, so the modality is read from where a unit LIVES
#: rather than guessed from its name.
#:
#: Only GENERIC category words live here. A store also carries project
#: directories named after whatever the work is called, and those are host
#: data: they belong in `ai_models.modality_dirs` in ~/.okuro/config.yaml, not
#: in this repo. Same rule the GPU names follow.
_DIR_MODALITY = {
    "text": "text", "llm": "text", "llms": "text", "llms-gguf": "text",
    "gguf": "text", "language": "text",
    "image": "image", "images": "image", "nsfw": "image", "comfyui": "image",
    "diffusion": "image", "checkpoints": "image", "lora": "image",
    "video": "video", "avatar": "video", "animation": "video",
    "audio": "audio", "voice": "audio", "music": "audio", "tts": "audio",
    "speech": "audio",
    "vision": "vision", "clip": "vision", "embedding": "text",
    "3d": "3d", "mesh": "3d",
}

MODALITY_DIRS_CONVENTION_KEY = "ai_models.modality_dirs"


def modality_dirs() -> dict:
    """The directory -> modality map for this host.

    The generic defaults, overlaid with whatever `ai_models.modality_dirs`
    declares. That key is where a host names its own project directories —
    which is host data and never belongs in the repo.
    """
    out = dict(_DIR_MODALITY)
    try:
        from okuro.yu.conventions import get_convention
        extra = get_convention(MODALITY_DIRS_CONVENTION_KEY, {}) or {}
        if isinstance(extra, dict):
            out.update({str(k).lower(): str(v) for k, v in extra.items()})
    except Exception:  # a bad convention must never break a fit
        pass
    return out


def _modality_for(rel_path: str, parsed: Any = None) -> str:
    """What KIND of model this is.

    The FAMILY wins when it is known, and the directory is the fallback. That
    precedence is the right way round and the other way was a measured bug: a
    text LLM filed under the store's `image/` bucket — which two of this host's
    27 registry aliases are — came back as an image model and was handed the
    diffusion placements instead of the KV-cache arithmetic. Where someone
    filed a file is a statement about the filer; the family is a statement
    about the model.

    A known family whose rule declares no modality is a text LLM: the media
    rules all declare one precisely so the text ones do not have to.
    """
    if parsed is not None and getattr(parsed, "family", None):
        return getattr(parsed, "modality", None) or "text"
    table = modality_dirs()
    for p in [p.lower() for p in str(rel_path or "").split("/") if p]:
        if p in table:
            return table[p]
    return "text"


def unit_modality(unit: dict) -> str:
    """What KIND of model an installed unit is — the public name for the rule.

    P4 needs this to answer "does this category have anything installed at
    all", which is what makes a candidate in an empty category interesting with
    no relation to compare against.
    """
    from okuro.ai_models import lineage as L

    family = unit.get("family")
    if family:
        return L.modality_for_family(family) or "text"
    return _modality_for(unit.get("rel_path"))


def subject_from_unit(unit: dict, *, context: Optional[int] = None) -> Subject:
    """A :class:`Subject` from a ``model_units`` row.

    Uses the lineage columns migration 152 added; re-parses the name only when
    they have not been written yet, so a fit never depends on the lineage pass
    having run first.
    """
    from okuro.ai_models import lineage as L

    parsed = None
    if not unit.get("family"):
        L.enrich_unit(unit)
        parsed = L.unit_parse(unit)

    def pick(col, attr):
        v = unit.get(col)
        if v is not None:
            return v
        return getattr(parsed, attr, None) if parsed else None

    total = pick("params_total_b", "params_total_b")
    active = pick("params_active_b", "params_active_b")
    quant = pick("quant", "quant")

    # Modality from the FAMILY when one is known, whether it came from the
    # stored column or from a fresh parse; the category directory is only the
    # fallback. See _modality_for for the bug that settled the precedence.
    family = unit.get("family") or getattr(parsed, "family", None)
    if family:
        modality = L.modality_for_family(family) or "text"
    else:
        modality = _modality_for(unit.get("rel_path"), parsed)

    gguf = None
    if (unit.get("format") or "") == "gguf":
        gguf = unit.get("primary_file") or L.enrich_unit(unit).get("primary_file")

    # The GGUF header is authoritative about MoE where a name is silent: it
    # publishes expert_count and expert_used_count, and a `64x2.6B` model whose
    # filename says neither is a real case on this host's hot store.
    s = Subject(
        kind="unit", subject_id=str(unit.get("unit_id") or ""),
        name=str(unit.get("name") or ""),
        size_gb=(unit.get("size_bytes") or 0) / _GiB,
        quant=quant, params_total_b=total, params_active_b=active,
        is_moe=bool(active),
        modality=modality,
        context=context or fit_context(),
        gguf_path=gguf, status=str(unit.get("status") or "ok"),
        note=unit.get("note"),
    )
    _refine_from_header(s)
    return s


def _refine_from_header(s: Subject) -> None:
    """Fill params and the MoE flag from the GGUF header when the name was silent."""
    dims = s.dims()
    if not dims:
        return
    if dims.get("expert_count") and dims.get("expert_used_count"):
        s.is_moe = True
    if (s.params_total_b is None or s.params_active_b is None) and dims.get("size_label"):
        from okuro.ai_models.lineage import _detect_sizes, _normalise
        total, active, moe, _ = _detect_sizes(_normalise(str(dims["size_label"])))
        if s.params_total_b is None and total:
            s.params_total_b = total
        if s.params_active_b is None and active:
            s.params_active_b = active
        if moe:
            s.is_moe = True
    # An expert model whose active-param count is nowhere in the name or the
    # size label can still be placed: experts_used/experts is the share of the
    # expert tensors a token touches.
    if s.is_moe and s.params_active_b is None and s.params_total_b:
        ec, eu = dims.get("expert_count"), dims.get("expert_used_count")
        if ec and eu:
            s.params_active_b = round(s.params_total_b * (eu / ec), 2)


def subject_from_release(candidate: Any, *, size_gb: Optional[float] = None,
                         modality: Optional[str] = None,
                         context: Optional[int] = None) -> Subject:
    """A :class:`Subject` from a discovery candidate.

    ``candidate`` may be a catalog id / HF name, a ``model_discoveries`` row, or
    anything with the same attributes. Nothing is downloaded and nothing is
    opened: a release is placed on its card size and its parsed name alone.
    """
    from okuro.ai_models import lineage as L

    if isinstance(candidate, dict):
        name = str(candidate.get("display_name") or candidate.get("catalog_id") or "")
        cid = str(candidate.get("catalog_id") or name)
        size_gb = size_gb if size_gb is not None else candidate.get("size_gb")
        modality = modality or candidate.get("modality")
    else:
        name = str(getattr(candidate, "display_name", None) or candidate)
        cid = str(getattr(candidate, "catalog_id", None) or name)
        size_gb = (size_gb if size_gb is not None
                   else getattr(candidate, "size_gb", None))
        modality = modality or getattr(candidate, "modality", None)

    p = L.parse(name)
    return Subject(
        kind="release", subject_id=cid, name=name,
        size_gb=float(size_gb or 0.0), quant=p.quant,
        params_total_b=p.params_total_b, params_active_b=p.params_active_b,
        is_moe=p.is_moe,
        modality=(modality or p.modality or "text"),
        context=context or fit_context(),
    )


def fit_unit(unit_id_or_row: Any, hardware: Optional[Hardware] = None, *,
             now: bool = False, context: Optional[int] = None) -> dict:
    """Placement space for one installed unit."""
    if isinstance(unit_id_or_row, dict):
        row = unit_id_or_row
    else:
        from okuro.ai_models.lineage import find_unit
        row, reason = find_unit(str(unit_id_or_row))
        if row is None:
            return {"found": False, "subject": str(unit_id_or_row),
                    "reason": reason}
    out = fit(subject_from_unit(row, context=context), hardware, now=now)
    out["found"] = True
    if row.get("status") and row["status"] != "ok":
        out["warning"] = (
            f"this unit is {row['status']}: {row.get('note') or 'no reason recorded'}. "
            f"The fit below is computed from its DECLARED size and says what it "
            f"would need if it were whole — it does not say it will load.")

    # A unit on a COLD store is placed exactly like any other — the arithmetic
    # is about VRAM and RAM, not about which disk the bytes came from — but
    # saying only "it fits" would be misleading: it is not on the hot store, so
    # running it means moving it first. The note says so and nothing is moved.
    tier = _store_tier(row.get("store"))
    if tier == "cold" and out.get("runnable_idle"):
        out["placement_note"] = (
            f"runnable if moved to a hot store — this unit lives on "
            f"`{row.get('store')}`, which is declared cold. The placement "
            f"above is the hardware answer; the move is a separate decision "
            f"and nothing has been moved.")
    out["store_tier"] = tier
    return out


def _store_tier(store: Optional[str]) -> Optional[str]:
    if not store:
        return None
    try:
        from okuro.ai_models.store_scan import resolve_store
        s = resolve_store(str(store))
        return s.tier if s else None
    except Exception:
        return None


def fit_release(candidate: Any, hardware: Optional[Hardware] = None, *,
                now: bool = False, size_gb: Optional[float] = None,
                context: Optional[int] = None) -> dict:
    """Placement space for a discovery candidate. P4's entry point."""
    return fit(subject_from_release(candidate, size_gb=size_gb,
                                    context=context), hardware, now=now)


# --- persistence ------------------------------------------------------------


def persist_fits(subject_kind: str, subject_id: str, result: dict) -> int:
    """Replace the stored placement set for one subject. Returns rows written."""
    from okuro.db import get_db
    import json as _json

    db = get_db()
    db.execute("DELETE FROM model_fits WHERE subject_kind = ? AND subject_id = ?",
               (subject_kind, subject_id))
    n = 0
    for p in result.get("placements", []):
        db.execute(
            "INSERT OR REPLACE INTO model_fits (subject_kind, subject_id, mode, "
            "gpu, est_vram_gb, vram_detail, est_ram_gb, offloaded_pct, "
            "speed_class, fits_idle, fits_now, basis, why, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))",
            (subject_kind, subject_id, p["mode"], p["gpu"] or "",
             p["est_vram_gb"], _json.dumps(p["vram_detail"]), p["est_ram_gb"],
             p["offloaded_pct"], p["speed_class"], int(p["fits_idle"]),
             int(p["fits_now"]), p["basis"], p["why"]))
        n += 1
    return n


def fit_all_units(*, now: bool = False, only_new: bool = True,
                  hardware: Optional[Hardware] = None) -> dict:
    """Compute and store the placement space for every unit in the inventory."""
    from okuro.db import get_db

    db = get_db()
    rows = [dict(r) for r in db.fetchall("SELECT * FROM model_units")]
    hw = hardware if hardware is not None else detect_hardware(now=now)
    have = set()
    if only_new:
        have = {r["subject_id"] for r in db.fetchall(
            "SELECT DISTINCT subject_id FROM model_fits WHERE subject_kind='unit'")}

    done = skipped = runnable = 0
    for row in rows:
        if only_new and row["unit_id"] in have:
            skipped += 1
            continue
        try:
            res = fit(subject_from_unit(row), hw)
        except Exception as exc:  # one bad unit must never fail the pass
            log.debug("fit failed for %s: %s", row.get("unit_id"), exc)
            continue
        persist_fits("unit", row["unit_id"], res)
        done += 1
        runnable += bool(res.get("runnable_idle"))
    return {"units": len(rows), "computed": done, "skipped": skipped,
            "runnable_idle": runnable, "hardware": hw.to_dict()}


def best_fits(unit_ids: Iterable[str], *, subject_kind: str = "unit") -> dict:
    """``subject_id -> the stored best placement``, in one query.

    The inventory's join. Mirrors P2's ``unit_consumers``: one query for the
    whole page, never a fit per row. ``subject_kind='release'`` is the same
    join for discovery candidates, whose fits P4 persists into the same table.
    """
    ids = [u for u in unit_ids if u]
    if not ids:
        return {}
    from okuro.db import get_db

    marks = ",".join("?" for _ in ids)
    rows = get_db().fetchall(
        f"SELECT subject_id, mode, gpu, est_vram_gb, est_ram_gb, speed_class, "
        f"offloaded_pct, fits_idle, fits_now, basis FROM model_fits "
        f"WHERE subject_kind = ? AND subject_id IN ({marks}) "
        f"AND fits_idle = 1", (subject_kind, *ids))
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        cur = out.get(d["subject_id"])
        rank = (_SPEED_ORDER.get(d["speed_class"], 9),
                d["est_vram_gb"] + d["est_ram_gb"])
        if cur is None or rank < cur["_rank"]:
            d["_rank"] = rank
            out[d["subject_id"]] = d
    for d in out.values():
        d.pop("_rank", None)
    return out


__all__ = [
    # pre-P3 surface, unchanged for existing callers (Map A: bundle, catalog)
    "QUANT_GB_PER_B", "CONTEXT_OVERHEAD_GB",
    "estimate_vram", "estimate_vram_detailed", "kv_cache_gb",
    "fits_gpu", "recommend_gpu",
    # P3 placement space
    "BASIS_GGUF", "BASIS_HEURISTIC", "BASIS_MEDIA",
    "DEFAULT_CONTEXT", "MEDIA_MODALITIES",
    "MODE_SINGLE", "MODE_SPLIT", "MODE_OFFLOAD", "MODE_MOE", "MODE_CPU",
    "MODE_MEDIA_SINGLE", "MODE_MEDIA_ENCODERS", "MODE_MEDIA_SWAP",
    "MODE_MEDIA_FP8", "MODALITY_DIRS_CONVENTION_KEY", "modality_dirs",
    "MOE_MIN_RESIDENT_FRAC", "OFFLOAD_USABLE_MAX_PCT",
    "RUNTIME_OVERHEAD_FLOOR_GB", "RUNTIME_OVERHEAD_FRAC", "VRAM_SAFETY_GB",
    "SPEED_FAST", "SPEED_USABLE", "SPEED_SLOW",
    "GPU", "Hardware", "Placement", "Subject",
    "best_fits", "detect_hardware", "fit", "fit_all_units", "fit_context",
    "fit_release", "fit_unit", "persist_fits",
    "subject_from_release", "subject_from_unit", "unit_modality",
]
