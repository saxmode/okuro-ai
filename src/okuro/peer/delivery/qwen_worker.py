# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: qwen_worker.py — the out-of-process worker for the Qwen3-TTS pro voice
#   engine. Runs in the isolated qwen-tts venv (OKURO_QWEN_PYTHON), driven by
#   tts_qwen.py over a tiny JSON job. Loads Qwen3-TTS-12Hz-1.7B-CustomVoice on
#   GPU0 (bf16) ONCE and renders every segment of the job against that one load,
#   each with its own speaker/instruct; per segment it tries the whole text in
#   one generate_custom_voice call and retries sentence-by-sentence on fault.
#   Returns the RAW voice — okuro's house post-fx is applied engine-agnostically
#   at the tts seam (voice_fx.house), never here.
#   Prints one JSON status line last. Never imported by okuro (torch pin differs).
# index: split_sentences | render | main
# AGENT_HEADER_END -->
"""Qwen3-TTS CustomVoice worker (isolated venv, JSON job in / wavs out).

Job (``--in job.json``) — always a list, even for one clip::

    {"segments": [
        {"text": str, "out": "/path/out.wav", "speaker": "sohee",
         "language": "english", "instruct": str|null, "speed": 1.0}
    ]}

The model load dominates the cost — ~47s against ~5s of audio — so the segment
list exists to amortise it. A dialogue asking for one clip per line via one job
each paid that load PER LINE: ~31 min for a 40-line podcast. Same load, N clips.

Contract: writes a mono wav per segment and prints exactly one JSON status line
last: ``{"ok": true, "segments": [{audio_s, sr, chunks}, ...], "peak_vram_gb"}``.
Any fault -> non-zero exit (tts_qwen degrades the channel to Kokoro).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# GPU0 (Ada) is the only supported CUDA device; pin defensively before torch.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# Model: resolved to a LOCAL snapshot directory so offline loading never triggers
# a hub revision check (which OfflineMode blocks). OKURO_QWEN_MODEL may be an
# explicit dir; otherwise we locate the cached snapshot under HF_HOME/hub.
_REPO = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"


def _resolve_model() -> str:
    """Return a local model dir (preferred) or the repo id as a last resort."""
    import glob
    env = os.environ.get("OKURO_QWEN_MODEL")
    if env and os.path.isdir(env):
        return env
    hub = os.path.join(os.environ.get("HF_HOME", ""), "hub")
    cache_name = "models--" + _REPO.replace("/", "--")
    snaps = sorted(glob.glob(os.path.join(hub, cache_name, "snapshots", "*")))
    for snap in snaps:
        if os.path.exists(os.path.join(snap, "config.json")):
            return snap
    return env or _REPO  # fall back to repo id (may require network)


def split_sentences(text: str) -> list[str]:
    """Coarse sentence split for the per-segment fallback (keeps terminators)."""
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def render(model, np, seg):
    """One segment against an already-loaded model -> (samples, sr, chunks).

    Each segment carries its OWN speaker/instruct: a dialogue is two people, so
    the voice cannot be hoisted to the job.

    Returns the RAW voice. The house post-fx chain used to be applied here, which
    is precisely how it became qwen-only — no other engine has a worker to hide
    it in, so air/advanced installs got no house sound at all. It now lives at
    the engine-agnostic tts seam (``voice_fx.house``). Do not re-add it here:
    the chain is not idempotent, and applying it twice is audible but cannot
    raise, so it would fail silently.
    """
    text = (seg.get("text") or "").strip()
    speaker = seg.get("speaker", "ono_anna")
    language = seg.get("language", "english")
    instruct = seg.get("instruct") or None
    speed = float(seg.get("speed", 1.0) or 1.0)

    def gen(t: str):
        wavs, sr = model.generate_custom_voice(
            t, speaker=speaker, language=language, instruct=instruct
        )
        return np.asarray(wavs[0], dtype=np.float32), int(sr)

    # Prefer ONE whole-text generation (stable single voice); on any fault
    # retry sentence-by-sentence and concatenate.
    chunks = 1
    try:
        samples, sr = gen(text)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"whole-text generate failed ({exc}); per-sentence retry\n")
        sents = split_sentences(text) or [text]
        pieces, sr = [], 24000
        gap = None
        for s in sents:
            w, sr = gen(s)
            if gap is None:
                gap = np.zeros(int(0.25 * sr), dtype=np.float32)
            pieces.append(w)
            pieces.append(gap)
        if not pieces:
            raise RuntimeError("no audio")
        samples = np.concatenate(pieces)
        chunks = len(sents)

    # Optional time-stretch (pace), pitch-preserving. 1.0 = untouched.
    if abs(speed - 1.0) > 1e-3:
        import librosa
        rate = max(0.5, min(2.0, speed))
        samples = librosa.effects.time_stretch(samples.astype(np.float32), rate=rate)

    return samples, sr, chunks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="job", required=True)
    args = ap.parse_args()
    job = json.loads(open(args.job).read())

    segments = job.get("segments") or []
    if not segments or not any((s.get("text") or "").strip() for s in segments):
        print(json.dumps({"ok": False, "error": "empty text"}))
        return 2

    import numpy as np
    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    # THE expensive step — once, for every segment in the job.
    model = Qwen3TTSModel.from_pretrained(
        _resolve_model(), device_map="cuda:0", dtype=torch.bfloat16
    )

    out = []
    for i, seg in enumerate(segments):
        try:
            samples, sr, chunks = render(model, np, seg)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": f"segment {i}: {exc}"}))
            return 3
        sf.write(seg["out"], samples, sr)
        out.append({
            "audio_s": round(len(samples) / sr, 2), "sr": sr, "chunks": chunks,
            "speaker": seg.get("speaker"),
        })

    peak = 0.0
    if torch.cuda.is_available():
        peak = round(torch.cuda.max_memory_allocated() / (1024 ** 3), 2)

    print(json.dumps({"ok": True, "segments": out, "peak_vram_gb": peak}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
