# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Optional embedding server — one model, all okuro services.
# index: imports | def _get_model | class _PendingRequest | def create_app | def main
# AGENT_HEADER_END -->
"""Optional embedding server — one model, all okuro services.

Start with: okuro embed serve

Design notes:
- .encode() is offloaded via asyncio.to_thread so it never blocks the event loop
- Requests arriving within OKURO_EMBED_BATCH_MS are coalesced into one encode call
- OKURO_EMBED_CONCURRENCY caps in-flight batches (GPU/CPU contention)
- /embed and /health response shapes are unchanged
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from okuro.embed.config import TIERS, TierSpec, load as load_config

logger = logging.getLogger("okuro.embed.server")


def _resolve_tier_spec() -> TierSpec:
    """Pick the tier spec from env var → config file → fallback (``high``).

    OKURO_EMBED_TIER is the authoritative runtime signal — install.py
    sets it from the persisted config so unit-file changes propagate
    without re-reading the file. The file fallback covers manual
    ``systemctl --user start`` after editing embed-config.yaml by hand.
    The "high" default preserves the prior behaviour for installs that
    predate the config file.
    """
    env_tier = os.environ.get("OKURO_EMBED_TIER", "").lower().strip()
    if env_tier in TIERS:
        return TIERS[env_tier]  # type: ignore[index]
    try:
        cfg = load_config()
    except (ValueError, OSError) as exc:
        logger.warning(
            "embed-config.yaml unreadable (%s) — defaulting tier=low "
            "(laptop-safe; recommended_tier upgrades GPU boxes)", exc
        )
        cfg = None
    if cfg is not None:
        return cfg.spec
    return TIERS["low"]


def _resolve_config_device() -> str:
    """Persisted device preference (``auto|cpu|mps|cuda:N``), default ``auto``.

    OKURO_EMBED_DEVICE overrides the file for the same reason OKURO_EMBED_TIER
    does — the unit file can pin it without a re-read. Absent both, ``auto``
    lets the runtime resolve cuda → mps → cpu. A malformed config falls back to
    ``auto`` rather than failing service start.
    """
    env_device = os.environ.get("OKURO_EMBED_DEVICE", "").strip()
    if env_device:
        return env_device
    try:
        cfg = load_config()
    except (ValueError, OSError):
        cfg = None
    return cfg.device if cfg is not None else "auto"


_TIER_SPEC: TierSpec = _resolve_tier_spec()
_CONFIG_DEVICE: str = _resolve_config_device()
_model = None
_model_name = _TIER_SPEC.model_id
_model_dim = _TIER_SPEC.dim
_load_time = 0.0


def _bundled_model_path() -> str:
    """Return the vendored model dir if present, else the HF id as fallback."""
    p = Path(__file__).parent / "models" / _TIER_SPEC.bundled_dirname
    return str(p) if (p / "config.json").exists() else _TIER_SPEC.model_id

_BATCH_WINDOW_MS = int(os.environ.get("OKURO_EMBED_BATCH_MS", "10"))
_MAX_BATCH_SIZE = int(os.environ.get("OKURO_EMBED_MAX_BATCH", "32"))
_MAX_CONCURRENT = int(os.environ.get("OKURO_EMBED_CONCURRENCY", "4"))
# Qwen3-Embedding-0.6B defaults to a 32k context window. Long files (jupyter
# bundles, search-index.json) trigger O(N^2) attention allocations that OOM
# even a 48 GB card. Cap the encode-side sequence length; SentenceTransformer
# truncates per-input above this.
#
# Default raised 512 → 2048 (F14): the 512 cap was inherited from bge-small and
# truncated the larger token chunks the cortex chunker now emits (~1024 tokens
# + contextual breadcrumb). 2048 comfortably fits a 1024-token chunk plus its
# breadcrumb header with margin, and is verified safe: 16×~2400-token docs
# encode in ~1s with negligible VRAM growth on a 48 GB card (a 0.6B model's
# attention at seq 2048 is tiny). Raise further via OKURO_EMBED_MAX_SEQ on
# bigger GPUs; lower it on memory-constrained installs.
_MAX_SEQ_LEN = int(os.environ.get("OKURO_EMBED_MAX_SEQ", "2048"))
# Internal encode() micro-batch. ST defaults to 32; at seq 2048 that spikes
# O(seq²) attention activations to several GB, which the CUDA caching allocator
# then pins as resident VRAM forever (the "10 GB idle" bug). Cap it so the peak
# block is small and releasable. Raise on big GPUs via OKURO_EMBED_ENCODE_BATCH.
_ENCODE_BATCH_SIZE = int(os.environ.get("OKURO_EMBED_ENCODE_BATCH", "8"))

_queue: "asyncio.Queue[_PendingRequest] | None" = None
_semaphore: asyncio.Semaphore | None = None
_batcher_task: asyncio.Task | None = None

# Encodes running right now. The semaphore already knows this as _value, but
# that is a CPython implementation detail; this counter is ours and cannot be
# renamed out from under /health. Read by the restart guard: killing the service
# mid-encode fails the caller's embed (cortex retries next tick), so it is worth
# a short wait rather than a blind kill.
_in_flight = 0


def busy_snapshot() -> dict:
    """What this service is doing right now — for /health and the restart guard.

    Two numbers, because they mean different things: `encoding` is work that dies
    on restart, `queued` is work that never started and whose caller is still
    blocked. Either being non-zero means a restart costs someone a failed embed.
    """
    return {
        "encoding": _in_flight,
        "queued": _queue.qsize() if _queue is not None else 0,
    }


def _resolve_device(configured: str) -> str:
    """Resolve the persisted device preference to a torch device string.

    ``configured`` is one of ``auto|cpu|mps|cuda:N`` (validated in config.py).
    Returns ``"cuda"``, ``"mps"`` or ``"cpu"``.

    - ``cuda:N`` ordinals are handled process-externally via
      CUDA_VISIBLE_DEVICES (see ``config.cuda_visible_devices()``), so inside
      the process the bare ``"cuda"`` already maps to the one visible device.
    - ``auto`` resolves cuda → mps → cpu.
    - An explicit accelerator that isn't actually present falls back to cpu so a
      stale config (e.g. ``mps`` on a Linux box) degrades instead of crashing.
    """
    import torch

    def _cuda_ok() -> bool:
        return torch.cuda.is_available()

    def _mps_ok() -> bool:
        mps = getattr(torch.backends, "mps", None)
        return mps is not None and mps.is_available()

    choice = (configured or "auto").lower()
    if choice.startswith("cuda"):
        return "cuda" if _cuda_ok() else "cpu"
    if choice == "mps":
        return "mps" if _mps_ok() else "cpu"
    if choice == "cpu":
        return "cpu"
    # auto
    if _cuda_ok():
        return "cuda"
    if _mps_ok():
        return "mps"
    return "cpu"


def _cpu_thread_cap() -> int:
    """Intra-op torch thread cap for the CPU path.

    Without a cap PyTorch grabs every physical core: on a GPU-less box the
    low-tier model pegs ~600% CPU (6 cores). Default to half the cores, max 4;
    override with OKURO_EMBED_CPU_THREADS for power users.
    """
    env_threads = os.environ.get("OKURO_EMBED_CPU_THREADS", "").strip()
    if env_threads:
        try:
            n = int(env_threads)
            if n >= 1:
                return n
        except ValueError:
            pass
    cores = os.cpu_count() or 1
    return min(4, max(1, cores // 2))


def _get_model():
    global _model, _load_time
    if _model is None:
        from sentence_transformers import SentenceTransformer
        import torch

        t0 = time.time()
        device = _resolve_device(_CONFIG_DEVICE)
        # bf16 weights on CUDA: halves weight VRAM (~2.4 GB fp32 → ~1.2 GB) with
        # negligible cosine-similarity impact for embeddings. CPU/MPS stay fp32
        # (bf16 matmul is slow/unsupported on many CPU builds).
        model_kwargs = None
        if device == "cuda":
            model_kwargs = {"dtype": torch.bfloat16}
        elif device == "cpu":
            # Bound intra-op threads once, on the CPU path only — CUDA/MPS
            # offload to the accelerator so throttling them would only slow the
            # host-side glue. See _cpu_thread_cap for the 600%-peg rationale.
            torch.set_num_threads(_cpu_thread_cap())
        _model = SentenceTransformer(
            _bundled_model_path(), device=device, model_kwargs=model_kwargs
        )
        _model.max_seq_length = _MAX_SEQ_LEN
        _load_time = time.time() - t0
    return _model


@dataclass
class _PendingRequest:
    texts: list[str]
    future: asyncio.Future
    start: int = 0
    end: int = 0


async def _run_encode(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    assert _semaphore is not None

    def _encode() -> list[list[float]]:
        vecs = model.encode(
            texts,
            convert_to_numpy=True,
            batch_size=_ENCODE_BATCH_SIZE,
        ).tolist()
        # Return the activation high-water block to the driver. Without this the
        # caching allocator pins the peak (seq²) segment as resident VRAM for the
        # process lifetime — the idle-10 GB bug. Cheap relative to the encode,
        # and the batcher coalesces calls so it isn't hot.
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        return vecs

    async with _semaphore:
        global _in_flight
        _in_flight += 1
        try:
            return await asyncio.to_thread(_encode)
        finally:
            # finally, not after the await: an encode that raises or is cancelled
            # must not leave the counter high forever, or the guard would refuse
            # every restart from then on.
            _in_flight -= 1


async def _batcher_loop() -> None:
    """Coalesce requests arriving within a short window into one encode call."""
    assert _queue is not None
    window_s = _BATCH_WINDOW_MS / 1000.0
    while True:
        try:
            first = await _queue.get()
        except asyncio.CancelledError:
            return

        batch: list[_PendingRequest] = [first]
        total_texts = len(first.texts)
        deadline = time.monotonic() + window_s

        while total_texts < _MAX_BATCH_SIZE:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                nxt = await asyncio.wait_for(_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            batch.append(nxt)
            total_texts += len(nxt.texts)

        combined: list[str] = []
        for item in batch:
            item.start = len(combined)
            combined.extend(item.texts)
            item.end = len(combined)

        try:
            vectors = await _run_encode(combined)
            for item in batch:
                if not item.future.done():
                    item.future.set_result(vectors[item.start : item.end])
        except Exception as exc:
            logger.exception("embed batch failed (%d items): %s", len(batch), exc)
            for item in batch:
                if not item.future.done():
                    item.future.set_exception(exc)


def create_app():
    from fastapi import FastAPI
    from pydantic import BaseModel

    app = FastAPI(title="okuro-embed", version="1.1.0")

    class EmbedRequest(BaseModel):
        texts: list[str]

    class EmbedResponse(BaseModel):
        embeddings: list[list[float]]
        model: str
        count: int

    @app.on_event("startup")
    async def startup() -> None:
        global _queue, _semaphore, _batcher_task
        _get_model()
        _queue = asyncio.Queue()
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        _batcher_task = asyncio.create_task(_batcher_loop())

    @app.on_event("shutdown")
    async def shutdown() -> None:
        global _batcher_task
        if _batcher_task is not None:
            _batcher_task.cancel()
            try:
                await _batcher_task
            except asyncio.CancelledError:
                pass
            _batcher_task = None

    @app.post("/embed", response_model=EmbedResponse)
    async def embed(req: EmbedRequest):
        if not req.texts:
            return EmbedResponse(embeddings=[], model=_model_name, count=0)
        assert _queue is not None, "embed server not initialized"
        loop = asyncio.get_running_loop()
        pending = _PendingRequest(texts=req.texts, future=loop.create_future())
        await _queue.put(pending)
        vectors = await pending.future
        return EmbedResponse(
            embeddings=vectors,
            model=_model_name,
            count=len(vectors),
        )

    @app.get("/health")
    async def health():
        loaded = _model is not None
        return {
            "status": "ok" if loaded else "loading",
            "model": _model_name,
            "tier": _TIER_SPEC.tier,
            "loaded": loaded,
            "load_time_s": round(_load_time, 2),
            "dimensions": _model_dim,
            # Live state, not capability. Everything below this line describes
            # what the service CAN do; these describe what it IS doing, which is
            # what a restart decision actually turns on.
            **busy_snapshot(),
            "max_concurrent": _MAX_CONCURRENT,
            "max_batch_size": _MAX_BATCH_SIZE,
            "max_seq_length": _MAX_SEQ_LEN,
            "batch_window_ms": _BATCH_WINDOW_MS,
        }

    return app


def main():
    # ps / top row rename — see okuro.daemon for the same pattern.
    try:
        import setproctitle
        setproctitle.setproctitle("okuro-embed")
    except ImportError:
        pass
    import uvicorn

    # audit(NF-2): default to loopback — mirrors the C1 fix for orchestrator.
    # On CEO/CTO laptops on hotel/conference wifi, an embed service on 0.0.0.0
    # is a CPU/GPU burn vector + topical info-leak (any LAN device can submit
    # text and get back vectors learning what the user is processing). Opt-in
    # via OKURO_EMBED_HOST=0.0.0.0 for intentional LAN exposure.
    from okuro.system.port_registry import embed_port

    host = os.environ.get("OKURO_EMBED_HOST", "127.0.0.1")
    port = embed_port()
    uvicorn.run(
        create_app(),
        host=host,
        port=port,
        log_level="warning",
    )
