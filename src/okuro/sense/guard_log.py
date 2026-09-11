# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: One place to record a deliberately-swallowed guard failure, so a
#   permanently-broken best-effort check stops being invisible.
# index:
#   imports
#   def guard_failed
# AGENT_HEADER_END -->
"""Recording for guards that swallow their own failures.

okuro's middleware is full of best-effort checks wrapped in a bare
``except Exception``, and that is correct: a freshness probe or a telemetry
stamp must never break tool dispatch. Every one of them was also SILENT, which
makes a permanently-failing guard indistinguishable from a guard with nothing
to catch — the assigned-role gate read the wrong source for weeks and nothing
reported it, because an unarmed gate looks exactly like a quiet one.

debug level, never warning: these fire on paths where failure is expected and
handled (cortex absent, no orchestrator state on this box, no profile yet). The
point is that ``OKURO_LOG_LEVEL=DEBUG`` can answer "is this guard running?"
without a debugger, not to add noise to normal operation.

Its own body is wrapped too. A logger that raises inside an exception handler
would convert a swallowed failure into a real one, which is the single thing
this file exists to prevent.
"""

import logging

logger = logging.getLogger("okuro.sense.guards")


def guard_failed(guard: str, exc: BaseException) -> None:
    """Record a swallowed guard failure. Never raises — it is itself a guard."""
    try:
        logger.debug("guard %s failed: %r", guard, exc)
    except Exception:  # noqa: BLE001 — see the module docstring
        pass
