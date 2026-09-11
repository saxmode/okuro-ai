# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Text-serve — the wired trigger that turns a pulled LLM bundle into a
#          live endpoint you can prompt. Reuse a warm engine (engine_registry)
#          or start one under a broker lease (EngineRunner), keep it warm, and
#          run a prompt through it (invoke_local). This is the missing link
#          between "model installed" and "test its text output" in Studio.
# index:
#   def list_text_bundles     (installed LLM bundles + warm flag)
#   def _pick_gpu             (GPU with the most free VRAM)
#   def ensure_engine         (reuse-or-start, keep warm; returns endpoint)
#   def generate_text         (prompt -> text via the served endpoint)
# AGENT_HEADER_END -->
"""Serve + prompt a local LLM bundle.

The full stack already exists — Broker (VRAM leases), EngineRunner (spawns a
llama-server/vLLM OpenAI server), EngineRegistry (model_id → endpoint),
invoke_local (prompt → text). What was missing is a call site that starts an
engine for a *pulled bundle* on demand and keeps it warm. That is this module.

Engines are kept running after first use (the registry + broker heartbeat hold
them warm) so a second prompt is fast; the broker reaps them when idle or when
VRAM is needed. A module-level ref to each runner keeps its heartbeat thread
alive for the orchestrator process lifetime.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

# Keep started runners referenced so their daemon heartbeat threads (and thus
# the broker lease) survive past the request that started them.
_RUNNERS: dict = {}
_START_LOCK = threading.Lock()

# Engines this module knows how to serve as OpenAI-compatible text servers.
_TEXT_ENGINES = ("llama-server", "vllm", "llama-cpp-python")


def _is_text_bundle(b) -> bool:
    return (b.format or "").lower() == "gguf" or (b.engine in _TEXT_ENGINES)


def list_text_bundles() -> list[dict]:
    """Installed LLM bundles, each flagged warm if an engine is already live."""
    from okuro.ai_models.bundle import scan_bundles
    from okuro.inference.engine_registry import EngineRegistry

    from okuro.ai_models import licensing

    reg = EngineRegistry()
    out: list[dict] = []
    for b in scan_bundles():
        if not _is_text_bundle(b):
            continue
        verdict = licensing.commercial_status(getattr(b, "license", None) or {})
        out.append(
            {
                "bundle_id": b.id,
                "display_name": b.display_name or b.id,
                "parameters": b.parameters,
                "engine": b.engine,
                "warm": reg.resolve(b.id) is not None,
                "commercial_status": verdict["status"],
                "commercial_allowed": verdict["allowed"],
                "license_id": verdict["license_id"],
            }
        )
    out.sort(key=lambda m: m["display_name"].lower())
    return out


def running_engines() -> list[dict]:
    """Text engines currently loaded + serving (registry is self-healing, so a
    dead-pid row is dropped on read). Gives the UI a live 'what's on the GPU'
    overview: model, endpoint, GPU, tier, uptime."""
    import time

    from okuro.inference.engine_registry import EngineRegistry

    now = time.time()
    out: list[dict] = []
    for row in EngineRegistry().list_running():
        mid = row.get("model_id", "")
        if mid == "comfyui":  # image runtime, not a text engine
            continue
        started = float(row.get("started_at", 0) or 0)
        out.append(
            {
                "model_id": mid,
                "endpoint": row.get("endpoint"),
                "gpu_index": row.get("gpu_index"),
                "tier": row.get("tier"),
                "uptime_s": round(now - started, 1) if started else None,
            }
        )
    return out


def _load_bundle(bundle_id: str):
    from okuro.ai_models.bundle import scan_bundles

    b = next((x for x in scan_bundles() if x.id == bundle_id), None)
    if b is None:
        raise ValueError(f"unknown bundle: {bundle_id}")
    return b


def _gpus_by_free() -> list[int]:
    """Non-display GPU indices, most-free VRAM first."""
    from okuro.capability import capabilities
    from okuro.inference.probe import read_gpu_used_mb

    scored: list[tuple[float, int]] = []
    for g in capabilities().get("gpus", []):
        if g.get("is_display"):
            continue
        idx = int(g.get("index", 0))
        free = float(g.get("vram_gb", 0) or 0) * 1024.0 - read_gpu_used_mb(idx)
        scored.append((free, idx))
    scored.sort(reverse=True)
    return [idx for _, idx in scored] or [0]


def stop_engine(bundle_id: str) -> bool:
    """Stop a running text engine and free its REAL VRAM (kill the process),
    release the broker lease, and unregister — works even for an engine started
    by another process (registry-based fallback). Broker eviction alone does
    NOT kill the process, so unloading must go through here."""
    import os
    import signal
    import time as _t

    # Clean path: an engine this process started.
    runner = _RUNNERS.pop(bundle_id, None)
    if runner is not None and runner.stop(bundle_id):
        return True

    # Cross-process: kill by the registry's pid, release its lease, unregister.
    from okuro.inference.broker import Broker
    from okuro.inference.engine_registry import EngineRegistry

    reg = EngineRegistry()
    row = next((r for r in reg.list_running() if r.get("model_id") == bundle_id), None)
    if row is None:
        return False
    pid = row.get("pid")
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
            for _ in range(30):  # wait up to ~3s for the process (and VRAM) to go
                _t.sleep(0.1)
                try:
                    os.kill(int(pid), 0)
                except ProcessLookupError:
                    break
        except ProcessLookupError:
            pass
        except Exception:  # noqa: BLE001
            pass
    if row.get("lease_id"):
        try:
            Broker().release(row["lease_id"])
        except Exception:  # noqa: BLE001
            pass
    reg.unregister(bundle_id)
    return True


def _text_engines_on(gpu_index: int, *, exclude: str) -> list[str]:
    """Other studio text engines on a GPU, oldest (longest uptime) first."""
    engs = [
        e for e in running_engines()
        if e.get("gpu_index") == gpu_index and e.get("model_id") != exclude
    ]
    engs.sort(key=lambda e: e.get("uptime_s") or 0, reverse=True)
    return [e["model_id"] for e in engs]


def ensure_engine(
    bundle_id: str,
    *,
    on_status: Optional[Callable[[str], None]] = None,
) -> tuple[str, bool]:
    """Return a live endpoint for ``bundle_id``; start one if none is warm.

    Returns ``(endpoint, started_now)``. Serialized so two concurrent prompts
    for the same bundle don't both spawn a server (the loser reuses the winner).
    """
    from okuro.inference.engine_registry import EngineRegistry

    reg = EngineRegistry()
    ep = reg.resolve(bundle_id)
    if ep:
        return ep, False

    with _START_LOCK:
        ep = reg.resolve(bundle_id)  # recheck — another request may have won
        if ep:
            return ep, False

        b = _load_bundle(bundle_id)
        if on_status:
            on_status(f"Loading {b.display_name or b.id} onto GPU…")

        from okuro.inference.broker import Broker, BrokerRejection
        from okuro.inference.engine import WARM, EngineRunner, EngineStartError

        def _spawn(gpu: int):
            runner = EngineRunner(Broker())
            # WARM (evictable) not the LLM default PINNED — a test model must
            # never permanently squat VRAM. We free VRAM ourselves (stop_engine),
            # since broker eviction wouldn't kill the process.
            eng = runner.start(b, gpu_index=gpu, tier=WARM, caller="studio-text")
            _RUNNERS[bundle_id] = runner
            return eng.endpoint

        last_err: Exception | None = None
        for gpu in _gpus_by_free():
            try:
                return _spawn(gpu), True
            except EngineStartError:
                ep = reg.resolve(bundle_id)  # raced up in parallel — reuse
                if ep:
                    return ep, False
                raise
            except BrokerRejection as exc:
                last_err = exc
                # Free real VRAM: unload other test engines on this GPU (oldest
                # first), then retry once. This is what makes "load a second
                # model" work on a full card.
                others = _text_engines_on(gpu, exclude=bundle_id)
                if not others:
                    continue
                for other in others:
                    if on_status:
                        on_status(f"Freeing VRAM — unloading {other.split('.')[-2] if '.' in other else other}…")
                    stop_engine(other)
                try:
                    return _spawn(gpu), True
                except BrokerRejection as exc2:
                    last_err = exc2
                    continue
        raise last_err or RuntimeError("no GPU could admit the model")


def generate_text(
    bundle_id: str,
    prompt: str,
    *,
    system_prompt: Optional[str] = None,
    temperature: float = 0.7,
    timeout: int = 120,
    on_status: Optional[Callable[[str], None]] = None,
) -> dict:
    """Ensure the bundle is served, then run one prompt → text.

    Returns ``{success, text, error, latency_s, endpoint, engine, warm_start}``.
    """
    from okuro.bridge.local import invoke_local

    endpoint, started = ensure_engine(bundle_id, on_status=on_status)
    if on_status:
        on_status("Generating…")

    t0 = time.time()
    r = invoke_local(
        endpoint,
        bundle_id,
        prompt,
        system_prompt=system_prompt,
        temperature=temperature,
        timeout=timeout,
    )
    return {
        "success": bool(r.get("success")),
        "text": r.get("output") or "",
        "error": r.get("error"),
        "latency_s": round(time.time() - t0, 2),
        "endpoint": endpoint,
        "warm_start": not started,
    }


def chat_stream(
    bundle_id: str,
    messages: list,
    *,
    temperature: float = 0.7,
    on_status: Optional[Callable[[str], None]] = None,
    on_token: Optional[Callable[[str], None]] = None,
) -> dict:
    """Ensure the bundle is served, then stream a multi-turn chat completion.

    Calls ``on_status`` while the engine loads and ``on_token`` for each token
    delta as it arrives. Returns ``{success, text, error, latency_s, endpoint,
    warm_start}`` — ``text`` is the full accumulated reply.
    """
    import time

    from okuro.bridge.local import stream_chat

    endpoint, started = ensure_engine(bundle_id, on_status=on_status)
    if on_status:
        on_status("Generating…")

    t0 = time.time()
    chunks: list[str] = []
    try:
        for delta in stream_chat(endpoint, bundle_id, messages, temperature=temperature):
            chunks.append(delta)
            if on_token:
                on_token(delta)
        return {
            "success": True,
            "text": "".join(chunks),
            "error": None,
            "latency_s": round(time.time() - t0, 2),
            "endpoint": endpoint,
            "warm_start": not started,
        }
    except Exception as exc:  # noqa: BLE001 — surface a mid-stream failure
        return {
            "success": False,
            "text": "".join(chunks),
            "error": str(exc),
            "latency_s": round(time.time() - t0, 2),
            "endpoint": endpoint,
            "warm_start": not started,
        }


__all__ = [
    "list_text_bundles",
    "running_engines",
    "ensure_engine",
    "generate_text",
    "chat_stream",
    "stop_engine",
]
