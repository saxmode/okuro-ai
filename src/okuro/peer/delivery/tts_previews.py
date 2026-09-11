# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.tts_previews — per-voice audition clips for the voice
#   settings UI. Every selectable voice of every roster (Qwen pro speakers,
#   Kokoro named ids, legacy MOSS candidate refs) gets an mp3 of one fixed
#   sentence so a user can hear a voice before choosing it. Kokoro renders on
#   demand (CPU, ~1s/clip); Qwen renders the WHOLE roster on ONE model load
#   (the load is ~47s and dominates everything — never loop it per voice).
# index: SAMPLE_TEXT | _fx_tag | _cache_tag | previews_dir | preview_path
#   | list_previews | generate_kokoro_previews | ensure_kokoro_previews
#   | generate_qwen_previews | ensure_qwen_previews | ensure_previews_async
#   | preview_status
# AGENT_HEADER_END -->
"""Per-voice audition clips for the voice-settings surface.

One fixed line — :data:`SAMPLE_TEXT` — rendered once per selectable voice and
cached as mp3, so the settings UI can play okuro's own greeting in any voice
before the user commits to it.

Two engines, two cost profiles:

* **Kokoro** (50+ named voices) — CPU, ~1s/clip. :func:`generate_kokoro_previews`
  renders them on demand; :func:`ensure_kokoro_previews` is the lazy cache.
* **Qwen** (the pro roster) — a GPU model load that costs ~47s against ~4s of
  audio. So the roster is rendered as ONE batch job against ONE load
  (:func:`generate_qwen_previews`): ~1-2 min for all nine, where a per-voice
  loop would be 9 x 47s ≈ 7 min. That is too long to hold an HTTP request, so
  :func:`ensure_previews_async` runs it once in the background and
  :func:`preview_status` reports readiness for the UI to poll.

Two deliberate omissions:

* **No brand drone.** ``voice_drone.brand`` beds the brief in a 2.5s chord
  pre-roll. On a 4s sample that means waiting to hear the voice, and bedding
  every candidate in the same chord defeats the one thing previews exist for:
  telling voices apart. See voice_drone.brand's docstring.
* **No pace.** Previews render at 1.0x. Pace is a separate setting; baking it in
  would make two voices differ by something that isn't the voice.

A preview must sound like what the user will actually hear, so both rosters
render through the ``tts`` synth seam and therefore carry okuro's house post-fx
exactly as a brief would — including honouring ``voice_fx.enabled=false``. Qwen
previews additionally carry okuro's locked instruct. The sample sentence is
okuro's own greeting, so a clip is "the okuro voice in this speaker": only the
speaker varies, which is exactly the axis the settings UI is choosing.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# The one sentence every voice preview speaks. Keep it verbatim across engines
# so previews are directly comparable by ear.
SAMPLE_TEXT = "Welcome to okuro — your personal AI operating system."

_KOKORO_PREFIX = "kokoro"
_QWEN_PREFIX = "qwen"
_PRO_PREFIX = "pro"

# Engines that have a preview roster. "pro" (MOSS candidate refs) is pre-baked
# by gen_pro_previews.py and only served, never generated here.
ENGINES = (_QWEN_PREFIX, _KOKORO_PREFIX, _PRO_PREFIX)

_lock = threading.Lock()
_running: set[str] = set()


def _fx_tag() -> str:
    """``fx`` | ``dry`` — the house-sound state these clips were rendered in.

    Assumes ``fx`` on any settings fault, matching what a render would actually
    do (the seam applies the chain unless it is explicitly switched off).
    """
    try:
        from okuro.peer.delivery import tts_settings
        return "fx" if (tts_settings.current().voice_fx or {}).get("enabled") else "dry"
    except Exception:  # noqa: BLE001
        return "fx"


def _cache_tag() -> str:
    """Cache key for everything about a clip EXCEPT the voice: the sentence and
    the house-sound state.

    Baked into every preview filename so that changing either invalidates every
    clip automatically. The alternative (a bare ``{engine}__{voice}.mp3``)
    silently serves clips of the OLD sentence forever, which is worse than a
    missing preview: the user hears something and believes it is current.

    The fx switch is in that same class and was added here for that same reason —
    a user who turns the house sound off and still auditions processed clips is
    choosing a voice by a sound okuro will not make. Flipping it re-renders the
    roster (kokoro lazily per clip, qwen as one background batch), which is the
    price of a preview that is true.
    """
    return f"{hashlib.sha256(SAMPLE_TEXT.encode()).hexdigest()[:8]}-{_fx_tag()}"


def previews_dir() -> Path:
    """Durable cache dir for preview mp3s, honouring ``$OKURO_HOME``."""
    home_env = os.environ.get("OKURO_HOME")
    base = Path(home_env) if home_env else Path.home() / ".okuro"
    return base / "voice-previews"


def _safe(voice: str) -> str:
    return voice.replace("/", "_").replace(":", "-")


def preview_path(engine: str, voice: str) -> Path:
    """Path to a voice's preview mp3 (``{engine}__{voice}__{cache_tag}.mp3``)."""
    return previews_dir() / f"{engine}__{_safe(voice)}__{_cache_tag()}.mp3"


def _prune_stale(engine: str, voice: str) -> None:
    """Drop this voice's clips of a PREVIOUS sentence / house-sound state.

    Scoped to the one voice being rewritten — the module's own cache entry, not
    user data. Without it a sentence or fx change leaves an orphan per voice
    forever.
    """
    keep = preview_path(engine, voice)
    for old in previews_dir().glob(f"{engine}__{_safe(voice)}*.mp3"):
        if old != keep:
            old.unlink(missing_ok=True)


def list_previews() -> dict[str, dict[str, str]]:
    """Map ``{engine: {voice: mp3_path}}`` of CURRENT previews present on disk.

    Clips of an older sample sentence are ignored (not listed, not served) —
    they are stale by definition.
    """
    out: dict[str, dict[str, str]] = {e: {} for e in ENGINES}
    d = previews_dir()
    if not d.exists():
        return out
    tag = _cache_tag()
    for p in sorted(d.glob(f"*__{tag}.mp3")):
        engine, _, rest = p.stem.partition("__")
        voice, _, _ = rest.rpartition("__")
        if engine in out and voice:
            out[engine][voice] = str(p)
    return out


def _write_mp3(samples, sr: int, path: Path) -> None:
    import soundfile as sf
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), samples, sr, format="MP3")


def _missing(engine: str, roster: list[str], force: bool) -> list[str]:
    return [v for v in roster if force or not preview_path(engine, v).exists()]


def generate_kokoro_previews(*, force: bool = False) -> dict[str, str]:
    """Render :data:`SAMPLE_TEXT` for every Kokoro voice → mp3 cache.

    Returns ``{voice: mp3_path}``. Idempotent: skips voices already cached
    unless ``force``. Best-effort per voice — a single failed voice is logged
    and skipped, never aborting the batch.
    """
    from okuro.peer.delivery import tts

    made: dict[str, str] = {}
    for voice in tts.list_voices():
        out = preview_path(_KOKORO_PREFIX, voice)
        if out.exists() and not force:
            made[voice] = str(out)
            continue
        try:
            samples, sr = tts.synth(SAMPLE_TEXT, voice, engine="kokoro")
            if len(samples):
                _write_mp3(samples, sr, out)
                _prune_stale(_KOKORO_PREFIX, voice)
                made[voice] = str(out)
        except Exception as exc:  # noqa: BLE001
            log.warning("kokoro preview failed for %s: %s", voice, exc)
    return made


def ensure_kokoro_previews() -> dict[str, str]:
    """Lazy cache: generate any missing Kokoro previews, return the full map."""
    return generate_kokoro_previews(force=False)


def generate_qwen_previews(*, force: bool = False) -> dict[str, str]:
    """Render :data:`SAMPLE_TEXT` for every Qwen speaker — ONE model load.

    Returns ``{speaker: mp3_path}``. The whole roster goes into a single
    ``synth_many`` job because the ~47s load, not the generation, is the cost:
    nine separate calls would pay it nine times (~7 min) for ~40s of audio.

    Each item overrides only the speaker, so every clip is okuro's voice (the
    locked instruct + the house fx chain) with the speaker swapped — the one
    axis the settings UI is actually choosing.

    ``fallback=False`` is load-bearing: the seam's job is to rescue a DELIVERY by
    degrading a failed Qwen batch to Kokoro, which here would hand back nine
    clips of one Kokoro voice labelled as the Qwen roster. A raised fault leaves
    the previews missing, which is honest and retryable.
    """
    from okuro.peer.delivery import tts, tts_settings

    roster = list(tts_settings.QWEN_SPEAKERS)
    made = {v: str(preview_path(_QWEN_PREFIX, v))
            for v in roster if preview_path(_QWEN_PREFIX, v).exists() and not force}
    todo = _missing(_QWEN_PREFIX, roster, force)
    if not todo:
        return made

    items = [{"text": SAMPLE_TEXT, "role": "narrator", "speaker": v, "speed": 1.0}
             for v in todo]
    log.info("qwen previews: rendering %d speaker(s) on one model load", len(todo))
    # raises → caller decides (never silently degrades to another engine's voice)
    results = tts.synth_many(items, engine=_QWEN_PREFIX, fallback=False)
    for voice, (samples, sr) in zip(todo, results):
        if not len(samples):
            log.warning("qwen preview empty for %s", voice)
            continue
        out = preview_path(_QWEN_PREFIX, voice)
        _write_mp3(samples, sr, out)
        _prune_stale(_QWEN_PREFIX, voice)
        made[voice] = str(out)
    return made


def ensure_qwen_previews() -> dict[str, str]:
    """Lazy cache: generate any missing Qwen previews (one load), return the map."""
    return generate_qwen_previews(force=False)


def roster(engine: str) -> list[str]:
    """Selectable voice ids for an engine — the previewable set."""
    if engine == _QWEN_PREFIX:
        from okuro.peer.delivery import tts_settings
        return list(tts_settings.QWEN_SPEAKERS)
    if engine == _KOKORO_PREFIX:
        from okuro.peer.delivery import tts
        return tts.list_voices()
    return []


def preview_status(engine: str) -> dict:
    """``{ready, pending, missing, total}`` for an engine's preview cache.

    The UI polls this: a Qwen batch takes ~1-2 min, which is far too long to
    hold an HTTP request open, so readiness is reported rather than awaited.
    """
    ids = roster(engine)
    have = list_previews().get(engine, {})
    missing = [v for v in ids if v not in have]
    return {
        "ready": bool(ids) and not missing,
        "pending": engine in _running,
        "missing": missing,
        "total": len(ids),
    }


def ensure_previews_async(engine: str) -> bool:
    """Kick a background render of an engine's missing previews. Idempotent.

    Returns True if a run was started (False if one is already in flight or
    nothing is missing). Fire-and-forget: progress is observed via
    :func:`preview_status`, faults are logged and leave the clips missing —
    a preview is a convenience, never a reason to fail the settings page.
    """
    with _lock:
        if engine in _running or engine not in (_QWEN_PREFIX, _KOKORO_PREFIX):
            return False
        if not preview_status(engine)["missing"]:
            return False
        _running.add(engine)

    def _run() -> None:
        try:
            if engine == _QWEN_PREFIX:
                ensure_qwen_previews()
            else:
                ensure_kokoro_previews()
        except Exception as exc:  # noqa: BLE001
            log.warning("%s preview generation failed: %s", engine, exc)
        finally:
            with _lock:
                _running.discard(engine)

    threading.Thread(target=_run, name=f"tts-previews-{engine}", daemon=True).start()
    return True
