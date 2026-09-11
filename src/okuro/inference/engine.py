# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Engine runner — launch/stop an OpenAI-compatible server for a bundle
#          under a broker lease (vLLM + llama-server).
# index:
#   imports
#   constants / default_tier
#   class EngineCommand
#   class EngineStartError
#   def _weights_target / _args_to_flags / build_command
#   def _default_spawn
#   class RunningEngine
#   class EngineRunner
#     start / _await_health / _heartbeat_loop / stop / stop_all / get / list_running
# AGENT_HEADER_END -->
"""Engine runner — start/stop an OpenAI-compatible HTTP server for a bundle,
holding a VRAM broker lease for the whole lifetime.

One runner turns a downloaded :class:`~okuro.ai_models.bundle.Bundle` into a
live endpoint the bridge ``local`` provider can call:

- **vLLM** — pro-tier LLM/VLM (safetensors, AWQ). ``--model`` is the bundle's
  ``files/`` dir (HF snapshot layout).
- **llama-server** (llama.cpp) — embeddings + portable/Metal/ROCm GGUF.
  ``--model`` is the single ``.gguf`` weight file; ``--embeddings`` is added
  for the embed capability.

The runner is the *single* thing that binds VRAM, so it is also the single
thing that talks to the broker: it requests a lease before spawning, heartbeats
while the server lives, and releases on stop. Tier follows the bundle's
capability (LLM=pinned, embeddings=warm, media=burst) unless overridden.

All side-effecting collaborators (process spawn, health probe, sleep, clock)
are injectable — same pattern as :class:`okuro.inference.broker.Broker` — so
the lifecycle is unit-testable without launching a real server.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from okuro.ai_models.bundle import Bundle
from okuro.bridge.local import check_http_ok
from okuro.inference.broker import BURST, PINNED, WARM, Broker, BrokerRejection, Lease
from okuro.inference.engine_registry import EngineRegistry
from okuro.db.engine import okuro_home

logger = logging.getLogger("okuro.inference.engine")

VLLM = "vllm"
LLAMA_SERVER = "llama-server"
LLAMA_CPP_PYTHON = "llama-cpp-python"  # in-venv OpenAI server, zero external install
_KNOWN_ENGINES = (VLLM, LLAMA_SERVER, LLAMA_CPP_PYTHON)
# GGUF-file engines serve a single .gguf; vLLM serves the files/ dir.
_GGUF_ENGINES = (LLAMA_SERVER, LLAMA_CPP_PYTHON)

_DEFAULT_HOST = "127.0.0.1"
# okuro-reserved fallback port — deliberately OUTSIDE tm-inference's 1320x
# cluster so a registry-miss fails with connection-refused instead of silently
# cross-talking to tm-inference's server (which owns 0.0.0.0:13207). In normal
# operation each engine binds a free port via _free_port() and publishes it to
# the registry; this constant is only the degenerate single-server default.
_DEFAULT_PORT = 13337
_DEFAULT_HEALTH_TIMEOUT_S = 180.0  # weight load for a 20-32B model can be slow
_DEFAULT_HEALTH_POLL_S = 2.0
_DEFAULT_HEARTBEAT_INTERVAL_S = 60.0
_TERMINATE_GRACE_S = 10.0

# Capability → default broker tier. LLM/VLM cold-start is 5-15s so it is pinned
# (never evicted); embeddings are warm (TTL); all media bursts (evict-first).
_DEFAULT_TIERS = {"llm": PINNED, "vlm": PINNED, "embed": WARM}


def default_tier(capability: str) -> str:
    """Broker tier a bundle of this capability should hold by default."""
    return _DEFAULT_TIERS.get(capability, BURST)


def _logs_root() -> Path:
    return Path(os.environ.get("OKURO_ENGINE_LOG_DIR", str(okuro_home() / "logs")))


def _free_port(host: str = _DEFAULT_HOST) -> int:
    """A free TCP port in okuro's reserved local-inference band (133xx).

    Each engine binds its own port so concurrent tenants (embeddings warm +
    LLM pinned + media burst) never collide with each other — nor with
    tm-inference on :13207. Falls back to an OS ephemeral port if the band is
    exhausted, so a launch is never blocked purely by a full band.
    """
    from okuro.system.port_registry import pick_free_port
    return pick_free_port(host=host)


@dataclass
class EngineCommand:
    """The resolved launch recipe for one bundle on one GPU/port."""

    argv: list[str]
    env: dict[str, str]
    endpoint: str
    engine: str
    health_path: str = "/health"  # llama-cpp-python has no /health → /v1/models


class EngineStartError(RuntimeError):
    """The server process failed to spawn or never became healthy in time."""


def _weights_target(bundle: Bundle) -> Path:
    """Path passed to the engine's ``--model`` flag.

    - llama-server: the single ``.gguf`` file (prefers a ``weights``-role file).
    - vLLM: the bundle's ``files/`` dir (HF snapshot layout of safetensors).
    """
    files_dir = bundle.path / "files"
    if bundle.engine in _GGUF_ENGINES:
        gguf = [f for f in bundle.files if f.name.lower().endswith(".gguf")]
        if not gguf:
            raise ValueError(f"bundle {bundle.id}: {bundle.engine} needs a .gguf file, none found")
        gguf.sort(key=lambda f: (f.role != "weights", f.name))  # weights role first
        return files_dir / gguf[0].name
    return files_dir


def _args_to_flags(engine_args: dict) -> list[str]:
    """Translate a bundle's ``engine_args`` dict into CLI flags.

    ``True`` → bare ``--flag``; ``False``/``None`` → omitted; list/tuple →
    repeated ``--flag value``; scalar → ``--flag value``.
    """
    flags: list[str] = []
    for key, value in engine_args.items():
        flag = f"--{key}"
        if value is True:
            flags.append(flag)
        elif value is False or value is None:
            continue
        elif isinstance(value, (list, tuple)):
            for item in value:
                flags += [flag, str(item)]
        else:
            flags += [flag, str(value)]
    return flags


def build_command(
    bundle: Bundle,
    *,
    gpu_index: int,
    port: int = _DEFAULT_PORT,
    host: str = _DEFAULT_HOST,
) -> EngineCommand:
    """Resolve the argv/env/endpoint to serve ``bundle`` on ``gpu_index``.

    Pure — computes the recipe without launching anything. ``gpu_index`` is
    pinned via ``CUDA_VISIBLE_DEVICES`` (the process then sees it as cuda:0;
    the broker still tracks the real index). The served model name is the
    bundle id so the bridge ``local`` provider can address it by id.
    """
    engine = bundle.engine
    if engine not in _KNOWN_ENGINES:
        raise ValueError(
            f"bundle {bundle.id}: unsupported engine {engine!r} "
            f"(known: {', '.join(_KNOWN_ENGINES)})"
        )
    model = str(_weights_target(bundle))
    extra = _args_to_flags(bundle.engine_args)
    health_path = "/health"

    if engine == VLLM:
        argv = [
            "python", "-m", "vllm.entrypoints.openai.api_server",
            "--model", model,
            "--served-model-name", bundle.id,
            "--host", host,
            "--port", str(port),
        ]
        if bundle.context_length:
            argv += ["--max-model-len", str(bundle.context_length)]
        if bundle.quant:
            argv += ["--quantization", bundle.quant.lower()]
        argv += extra
    elif engine == LLAMA_SERVER:
        argv = [
            "llama-server",
            "--model", model,
            "--alias", bundle.id,
            "--host", host,
            "--port", str(port),
        ]
        if bundle.capability == "embed":
            argv.append("--embeddings")
        if bundle.context_length:
            argv += ["--ctx-size", str(bundle.context_length)]
        # Native CUDA build serves on the GPU pinned via CUDA_VISIBLE_DEVICES:
        # offload every layer unless the bundle overrides ``n-gpu-layers``/``ngl``.
        if "n-gpu-layers" not in bundle.engine_args and "ngl" not in bundle.engine_args:
            argv += ["--n-gpu-layers", "999"]
        argv += extra
    else:  # llama-cpp-python — in-venv OpenAI server (python -m llama_cpp.server)
        argv = [
            sys.executable, "-m", "llama_cpp.server",
            "--model", model,
            "--model_alias", bundle.id,
            "--host", host,
            "--port", str(port),
        ]
        if bundle.capability == "embed":
            argv += ["--embedding", "True"]
        if bundle.context_length:
            argv += ["--n_ctx", str(bundle.context_length)]
        argv += extra
        health_path = "/v1/models"  # no /health route

    env = {"CUDA_VISIBLE_DEVICES": str(gpu_index)}
    endpoint = f"http://{host}:{port}"
    return EngineCommand(
        argv=argv, env=env, endpoint=endpoint, engine=engine, health_path=health_path
    )


def _default_detection() -> dict:
    """Real hardware detection for the edition gate (lazy — avoids importing the
    capability probe unless an engine is actually started)."""
    from okuro.capability import capabilities

    return capabilities()


def _default_spawn(argv: list[str], env: dict[str, str], log_path: Path) -> subprocess.Popen:
    """Launch the server, appending stdout+stderr to ``log_path``.

    ``start_new_session`` isolates the child in its own process group so a
    signal to okuro doesn't cascade into the model server. The log fd is dup'd
    into the child, so the parent closes its copy immediately.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    full_env = {**os.environ, **env}
    fh = open(log_path, "ab")
    try:
        return subprocess.Popen(
            argv,
            env=full_env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        fh.close()


@dataclass
class RunningEngine:
    """A live model server + its broker lease."""

    bundle_id: str
    endpoint: str
    gpu_index: int
    tier: str
    lease: Lease
    process: object  # Popen-like: poll/terminate/kill/wait/returncode
    log_path: Optional[Path] = None
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _hb: Optional[threading.Thread] = field(default=None, repr=False)


# Spawn/health/sleep/clock are typed loosely so tests can inject fakes.
_Spawn = Callable[[list, dict, Path], object]
_Health = Callable[[str], dict]


class EngineRunner:
    """Starts and stops bundle-backed model servers under broker leases."""

    def __init__(
        self,
        broker: Broker,
        *,
        registry: Optional[EngineRegistry] = None,
        spawn: Optional[_Spawn] = None,
        health: Optional[_Health] = None,
        sleep: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        health_timeout_s: float = _DEFAULT_HEALTH_TIMEOUT_S,
        health_poll_s: float = _DEFAULT_HEALTH_POLL_S,
        heartbeat_interval_s: float = _DEFAULT_HEARTBEAT_INTERVAL_S,
        detection: Optional[Callable[[], dict]] = None,
    ):
        self._broker = broker
        # Hardware detection for the edition gate — injectable so tests (and a
        # no-GPU CI) don't depend on the host's real GPUs.
        self._detection = detection or _default_detection
        # Registry lives in the broker's db so both halves share one file.
        self._registry = registry or EngineRegistry(getattr(broker, "db_path", None))
        self._spawn = spawn or _default_spawn
        self._health = health or check_http_ok
        self._sleep = sleep or time.sleep
        self._clock = clock or time.time
        self.health_timeout_s = health_timeout_s
        self.health_poll_s = health_poll_s
        self.heartbeat_interval_s = heartbeat_interval_s
        self._running: dict[str, RunningEngine] = {}

    def start(
        self,
        bundle: Bundle,
        *,
        gpu_index: int,
        tier: Optional[str] = None,
        port: Optional[int] = None,
        host: str = _DEFAULT_HOST,
        caller: str = "",
    ) -> RunningEngine:
        """Lease VRAM, spawn the server, block until healthy, start heartbeat.

        Raises :class:`~okuro.inference.broker.BrokerRejection` if the model
        won't fit, or :class:`EngineStartError` if the process dies or never
        reports healthy. The lease is always released on any failure path.
        """
        # Edition gate — never stand up a local engine on okuro-air (no local
        # GPU). Belt-and-suspenders: the broker would also reject, but this
        # fails fast with an edition-aware message. Uses real hardware
        # detection (an empty dict would falsely read as air).
        from okuro.ai_models.edition import detect_edition, local_inference_enabled

        detection = self._detection()
        if not local_inference_enabled(detection):
            raise EngineStartError(
                f"local inference is off on okuro-{detect_edition(detection)} "
                f"(no local GPU); cannot start an engine for bundle {bundle.id}"
            )

        if bundle.id in self._running:
            raise EngineStartError(f"engine already running for bundle {bundle.id}")
        tier = tier or default_tier(bundle.capability)
        # Each engine binds its own free port (published to the registry below),
        # so tenants never collide with each other or with tm-inference.
        if port is None:
            port = _free_port(host)
        cmd = build_command(bundle, gpu_index=gpu_index, port=port, host=host)

        # Lease first — never bind VRAM the broker hasn't admitted. On rejection,
        # surface WHO holds the GPU + the reclaim options as a proactive signal
        # (deduped, non-fatal) before propagating, so the user can act.
        try:
            lease = self._broker.request(
                bundle.id,
                bundle.vram_gb,
                gpu_index,
                tier=tier,
                caller=caller or f"engine:{bundle.id}",
                ttl_s=max(self.heartbeat_interval_s * 4, 60.0),
            )
        except BrokerRejection:
            try:
                from okuro.inference.reclaim import file_blocked_signal, plan_reclaim

                file_blocked_signal(plan_reclaim(gpu_index, bundle.vram_gb, broker=self._broker))
            except Exception:  # pragma: no cover - signal filing must not mask the reject
                pass
            raise

        log_path = _logs_root() / f"engine-{bundle.id}.log"
        try:
            proc = self._spawn(cmd.argv, cmd.env, log_path)
        except Exception as exc:
            self._broker.release(lease.lease_id)
            raise EngineStartError(f"spawn failed for bundle {bundle.id}: {exc}") from exc

        try:
            self._await_health(cmd.endpoint + cmd.health_path, proc)
        except Exception:
            self._terminate(proc)
            self._broker.release(lease.lease_id)
            raise

        eng = RunningEngine(
            bundle_id=bundle.id,
            endpoint=cmd.endpoint,
            gpu_index=gpu_index,
            tier=tier,
            lease=lease,
            process=proc,
            log_path=log_path,
        )
        eng._hb = threading.Thread(
            target=self._heartbeat_loop, args=(eng,), name=f"engine-hb-{bundle.id}", daemon=True
        )
        eng._hb.start()
        self._running[bundle.id] = eng
        # Publish to the durable registry so other processes (bridge, MCP, CLI)
        # can resolve this model's live endpoint.
        self._registry.register(
            bundle.id, cmd.endpoint, gpu_index, getattr(proc, "pid", 0),
            lease_id=lease.lease_id, tier=tier,
        )
        logger.info("engine up: %s on gpu%d %s (%s)", bundle.id, gpu_index, cmd.endpoint, tier)
        return eng

    def _await_health(self, health_url: str, proc) -> None:
        """Poll the readiness URL until the server answers, the process dies, or timeout."""
        deadline = self._clock() + self.health_timeout_s
        while self._clock() < deadline:
            rc = proc.poll()
            if rc is not None:
                raise EngineStartError(f"engine process exited early (rc={rc})")
            if self._health(health_url).get("healthy"):
                return
            self._sleep(self.health_poll_s)
        raise EngineStartError(f"engine health check timed out after {self.health_timeout_s:.0f}s")

    def _heartbeat_loop(self, eng: RunningEngine) -> None:
        """Renew the broker lease until stop is signalled or the process dies."""
        while not eng._stop.wait(self.heartbeat_interval_s):
            if eng.process.poll() is not None:
                logger.warning("engine %s died; heartbeat loop exiting", eng.bundle_id)
                return
            if not self._broker.heartbeat(eng.lease.lease_id):
                logger.warning("engine %s lease %s gone; heartbeat loop exiting",
                               eng.bundle_id, eng.lease.lease_id)
                return

    def _terminate(self, proc) -> None:
        """SIGTERM the server, escalating to SIGKILL after a grace period."""
        try:
            proc.terminate()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("terminate() raised: %s", exc)
            return
        try:
            proc.wait(timeout=_TERMINATE_GRACE_S)
        except Exception:
            try:
                proc.kill()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("kill() raised: %s", exc)

    def stop(self, bundle_id: str) -> bool:
        """Stop a running engine and release its lease. False if not running."""
        eng = self._running.pop(bundle_id, None)
        if eng is None:
            return False
        eng._stop.set()
        if eng._hb is not None:
            eng._hb.join(timeout=_TERMINATE_GRACE_S)
        self._terminate(eng.process)
        self._broker.release(eng.lease.lease_id)
        self._registry.unregister(bundle_id)
        logger.info("engine down: %s", bundle_id)
        return True

    def stop_all(self) -> int:
        """Stop every running engine. Returns the count stopped."""
        count = 0
        for bundle_id in list(self._running.keys()):
            if self.stop(bundle_id):
                count += 1
        return count

    def get(self, bundle_id: str) -> Optional[RunningEngine]:
        return self._running.get(bundle_id)

    def list_running(self) -> list[RunningEngine]:
        return list(self._running.values())
