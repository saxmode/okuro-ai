# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Local CPU speech-to-text for okuro-notes voice capture. faster-whisper
#   (CTranslate2 int8) running entirely on CPU — no GPU, no cloud, stays on LAN
#   (SYS-LAN). Replaces the cloud Groq path in /api/stt as the local-first
#   transcriber. Model is lazy-loaded + cached process-wide.
# index: def is_available | def transcribe_bytes
# AGENT_HEADER_END -->
"""Local CPU Whisper transcription via faster-whisper.

Multilingual `small` by default (handles the user's en + de-CH), int8 on CPU.
Override the model with OKURO_WHISPER_MODEL (e.g. 'base', 'small.en', 'medium').
The model downloads to the HuggingFace cache on first use (~240MB for small int8)
and is then reused. Loading is guarded by a lock so concurrent first requests
don't each spin up a model.
"""

from __future__ import annotations

import io
import logging
import threading

logger = logging.getLogger(__name__)

_model = None
_lock = threading.Lock()


def is_available() -> bool:
    """True when faster-whisper is importable (CPU transcription possible)."""
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


_model_key: str | None = None


def _get_model():
    """Lazy-load the batch model resolved by the STT tier authority.

    The tier picks model size + device + compute (Air→small/CPU, Plus→medium,
    Pro→large-v3/GPU); OKURO_WHISPER_MODEL still pins the size. Cached per
    resolved key so a tier change (via Settings) rebuilds on next request.
    """
    global _model, _model_key
    from okuro.voice import tier as _tier

    backend = _tier.resolve_stt("batch", language=None)
    key = backend.describe()
    if _model is None or _model_key != key:
        with _lock:
            if _model is None or _model_key != key:
                from faster_whisper import WhisperModel

                dev, index = ("cuda", int(backend.device.split(":", 1)[1])) \
                    if backend.device.startswith("cuda:") else (backend.device, None)
                logger.info("loading faster-whisper '%s' (%s)", backend.model_size, key)
                kwargs: dict = {"device": dev, "compute_type": backend.compute_type}
                if index is not None:
                    kwargs["device_index"] = index
                _model = WhisperModel(backend.model_size, **kwargs)
                _model_key = key
    return _model


def transcribe_bytes(audio: bytes, language: str | None = None) -> str:
    """Transcribe an audio clip (WAV/webm/etc.; decoded by PyAV internally).

    language=None auto-detects (lets the user mix en/de). Pass a BCP-47-ish code
    like 'en' or 'de' to pin it. VAD filtering trims silence so short dictations
    don't pad the decode.
    """
    model = _get_model()
    segments, _info = model.transcribe(
        io.BytesIO(audio),
        beam_size=1,
        language=language,
        vad_filter=True,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()
