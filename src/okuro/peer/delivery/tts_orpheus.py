# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.tts_orpheus — Orpheus-3B expressive TTS backend.
#   synth(text, voice, speed) -> (mono float32 samples, 24kHz). Runs CPU-only
#   via llama.cpp (Q8_0 GGUF, n_ctx=4096 ~4.4GB) + SNAC 24kHz ONNX decoder.
#   Loaded behind tts.synth; Kokoro is the fallback when this is unavailable.
# index: imports | consts | def available | def _load | def synth |
#   def voice_for_role | def _patch_llama_ctx | def _resolve_q8
# AGENT_HEADER_END -->
"""Orpheus-3B voice backend — the expressive engine behind ``tts.synth``.

Why Orpheus over the Kokoro floor: Kokoro-82M is fast but flat (no prosody/
emotion). Orpheus-3B is an autoregressive LM over SNAC audio tokens, so it
produces markedly more human, expressive speech — at the cost of being slow
(~7x realtime on CPU) and heavier (~4.4 GB RAM). For OFFLINE/batch briefs
(podcast + audio-summary) that trade is worth it; realtime paths stay on
Kokoro.

Config locked by the 2026-07 CPU spike (see write_memory ae264f6a):
  - Q8_0 GGUF (``lex-au/Orpheus-3b-FT-Q8_0.gguf``) — Q4_K_M produced audible
    "compression" clicks on some phonemes; Q8_0 clears them. +0.1 GB RAM only
    (weights are mmap'd), a little slower per token.
  - ``n_ctx=4096`` — orpheus-cpp hardcodes ``Llama(n_ctx=0)``, which makes
    llama.cpp allocate a KV cache for the model's FULL 131k trained context
    (~14 GB wasted → 18.6 GB peak). Forcing n_ctx=4096 cuts peak to 4.4 GB.
  - Voices: host A = ``leo`` (temp 0.6), host B = ``jess`` (temp 0.5). ``tara``
    (breathy) and ``leah``/``zoe`` (drawly/slow) were rejected by ear.

Never raises for *expected* absence: ``available()`` gates import; ``synth``
raises only on a genuine backend fault so ``tts.synth`` can fall back to
Kokoro (HR-C3).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

SAMPLE_RATE = 24_000  # SNAC decoder output rate (matches Kokoro — mix stays sr-consistent)

# Q8_0 GGUF of the English Orpheus 0.1 finetune. Overridable for pinning/mirrors.
_Q8_REPO = os.environ.get("OKURO_ORPHEUS_GGUF_REPO", "lex-au/Orpheus-3b-FT-Q8_0.gguf")
_Q8_FILE = os.environ.get("OKURO_ORPHEUS_GGUF_FILE", "Orpheus-3b-FT-Q8_0.gguf")
_N_CTX = int(os.environ.get("OKURO_ORPHEUS_N_CTX", "4096"))

# Semantic role -> Orpheus voice id. Host A/B for the two-host podcast; narrator
# for the single-voice audio summary. Env-overridable so a brand can repin.
DEFAULT_VOICES = {
    "host_a": os.environ.get("OKURO_ORPHEUS_HOST_A", "leo"),
    "host_b": os.environ.get("OKURO_ORPHEUS_HOST_B", "jess"),
    "narrator": os.environ.get("OKURO_ORPHEUS_NARRATOR", "leo"),
}

# Per-voice sampling temperature (pace/expressiveness lever). Females run a touch
# lower to tighten pacing (spike finding: 0.5 keeps jess/mia pace-matched to leo).
_VOICE_TEMP = {"leo": 0.6, "dan": 0.6, "zac": 0.6, "jess": 0.5, "mia": 0.5}
_DEFAULT_TEMP = 0.6
_TOP_P = 0.9
_MAX_TOKENS = 1200

_MODEL = None       # lazy OrpheusCpp singleton (load is multi-second; reuse)
_LOAD_FAILED = False  # sticky: once load fails, stop retrying within the process


def available() -> bool:
    """True when the Orpheus code stack is importable (weights lazy-load later).

    Cheap import probe only — does NOT download the ~4 GB model. A prior load
    failure sticks (``_LOAD_FAILED``) so callers fall back to Kokoro fast.
    """
    if _LOAD_FAILED:
        return False
    try:
        import llama_cpp  # noqa: F401
        import orpheus_cpp  # noqa: F401
        return True
    except Exception:
        return False


def voice_for_role(role: str) -> str:
    """Map a semantic role (host_a|host_b|narrator) to an Orpheus voice id."""
    return DEFAULT_VOICES.get(role, DEFAULT_VOICES["narrator"])


def _patch_llama_ctx() -> None:
    """Force n_ctx/mmap on every llama.cpp model orpheus-cpp constructs.

    orpheus-cpp 0.0.3 calls ``Llama(n_ctx=0)`` (full 131k context → ~14 GB KV
    cache). We wrap the class so the KV cache is sized to n_ctx=4096 instead.
    Idempotent: re-wrapping a patched class is a no-op guard.
    """
    import llama_cpp

    if getattr(llama_cpp.Llama, "_okuro_ctx_patched", False):
        return
    _orig = llama_cpp.Llama

    class _PatchedLlama(_orig):  # type: ignore[valid-type, misc]
        _okuro_ctx_patched = True

        def __init__(self, *a, **kw):
            kw["n_ctx"] = _N_CTX
            kw["use_mmap"] = True
            kw["use_mlock"] = False
            super().__init__(*a, **kw)

    llama_cpp.Llama = _PatchedLlama


def _resolve_q8():
    """Point orpheus-cpp's LM at the local Q8_0 gguf; leave SNAC alone.

    orpheus-cpp's filename resolver lowercases repo files, which 404s the
    case-sensitive ``Orpheus-3b-FT-Q8_0.gguf``. We pre-download the exact file
    and monkeypatch ``orpheus_cpp.model.hf_hub_download`` so the LM loads the
    Q8 weights while the SNAC decoder download passes through untouched.
    """
    from huggingface_hub import hf_hub_download as _real_dl

    q8_path = _real_dl(_Q8_REPO, filename=_Q8_FILE)
    log.info("orpheus: Q8 LM at %s", q8_path)

    import orpheus_cpp.model as ocm

    def _patched(repo_id, filename=None, **kw):
        if "snac" in str(repo_id).lower():
            return _real_dl(repo_id, filename=filename, **kw)
        return q8_path

    ocm.hf_hub_download = _patched


def _load():
    """Lazy-construct the OrpheusCpp singleton (patched ctx + Q8 LM, EN)."""
    global _MODEL, _LOAD_FAILED
    if _MODEL is not None:
        return _MODEL
    if _LOAD_FAILED:
        raise RuntimeError("orpheus backend previously failed to load")
    try:
        _patch_llama_ctx()
        _resolve_q8()
        from orpheus_cpp import OrpheusCpp

        _MODEL = OrpheusCpp(verbose=False, lang="en")
        log.info("orpheus: model loaded (n_ctx=%d, Q8)", _N_CTX)
        return _MODEL
    except Exception as exc:
        _LOAD_FAILED = True
        log.warning("orpheus: load failed (%s) — falling back to Kokoro", exc)
        raise


def synth(text: str, voice: str = "leo", *, speed: float = 1.0):
    """Synthesize ``text`` in an Orpheus ``voice`` → (mono float32, 24000).

    ``speed`` is accepted for API parity with the Kokoro backend but Orpheus has
    no speed knob (pace is voice/temperature-intrinsic); it is ignored. Raises
    on a backend fault so ``tts.synth`` can degrade to Kokoro.
    """
    import numpy as np

    text = (text or "").strip()
    if not text:
        return np.zeros(0, dtype="float32"), SAMPLE_RATE

    model = _load()
    temp = _VOICE_TEMP.get(voice, _DEFAULT_TEMP)
    opts = {"voice_id": voice, "temperature": temp,
            "top_p": _TOP_P, "max_tokens": _MAX_TOKENS}

    chunks: list = []
    sr = SAMPLE_RATE
    for _sr, chunk in model.stream_tts_sync(text, options=opts):
        sr = _sr
        chunks.append(np.asarray(chunk, dtype="float32").reshape(-1))
    if not chunks:
        raise RuntimeError("orpheus produced no audio")

    audio = np.concatenate(chunks)
    # SNAC may emit int16-range floats; normalize to [-1, 1] like the spike did.
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 1.5:
        audio = audio / 32768.0
    return np.asarray(audio, dtype="float32"), int(sr)
