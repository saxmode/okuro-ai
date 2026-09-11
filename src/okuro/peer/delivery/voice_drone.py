# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.voice_drone — the okuro brand chord-drone bed. Wraps a
#   finished voice deliverable with a low, open quartal pad (default D–G–C–F): a
#   pre-roll intro before the voice, sustain underneath, and a slow fade-out tail
#   after. Founder-tuned; numpy+scipy only (no new dep). Call brand() at the
#   write site — see its docstring for who does and does not get the bed.
# index: def apply | def brand
# AGENT_HEADER_END -->
"""okuro voice drone — a low, open chord pad under okuro's voice.

The drone is a BRAND sound, not a brief feature: it marks audio okuro produced.
``brand(voice, sr)`` is the way in — one line at a deliverable's write site.
``apply(voice, sr, cfg)`` is the renderer beneath it, taking explicit config.

Either returns a NEW array = [pre-roll intro] + voice + [fade-out tail], with the
pad mixed under at ``level`` and carved dark so it never fights the words. Never
raises into the caller — any fault returns the voice untouched, so the delivery
always ships.

The bed is PROPORTIONAL: full size on a long clip, scaled down on a short one
(``full_at_s``), so branding never outweighs content. A very short clip gets no
bed at all rather than a blip of pad.
"""
from __future__ import annotations

import sys


def brand(voice, sr):
    """Lay the okuro brand drone under a finished deliverable. Never raises.

    Call this immediately BEFORE writing the file — the bed adds pre-roll + tail,
    so any duration computed off the array has to be taken after, or it lies.

    Owns the config lookup and the never-raise guard so no call site can get
    either wrong, and so the bed's policy lives in one place rather than being
    re-derived five times. Globally switchable via ``voice_drone.enabled`` in
    ~/.okuro/tts-config.yaml.

    Engine-agnostic by construction: call sites hand over a finished mix, so pro
    (Qwen) and air/advanced (Kokoro) get an identical bed for free — do NOT add
    per-engine drone handling.

    WHO GETS IT: anything okuro renders as speech — the morning brief, the
    summary and podcast channels, prism audio-briefs, studio narration.
    WHO DOES NOT: voice-audition previews (tts_previews, gen_pro_previews). A
    2.5s pad before a 6s sample means waiting to hear each voice, and bedding
    every candidate in the same chord is what makes two voices hard to tell
    apart — the previews exist precisely to tell them apart.
    """
    try:
        from okuro.peer.delivery import tts_settings as _ts

        return apply(voice, sr, getattr(_ts.current(), "voice_drone", None))
    except Exception:  # noqa: BLE001 — branding must never break a delivery
        return voice


def apply(voice, sr, cfg):
    if not cfg or not cfg.get("enabled"):
        return voice
    try:
        import numpy as np
        from scipy.signal import butter, lfilter
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"voice drone skipped ({exc})\n")
        return voice
    try:
        v = np.asarray(voice, dtype=np.float64).reshape(-1)
        notes = cfg.get("notes") or [73.42, 98.00, 130.81, 174.61]   # D–G–C–F
        level = float(cfg.get("level", 0.13))
        cut = float(cfg.get("cutoff_hz", 4800.0))
        pre = float(cfg.get("pre_roll_s", 2.5))
        tail = float(cfg.get("tail_s", 2.5))
        fin = float(cfg.get("fade_in_s", min(2.0, pre)))
        fout = float(cfg.get("fade_out_s", tail))
        det = float(cfg.get("detune_cents", 4.0))
        brate = float(cfg.get("breath_hz", 0.07))
        bdepth = float(cfg.get("breath", 0.15))

        # Scale the bed to the clip. The times above are founder-tuned for a
        # ~2min brief, where 5s of pad is 4% overhead; the same 5s around a 15s
        # audio-brief is the bed talking over okuro. Below `full_at_s` the bed
        # stays a CONSTANT FRACTION of the clip (linear k) — with the defaults,
        # 1/6 of it at any length — so it can never outweigh the content. Long
        # clips are unaffected (k=1) and the brief is untouched.
        #
        # Scaling shrinks rather than removes: a very short clip keeps a
        # proportionally tiny (inaudible) bed, and only a sub-sample one
        # disappears via the `N <= nv` guard. That is deliberate — a length
        # threshold would put a cliff in the middle of the range, and a 40ms pad
        # is harmless where a 2.5s one is not.
        full_at = float(cfg.get("full_at_s", 30.0))
        if full_at > 0:
            k = min(1.0, (len(v) / float(sr)) / full_at)
            pre *= k; tail *= k; fin *= k; fout *= k

        pre_s = int(pre * sr); nv = len(v); N = pre_s + nv + int(tail * sr)
        if N <= nv:
            return voice
        t = np.arange(N) / sr
        mixv = np.zeros(N); mixv[pre_s:pre_s + nv] = v

        pad = np.zeros(N)
        for f in notes:
            for d in (-det, 0.0, det):
                ff = float(f) * 2 ** (d / 1200.0); ph = 2 * np.pi * ff * t
                pad += np.sin(ph) + 0.30 * np.sin(2 * ph) + 0.16 * np.sin(3 * ph) + 0.08 * np.sin(4 * ph)
                pad += 0.18 * np.sin(2 * np.pi * (ff * 2) * t)
        pad *= (1 - bdepth) + bdepth * np.sin(2 * np.pi * brate * t)         # slow breathing
        b, a = butter(4, min(cut, sr / 2 - 1) / (sr / 2), "low");  pad = lfilter(b, a, pad)
        b, a = butter(2, 55 / (sr / 2), "high");                   pad = lfilter(b, a, pad)
        pad /= (np.max(np.abs(pad)) + 1e-9)

        env = np.ones(N)
        fi = max(1, int(fin * sr)); fo = max(1, int(fout * sr)); ve = pre_s + nv
        env[:fi] = np.linspace(0, 1, fi)
        end = min(fo, N - ve)
        if end > 0:
            env[ve:ve + end] = np.linspace(1, 0, end)
        env[ve + end:] = 0.0

        vpeak = np.max(np.abs(v)) + 1e-9
        out = mixv + pad * env * (level * vpeak)
        peak = float(np.max(np.abs(out))) if out.size else 0.0
        if peak > 1.0:
            out = out / peak * 0.98
        return out.astype(np.float32)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"voice drone error ({exc}) — no bed\n")
        return voice
