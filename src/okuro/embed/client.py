# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Embedding client for okuro.
# index: imports | def _local_fallback | def embed | def embed_one | def embed_query | def to_bytes
# AGENT_HEADER_END -->
"""
Embedding client for okuro.

Calls a shared HTTP embedding service (default http://127.0.0.1:13334).
Falls back to loading a local model in-process if the service is unreachable —
but logs a warning so the fallback is not silent.

sentence-transformers is an optional dependency (one of the embed-* extras).
When it isn't installed, embed() raises ``EmbeddingsUnavailable`` with a clear
install hint instead of a confusing ImportError mid-request. Callers that
don't require embeddings (lexical search paths) should catch this and degrade.
"""

import concurrent.futures
import json
import logging
import os
import struct
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("okuro.embed.client")

from okuro.embed.config import TIERS, TierSpec, load as load_config
from okuro.system.port_registry import embed_url as _resolve_embed_url


def _resolve_tier_spec() -> TierSpec:
    """Same tier-resolution precedence as embed.server — env → file → high."""
    env_tier = os.environ.get("OKURO_EMBED_TIER", "").lower().strip()
    if env_tier in TIERS:
        return TIERS[env_tier]  # type: ignore[index]
    try:
        cfg = load_config()
    except (ValueError, OSError):
        cfg = None
    if cfg is not None:
        return cfg.spec
    return TIERS["high"]


_TIER_SPEC: TierSpec = _resolve_tier_spec()
_OKURO_EMBED_URL = _resolve_embed_url()
_TIMEOUT = float(os.environ.get("OKURO_EMBED_TIMEOUT", "30"))
# Mirror server-side cap. Qwen3-Embedding-0.6B's 32k context produces
# multi-GB attention allocations on long inputs. Truncate at the same
# budget the embed service uses so daemon-side fallback can't OOM the GPU.
# Default raised 512 → 2048 (F14) to match the server and fit the larger
# token chunks the cortex chunker now emits. Override via OKURO_EMBED_MAX_SEQ.
_LOCAL_MAX_SEQ_LEN = int(os.environ.get("OKURO_EMBED_MAX_SEQ", "2048"))
# Hard ceiling on the in-process fallback (model load + encode). The fallback
# exists to survive a brief okuro-embed outage, NOT to freeze a tool call: a
# stalled local load that used to block read_memory for ~2.7h must instead
# fail fast so callers degrade (e.g. to lexical search). Override via
# OKURO_EMBED_FALLBACK_TIMEOUT.
_FALLBACK_TIMEOUT = float(os.environ.get("OKURO_EMBED_FALLBACK_TIMEOUT", "20"))

# When the embed HTTP service is unreachable we fall back to in-process
# encoding, but retrying HTTP on every single call spams the log with
# "connection refused" warnings during onboarding — before the service has
# been installed there's nothing listening on the embed port and every
# roles_match / cortex lookup logs a fresh ECONNREFUSED. Track the last
# failure and skip the HTTP probe until the circuit-breaker window elapses,
# so a long batch of embedding lookups produces ONE warning instead of dozens.
_SERVICE_BREAKER_S = 30.0
_service_failed_at: float = 0.0

_local_model = None
_fallback_warned = False
# Single-worker pool used to bound the in-process fallback. Lazily created so
# importing the client costs nothing when the HTTP service is healthy.
_fallback_executor: concurrent.futures.ThreadPoolExecutor | None = None


class EmbeddingsUnavailable(RuntimeError):
    """sentence-transformers isn't installed and the HTTP embed service is unreachable."""


def _local_model_dir() -> str | None:
    """Vendored model dir for the active tier IF present on disk, else None.

    ``None`` means an in-process load would require pulling the model from
    HuggingFace over the network — which must NEVER happen synchronously inside
    a tool call (see ``_local_fallback``).
    """
    p = Path(__file__).parent / "models" / _TIER_SPEC.bundled_dirname
    return str(p) if (p / "config.json").exists() else None


def _bundled_model_path() -> str:
    """Return the vendored model dir if present, else the HF id as fallback."""
    return _local_model_dir() or _TIER_SPEC.model_id


def _run_bounded(fn, timeout: float):
    """Run ``fn`` on the fallback worker, raising TimeoutError past ``timeout``.

    On timeout the worker thread is intentionally left running (a blocked
    native load/encode can't be cancelled) but the caller returns immediately.
    Because the fallback never touches the network (see ``_local_fallback``),
    a lingering worker does bounded local-disk/CPU work only. Subsequent calls
    queue behind it and also fail fast — never hang.
    """
    global _fallback_executor
    if _fallback_executor is None:
        _fallback_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="okuro-embed-fallback"
        )
    fut = _fallback_executor.submit(fn)
    return fut.result(timeout=timeout)


def _encode_local(texts: list[str]) -> list[list[float]]:
    """Load (once) and run the vendored model. Caller guarantees it exists."""
    global _local_model
    if _local_model is None:
        from sentence_transformers import SentenceTransformer

        # Force float32 weights. The model on disk carries a bf16 torch_dtype;
        # loaded as-is, the in-process path fed float32 inputs into bf16 weights
        # → "mat1 and mat2 must have the same dtype: float != BFloat16" matmul
        # crash. float32 is the safe last-resort dtype and matches the HTTP
        # server's CPU/MPS path.
        _model_kwargs = None
        try:
            import torch as _torch
            _model_kwargs = {"dtype": _torch.float32}
        except Exception:
            _model_kwargs = None
        _local_model = SentenceTransformer(
            _local_model_dir(), model_kwargs=_model_kwargs,
        )
        _local_model.max_seq_length = _LOCAL_MAX_SEQ_LEN
    return _local_model.encode(texts, convert_to_numpy=True).tolist()


def is_available() -> bool:
    """True when in-process embedding is possible (sentence-transformers installed)."""
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


_INSTALL_HINT = (
    "sentence-transformers is not installed. Re-run ./install.sh from the "
    "okuro repo (it now installs the embed extras automatically), or do it "
    "manually from the venv: `./venv/bin/pip install -e '.[embed-cpu]' "
    "--index-url https://download.pytorch.org/whl/cpu` "
    "(swap embed-cpu → embed-apple on macOS arm64, embed-cuda for NVIDIA, "
    "embed-rocm for AMD)."
)


def _local_fallback(texts: list[str]) -> list[list[float]]:
    """Last resort: load model in-process — bounded and offline-only.

    Two hard guarantees so a fallback can never freeze a tool call (the P1
    MCP-freeze defect):

    1. No synchronous network download. If the active tier's model isn't
       vendored on disk, loading it would pull from HuggingFace inside the
       request — raise ``EmbeddingsUnavailable`` instead. The model must be
       served by okuro-embed or pre-fetched out-of-band, never downloaded here.
    2. Bounded. The model load + encode runs under a hard timeout; a stall
       raises ``EmbeddingsUnavailable`` rather than blocking indefinitely.

    Callers already degrade on ``EmbeddingsUnavailable`` (e.g. lexical search).
    """
    global _fallback_warned
    if not _fallback_warned:
        logger.warning(
            "okuro-embed service unreachable at %s — loading model in-process. "
            "Each process doing this wastes ~600MB RAM. "
            "Check that okuro-embed.service is running.",
            _OKURO_EMBED_URL,
        )
        _fallback_warned = True

    try:
        import sentence_transformers  # noqa: F401
    except ImportError as exc:
        raise EmbeddingsUnavailable(_INSTALL_HINT) from exc

    if _local_model is None and _local_model_dir() is None:
        raise EmbeddingsUnavailable(
            f"okuro-embed is unreachable and the '{_TIER_SPEC.tier}' tier model "
            f"({_TIER_SPEC.model_id}) is not vendored on disk. Refusing to "
            "download from HuggingFace inside a request (it would freeze the "
            "caller). Start okuro-embed (`systemctl --user start okuro-embed`) "
            "or pre-fetch the model out-of-band."
        )

    try:
        return _run_bounded(lambda: _encode_local(texts), _FALLBACK_TIMEOUT)
    except concurrent.futures.TimeoutError as exc:
        raise EmbeddingsUnavailable(
            f"in-process embedding stalled past {_FALLBACK_TIMEOUT:.0f}s while "
            "okuro-embed is down — failing fast instead of hanging. Start "
            "okuro-embed (`systemctl --user start okuro-embed`)."
        ) from exc


def embed(texts: list[str]) -> list[list[float]]:
    """Generate embeddings via HTTP service, fallback to local model on error."""
    global _service_failed_at

    if not texts:
        return []

    # Circuit-breaker: skip the HTTP probe while it recently failed.
    # Without this, during onboarding (before the embed service is even
    # installed) every roles_match / cortex query repeats the
    # connect-refuse round trip + warning log. One warning per
    # _SERVICE_BREAKER_S window is enough.
    now = time.monotonic()
    if _service_failed_at and (now - _service_failed_at) < _SERVICE_BREAKER_S:
        return _local_fallback(texts)

    try:
        payload = json.dumps({"texts": texts}).encode()
        req = urllib.request.Request(
            f"{_OKURO_EMBED_URL}/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read())
            # Success resets the breaker — transient blips recover quickly.
            _service_failed_at = 0.0
            return data["embeddings"]
    except Exception as exc:
        # First failure in this window: warn once. Subsequent failures
        # within _SERVICE_BREAKER_S skip the warning (handled by the early
        # return above).
        if not _service_failed_at or (now - _service_failed_at) >= _SERVICE_BREAKER_S:
            logger.warning(
                "okuro-embed unreachable at %s (%s) — using in-process model; "
                "suppressing this warning for %ds",
                _OKURO_EMBED_URL, exc, int(_SERVICE_BREAKER_S),
            )
        _service_failed_at = now
        return _local_fallback(texts)


def embed_one(text: str) -> list[float]:
    """Generate embedding for a single text (DOCUMENT side — embed raw)."""
    return embed([text])[0]


# Instruction-tuned models (Qwen3-Embedding) are ASYMMETRIC: a retrieval QUERY
# must be wrapped "Instruct: {task}\nQuery: {q}" while documents are embedded
# raw. Skipping the wrapper collapses discrimination (every doc lands at
# cos~0.6) — the regression that made roles_match rank a UX task onto
# frontend-engineer. Non-instruction models (gte-modernbert) take the text
# as-is. Detect by the active tier's model id so switching tiers stays correct.
_DEFAULT_QUERY_INSTRUCTION = (
    "Given a task, retrieve the description of the expert role best suited to perform it."
)


def _is_instruction_model(spec: TierSpec = _TIER_SPEC) -> bool:
    return "qwen3-embedding" in spec.model_id.lower()


def embed_query(text: str, instruction: str | None = None) -> list[float]:
    """Embed a retrieval QUERY (asymmetric).

    For instruction-tuned models the query is wrapped with an instruction
    prefix; documents must still be embedded via ``embed``/``embed_one`` (raw).
    For non-instruction models the text is embedded unchanged, so callers can
    always route queries through here regardless of the active tier.
    """
    if _is_instruction_model():
        instr = instruction or _DEFAULT_QUERY_INSTRUCTION
        text = f"Instruct: {instr}\nQuery: {text}"
    return embed_one(text)


def to_bytes(vec: list[float]) -> bytes:
    """Pack float list into bytes for sqlite-vec (little-endian float32)."""
    return struct.pack(f"<{len(vec)}f", *vec)
