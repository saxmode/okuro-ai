# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.voice_fx — the okuro voice post-fx chain, a 1:1 Python
#   port of the browser fx-lab (Web Audio) graph the founder tuned on, so the
#   config numbers (Hz / dB / Q / seconds / ms / mix) mean EXACTLY what they did
#   there. RBJ biquads (matching Web Audio BiquadFilter), an exponential-noise IR
#   reverb (matching the ConvolverNode makeIR), a feedback-comb delay, an
#   LFO-modulated fractional-delay chorus, and a soft-knee compressor. numpy+scipy
#   only (both already in okuro's main venv). Call house() at the synth seam —
#   see its docstring for who does and does not get the chain.
# index: def _rbj | def _chorus | def _delay | def _reverb | def _compress
#   | def apply | def house
# AGENT_HEADER_END -->
"""okuro voice post-fx — exact port of the fx-lab (Web Audio) chain.

The fx chain is the HOUSE sound: it is what okuro's voice IS, not what one
engine's voice is. ``house(samples, sr)`` is the way in — one line at the synth
seam. ``apply(samples, sr, fx)`` is the renderer beneath it, taking explicit
config.

Signal path mirrors fx-lab: series [highpass -> EQ(low-shelf,peak,high-shelf) ->
compressor -> lowpass] into a pre-mix, then PARALLEL wet sends (chorus, delay,
reverb) summed on top of the full-dry pre-mix. Every stage reproduces the Web
Audio node math so a value tuned in the browser sounds the same here.

Neither entry point ever raises into the caller — on any fault the input ships
dry, so the voice always ships.
"""
from __future__ import annotations

import sys


def house(samples, sr):
    """Lay okuro's house post-fx on a freshly synthesized voice. Never raises.

    Call this at the SYNTH seam (``tts.synth`` / ``tts.synth_many``), once per
    voice clip. Owns the config lookup and the never-raise guard so no call site
    can get either wrong, and so the chain's policy lives in one place rather
    than being re-derived per engine — which is exactly how it ended up applied
    inside ``qwen_worker`` and therefore missing from every other engine.

    Engine-agnostic by construction: it takes finished samples, so Qwen, Kokoro
    and Orpheus all wear the same house sound for free. Do NOT add per-engine fx
    handling, and do NOT apply it a second time downstream — the chain is not
    idempotent and a double pass is audible but silent (it cannot raise).

    WHY HERE AND NOT AT THE WRITE SITE: fx is a property of A VOICE; the drone
    (``voice_drone.brand``) is a property of A DELIVERABLE. The flow is
    ``synth -> fx -> [podcast choreography] -> normalize -> drone -> write``.
    Applied at the write site the chain would process the mixed show including
    the brand drone, and would smear the per-line clips the choreography needs.

    Globally switchable via ``voice_fx.enabled`` in ~/.okuro/tts-config.yaml —
    off returns the input array untouched (identity, not a dry re-render).
    """
    if samples is None or len(samples) == 0:
        return samples
    try:
        from okuro.peer.delivery import tts_settings as _ts

        return apply(samples, sr, getattr(_ts.current(), "voice_fx", None))
    except Exception:  # noqa: BLE001 — the house sound must never break a delivery
        return samples


def _rbj(sig, sr, kind, f0, q=0.707, gain_db=0.0):
    """One RBJ biquad, matching Web Audio's BiquadFilter for the given type."""
    import numpy as np
    from scipy.signal import lfilter
    w0 = 2.0 * np.pi * float(f0) / sr
    cw, sw = np.cos(w0), np.sin(w0)
    A = 10.0 ** (gain_db / 40.0)
    if kind in ("lowpass", "highpass", "peaking"):
        alpha = sw / (2.0 * q)
    if kind == "highpass":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]; a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "lowpass":
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]; a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "peaking":
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]; a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    else:  # low/high shelf — Web Audio uses a fixed unity slope (S=1 -> alpha = sin/2 * sqrt2)
        alpha = sw / 2.0 * np.sqrt(2.0)
        ta = 2.0 * np.sqrt(A) * alpha
        if kind == "lowshelf":
            b = [A * ((A + 1) - (A - 1) * cw + ta), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - ta)]
            a = [(A + 1) + (A - 1) * cw + ta, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - ta]
        else:  # highshelf
            b = [A * ((A + 1) + (A - 1) * cw + ta), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - ta)]
            a = [(A + 1) - (A - 1) * cw + ta, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - ta]
    a0 = a[0]
    return lfilter([b[0] / a0, b[1] / a0, b[2] / a0], [1.0, a[1] / a0, a[2] / a0], sig)


def _chorus(sig, sr, rate_hz, depth_ms, base_ms=20.0):
    """LFO-modulated fractional delay (fx-lab chorus, base 20 ms ± depth_ms)."""
    import numpy as np
    n = len(sig)
    t = np.arange(n) / sr
    d = (base_ms / 1000.0 + (depth_ms / 1000.0) * np.sin(2 * np.pi * rate_hz * t)) * sr
    idx = np.arange(n) - d
    i0 = np.floor(idx).astype(np.int64)
    frac = idx - i0
    i0c = np.clip(i0, 0, n - 1); i1c = np.clip(i0 + 1, 0, n - 1)
    y = (1 - frac) * sig[i0c] + frac * sig[i1c]
    y[idx < 0] = 0.0
    return y


def _delay(sig, sr, seconds, feedback):
    """Feedback comb (fx-lab DelayNode + feedback gain): y - fb*y[n-D] = x[n-D]."""
    import numpy as np
    from scipy.signal import lfilter
    D = max(1, int(round(seconds * sr)))
    b = np.zeros(D + 1); b[D] = 1.0
    a = np.zeros(D + 1); a[0] = 1.0; a[D] = -float(feedback)
    return lfilter(b, a, sig)


def _reverb(sig, sr, decay_s):
    """Convolution with an exp-decay noise IR (matches fx-lab makeIR: (1-i/L)^2.5),
    energy-normalized so mix level is stable and deterministic (fixed seed)."""
    import numpy as np
    from scipy.signal import fftconvolve
    L = max(1, int(decay_s * sr))
    i = np.arange(L)
    ir = (np.random.RandomState(1234).rand(L) * 2 - 1) * np.power(1 - i / L, 2.5)
    ir /= (np.sqrt(np.sum(ir ** 2)) + 1e-9)
    return fftconvolve(sig, ir)[:len(sig)]


def _compress(sig, sr, threshold_db, ratio, knee=30.0, attack=0.003, release=0.25):
    """Soft-knee feed-forward compressor approximating Web Audio DynamicsCompressor."""
    import numpy as np
    eps = 1e-9
    lvl = 20 * np.log10(np.abs(sig) + eps)
    over = lvl - threshold_db
    gr = np.zeros_like(lvl)  # gain reduction (dB, <= 0)
    above = over > knee / 2
    mid = (over > -knee / 2) & (~above)
    gr[above] = (threshold_db + (lvl[above] - threshold_db) / ratio) - lvl[above]
    gr[mid] = (1.0 / ratio - 1.0) * (over[mid] + knee / 2) ** 2 / (2 * knee)
    aC = np.exp(-1.0 / (attack * sr)); rC = np.exp(-1.0 / (release * sr))
    g = np.empty_like(gr); prev = 0.0
    for n in range(len(gr)):
        target = gr[n]
        coef = aC if target < prev else rC   # faster onset (attack), slower recovery (release)
        prev = coef * prev + (1 - coef) * target
        g[n] = prev
    return sig * (10.0 ** (g / 20.0))


def apply(samples, sr, fx):
    """Apply the fx chain to a mono float array. Returns input dry on any fault."""
    if not fx or not fx.get("enabled"):
        return samples
    try:
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"voice fx skipped ({exc})\n")
        return samples
    try:
        x = np.asarray(samples, dtype=np.float64).reshape(-1)
        # --- series ---
        if fx.get("highpass_hz"):
            x = _rbj(x, sr, "highpass", fx["highpass_hz"], q=1.0)
        eq = fx.get("eq") or {}
        if eq:
            x = _rbj(x, sr, "lowshelf", eq["low_hz"], gain_db=eq["low_db"])
            x = _rbj(x, sr, "peaking", eq["mid_hz"], q=eq["mid_q"], gain_db=eq["mid_db"])
            x = _rbj(x, sr, "highshelf", eq["high_hz"], gain_db=eq["high_db"])
        cp = fx.get("compressor")
        if cp:
            x = _compress(x, sr, cp["threshold_db"], cp["ratio"])
        if fx.get("lowpass_hz"):
            x = _rbj(x, sr, "lowpass", fx["lowpass_hz"], q=1.0)
        pre = x
        # --- parallel wet sends summed on top of full dry ---
        out = pre.copy()
        ch = fx.get("chorus")
        if ch:
            out = out + float(ch["mix"]) * _chorus(pre, sr, ch["rate_hz"], ch["depth_ms"])
        dl = fx.get("delay")
        if dl:
            out = out + float(dl["mix"]) * _delay(pre, sr, dl["seconds"], dl["feedback"])
        rv = fx.get("reverb")
        if rv:
            out = out + float(rv["mix"]) * _reverb(pre, sr, rv["decay_s"])
        if fx.get("gain_db"):
            out = out * (10.0 ** (float(fx["gain_db"]) / 20.0))
        peak = float(np.max(np.abs(out))) if out.size else 0.0
        if peak > 1.0:
            out = out / peak * 0.98
        return out.astype(np.float32)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"voice fx error ({exc}) — shipping dry\n")
        return samples
