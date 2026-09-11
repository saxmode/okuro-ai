# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Identify the unknown-tail VRAM holder — the bare "python3" holding
#          GBs that matched no provider signature. Extended deterministic labels
#          first (game-stream, display server, training, okuro-embed, other
#          inference UIs); a fast-tier model only for the genuine unknown. This
#          is where a model earns its place — the ambiguous 10%, not the certain.
# index:
#   def identify_process
#   def _model_identify
# AGENT_HEADER_END -->
"""Read-only identification of an unclassified GPU process.

The provider inventory + tenant classifier cover the certain cases; this labels
what's left so the contention modal can say WHAT a holder is (and whether it
even looks like something worth unloading) instead of just "python3". Uses a
fast-draft model for the residue — injectable so tests never call one.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

# label · cmdline/name needles · is it a GPU inference server (reclaim candidate)?
_KNOWN_LABELS = [
    ("game-stream (Sunshine)", ("sunshine", "moonlight"), False),
    ("display server", ("xorg", "xwayland", "gnome-shell", "kwin", "/usr/lib/xorg"), False),
    ("okuro embeddings", ("okuro-embed", "okuro.embed"), False),
    ("okuro daemon", ("okuro-daemon", "okuro.daemon"), False),
    ("jupyter / notebook", ("jupyter", "ipykernel"), False),
    ("training run", ("torchrun", "accelerate launch", "deepspeed", "train.py"), False),
    ("stable-diffusion webui", ("stable-diffusion-webui", "a1111", "sd.next"), True),
    ("text-generation-webui", ("text-generation-webui", "oobabooga"), True),
    ("kobold", ("koboldcpp", "kobold"), True),
]

_ID_SYS = (
    "Identify a Linux process from its command line. Decide if it is a GPU/LLM "
    "or image inference server. Reply STRICT JSON only: {\"label\": short human "
    "name, \"is_inference\": true|false, \"reclaim_hint\": "
    "\"graceful_api\"|\"process_stop\"|\"none\"}."
)


def identify_process(
    pid: int,
    name: str = "",
    cmdline: str = "",
    *,
    invoke_fn: Optional[Callable] = None,
) -> dict:
    """Label an unclassified process → {label, is_inference, reclaim_hint,
    confidence, source}. Deterministic patterns first; a fast model only when
    nothing matches."""
    hay = f"{cmdline} {name}".lower()
    for label, needles, is_inf in _KNOWN_LABELS:
        if any(n in hay for n in needles):
            return {
                "label": label,
                "is_inference": is_inf,
                "reclaim_hint": "process_stop" if is_inf else "none",
                "confidence": 0.9,
                "source": "pattern",
            }
    return _model_identify(cmdline or name, invoke_fn)


def _model_identify(text: str, invoke_fn: Optional[Callable]) -> dict:
    """Ask a fast-draft model to identify the process. Degrades to 'unknown'."""
    fallback = {"label": (text.split()[0] if text else "unknown")[:60],
                "is_inference": False, "reclaim_hint": "none",
                "confidence": 0.0, "source": "unknown"}
    if not text:
        return fallback
    if invoke_fn is None:
        from okuro.bridge import invoke as invoke_fn
    try:
        r = invoke_fn(f"Command line:\n{text}\n\nWhat is this process?",
                      capability="fast-draft", system_prompt=_ID_SYS, timeout=30)
        if r.get("success") and r.get("output"):
            m = re.search(r"\{.*\}", r["output"], re.DOTALL)
            if m:
                d = json.loads(m.group(0))
                if d.get("label"):
                    return {
                        "label": str(d["label"])[:60],
                        "is_inference": bool(d.get("is_inference")),
                        "reclaim_hint": d.get("reclaim_hint", "none"),
                        "confidence": 0.5,
                        "source": "model",
                    }
    except Exception:
        pass
    return fallback
