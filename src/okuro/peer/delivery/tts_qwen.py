# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Qwen3-TTS CustomVoice backend — the pro-edition NARRATOR engine behind
#   tts.synth on okuro-pro (replaced MOSS 2026-07-14: kills the MOSS Chinese-drift
#   bug). Qwen pins its own torch/transformers, so it runs as a subprocess against
#   its isolated venv (OKURO_QWEN_PYTHON) driving qwen_worker.py. One subprocess
#   per artifact: the whole script is rendered in a single model load (worker
#   one-shots, per-sentence fallback on fault). VRAM reserved via the inference
#   broker (BURST) so a brief never OOMs; broker rejection / any fault raises so
#   the caller degrades to Kokoro (HR-C3).
# index: paths/config | def _gpu_lease | def available | def synth
# AGENT_HEADER_END -->
"""Qwen3-TTS CustomVoice narrator backend — subprocess seam to the isolated venv.

Why subprocess, not import: the qwen-tts stack pins its own torch/transformers,
incompatible with okuro's venv. Per okuro's module rule (communicate via
subprocess, never cross-import an incompatible dependency set), Qwen lives in a
dedicated venv at ``OKURO_QWEN_PYTHON`` and is driven by ``qwen_worker.py`` over
a tiny JSON job contract.

Selected by :func:`okuro.peer.delivery.tts.active_engine` — returns ``"qwen"``
only on the *pro* edition and only when :func:`available` is true. Voice / language
/ instruct / pace come from :mod:`tts_settings` (``qwen_*`` fields), so the founder
retunes without code changes. GPU0 (the Ada) is pinned; the worker re-pins it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("okuro.peer.delivery.tts_qwen")

# Isolated venv python + worker + HF cache (where the isolated venv downloaded the
# weights). All overridable for non-default installs.
def _conv(key: str, default: str) -> str:
    from okuro.yu.conventions import get_convention

    return str(get_convention(key, default))


_DEFAULT_QWEN_PYTHON = _conv("peer.qwen_tts_python", str(Path.home() / ".okuro/venvs/qwen-tts/bin/python"))
_DEFAULT_HF_HOME = _conv("peer.qwen_hf_home", str(Path.home() / ".cache/huggingface"))
_WORKER = Path(__file__).with_name("qwen_worker.py")

_TIMEOUT_S = int(os.environ.get("OKURO_QWEN_TIMEOUT_S", "1800"))
# 1.7B bf16 loads in ~5 GB; reserve headroom so the broker never stacks a second
# heavy tenant on top. GPU0 (the Ada) is the only supported device.
_VRAM_GB = float(os.environ.get("OKURO_QWEN_VRAM_GB", "8"))
_GPU_INDEX = int(os.environ.get("OKURO_QWEN_GPU", "0"))


@contextlib.contextmanager
def _gpu_lease():
    """Reserve Qwen's VRAM through okuro's inference broker for the job's life.

    Qwen is a BURST tenant (media): the broker admits it only if it genuinely
    fits (accounting for non-okuro GPU users too), evicting other BURST/WARM
    leases if needed; otherwise it rejects and we raise so the channel degrades
    to Kokoro rather than OOMing. A heartbeat keeps the lease alive across the
    worker run. If the broker module is absent (non-okuro deployment) we run
    unreserved with a warning.
    """
    try:
        from okuro.inference.broker import BURST, Broker, BrokerRejection
    except Exception as exc:  # broker not present in this deployment
        log.warning("inference broker unavailable (%s) — running Qwen unreserved", exc)
        yield
        return

    broker = Broker()
    try:
        lease = broker.request("qwen3-tts-customvoice", _VRAM_GB, _GPU_INDEX,
                               tier=BURST, caller="tts_qwen")
    except BrokerRejection as exc:
        raise RuntimeError(f"GPU busy — pro audio engine not admitted: {exc}") from exc

    stop = threading.Event()

    def _beat():
        while not stop.wait(60.0):
            broker.heartbeat(lease.lease_id)

    hb = threading.Thread(target=_beat, name="qwen-lease-hb", daemon=True)
    hb.start()
    try:
        yield
    finally:
        stop.set()
        broker.release(lease.lease_id)


def _qwen_python() -> str:
    return os.environ.get("OKURO_QWEN_PYTHON", _DEFAULT_QWEN_PYTHON)


def available() -> bool:
    """True when the isolated venv python and worker script both exist.

    Cheap (two path stats) so :func:`tts.active_engine` can call it on the hot
    path without probing the GPU or importing torch.
    """
    try:
        return Path(_qwen_python()).exists() and _WORKER.exists()
    except Exception:
        return False


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = "0"          # the Ada (GPU0) only
    env["HF_HOME"] = os.environ.get("OKURO_QWEN_HF_HOME", _DEFAULT_HF_HOME)
    env["HF_HUB_OFFLINE"] = "1"                # weights already cached — never hit net
    env["TRANSFORMERS_OFFLINE"] = "1"
    return env


_BAKED_INSTRUCT = (
    "Speak slowly and calmly, like a meditation guide reading a system readout. "
    "Neutral, precise, flat affect, unhurried but efficient. No enthusiasm, no "
    "drama, purely fact-stating."
)


def _voice_for(role: str):
    """(speaker, language, instruct, speed) for a semantic role.

    The speaker comes from the two-voice model — ``tts_settings.role_speaker``
    resolves okuro's gender voice for narrator/host_a and the OTHER gender's
    voice for host_b — so a dialogue is always two people and "which one is
    okuro" is one setting.

    What separates the two is the speaker and the delivery: the co-host does NOT
    get okuro's locked meditative instruct, so it talks like a co-host rather
    than a second okuro reading a readout. The house post-fx chain is NOT here —
    every okuro voice wears it regardless of engine, so it is applied at the tts
    seam (``voice_fx.house``), which is engine-agnostic by construction.

    Baked defaults on any settings fault — a broken config must never break the
    daily brief.
    """
    speaker, language, speed = "sohee", "english", 1.0
    instruct = _BAKED_INSTRUCT
    try:
        from okuro.peer.delivery import tts_settings
        s = tts_settings.current()
        speaker = tts_settings.role_speaker(role, "qwen", s) or speaker
        language = s.qwen_language or language
        instruct = s.qwen_instruct or instruct
        speed = float(s.qwen_speed)
        is_okuro = tts_settings.is_okuro_role(role)
    except Exception as exc:  # noqa: BLE001
        log.warning("tts_settings unavailable (%s) — Qwen uses baked defaults", exc)
        speaker = "aiden" if role == "host_b" else speaker
        is_okuro = role != "host_b"
    return speaker, language, (instruct if is_okuro else None), speed


def synth(text: str, *, role: str = "narrator", speed: float | None = None):
    """Synthesize ``text`` → (mono float32 ndarray, sr). Whole script, one load.

    ``role`` picks the voice: ``host_b`` is the co-host, anything else is okuro
    itself (see :func:`_voice_for`). Raises on any worker fault (missing venv,
    non-zero exit, no audio) so the caller can fall back to Kokoro. Never returns
    silent audio silently.
    """
    import numpy as np

    text = (text or "").strip()
    if not text:
        return np.zeros(0, dtype="float32"), 24000
    return synth_many([{"text": text, "role": role, "speed": speed}])[0]


def synth_many(items: list[dict]):
    """Render several lines against ONE model load → ``[(samples, sr), ...]``.

    Each item is ``{"text": str, "role": str, "speed": float|None,
    "speaker": str|None}`` and keeps its own voice, so a two-host dialogue is a
    single job. ``speaker`` overrides the one ``role`` would resolve to and is
    what lets the voice-preview batch render the WHOLE roster on one load — every
    other trait (language, instruct, pace) still comes from the role, so a
    preview is the okuro voice with only the speaker swapped.

    Returns the RAW voice: okuro's house post-fx is applied by the caller at the
    engine-agnostic tts seam (``tts.synth_many`` → ``voice_fx.house``).

    This exists because the model load, not the generation, is the cost: ~47s of
    load against ~5s of audio. Rendering a dialogue line-by-line paid it PER LINE
    — about 31 minutes for a 40-line podcast — which is why the per-line loop was
    never a viable pro path, quite apart from every line coming out in one voice.

    Order is preserved and empty texts are skipped (they still occupy a slot, as
    a zero-length clip) so callers can zip results back onto their lines.
    """
    import numpy as np
    import soundfile as sf

    if not items:
        return []
    if not available():
        raise RuntimeError(
            f"Qwen venv/worker not found (OKURO_QWEN_PYTHON={_qwen_python()!r}, "
            f"worker={_WORKER}) — install the pro-edition audio engine."
        )

    with tempfile.TemporaryDirectory(prefix="qwen-") as td:
        segments, slots = [], []
        for i, item in enumerate(items):
            text = (item.get("text") or "").strip()
            if not text:
                slots.append(None)
                continue
            speaker, language, instruct, cfg_speed = _voice_for(item.get("role") or "narrator")
            speaker = item.get("speaker") or speaker
            out_wav = Path(td) / f"out-{i:04d}.wav"
            speed = item.get("speed")
            segments.append({
                "text": text, "out": str(out_wav),
                "speaker": speaker, "language": language, "instruct": instruct,
                "speed": float(speed if speed is not None else cfg_speed),
            })
            slots.append(out_wav)
        if not segments:
            return [(np.zeros(0, dtype="float32"), 24000) for _ in items]

        job_path = Path(td) / "job.json"
        job_path.write_text(json.dumps({"segments": segments}))

        with _gpu_lease():                     # reserve VRAM for the whole run
            proc = subprocess.run(
                [_qwen_python(), str(_WORKER), "--in", str(job_path)],
                env=_worker_env(), capture_output=True, text=True,
                timeout=_TIMEOUT_S * max(1, len(segments)),
            )
        if proc.returncode != 0:
            raise RuntimeError(
                f"qwen_worker exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()[-500:]}"
            )
        stats = {}
        for line in reversed((proc.stdout or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    stats = json.loads(line)
                except Exception:
                    stats = {}
                break
        if not stats.get("ok", False) or not all(p.exists() for p in slots if p):
            raise RuntimeError(
                f"qwen_worker produced no audio: {stats or (proc.stdout or '')[-300:]}"
            )
        log.info("qwen synth ok: %d segment(s), %.1fs audio, vram=%sGB, speakers=%s",
                 len(segments),
                 sum(s.get("audio_s", 0.0) for s in stats.get("segments", [])),
                 stats.get("peak_vram_gb"),
                 ",".join(sorted({s["speaker"] for s in segments})))

        out = []
        for slot in slots:
            if slot is None:
                out.append((np.zeros(0, dtype="float32"), 24000))
                continue
            samples, sr = sf.read(str(slot), dtype="float32")
            if getattr(samples, "ndim", 1) > 1:
                samples = samples.mean(axis=1)
            out.append((np.asarray(samples, dtype="float32"), int(sr)))
        return out
