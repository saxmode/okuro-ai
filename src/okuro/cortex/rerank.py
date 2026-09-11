# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Optional cross-encoder reranker for cortex search — config-gated, default OFF, graceful fallback.
# index:
#   imports
#   class Reranker (protocol)
#   class CrossEncoderReranker
#   def _rerank_enabled
#   def get_reranker
# AGENT_HEADER_END -->
"""Optional cross-encoder reranker for cortex hybrid search (audit F26).

A cross-encoder rescoring the fused top-N (query, passage) pairs is the single
biggest precision lever in IR — but it is NOT something every install can run
(needs the model weights + a GPU/CPU budget + the sentence-transformers dep).
So this module is built as a clean, swappable seam:

  * **Default OFF.** Reranking only activates when explicitly enabled via
    config/env AND a model is importable. No silent GPU grab.
  * **Graceful degradation.** If the dep is missing, the model can't load, or
    a GPU isn't available, ``get_reranker`` returns ``None`` (logged once) and
    search falls back to the RRF fusion order — never crashes, never blocks.
  * **Lazy + cached.** The model is imported and loaded on first use and cached
    process-wide, so the import cost is paid once and only when reranking is on.

Enable per install with::

    OKURO_CORTEX_RERANK=1                      # turn it on
    OKURO_CORTEX_RERANK_MODEL=BAAI/bge-reranker-v2-m3   # optional override

VRAM cost (FYI): bge-reranker-v2-m3 (~568M params) ≈ 2.2 GB VRAM in fp16 for a
batch of 30 short passages; the smaller bge-reranker-base (~278M) ≈ 1.1 GB.
CPU works too (slower). Reranking only the fused top-N (default 30) keeps the
per-query cost bounded.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional, Protocol

log = logging.getLogger(__name__)

DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"


class Reranker(Protocol):
    """Minimal rerank seam — swap any implementation that scores (query, passage)."""

    def score(self, query: str, passages: list[str]) -> list[float]:
        """Return one relevance score per passage (higher = more relevant)."""
        ...


class CrossEncoderReranker:
    """sentence-transformers CrossEncoder wrapper. Lazy-loads the model."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                # Imported here so a missing dep only matters when rerank is on.
                from sentence_transformers import CrossEncoder
                import torch

                # bf16 weights on CUDA halve weight VRAM with negligible ranking
                # impact; CPU stays fp32. Same footprint discipline as embed/server.
                model_kwargs = None
                if torch.cuda.is_available():
                    model_kwargs = {"dtype": torch.bfloat16}
                self._model = CrossEncoder(
                    self.model_name, model_kwargs=model_kwargs
                )
        return self._model

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        model = self._ensure_model()
        pairs = [(query, p) for p in passages]
        # Cap the micro-batch so CrossEncoder.predict() (ST default 32) doesn't
        # spike O(seq²) attention that the CUDA allocator then pins as resident
        # VRAM for the process lifetime — same idle-VRAM bug as embed/server.
        batch_size = int(os.environ.get("OKURO_CORTEX_RERANK_BATCH", "8"))
        preds = model.predict(pairs, batch_size=batch_size)
        # Release the activation high-water block back to the driver.
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 — never let cleanup break scoring
            pass
        # Normalise numpy / scalar outputs to a plain float list.
        try:
            return [float(x) for x in preds]
        except TypeError:
            return [float(preds)]


# Process-wide cache + a sentinel so a failed load is not retried every query.
_RERANKER: Optional[Reranker] = None
_RESOLVED = False
_RESOLVE_LOCK = threading.Lock()


def _rerank_enabled(config) -> tuple[bool, str]:
    """Resolve (enabled, model_name) from env (and optionally config).

    Off unless ``OKURO_CORTEX_RERANK`` is a truthy flag. The model name comes
    from ``OKURO_CORTEX_RERANK_MODEL`` or a config attribute, else the default.
    """
    flag = os.environ.get("OKURO_CORTEX_RERANK", "").strip().lower()
    enabled = flag in {"1", "true", "yes", "on"}
    # Allow a config object to opt in too (e.g. VectorConfig.rerank=True).
    if not enabled and getattr(config, "rerank", False):
        enabled = True
    model = (
        os.environ.get("OKURO_CORTEX_RERANK_MODEL", "").strip()
        or getattr(config, "rerank_model", "")
        or DEFAULT_RERANK_MODEL
    )
    return enabled, model


def get_reranker(config) -> Optional[Reranker]:
    """Return the active reranker, or ``None`` if disabled/unavailable.

    Resolved once per process and cached. A missing dependency or a model that
    fails to load is logged ONCE and cached as ``None`` so search keeps using
    the fusion order without retrying the expensive import every query.

    Pass ``_reset_for_tests=...`` is not supported here — tests inject a stub
    via :func:`set_reranker_for_test`.
    """
    global _RERANKER, _RESOLVED
    if _RESOLVED:
        return _RERANKER
    with _RESOLVE_LOCK:
        if _RESOLVED:
            return _RERANKER
        enabled, model = _rerank_enabled(config)
        if not enabled:
            _RERANKER = None
            _RESOLVED = True
            return None
        try:
            reranker = CrossEncoderReranker(model)
            # Eager-load now so a missing dep/model surfaces here (logged once)
            # rather than on the first user query.
            reranker._ensure_model()
            _RERANKER = reranker
            log.info("cortex reranker active: %s", model)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "cortex reranker requested (%s) but unavailable (%s) — "
                "falling back to fusion ranking", model, exc,
            )
            _RERANKER = None
        _RESOLVED = True
        return _RERANKER


def set_reranker_for_test(reranker: Optional[Reranker]) -> None:
    """Test hook: inject a stub reranker (or None) and mark resolution done."""
    global _RERANKER, _RESOLVED
    _RERANKER = reranker
    _RESOLVED = True


def reset_reranker_cache() -> None:
    """Test hook: clear the cached resolution so the next get_reranker re-runs."""
    global _RERANKER, _RESOLVED
    _RERANKER = None
    _RESOLVED = False
