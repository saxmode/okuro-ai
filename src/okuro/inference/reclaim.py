# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: VRAM reclaim — when okuro can't lease because an EXTERNAL server holds
#          the GPU, name the blocker (tenants_report × provider inventory), tell
#          the user what can be done, and execute the reclaim THEY choose. There
#          is no generic OS reclaim of a foreign process's VRAM: graceful unload
#          via the server's own API, or stop the process. Never automatic.
# index:
#   def plan_reclaim        (structured blocked report for the modal)
#   def to_signal           (blocked report → proactive-signal payload)
#   def execute_reclaim     (user-initiated: graceful_api | process_stop)
#   def _stop_process       (platform-agnostic SIGTERM / taskkill)
# AGENT_HEADER_END -->
"""Reclaim occupied VRAM — inform first, act only on the user's click.

``plan_reclaim`` cross-references who holds a GPU (broker.tenants_report) with
how each holder can be reclaimed (inventory.detect_providers) → the payload the
contention modal renders. ``execute_reclaim`` runs the single action the user
picked: a graceful unload HTTP call (ComfyUI ``/free``, Ollama ``stop``) that
keeps the server up, or — for servers with no unload (vLLM, llama-server) — a
process stop, which is destructive and must be confirmed. All effects are
injectable so nothing is killed in a test.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import urllib.request
from typing import Callable, Optional


def plan_reclaim(gpu_index: int, needed_gb: float, *, broker=None, providers=None) -> dict:
    """Structured 'GPU blocked' report: who holds it, how much, and the reclaim
    action available for each holder. ``can_reclaim`` is True iff at least one
    holder has a known (graceful or process) method."""
    if broker is None:
        from .broker import Broker

        broker = Broker()
    if providers is None:
        from .inventory import detect_providers

        providers = detect_providers()
    from .inventory import reclaim_for_tenant

    by_name = {p.provider: p for p in providers}
    gpu = broker.tenants_report(gpu_index).get(gpu_index, {})
    holders = []
    for ext in gpu.get("external", []):
        prov = by_name.get(ext.get("tenant"))
        endpoint = prov.endpoint if prov else None
        holders.append({
            "tenant": ext.get("tenant"),
            "pid": ext.get("pid"),
            "vram_mb": ext.get("used_mb"),
            "reclaim": reclaim_for_tenant(ext.get("tenant"), endpoint),
            "endpoint": endpoint,
            "models": prov.loaded_models if prov else [],
        })
    return {
        "gpu_index": gpu_index,
        "needed_gb": round(needed_gb, 1),
        "smi_free_gb": gpu.get("smi_free_gb"),
        "holders": holders,
        "can_reclaim": any(h["reclaim"] != "none" for h in holders),
    }


def to_signal(plan: dict) -> dict:
    """Blocked report → signal_add(...) kwargs (proactive, warn)."""
    names = ", ".join(f"{h['tenant']}({h['vram_mb']}MB)" for h in plan["holders"]) or "unknown"
    return {
        "source": "proactive",
        "severity": "warn",
        "summary": f"GPU{plan['gpu_index']} blocked — okuro needs {plan['needed_gb']}GB; "
                   f"held by {names}",
        "source_ref": f"gpu-blocked:{plan['gpu_index']}",
        "evidence": plan,
        "suggested_action": "Unload a holder (okuro models gpu) or free VRAM to let okuro run.",
    }


def file_blocked_signal(plan: dict, *, signal_add_fn=None, existing_refs=None):
    """File the blocked report as a proactive signal, deduped on the GPU ref so
    a retry loop doesn't spam the inbox. Returns the new signal row or None."""
    ref = f"gpu-blocked:{plan['gpu_index']}"
    if existing_refs is None:
        try:
            from okuro.sense.signals import signal_list

            existing_refs = {s.get("source_ref") for s in signal_list(status="open", limit=200)}
        except Exception:
            existing_refs = set()
    if ref in existing_refs:
        return None
    if signal_add_fn is None:
        from okuro.sense.signals import signal_add as signal_add_fn
    return signal_add_fn(**to_signal(plan))


def execute_reclaim(
    holder: dict,
    *,
    http_post: Optional[Callable] = None,
    run_cmd: Optional[Callable] = None,
    killer: Optional[Callable] = None,
) -> dict:
    """Run the reclaim the user chose for one holder. Injectable effects.

    graceful_api  → ComfyUI ``/free`` or ``ollama stop <model>`` (server stays up)
    process_stop  → stop the process (destructive; caller must have confirmed)
    none          → nothing safe to do automatically; unload manually
    """
    method = holder.get("reclaim")
    tenant = holder.get("tenant")

    if method == "graceful_api":
        if tenant == "comfyui" and holder.get("endpoint"):
            return _post(f"{holder['endpoint']}/free",
                         {"unload_models": True, "free_memory": True}, http_post)
        if tenant == "ollama":
            models = holder.get("models") or []
            if not models:
                return {"ok": False, "reason": "ollama: no loaded model to unload"}
            endpoint = holder.get("endpoint")
            if endpoint:
                # Verified: keep_alive=0 on /api/generate unloads immediately
                # (docs.ollama.com/faq). HTTP path works remotely; no CLI needed.
                res = [_post(f"{endpoint}/api/generate", {"model": m, "keep_alive": 0}, http_post)
                       for m in models]
                return {"ok": all(r.get("ok") for r in res), "action": "ollama keep_alive=0",
                        "models": models}
            run = run_cmd or _run  # no endpoint → local CLI fallback
            res = [run(["ollama", "stop", m]) for m in models]
            return {"ok": all(r.get("ok") for r in res), "action": "ollama stop", "models": models}
        return {"ok": False, "reason": f"no graceful handler for {tenant}"}

    if method == "process_stop":
        pid = holder.get("pid")
        if not pid:
            return {"ok": False, "reason": "no pid"}
        return _stop_process(int(pid), killer)

    return {"ok": False, "reason": "no known unload method — unload it manually"}


def _post(url: str, payload: dict, http_post: Optional[Callable]) -> dict:
    if http_post is not None:
        return http_post(url, payload)
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"ok": 200 <= resp.status < 300, "action": "graceful_api", "url": url}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def _run(argv: list) -> dict:
    try:
        subprocess.run(argv, check=True, capture_output=True, timeout=15)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def _stop_process(pid: int, killer: Optional[Callable] = None) -> dict:
    """Stop a process cross-platform: SIGTERM (POSIX) / taskkill (Windows)."""
    if killer is not None:
        return killer(pid)
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           check=True, capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
        return {"ok": True, "action": "process_stop", "pid": pid}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}
