# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: MOSS-TTSD v1.0 backend — the expressive DIALOGUE engine behind
#   tts.synth_dialogue on the okuro-pro edition. MOSS pins transformers==5.0.0
#   (okuro runs 5.5.3) so it CANNOT be imported in-process; it runs as a
#   subprocess against its own isolated venv (OKURO_MOSS_PYTHON) driving
#   moss_worker.py. One subprocess per media artifact: the whole [S1]/[S2]
#   dialogue is generated in a single model load (MOSS is dialogue-native —
#   native turn-taking, no per-line concatenation). VRAM is reserved through
#   the inference broker (BURST tier) for the run so a podcast can never OOM;
#   broker rejection or any fault raises so the caller degrades to Kokoro (HR-C3).
# index:
#   paths / config
#   def _gpu_lease   (broker reservation)
#   def available
#   def synth_dialogue
# AGENT_HEADER_END -->
"""MOSS-TTSD dialogue backend — subprocess seam to the isolated venv.

DEPRECATED for pro (2026-07-14): the pro narrator engine is now Qwen3-TTS
(``tts_qwen``), which fixed MOSS's Chinese-drift bug. MOSS is RETIRED from
``active_engine`` auto-selection but intentionally kept on disk — reachable only
via the explicit ``OKURO_TTS_ENGINE=moss`` pin (e.g. its native [S1]/[S2]
dialogue path). Do not delete.

Why subprocess, not import: MOSS-TTSD pins transformers==5.0.0 / torch==2.9.1,
incompatible with okuro's own venv (transformers 5.5.3). Per okuro's module rule
(communicate via subprocess, never cross-import an incompatible dependency set),
MOSS lives in a dedicated venv at ``OKURO_MOSS_PYTHON`` and is driven by
``moss_worker.py`` over a tiny JSON job contract.

The engine is selected by :func:`okuro.peer.delivery.tts.active_engine` — it
returns ``"moss"`` only on the *pro* edition, and only when :func:`available`
here is true. GPU0 (the supported CUDA device) is pinned for the worker; the
worker also pins it defensively.
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

log = logging.getLogger("okuro.peer.delivery.tts_moss")

# Isolated venv python + worker script. Overridable via env; machine
# locations come from conventions (peer.moss_python / peer.hf_home).
def _conv(key: str, default: str) -> str:
    from okuro.yu.conventions import get_convention

    return str(get_convention(key, default))


_DEFAULT_MOSS_PYTHON = _conv("peer.moss_python", str(Path.home() / ".okuro/venvs/moss/bin/python"))
_DEFAULT_HF_HOME = _conv("peer.hf_home", str(Path.home() / ".cache/huggingface"))
_WORKER = Path(__file__).with_name("moss_worker.py")

# Per-job generation cap handed to the worker (it chunks longer dialogues).
_MAX_NEW_TOKENS = int(os.environ.get("OKURO_MOSS_MAX_NEW_TOKENS", "4096"))
# Hard ceiling so a wedged worker can never hang a media job forever.
_TIMEOUT_S = int(os.environ.get("OKURO_MOSS_TIMEOUT_S", "1800"))
# Measured peak is ~24.2 GB; reserve a touch more so the broker never admits a
# second heavy tenant on top of us. GPU0 (the Ada) is the only supported device.
_VRAM_GB = float(os.environ.get("OKURO_MOSS_VRAM_GB", "25"))
_GPU_INDEX = int(os.environ.get("OKURO_MOSS_GPU", "0"))


@contextlib.contextmanager
def _gpu_lease():
    """Reserve MOSS's VRAM through okuro's inference broker for the job's life.

    MOSS is a BURST tenant (media): the broker admits it only if it genuinely
    fits (``min(ledger_free, nvidia-smi free) − headroom`` — which also accounts
    for non-okuro GPU users like ComfyUI), evicting other BURST/WARM leases if
    needed. If it still can't fit, the broker rejects and we raise — the channel
    then degrades to Kokoro rather than OOMing the box. A daemon heartbeat keeps
    the lease alive across the (possibly minutes-long) worker run.

    If the broker module is unavailable (a non-okuro deployment) we run
    unreserved with a warning — best effort; a full install always has it.
    """
    try:
        from okuro.inference.broker import BURST, Broker, BrokerRejection
    except Exception as exc:  # broker not present in this deployment
        log.warning("inference broker unavailable (%s) — running MOSS unreserved", exc)
        yield
        return

    broker = Broker()
    try:
        lease = broker.request("moss-ttsd-v1.0", _VRAM_GB, _GPU_INDEX,
                               tier=BURST, caller="tts_moss")
    except BrokerRejection as exc:
        raise RuntimeError(f"GPU busy — pro audio engine not admitted: {exc}") from exc

    stop = threading.Event()

    def _beat():
        while not stop.wait(60.0):
            broker.heartbeat(lease.lease_id)

    hb = threading.Thread(target=_beat, name="moss-lease-hb", daemon=True)
    hb.start()
    try:
        yield
    finally:
        stop.set()
        broker.release(lease.lease_id)


def _moss_python() -> str:
    return os.environ.get("OKURO_MOSS_PYTHON", _DEFAULT_MOSS_PYTHON)


def available() -> bool:
    """True when the isolated venv python and worker script both exist.

    Cheap (two path stats) so :func:`tts.active_engine` can call it on the hot
    path without probing the GPU or importing torch.
    """
    try:
        return Path(_moss_python()).exists() and _WORKER.exists()
    except Exception:
        return False


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    # The Ada (GPU0, sm_89) is the only CUDA device okuro's stack supports; the
    # Blackwell GPU1 is unsupported by the pinned torch and MUST NOT be touched.
    env["CUDA_VISIBLE_DEVICES"] = "0"
    # FORCE the MOSS model cache (hard override — the okuro process inherits its
    # own HF_HOME, which is NOT where the isolated venv downloaded the weights),
    # and go fully offline: the model is already cached, so a hub revision check
    # on the trust_remote_code files must never reach the network — otherwise a
    # slow/flaky connection wedges the whole render before the GPU ever loads.
    env["HF_HOME"] = os.environ.get("OKURO_MOSS_HF_HOME", _DEFAULT_HF_HOME)
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    return env


def synth_dialogue(script: str, *, seed: int | None = None,
                   max_new_tokens: int | None = None):
    """Synthesize a whole ``[S1]/[S2]``-tagged dialogue → (float32 ndarray, sr).

    ``script`` is the complete conversation with speaker tags (``[S1]`` host_a,
    ``[S2]`` host_b); plain untagged text renders as a single speaker (the
    narrator path). One model load renders the entire script — MOSS handles
    turn-taking natively, so callers do NOT loop per line.

    Raises on any worker fault (missing venv, non-zero exit, bad output) so the
    channel can fall back to Kokoro. Never returns silent/empty audio silently.
    """
    import numpy as np
    import soundfile as sf

    script = (script or "").strip()
    if not script:
        return np.zeros(0, dtype="float32"), 24000
    if not available():
        raise RuntimeError(
            f"MOSS venv/worker not found (OKURO_MOSS_PYTHON={_moss_python()!r}, "
            f"worker={_WORKER}) — install the pro-edition audio engine."
        )

    with tempfile.TemporaryDirectory(prefix="moss-") as td:
        out_wav = Path(td) / "out.wav"
        job = {
            "text": script,
            "out": str(out_wav),
            "max_new_tokens": int(max_new_tokens or _MAX_NEW_TOKENS),
        }
        if seed is not None:
            job["seed"] = int(seed)
        # User-configurable voice + pace (single delivery store). Best-effort:
        # if the store is unreadable the worker uses its own baked defaults.
        try:
            from okuro.peer.delivery import tts_settings
            s = tts_settings.current()
            job["speed"] = float(s.pro_speed)
            # Named voice-clone references: [S1] female, [S2] male. The worker
            # builds/refreshes its ref cache from these source wavs so a cache
            # clear can never silently revert to seed-auditioned voices.
            fem, mal = tts_settings.source_wav(s.pro_female_ref), tts_settings.source_wav(s.pro_male_ref)
            if fem.exists() and mal.exists():
                job["refs"] = {
                    "s1_wav": str(fem), "s2_wav": str(mal),
                    "text": tts_settings.PRO_REF_TEXT,
                }
        except Exception as exc:  # noqa: BLE001
            log.warning("tts_settings unavailable (%s) — MOSS uses baked defaults", exc)
        job_path = Path(td) / "job.json"
        job_path.write_text(json.dumps(job))

        # Reserve VRAM through the broker for the whole worker run — no OOM.
        with _gpu_lease():
            proc = subprocess.run(
                [_moss_python(), str(_WORKER), "--in", str(job_path)],
                env=_worker_env(), capture_output=True, text=True, timeout=_TIMEOUT_S,
            )
        if proc.returncode != 0:
            raise RuntimeError(
                f"moss_worker exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()[-500:]}"
            )
        # Worker prints one JSON status line last; parse it for logging/telemetry.
        stats = {}
        for line in reversed((proc.stdout or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    stats = json.loads(line)
                except Exception:
                    stats = {}
                break
        if not stats.get("ok", False) or not out_wav.exists():
            raise RuntimeError(
                f"moss_worker produced no audio: {stats or (proc.stdout or '')[-300:]}"
            )
        log.info(
            "moss synth ok: %.1fs audio, rtf=%s, chunks=%s, vram=%sGB",
            stats.get("audio_s", 0.0), stats.get("rtf"),
            stats.get("chunks"), stats.get("peak_vram_gb"),
        )
        samples, sr = sf.read(str(out_wav), dtype="float32")
        if getattr(samples, "ndim", 1) > 1:  # collapse any stray stereo to mono
            samples = samples.mean(axis=1)
        return np.asarray(samples, dtype="float32"), int(sr)
