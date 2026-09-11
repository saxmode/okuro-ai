# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Headless ComfyUI runtime adapter — the image/video counterpart to
#          engine.py's llama-server. ComfyUI runs a node GRAPH, not an OpenAI
#          chat completion, so it CANNOT go through engine.py; this is a sibling
#          runner. It talks to ComfyUI ONLY over its HTTP API (POST /prompt,
#          GET /history, GET /view) as a SEPARATE PROCESS: ComfyUI is GPL-3.0, so
#          arm's-length HTTP is the boundary that keeps okuro (a sold product)
#          proprietary. okuro NEVER imports, forks, vendors, or ships ComfyUI —
#          it discovers an installed one, or launches the user's installed copy
#          under a broker VRAM lease. Entry is gated on the edition (air = off)
#          and the model's commercial licence (licensing.py, 3-state).
# index:
#   errors: ComfyError / ComfyNotInstalled / ComfyUnavailable / ComfyGenerationError
#   ComfyEndpoint / ComfyResult                (dataclasses)
#   class ComfyAdapter
#     resolve_endpoint (discover→launch) / generate / shutdown / stop_all
# AGENT_HEADER_END -->
"""Headless ComfyUI adapter — generate images (video later) over ComfyUI's HTTP API.

Flow of :meth:`ComfyAdapter.generate`:

    edition gate → licence gate → build graph (comfy_workflows) → resolve an
    endpoint (reuse a live ComfyUI, else launch okuro's own under a BURST lease)
    → POST /prompt → poll GET /history/{id} → GET /view the output bytes.

GPL boundary (load-bearing, do not "optimise" away): every interaction is HTTP
to a separate process. There is no ``import comfy`` anywhere in okuro, and okuro
does not distribute ComfyUI — the launch path locates an *already-installed*
ComfyUI (``main.py`` + its own venv) and shells out to it. No install found →
:class:`ComfyNotInstalled` with guidance, never an auto-download.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from okuro.inference.broker import BURST, Broker, Lease
from okuro.inference import comfy_workflows, comfy_registry
from okuro.db.engine import okuro_home

logger = logging.getLogger("okuro.inference.comfy")

_DEFAULT_HOST = "127.0.0.1"
# okuro targets its OWN coupled ComfyUI (provisioned by comfy_install) — it does
# NOT scan for and reuse foreign ComfyUI instances. OKURO_COMFYUI_ENDPOINTS is an
# EXPLICIT operator hook (comma-separated) for pointing okuro at a specific
# instance it manages/trusts; there is deliberately NO default port scan.
_OWN_MODEL_ID = "comfyui"     # engine-registry key for okuro's own instance
_HEALTH_TIMEOUT_S = 120.0     # cold ComfyUI start (torch import + node scan)
_HEALTH_POLL_S = 2.0
_HEARTBEAT_INTERVAL_S = 60.0
_GEN_TIMEOUT_S = 300.0        # a single image; video will need its own budget
_POLL_INTERVAL_S = 1.0
_TERMINATE_GRACE_S = 10.0
# Conservative default lease size when the caller doesn't estimate one. Real
# callers pass vram_gb from the bundle/fitting; this only avoids a zero-lease.
_DEFAULT_CKPT_VRAM_GB = 10.0


class ComfyError(RuntimeError):
    """Base for ComfyUI adapter failures."""


class ComfyNotInstalled(ComfyError):
    """No live ComfyUI to reuse and no installed ComfyUI to launch."""


class ComfyUnavailable(ComfyError):
    """local inference is off for this edition (air)."""


class ComfyLicenseBlocked(ComfyError):
    """The selected model's licence does not permit this org's commercial use."""

    def __init__(self, message: str, gate: dict):
        super().__init__(message)
        self.gate = gate


class ComfyGenerationError(ComfyError):
    """Submission failed, timed out, or produced no image."""


class ComfyCancelled(ComfyError):
    """The caller cancelled the generation mid-flight (ComfyUI /interrupt)."""


@dataclass
class ComfyEndpoint:
    """A resolved ComfyUI endpoint and, if okuro launched it, its lifecycle."""

    endpoint: str
    launched: bool = False
    pid: int = 0
    lease: Optional[Lease] = None
    process: object = None
    log_path: Optional[Path] = None
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _hb: Optional[threading.Thread] = field(default=None, repr=False)


@dataclass
class ComfyResult:
    prompt_id: str
    images: list[bytes]
    endpoint: str
    meta: dict = field(default_factory=dict)


def _default_get(url: str) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode() or "{}")


def _default_get_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _default_post(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode() or "{}")


def _default_spawn(argv: list[str], env: dict[str, str], cwd: Path, log_path: Path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "ab", buffering=0)
    return subprocess.Popen(
        argv, cwd=str(cwd), env={**os.environ, **env},
        stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)


def _free_port(host: str = _DEFAULT_HOST) -> int:
    # okuro's own ComfyUI binds inside the reserved local-inference band (133xx).
    from okuro.system.port_registry import pick_free_port
    return pick_free_port(host=host)


# ComfyUI node class_type → a human progress phrase (never node/graph jargon to
# the user). Used to label `executing` transitions; sampler `progress` messages
# drive the actual percentage.
_NODE_PHASE = {
    "CheckpointLoaderSimple": "Loading the model…",
    "UNETLoader": "Loading the model…",
    "DualCLIPLoader": "Loading the model…",
    "VAELoader": "Loading the model…",
    "CLIPTextEncode": "Understanding your prompt…",
    "FluxGuidance": "Understanding your prompt…",
    "KSampler": "Generating…",
    "VAEDecode": "Finishing the image…",
    "SaveImage": "Saving…",
}


def _default_ws_connect(url: str):
    import websocket  # websocket-client
    return websocket.create_connection(url, timeout=5)


def _logs_root() -> Path:
    return Path(os.environ.get("OKURO_ENGINE_LOG_DIR", str(okuro_home() / "logs")))


def _explicit_endpoints() -> tuple[str, ...]:
    """Operator-configured endpoints okuro may target (OKURO_COMFYUI_ENDPOINTS).

    EXPLICIT only — an unset var yields nothing. okuro never auto-discovers a
    foreign ComfyUI; without a hook it launches its own provisioned instance.
    """
    env = os.environ.get("OKURO_COMFYUI_ENDPOINTS", "").strip()
    if not env:
        return ()
    return tuple(u.strip().rstrip("/") for u in env.split(",") if u.strip())


class ComfyAdapter:
    """Run generations on okuro's OWN coupled ComfyUI.

    okuro ships ComfyUI as a side-service (installed by comfy_install) and owns
    its lifecycle: this adapter reconnects to okuro's running instance or
    launches it under a broker VRAM lease, then submit→poll→fetch. It never
    scans for / reuses a foreign ComfyUI — an explicit OKURO_COMFYUI_ENDPOINTS
    hook is the only way to target an externally-managed instance. All I/O
    collaborators are injectable so the lifecycle is unit-testable.
    """

    def __init__(
        self,
        broker: Broker,
        *,
        http_get: Optional[Callable[[str], dict]] = None,
        http_get_bytes: Optional[Callable[[str], bytes]] = None,
        http_post: Optional[Callable[[str, dict], dict]] = None,
        spawn: Optional[Callable] = None,
        ws_connect: Optional[Callable[[str], object]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        detection: Optional[Callable[[], dict]] = None,
        registry=None,
        home: Optional[Path] = None,
        endpoints: Optional[tuple[str, ...]] = None,
        health_timeout_s: float = _HEALTH_TIMEOUT_S,
        gen_timeout_s: float = _GEN_TIMEOUT_S,
        poll_interval_s: float = _POLL_INTERVAL_S,
    ):
        self._broker = broker
        self._get = http_get or _default_get
        self._get_bytes = http_get_bytes or _default_get_bytes
        self._post = http_post or _default_post
        self._spawn = spawn or _default_spawn
        self._ws_connect = ws_connect or _default_ws_connect
        self._sleep = sleep or time.sleep
        self._clock = clock or time.time
        self._detection = detection or self._real_detection
        # Durable registry tracks okuro's OWN running ComfyUI (endpoint+pid), so
        # a fresh process reconnects to it instead of launching a second one.
        if registry is None:
            from okuro.inference.engine_registry import EngineRegistry
            registry = EngineRegistry(getattr(broker, "db_path", None))
        self._registry = registry
        # Where okuro's own ComfyUI lives (comfy_install home) — None = default.
        self._home = home
        # Explicit operator hooks only; None = read the env (no default scan).
        self._endpoints = _explicit_endpoints() if endpoints is None else endpoints
        self.health_timeout_s = health_timeout_s
        self.gen_timeout_s = gen_timeout_s
        self.poll_interval_s = poll_interval_s
        self._owned: list[ComfyEndpoint] = []

    # -- gates -----------------------------------------------------------------

    @staticmethod
    def _real_detection() -> dict:
        # Same source engine.py's edition gate uses — real GPU capabilities.
        from okuro.capability import capabilities
        return capabilities()

    def _edition_gate(self) -> None:
        from okuro.ai_models.edition import detect_edition, local_inference_enabled
        detection = self._detection()
        if not local_inference_enabled(detection):
            raise ComfyUnavailable(
                f"local inference is off on okuro-{detect_edition(detection)} "
                f"(no local GPU); the ComfyUI image/video engine is disabled")

    @staticmethod
    def _license_gate(license: Optional[dict], org_revenue_usd: Optional[float],
                      *, family: Optional[str], base_model: Optional[str]) -> dict:
        """Commercial licence gate. Hard-blocks only when a COMMERCIAL context is
        declared (``org_revenue_usd`` given) and the model isn't cleared for it —
        a paying org must not run a non-commercial / over-cap model. Personal use
        (no revenue declared) is never hard-blocked: a non-commercial licence
        still permits personal, non-commercial generation, so ``g['reason']`` is
        surfaced as an advisory instead (per the product decision: flag
        non-commercial behind a warning, don't wall it off).

        The block decision itself lives in ``licensing.blocks()`` — the single
        canonical source enforcement routes through; ``gate()`` is called here
        only for the advisory ``reason``/return payload."""
        from okuro.ai_models import licensing
        g = licensing.gate(license, org_revenue_usd, family=family, base_model=base_model)
        if licensing.blocks(license, org_revenue_usd, family=family, base_model=base_model):
            raise ComfyLicenseBlocked(g["reason"], g)
        return g

    # -- endpoint resolution ---------------------------------------------------

    def _probe(self, endpoint: str) -> bool:
        try:
            stats = self._get(f"{endpoint}/system_stats")
            return isinstance(stats, dict) and ("system" in stats or "devices" in stats)
        except Exception:
            return False

    def _explicit_hook(self) -> Optional[str]:
        """An operator-configured endpoint that is actually up, or None."""
        for ep in self._endpoints:
            if self._probe(ep):
                logger.info("comfy: using configured ComfyUI at %s", ep)
                return ep
        return None

    def _own_running(self) -> Optional[str]:
        """okuro's own ComfyUI endpoint if it is registered + live, else None."""
        try:
            ep = self._registry.resolve(_OWN_MODEL_ID)
        except Exception:
            ep = None
        if ep and self._probe(ep):
            return ep
        return None

    def _launch(self, *, gpu_index: int, vram_gb: float, caller: str) -> ComfyEndpoint:
        """Launch okuro's OWN provisioned ComfyUI under a broker lease."""
        self._edition_gate()
        from okuro.inference import comfy_install

        home = self._home or comfy_install.comfy_home()
        if not comfy_install.is_installed(home):
            raise ComfyNotInstalled(
                f"okuro's ComfyUI is not installed at {home}. Provision it first: "
                f"comfy_install.install(consent=True) — okuro ships ComfyUI as a "
                f"coupled side-service and will not reuse a foreign instance.")
        port = _free_port()
        endpoint = f"http://{_DEFAULT_HOST}:{port}"
        lease = self._broker.request(
            _OWN_MODEL_ID, vram_gb or _DEFAULT_CKPT_VRAM_GB, gpu_index,
            tier=BURST, caller=caller or "comfy:launch",
            ttl_s=max(_HEARTBEAT_INTERVAL_S * 4, self.gen_timeout_s + 60.0))
        argv = [str(comfy_install.venv_python(home)), "-s", "main.py",
                "--port", str(port), "--listen", _DEFAULT_HOST]
        cfg = home / "extra_model_paths.yaml"
        if cfg.exists():
            argv += ["--extra-model-paths-config", str(cfg)]
        env = {"CUDA_VISIBLE_DEVICES": str(gpu_index)}
        log_path = _logs_root() / f"comfy-{port}.log"
        try:
            proc = self._spawn(argv, env, home, log_path)
        except Exception as exc:
            self._broker.release(lease.lease_id)
            raise ComfyError(f"failed to spawn ComfyUI: {exc}") from exc
        ep = ComfyEndpoint(endpoint=endpoint, launched=True,
                           pid=getattr(proc, "pid", 0), lease=lease,
                           process=proc, log_path=log_path)
        try:
            self._await_health(ep)
        except Exception:
            self.shutdown(ep)
            raise
        ep._hb = threading.Thread(target=self._heartbeat_loop, args=(ep,),
                                  name=f"comfy-hb-{port}", daemon=True)
        ep._hb.start()
        self._owned.append(ep)
        # Publish so other processes reconnect to THIS instance, not a new one.
        try:
            self._registry.register(_OWN_MODEL_ID, endpoint, gpu_index,
                                    ep.pid, lease_id=lease.lease_id, tier=BURST)
        except Exception as exc:  # pragma: no cover - registry is best-effort
            logger.debug("comfy: registry.register failed: %s", exc)
        logger.info("comfy: launched okuro ComfyUI at %s on gpu%d (pid %s)",
                    endpoint, gpu_index, ep.pid)
        return ep

    def _await_health(self, ep: ComfyEndpoint) -> None:
        deadline = self._clock() + self.health_timeout_s
        while self._clock() < deadline:
            proc = ep.process
            if proc is not None and proc.poll() is not None:
                raise ComfyError(f"ComfyUI process exited early (rc={proc.poll()})")
            if self._probe(ep.endpoint):
                return
            self._sleep(_HEALTH_POLL_S)
        raise ComfyError(f"ComfyUI health check timed out after {self.health_timeout_s:.0f}s")

    def _heartbeat_loop(self, ep: ComfyEndpoint) -> None:
        while not ep._stop.wait(_HEARTBEAT_INTERVAL_S):
            proc = ep.process
            if proc is not None and proc.poll() is not None:
                return
            if ep.lease and not self._broker.heartbeat(ep.lease.lease_id):
                return

    def resolve_endpoint(self, *, gpu_index: int = 0, vram_gb: float = 0.0,
                         caller: str = "") -> ComfyEndpoint:
        """Target okuro's own ComfyUI: an explicit operator hook, else the
        registered running instance, else launch the provisioned one. Never
        reuses an auto-discovered foreign ComfyUI."""
        hook = self._explicit_hook()
        if hook is not None:
            return ComfyEndpoint(endpoint=hook, launched=False)
        own = self._own_running()
        if own is not None:
            logger.info("comfy: reconnecting to okuro's ComfyUI at %s", own)
            return ComfyEndpoint(endpoint=own, launched=False)
        return self._launch(gpu_index=gpu_index, vram_gb=vram_gb, caller=caller)

    # -- submit / poll / fetch -------------------------------------------------

    def _submit(self, endpoint: str, graph: dict, client_id: str) -> str:
        resp = self._post(f"{endpoint}/prompt", {"prompt": graph, "client_id": client_id})
        if not isinstance(resp, dict) or not resp.get("prompt_id"):
            node_errors = (resp or {}).get("node_errors") or (resp or {}).get("error")
            raise ComfyGenerationError(f"ComfyUI rejected the workflow: {node_errors or resp}")
        return resp["prompt_id"]

    def _interrupt(self, endpoint: str) -> None:
        """Ask ComfyUI to stop the currently-executing prompt (best-effort)."""
        try:
            self._post(f"{endpoint}/interrupt", {})
        except Exception as exc:  # pragma: no cover - cancel is best-effort
            logger.debug("comfy: interrupt failed: %s", exc)

    def _await_result(self, endpoint: str, prompt_id: str,
                      should_cancel: Optional[Callable[[], bool]] = None) -> dict:
        deadline = self._clock() + self.gen_timeout_s
        while self._clock() < deadline:
            if should_cancel is not None and should_cancel():
                self._interrupt(endpoint)
                raise ComfyCancelled("generation cancelled")
            hist = self._get(f"{endpoint}/history/{prompt_id}")
            entry = (hist or {}).get(prompt_id)
            if entry:
                status = (entry.get("status") or {})
                if status.get("status_str") == "error":
                    raise ComfyGenerationError(f"ComfyUI reported an execution error: {status}")
                if entry.get("outputs"):
                    return entry["outputs"]
            self._sleep(self.poll_interval_s)
        raise ComfyGenerationError(f"generation timed out after {self.gen_timeout_s:.0f}s")

    def _pump_progress(self, endpoint: str, client_id: str, graph: dict,
                       on_progress: Callable[[dict], None],
                       stop: threading.Event) -> None:
        """Relay ComfyUI /ws progress into ``on_progress`` (Gap C).

        ComfyUI pushes ``progress`` (sampler step value/max) and ``executing``
        (current node) messages over its websocket, keyed to the submitting
        client_id. This translates them to okuro's progress contract
        (``{phase, message, pct}``) — human phrases, never node/graph jargon —
        until the result lands (``stop`` set) or the socket closes. Best-effort:
        any ws failure is swallowed (generation still completes via /history).
        """
        node_class = {nid: n.get("class_type") for nid, n in graph.items()}
        ws_url = (endpoint.replace("https://", "wss://").replace("http://", "ws://")
                  + f"/ws?clientId={client_id}")
        try:
            ws = self._ws_connect(ws_url)
        except Exception as exc:  # pragma: no cover - ws optional
            logger.debug("comfy: progress ws connect failed: %s", exc)
            return
        try:
            while not stop.is_set():
                try:
                    raw = ws.recv()
                except Exception:
                    return
                if not raw or isinstance(raw, (bytes, bytearray)):
                    continue  # binary = preview frames; skip
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                t, data = msg.get("type"), msg.get("data") or {}
                if t == "progress" and data.get("max"):
                    pct = int(100 * float(data.get("value", 0)) / float(data["max"]))
                    on_progress({"phase": "generate", "message": "Generating…",
                                 "pct": max(0, min(100, pct))})
                elif t == "executing":
                    node = data.get("node")
                    if node is None:
                        return  # graph finished
                    phrase = _NODE_PHASE.get(node_class.get(str(node)), "Working…")
                    on_progress({"phase": "generate", "message": phrase})
        finally:
            try:
                ws.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def _fetch_images(self, endpoint: str, outputs: dict) -> list[bytes]:
        images: list[bytes] = []
        for node in outputs.values():
            for img in (node.get("images") or []):
                if img.get("type") == "temp":
                    continue  # previews, not the saved result
                q = urllib.parse.urlencode({
                    "filename": img.get("filename", ""),
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output"),
                })
                images.append(self._get_bytes(f"{endpoint}/view?{q}"))
        return images

    # -- public ----------------------------------------------------------------

    def generate(
        self,
        family: Optional[str],
        plan: dict,
        *,
        ckpt_name: str,
        gpu_index: int = 0,
        vram_gb: float = 0.0,
        seed: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        batch_size: int = 1,
        filename_prefix: str = "okuro",
        license: Optional[dict] = None,
        org_revenue_usd: Optional[float] = None,
        base_model: Optional[str] = None,
        workflow: Optional[dict] = None,
        components: Optional[dict] = None,
        endpoint: Optional[ComfyEndpoint] = None,
        client_id: Optional[str] = None,
        on_progress: Optional[Callable[[dict], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> ComfyResult:
        """Generate from an optimizer ``plan`` on model ``ckpt_name``.

        Gates on edition then commercial licence (when ``license`` is provided),
        builds the graph, resolves an endpoint, and runs submit→poll→fetch. When
        ``workflow`` (a GenerativeWorkflowRegistry row: ``graph`` + ``input_map``)
        is given, the runtime fills that registered graph's declared slots;
        otherwise it falls back to the hand-coded family builder. Raises
        :class:`ComfyUnavailable` / :class:`ComfyLicenseBlocked` /
        :class:`ComfyNotInstalled` / :class:`ComfyGenerationError`.
        """
        self._edition_gate()
        gate = self._license_gate(license, org_revenue_usd,
                                  family=family, base_model=base_model)
        if seed is None:
            seed = int.from_bytes(os.urandom(4), "big")
        if workflow is not None:
            graph = comfy_registry.apply_plan(
                workflow["graph"], workflow.get("input_map") or {},
                plan=plan, ckpt_name=ckpt_name, seed=seed, width=width, height=height)
        elif components:
            # Split-weight model (Flux/SD3.5): UNET + dual CLIP + VAE from
            # separate files rather than an all-in-one checkpoint (V4).
            graph = comfy_workflows.build_multifile_workflow(
                family, plan, components=components, seed=seed,
                width=width, height=height, batch_size=batch_size,
                filename_prefix=filename_prefix)
        else:
            graph = comfy_workflows.build_workflow(
                family, plan, ckpt_name=ckpt_name, seed=seed,
                width=width, height=height, batch_size=batch_size,
                filename_prefix=filename_prefix)

        ep = endpoint or self.resolve_endpoint(
            gpu_index=gpu_index, vram_gb=vram_gb, caller=f"comfy:{ckpt_name}")
        cid = client_id or os.urandom(8).hex()
        # Gap C: when a progress sink is given, pump ComfyUI's /ws step progress
        # into it on a side thread for the duration of the run.
        stop = threading.Event()
        pump: Optional[threading.Thread] = None
        if on_progress is not None:
            pump = threading.Thread(
                target=self._pump_progress,
                args=(ep.endpoint, cid, graph, on_progress, stop),
                name="comfy-progress", daemon=True)
            pump.start()
        try:
            prompt_id = self._submit(ep.endpoint, graph, cid)
            outputs = self._await_result(ep.endpoint, prompt_id, should_cancel)
            images = self._fetch_images(ep.endpoint, outputs)
        finally:
            # Stop the progress pump; a ComfyUI we launched keeps its BURST lease
            # and stays warm for the next gen (torn down on shutdown()). A
            # discovered (external) endpoint is not ours to stop.
            stop.set()
            if pump is not None:
                pump.join(timeout=2.0)
        if not images:
            raise ComfyGenerationError("workflow completed but produced no output image")
        return ComfyResult(prompt_id=prompt_id, images=images, endpoint=ep.endpoint,
                           meta={"seed": seed, "family": family, "ckpt": ckpt_name,
                                 "license": gate, "launched": ep.launched,
                                 "workflow_id": workflow.get("workflow_id") if workflow else None})

    def shutdown(self, ep: ComfyEndpoint) -> bool:
        """Stop a ComfyUI okuro launched and release its lease. No-op for reused."""
        if not ep.launched:
            return False
        ep._stop.set()
        if ep._hb is not None:
            ep._hb.join(timeout=_TERMINATE_GRACE_S)
        proc = ep.process
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=_TERMINATE_GRACE_S)
            except Exception:
                try:
                    proc.kill()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("comfy kill raised: %s", exc)
        if ep.lease is not None:
            self._broker.release(ep.lease.lease_id)
        try:
            self._registry.unregister(_OWN_MODEL_ID)
        except Exception as exc:  # pragma: no cover - best-effort
            logger.debug("comfy: registry.unregister failed: %s", exc)
        if ep in self._owned:
            self._owned.remove(ep)
        logger.info("comfy: shut down %s", ep.endpoint)
        return True

    def stop_all(self) -> int:
        count = 0
        for ep in list(self._owned):
            if self.shutdown(ep):
                count += 1
        return count
