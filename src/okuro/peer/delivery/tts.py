# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.tts — voice synthesis capability (Kokoro fp32 CPU
#   floor). synth(text, voice, speed) -> (mono float32 samples, sample_rate).
#   Backend resolves to bundled Kokoro-82M ONNX on CPU; no GPU required.
#   THE seam: every voice okuro speaks leaves here, so the house post-fx chain
#   (voice_fx.house) is applied here — once, for every engine.
# index: imports | def available | def list_voices | def synth | def _synth_raw |
#   def synth_many | def _model_paths | def _ensure_models | def _engine
#   | def _download
# AGENT_HEADER_END -->
"""Voice synthesis capability for the delivery pipeline.

`synth(text, voice, role=…)` is the single seam every audio channel (tts,
podcast) calls. It dispatches to the active engine: **Qwen3-TTS CustomVoice**
(the pro voice — see ``tts_qwen``) on a pro edition when its venv is
installed, else **Kokoro-82M ONNX** (fp32, CPU) — the *floor* that needs no
GPU and no API key, so every okuro install can still produce audio. Engine
choice is ``active_engine()`` (env ``OKURO_TTS_ENGINE``); any pro-engine fault
degrades to Kokoro mid-call (HR-C3). Semantic roles (host_a/host_b/narrator)
keep callers backend-agnostic.

MOSS and Orpheus are RETIRED (2026-07-16) and unreachable by any route.

Because this is the seam EVERY voice leaves through, it is also where okuro's
house post-fx chain is applied (``voice_fx.house``) — exactly once, for whatever
engine rendered the samples. It used to live inside ``qwen_worker``, which made
the house sound an accident of which engine your hardware got: pro (Qwen) voices
were processed, air/advanced (Kokoro) voices were dry everywhere.

Benchmarked 2026-06-07 (Ryzen 7 7700, CPU-only): fp32 = 4.0x real-time (3.8x
even capped to 2 cores — NOT thread-bound). **int8 is deliberately avoided**:
onnxruntime dynamic int8 quant runs ~3.6x SLOWER than fp32 on CPU here.

Model weights (~338 MB) are NOT shipped in the wheel. They lazy-download to
``OKURO_KOKORO_DIR`` (default ``~/.okuro/models/kokoro``) on first synth, so a
fresh install gets podcasts "for free" with one one-time fetch and no manual
steps. The ``kokoro-onnx`` package itself IS a core dependency (the code), the
weights are the only deferred part.
"""

from __future__ import annotations

import logging
import os
import tempfile
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 24_000          # Kokoro fixed output rate
DEFAULT_VOICE = "af_heart"    # warm female; default narrator / host A
DEFAULT_HOST_B = "am_michael"  # measured male; default host B

# fp32 (NOT int8 — see module docstring). voices bin holds 26 style vectors.
_RELEASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0"
)
_MODEL_FILES = {
    "kokoro-v1.0.onnx": f"{_RELEASE}/kokoro-v1.0.onnx",
    "voices-v1.0.bin": f"{_RELEASE}/voices-v1.0.bin",
}

_ENGINE = None  # lazy Kokoro singleton (load is ~0.7s; reuse across lines)

# ----------------------------------------------------------------------
# Engine selection — Qwen3-TTS is the pro voice; Kokoro is the universal
# FALLBACK. `synth` dispatches by the active engine and degrades to Kokoro on
# any pro-engine fault (HR-C3). Semantic roles
# (host_a/host_b/narrator) map to each engine's own voice ids so callers
# stay backend-agnostic.
# ----------------------------------------------------------------------

# Kokoro voice ids per semantic role (the floor engine).
_KOKORO_ROLE_VOICES = {
    "host_a": DEFAULT_VOICE,     # af_heart — warm female
    "host_b": DEFAULT_HOST_B,    # am_michael — measured male
    "narrator": DEFAULT_VOICE,
}

# MOSS-TTSD speaker tags per role. MOSS is dialogue-native: turns are tagged
# [S1]/[S2] inline in one script, not selected as per-line voice ids. narrator
# (single-speaker) needs no tag.
_MOSS_ROLE_VOICES = {
    "host_a": "[S1]",
    "host_b": "[S2]",
    "narrator": "",
}

# Qwen role map used ONLY when tts_settings can't be read (import/IO fault). The
# shipped pair with okuro female: sohee is okuro, aiden co-hosts. Distinct by
# construction, so even the degraded path can't collapse a dialogue into one voice.
_QWEN_FALLBACK = {
    "host_a": "sohee",
    "host_b": "aiden",
    "narrator": "sohee",
}


_EDITION = None  # cached okuro edition for this process (hardware is stable/run)


def _edition() -> str:
    """Resolved okuro edition (air/advanced/pro), cached for the process.

    The single hardware/subscription axis (see ``ai_models.edition``). Cached
    because ``capabilities()`` probes the GPU — we only want that once, not on
    every synth. Degrades to 'air' if detection fails (safe: air = Kokoro floor).
    """
    global _EDITION
    if _EDITION is not None:
        return _EDITION
    try:
        from okuro.ai_models.edition import detect_edition
        from okuro.capability import capabilities
        _EDITION = detect_edition(capabilities())
    except Exception as exc:
        log.warning("edition detection failed (%s) — assuming 'air'", exc)
        _EDITION = "air"
    return _EDITION


def active_engine() -> str:
    """Resolve the TTS engine for this process: 'qwen' | 'kokoro'.

    ``OKURO_TTS_ENGINE`` pins it explicitly; the default 'auto' derives the
    engine from the okuro **edition** — the one hardware axis — so a 'pro' box
    runs the pro voice model for *standard* podcast/summary/brief generation
    instead of the air-tier floor:

        pro (>16 GB GPU) → Qwen3-TTS CustomVoice (calm neutral narrator) when its
                           isolated venv is installed, else Kokoro.
        advanced / air   → Kokoro-82M ONNX (the reliable CPU floor).

    The roster is Qwen + Kokoro. MOSS-TTSD (Chinese-drift bug) and Orpheus (CPU
    path too slow) are RETIRED — both were dropped from auto-selection but left
    PINNABLE, which is not the same as gone: a leftover ``OKURO_TTS_ENGINE=moss``
    in a shell or unit file would still route audio to the bug it was retired
    for, and silently, since a pin bypasses every availability probe. An
    unrecognised pin now degrades to Kokoro rather than being honoured.

    Kokoro is always the universal fallback; any pro-engine fault degrades
    mid-call (HR-C3).
    """
    pref = os.environ.get("OKURO_TTS_ENGINE", "auto").strip().lower()
    if pref in ("kokoro", "qwen"):
        return pref
    if pref in ("moss", "orpheus"):
        log.warning(
            "OKURO_TTS_ENGINE=%s names a retired engine — using the auto "
            "roster (qwen/kokoro) instead. Unset it.", pref,
        )
    if _edition() == "pro":
        try:
            from okuro.peer.delivery import tts_qwen
            if tts_qwen.available():
                return "qwen"
        except Exception:
            pass
    return "kokoro"


def _configured_role_voice(role: str, engine: str) -> str | None:
    """User-configured voice for a role, derived from the two-voice model.

    The store holds a standard female + a standard male voice per engine and
    ``okuro_gender``; ``tts_settings.role_speaker`` does the derivation — okuro's
    gender voice for narrator/host_a, the other for host_b. This function is only
    the best-effort wrapper: any import/read fault returns None so the caller
    falls back to the shipped role map, because a broken setting must never break
    the daily brief.
    """
    try:
        from okuro.peer.delivery import tts_settings
        return tts_settings.role_speaker(role, engine) or None
    except Exception:
        return None


def role_voice(role: str, engine: str | None = None) -> str:
    """Voice id for a semantic role on the given (or active) engine.

    Retired engines resolve to the Kokoro floor's voice, not to their own.
    `role_voice(role, "orpheus")` used to return "leo" — no audio ever came of
    it (available() is False), but it handed callers a voice id for a backend
    that cannot render, which is a lie waiting for a caller to believe it.
    """
    engine = engine or active_engine()
    if engine == "qwen":
        return _configured_role_voice(role, "qwen") or _QWEN_FALLBACK.get(role, "sohee")
    # Kokoro: user setting (tts_settings) overrides the shipped role map.
    return _configured_role_voice(role, "kokoro") or _KOKORO_ROLE_VOICES.get(role, DEFAULT_VOICE)


def host_voices(engine: str | None = None) -> tuple[str, str]:
    """(host_a_voice, host_b_voice) for the given/active engine."""
    engine = engine or active_engine()
    return role_voice("host_a", engine), role_voice("host_b", engine)


def synth_many(items: list[dict], *, engine: str | None = None,
               fallback: bool = True):
    """Render many lines → ``[(samples, sr), ...]``, one per item, order kept.

    Each item is ``{"text", "role", "voice"?, "speaker"?, "speed"?}``. The seam
    exists so a dialogue channel can hand over its whole line list and let the
    ENGINE decide how to batch, instead of every caller re-learning each engine's
    cost model.

    Qwen amortises one model load (~47s) across the batch — line-by-line it was
    ~31 min for a 40-line show. Engines with no batch advantage just loop, so the
    caller's code is identical either way. Any qwen fault degrades the WHOLE
    batch to per-line Kokoro rather than half-rendering (HR-C3).

    ``fallback=False`` disables that degradation and re-raises instead. For a
    delivery, a Kokoro rescue is strictly better than no audio; for a caller
    RENDERING A SPECIFIC ENGINE'S ROSTER (voice previews), it is worse than
    nothing — every clip would silently come back in the wrong engine's default
    voice, and the user would audition a roster that does not exist.

    Every returned clip carries the house fx exactly once AND leaves levelled:
    the batch path applies both here, the loop path gets them from :func:`synth`.
    Both paths must stay in step — the batch path is the one a podcast uses, so a
    miss here is exactly where an unlevelled dialogue would come from.
    """
    from okuro.peer.delivery import voice_fx

    engine = engine or active_engine()
    if engine == "qwen":
        try:
            from okuro.peer.delivery import tts_qwen
            return [(_level(voice_fx.house(s, sr)), sr)
                    for s, sr in tts_qwen.synth_many(items)]
        except Exception as exc:
            if not fallback:
                raise
            log.warning("qwen batch synth failed (%s) — falling back to Kokoro", exc)
            engine = "kokoro"
    return [
        synth(it.get("text") or "", it.get("voice"), speed=it.get("speed") or 1.0,
              role=it.get("role"), engine=engine)
        for it in items
    ]


def role_voices_distinct(engine: str | None = None) -> bool:
    """True when this engine gives the two hosts DIFFERENT voices.

    A dialogue format is only a dialogue if the hosts sound like two people. Not
    every engine can do that — Qwen CustomVoice rendered one configured speaker
    for every role until the settings model gave it a second voice — and an
    engine that can't fails SILENTLY: you get one voice talking to itself, which
    reads as a broken render rather than a missing capability.

    Derived, not declared: the capability simply IS whether the two roles resolve
    to different voice ids. So an engine that gains per-role voices satisfies
    this the moment it does, with no registry to update in lockstep.
    """
    a, b = host_voices(engine)
    return bool(a) and bool(b) and a != b


def narrator_voice(engine: str | None = None) -> str:
    """Single-narrator voice for the given/active engine (audio summary)."""
    return role_voice("narrator", engine or active_engine())


def _model_dir() -> Path:
    """Cache dir for Kokoro weights. Honours OKURO_KOKORO_DIR."""
    override = os.environ.get("OKURO_KOKORO_DIR")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("HOME", str(Path.home())))
    return home / ".okuro" / "models" / "kokoro"


def _model_paths() -> tuple[Path, Path]:
    d = _model_dir()
    return d / "kokoro-v1.0.onnx", d / "voices-v1.0.bin"


def available() -> bool:
    """True when the kokoro-onnx package is importable (code present)."""
    try:
        import kokoro_onnx  # noqa: F401
        return True
    except Exception:
        return False


def _download(url: str, dest: Path) -> None:
    """Stream ``url`` to ``dest`` atomically (temp file + rename)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("kokoro: downloading %s -> %s", url, dest)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        with urllib.request.urlopen(url) as resp, open(tmp_path, "wb") as fh:
            while True:
                chunk = resp.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                fh.write(chunk)
        tmp_path.replace(dest)
        log.info("kokoro: fetched %s (%d bytes)", dest.name, dest.stat().st_size)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _ensure_models() -> tuple[Path, Path]:
    """Return (onnx, voices) paths, downloading any that are missing.

    One-time ~338 MB fetch on first ever synth. Subsequent calls are cache
    hits. Raises RuntimeError if a download fails (caller degrades to a
    non-audio fallback per HR-C3).
    """
    onnx_path, voices_path = _model_paths()
    for path, name in ((onnx_path, "kokoro-v1.0.onnx"), (voices_path, "voices-v1.0.bin")):
        if path.exists() and path.stat().st_size > 0:
            continue
        try:
            _download(_MODEL_FILES[name], path)
        except Exception as exc:  # network, disk, permissions
            raise RuntimeError(f"kokoro model fetch failed for {name}: {exc}") from exc
    return onnx_path, voices_path


def _engine():
    """Lazy-load and cache the Kokoro engine."""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    if not available():
        raise RuntimeError(
            "kokoro-onnx is not installed. It is a core okuro dependency — "
            "run `pip install -e .` (or okuro update) to install it."
        )
    onnx_path, voices_path = _ensure_models()
    from kokoro_onnx import Kokoro
    _ENGINE = Kokoro(str(onnx_path), str(voices_path))
    return _ENGINE


def list_voices() -> list[str]:
    """Available Kokoro voice ids (26). Empty list if engine unavailable."""
    try:
        eng = _engine()
    except Exception:
        return []
    names = getattr(eng, "voices", None)
    if isinstance(names, dict):
        return sorted(names.keys())
    try:
        return sorted(eng.get_voices())  # type: ignore[attr-defined]
    except Exception:
        return []


def _resolve_voice(eng, voice):
    """Resolve a voice name or a BLEND spec to what Kokoro.create wants.

    A blend spec mixes style vectors: ``"af_heart:70,af_bella:30"`` →
    weighted, normalized average of the two voices' style vectors (a 510x1x256
    ndarray). This is Kokoro's one timbre lever beyond raw voice selection —
    used to give each host a distinct, warmer voice. Plain names pass through.
    """
    if not isinstance(voice, str) or (":" not in voice and "," not in voice):
        return voice
    import numpy as np
    acc = None
    total = 0.0
    for part in voice.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, _, w = part.rpartition(":")
            try:
                weight = float(w)
            except ValueError:
                name, weight = part, 50.0
        else:
            name, weight = part, 50.0
        try:
            vec = np.asarray(eng.voices[name.strip()], dtype="float32")
        except Exception:
            continue
        acc = vec * weight if acc is None else acc + vec * weight
        total += weight
    if acc is None or total <= 0:
        return DEFAULT_VOICE
    return acc / total


# Loudness every voice leaves the synth seam at. The absolute value barely
# matters — every channel peak-normalizes its finished mix — but the CONSISTENCY
# does: it is what stops one speaker sitting louder than another.
_TARGET_RMS = 0.05
_PEAK_CEILING = 0.95


def _level(samples):
    """Bring one freshly synthesized clip to okuro's speaking loudness.

    Speakers are not born equal: measured post-fx on the qwen roster, aiden came
    out +6.35 dB louder than sohee — so which voice you picked decided how loud
    that host was, and a two-host show had one host shouting. That is a defect,
    not a taste, so this sits BESIDE voice_fx.house() rather than inside it: it
    must survive the house-sound switch being turned off.

    Per clip, one gain — internal dynamics are untouched, and a channel's own
    intentional moves (the podcast choreography's ducking/holds, applied later)
    still land. Order matters: level here, choreograph after, peak-normalize the
    finished mix last.

    Never raises — a fault ships the clip unlevelled.
    """
    try:
        import numpy as np

        x = np.asarray(samples, dtype="float32").reshape(-1)
        if not x.size:
            return x
        rms = float(np.sqrt(np.mean(x.astype("float64") ** 2)))
        if rms < 1e-5:                       # silence — nothing to level onto
            return x
        gain = _TARGET_RMS / rms
        peak = float(np.max(np.abs(x))) * gain
        if peak > _PEAK_CEILING:             # never buy loudness with clipping
            gain *= _PEAK_CEILING / peak
        return (x * gain).astype("float32")
    except Exception as exc:  # noqa: BLE001
        log.warning("voice levelling skipped (%s)", exc)
        return samples


def synth(text: str, voice: str | None = None, *, speed: float = 1.0,
          lang: str = "en-us", engine: str | None = None, role: str | None = None):
    """Synthesize ``text`` → (mono float32 ndarray, sample_rate @ 24 kHz).

    Backend-agnostic seam. ``engine`` defaults to :func:`active_engine`
    (Qwen on a pro edition when available, else Kokoro). ``role``
    (host_a|host_b|narrator) resolves an engine-appropriate voice when
    ``voice`` is omitted, and picks the Kokoro voice used if a pro synth
    falls back.

    ``voice`` — an engine-native voice id (Orpheus: ``leo``/``jess``…; Kokoro:
    a voice id or blend spec ``"a:70,b:30"``). If given, it is used on the
    active engine as-is.

    The returned samples wear okuro's house post-fx (``voice_fx.house``) and
    leave at a consistent loudness (:func:`_level`) — every engine, one
    application point, globally switchable via ``voice_fx.enabled``.

    Never leaves the caller without audio when Kokoro is installed: any Orpheus
    fault (missing weights, load/synth error) logs and degrades to Kokoro
    (HR-C3). Raises only if the final Kokoro backend is also unavailable.
    """
    from okuro.peer.delivery import voice_fx

    samples, sr = _synth_raw(text, voice, speed=speed, lang=lang, engine=engine,
                             role=role)
    return _level(voice_fx.house(samples, sr)), sr


def _synth_raw(text: str, voice: str | None = None, *, speed: float = 1.0,
               lang: str = "en-us", engine: str | None = None,
               role: str | None = None):
    """:func:`synth` without the house fx — engine dispatch only.

    Split out so the fx chain has exactly ONE application point despite the
    engine ladder's several return paths (qwen / moss / orpheus each degrade to
    Kokoro mid-call). Internal: callers want :func:`synth`.
    """
    import numpy as np

    text = (text or "").strip()
    if not text:
        return np.zeros(0, dtype="float32"), SAMPLE_RATE

    engine = engine or active_engine()
    if voice is None:
        voice = role_voice(role or "narrator", engine)

    if engine == "qwen":
        # Qwen renders the whole passed text in one model load (worker one-shots,
        # per-sentence fallback). Any fault degrades to Kokoro (HR-C3).
        try:
            from okuro.peer.delivery import tts_qwen
            return tts_qwen.synth(text, role=role or "narrator")
        except Exception as exc:
            log.warning("qwen synth failed (%s) — falling back to Kokoro", exc)
            engine = "kokoro"
            voice = role_voice(role or "narrator", "kokoro")

    if engine in ("moss", "orpheus"):
        # Retired engines. active_engine() can no longer return either, but
        # `engine=` is a caller-supplied string, so a stale call site could
        # still name one — route it to the floor rather than to a retired
        # backend. Remap the voice too: an id like "leo" (Orpheus) or a
        # [S1]/[S2] tag (MOSS) is meaningless to Kokoro.
        log.warning(
            "synth(engine=%r) names a retired engine — using Kokoro", engine,
        )
        engine = "kokoro"
        voice = role_voice(role or "narrator", "kokoro")

    eng = _engine()
    samples, sr = eng.create(text, voice=_resolve_voice(eng, voice), speed=speed, lang=lang)
    return np.asarray(samples, dtype="float32"), int(sr)


def synth_dialogue(script: str, *, engine: str | None = None, seed: int | None = None):
    """Always raises — no engine in the roster renders a dialogue in one call.

    MOSS-TTSD did (natively turn-taking, one model load for a whole script),
    which is why this seam exists; callers catch ``NotImplementedError`` and
    fall back to their per-line :func:`synth` loop, so raising is the correct
    and already-exercised path.

    Kept as an explicit raise rather than deleted: the fallback contract lives
    in the callers, and a future engine with native dialogue support belongs
    here. Retired 2026-07-16 with MOSS.

    Retiring an engine means removing every ROUTE to it, not just the default
    one. active_engine() stopped returning "moss", so the `engine or
    active_engine()` default could no longer reach the backend — but `engine=`
    is caller-supplied, and `synth_dialogue(script, engine="moss")` walked
    straight past the retirement into tts_moss. That escape hatch survived the
    retirement commit because the default path was checked and the explicit
    one was not; an acceptance-test agent found it by stubbing the backend and
    watching it get reached.
    """
    engine = engine or active_engine()
    raise NotImplementedError(
        f"synth_dialogue has no engine (engine={engine!r}); MOSS was retired "
        f"2026-07-16 — use the per-line synth() loop"
    )
