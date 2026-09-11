#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""Pre-generate pro-tier (MOSS) voice previews — one model load, all candidates.

RUN BY THE MOSS VENV (not okuro's .venv):

    CUDA_VISIBLE_DEVICES=0 <moss-venv>/bin/python gen_pro_previews.py

For every ``candidate_NN.wav`` in the durable sources dir it renders the fixed
audition line — "This could be your okuro voice." — as a single-speaker
voice-clone of that candidate, and writes ``pro__candidate_NN.mp3`` into the
okuro voice-previews cache (``~/.okuro/voice-previews`` or ``$OKURO_HOME``).

MOSS needs a GPU model load, so these are baked once here rather than on the
web request path (unlike the CPU Kokoro previews, which render on demand). Rerun
after adding/changing candidate voices. Reuses moss_worker's model + decode; no
okuro import (the MOSS venv can't import okuro's incompatible transformers).
"""
import hashlib
import importlib.util
import os
from pathlib import Path

import soundfile as sf

# --- load moss_worker by path (model load + generate_chunk + decode) -----------
WORKER = Path(__file__).with_name("moss_worker.py")
spec = importlib.util.spec_from_file_location("moss_worker", str(WORKER))
mw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mw)

# Kept verbatim in sync with tts_previews.SAMPLE_TEXT — previews are only
# comparable by ear if every engine speaks the same line. Can't import it: the
# MOSS venv can't import okuro (incompatible transformers pin).
SAMPLE_TEXT = "Welcome to okuro — your personal AI operating system."
# The transcript the candidate wavs were generated from (voice-clone needs the
# reference transcript to match the reference audio). Kept in sync with
# tts_settings.PRO_REF_TEXT / the sources manifest.
REF_TEXT = (
    "The deployment completed successfully. All three services restarted "
    "within expected limits, and memory usage remains stable."
)
SOURCES_DIR = Path(
    os.environ.get(
        "OKURO_MOSS_REF_SOURCES",
        str(Path.home() / ".okuro/media/moss/refs/sources"),
    )
)


def previews_dir() -> Path:
    home_env = os.environ.get("OKURO_HOME")
    base = Path(home_env) if home_env else Path.home() / ".okuro"
    return base / "voice-previews"


def text_tag() -> str:
    """Mirror of tts_previews._text_tag — the sample sentence's cache key.

    Preview filenames carry it so changing the sentence invalidates every clip
    instead of serving yesterday's line forever. Same algorithm, or okuro will
    look straight past what this script writes.
    """
    return hashlib.sha256(SAMPLE_TEXT.encode()).hexdigest()[:8]


def main() -> int:
    import torch  # noqa: F401  (imported for side effect / parity with worker)

    out_dir = previews_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    wavs = sorted(SOURCES_DIR.glob("candidate_*.wav"))
    if not wavs:
        print(f"[preview] no candidate wavs in {SOURCES_DIR}")
        return 1

    proc, model, attn = mw.load_model()
    sr = int(proc.model_config.sampling_rate)
    print(f"[preview] model loaded attn={attn} sr={sr}; {len(wavs)} candidates")

    made = 0
    for wav_path in wavs:
        name = wav_path.stem  # candidate_NN
        try:
            y = mw._load_wav_mono(wav_path, sr)
            wt = torch.from_numpy(y).float().unsqueeze(0)
            codes = proc.encode_audios_from_wav([wt], sampling_rate=sr)[0]
            refs = {"S1": {"codes": codes, "text": f"[S1] {REF_TEXT}"}}
            wav, truncated = mw.generate_chunk(
                proc, model, f"[S1] {SAMPLE_TEXT}", refs, ["S1"], max_new_tokens=512)
            if not wav.size:
                print(f"[preview] {name}: EMPTY, skipped")
                continue
            out = out_dir / f"pro__{name}__{text_tag()}.mp3"
            sf.write(str(out), wav, sr, format="MP3")
            made += 1
            print(f"[preview] {name} -> {out.name} "
                  f"({wav.size / sr:.1f}s{' TRUNC' if truncated else ''})")
        except Exception as e:  # noqa: BLE001
            print(f"[preview] {name}: FAILED {type(e).__name__}: {e}")

    print(f"[preview] done: {made}/{len(wavs)} pro previews -> {out_dir}")
    return 0 if made else 1


if __name__ == "__main__":
    raise SystemExit(main())
