# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.progress — ContextVar progress sink so long renders
#   (podcast synth) can report stage + fraction WITHOUT changing channel
#   signatures. The background media-job runner sets a sink in its own thread;
#   the channel calls report() as it works. No sink set -> no-op.
# index: imports | def set_sink | def reset_sink | def report
# AGENT_HEADER_END -->
"""Optional progress reporting for the delivery pipeline.

A render channel can call ``report(stage, frac)`` to surface progress. It only
does anything when a caller has installed a sink via ``set_sink`` in the SAME
thread (ContextVars don't cross threads, which is exactly what we want — the
background job runner sets the sink, then calls delivery.send synchronously in
that thread, so the channel's report() reaches the right job). Synchronous
callers that never set a sink are completely unaffected.
"""

from __future__ import annotations

import contextvars
from typing import Callable, Optional

_SINK: contextvars.ContextVar = contextvars.ContextVar(
    "okuro_delivery_progress", default=None
)


def set_sink(cb: Optional[Callable[[str, float], None]]):
    """Install a progress callback for the current context. Returns a token."""
    return _SINK.set(cb)


def reset_sink(token) -> None:
    try:
        _SINK.reset(token)
    except (ValueError, LookupError):
        pass


def report(stage: str, frac: float) -> None:
    """Report progress to the active sink (no-op if none). frac clamped 0..1."""
    cb = _SINK.get()
    if cb is None:
        return
    try:
        cb(stage, max(0.0, min(1.0, float(frac))))
    except Exception:
        pass
