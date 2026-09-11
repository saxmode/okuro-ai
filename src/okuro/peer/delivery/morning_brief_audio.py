# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.morning_brief_audio — renders a morning-brief SCRIPT
#   (morning_brief.compose_script() output) to audio through the edition-tiered
#   TTS pipeline (tts.active_engine() -> Qwen3-TTS on pro, Kokoro on air/advanced
#   and as fallback; MOSS retired to pin-only). The brief is a single-narrator
#   monologue; the pro engine renders it in ONE model load so its voice stays
#   stable. Reuses channels.summary._segments, channels.podcast
#   _normalize/_storage_root, tts.synth/synth_dialogue.
# index: imports | def render_brief_audio
# AGENT_HEADER_END -->
"""Morning-brief audio renderer.

``compose_script()`` (see ``morning_brief.py``) already returns the FINAL
spoken script under a hard structural contract (verbatim opener, fixed
section order, exactly 3 action items). That text must reach the listener
UNCHANGED — so, unlike ``channels/summary.py``, this renderer skips the
LLM-narration rewrite step and feeds the composed script straight into the
same per-segment synth -> normalize -> MP3 pipeline every other audio
channel uses.

Voice is whatever the pipeline's narrator role resolves to — Qwen's configured
speaker (default ``ono_anna``, calm neutral) on pro, or Kokoro's configured
``kokoro_narrator`` (default ``af_heart``) on the fallback engine. The pro engine
renders the whole brief in ONE model load so the single voice stays stable end to
end. (MOSS is retired to the ``OKURO_TTS_ENGINE=moss`` pin.)

Never raises into the caller — failures land on the returned dict's "error".
"""

from __future__ import annotations

import logging
import time
import uuid

log = logging.getLogger(__name__)


def render_brief_audio(script: str) -> dict:
    """Synthesize ``script`` (a ``compose_script()`` output) to MP3.

    Returns ``{"path", "audio_seconds", "engine", "voice", "segments",
    "render_ms"}`` on success, or ``{"error": str}`` on any failure —
    mirrors ``ChannelOutput``'s error-not-raise contract without requiring
    an Outline/Theme (the morning brief has neither). ``audio_seconds`` is
    the length of the rendered clip; ``render_ms`` is the wall-clock time
    spent synthesizing it — the two measure different things and are not
    expected to match.
    """
    start = time.monotonic()
    try:
        from okuro.peer.delivery import tts
        from okuro.peer.delivery.channels.podcast import _normalize, _storage_root
        from okuro.peer.delivery.channels.summary import _segments
        import numpy as np
        import soundfile as sf
    except Exception as exc:
        return {"error": f"audio deps missing: {exc}"}

    text = (script or "").strip()
    if not text:
        return {"error": "empty script"}

    try:
        segments = _segments(text)
        engine = tts.active_engine()
        sr = tts.SAMPLE_RATE
        mix = None

        # Pro edition renders the WHOLE brief in ONE model load so the single
        # narrator voice stays stable start to finish. Qwen (the pro engine as of
        # 2026-07-14) one-shots the whole script via tts.synth; MOSS (retired,
        # reachable only via the OKURO_TTS_ENGINE=moss pin) renders it through its
        # dialogue path. Any pro-engine fault -> per-segment Kokoro fallback.
        if engine == "qwen":
            try:
                samples, sr = tts.synth(text, None, role="narrator", engine="qwen")
                if len(samples):
                    mix = _normalize(np.asarray(samples, dtype="float32"), np)
            except Exception as exc:
                log.warning("qwen brief render failed (%s) — per-segment Kokoro fallback", exc)
                mix, engine = None, "kokoro"
        elif engine == "moss":
            try:
                samples, sr = tts.synth_dialogue(text)
                if len(samples):
                    mix = _normalize(np.asarray(samples, dtype="float32"), np)
            except Exception as exc:
                log.warning("moss brief render failed (%s) — per-segment Kokoro fallback", exc)
                mix, engine = None, "kokoro"

        if mix is None:
            clips: list["np.ndarray"] = []
            gap = np.zeros(int(0.28 * sr), dtype="float32")
            for seg in segments:
                samples, sr = tts.synth(seg, None, role="narrator", engine=engine)
                if len(samples):
                    clips.append(np.asarray(samples, dtype="float32"))
                    clips.append(gap)
            if not clips:
                raise RuntimeError("no audio produced")
            mix = _normalize(np.concatenate(clips), np)

        # okuro brand chord-drone bed (pre-roll intro + fade-out tail). Before the
        # write, so audio_seconds below counts the bed. brand() owns the config
        # lookup and the never-raise guard.
        from okuro.peer.delivery import voice_drone
        mix = voice_drone.brand(mix, sr)

        out_path = _storage_root() / f"morning-brief-{uuid.uuid4().hex[:12]}.mp3"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out_path), mix, sr, format="MP3")
        try:  # index into the assets media bucket (never block a delivery)
            from okuro.assets import store as _assets
            _assets.register_delivery_file(out_path)
        except Exception:  # noqa: BLE001
            pass

        return {
            "path": str(out_path),
            "audio_seconds": round(len(mix) / sr, 1),
            "engine": engine,
            "voice": tts.narrator_voice(engine),
            "segments": len(segments),
            "render_ms": int((time.monotonic() - start) * 1000),
        }
    except Exception as exc:
        log.exception("morning brief audio render failed")
        return {"error": f"brief audio render failed: {exc}"}
