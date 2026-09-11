# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: One observable resolver for "requested id -> allowed id", so a substituted
#   default is never indistinguishable from a chosen value.
# index:
#   class Resolution
#   def resolve
#   def coerce
#   def substitutions
# AGENT_HEADER_END -->
"""Resolve a requested id against an allowed set — and say when you did not.

THE CLASS THIS EXISTS FOR. `x if x in ALLOWED else DEFAULT` returns a value that
carries no trace of having been substituted. The caller cannot tell "you asked
for okuro" from "you asked for meridian and I gave you okuro", the log cannot
either, and neither can a test. It fails in three directions at once:

  · BEHAVIOUR — a renamed or mistyped id degrades silently. Measured
    2026-08-08: the A4 board kit's per-brand height coefficients were re-keyed,
    so a deck asking for a now-unknown brand fell through to okuro's 10.285
    char-width instead of the 8.722 it was calibrated for. An 18% error in
    every height estimate, no warning anywhere.
  · DIAGNOSIS — the brain records DEFAULT-AS-RUNTIME-FACT as okuro's dominant
    agent error class, six occurrences in one session, and names the structural
    cause: "every unlogged resolved value is a place a future agent can assert
    a default unchallenged". An agent greps a fallback and reports it as what
    ran, because nothing in the run can contradict it.
  · REMEDY — that same record warns that any fix relying on the author
    remembering to log is already falsified. So this is not a convention. It is
    a function, and `tests/test_silent_coercion.py` fails the build when a new
    hand-rolled site appears.

NOT EVERY COERCION IS A DEFECT, and this module does not pretend otherwise.
Clamping a free-text severity to `med` is fine — the input was never an
identity. What is not fine is coercing something a caller CHOSE: a brand, a
provider, an engine, a delivery channel. `resolve` takes `severity=` so the
distinction is stated at the call site rather than argued about later.

USAGE

    brand = coerce(requested, a4_brands(), DEFAULT_BRAND, what="prism.brand")

`coerce` is the drop-in: same return type as the ternary it replaces. Reach for
`resolve` when the caller wants to branch on whether a substitution happened —
that is the whole point of it being visible.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Every substitution this process has made, newest last: (what, requested).
#: Bounded, because a hot loop resolving junk must not become a memory leak —
#: the first occurrence is the informative one and repeats add nothing.
_MAX_RECORDED = 256
_seen: set[tuple[str, str]] = set()
_order: list[tuple[str, str]] = []
_lock = threading.Lock()


@dataclass(frozen=True)
class Resolution:
    """What was asked for, what came back, and whether those differ."""

    value: str
    requested: str | None
    substituted: bool
    what: str

    def __str__(self) -> str:  # so a stray f-string still reads correctly
        return self.value


def _record(what: str, requested: str) -> bool:
    """Remember this substitution. True if it is the first of its kind."""
    key = (what, requested)
    with _lock:
        if key in _seen:
            return False
        if len(_order) < _MAX_RECORDED:
            _seen.add(key)
            _order.append(key)
        return True


def resolve(
    requested: str | None,
    allowed: Iterable[str],
    default: str,
    *,
    what: str,
    severity: int = logging.WARNING,
) -> Resolution:
    """Resolve `requested` against `allowed`, reporting any substitution.

    `what` names the axis being resolved (`"prism.brand"`, `"bridge.provider"`)
    and appears in the log line and in :func:`substitutions`. It is required
    because "unknown id, using default" with no subject is unactionable.

    `severity` is the log level for a substitution. Drop it to `logging.DEBUG`
    where coercing junk to a default is the DESIGNED behaviour rather than a
    degradation — a free-text severity, say. Stating it at the call site is the
    point: it makes "this fallback is fine" a decision somebody wrote down.
    """
    allowed_set = frozenset(allowed)
    if requested and requested in allowed_set:
        return Resolution(requested, requested, False, what)

    if requested and _record(what, requested):
        log.log(
            severity,
            "%s: %r is not one of %s — using %r. If that id was renamed, the "
            "caller is now getting a different value, not an error.",
            what, requested, sorted(allowed_set)[:8], default,
        )
    return Resolution(default, requested, bool(requested), what)


def coerce(
    requested: str | None,
    allowed: Iterable[str],
    default: str,
    *,
    what: str,
    severity: int = logging.WARNING,
) -> str:
    """:func:`resolve`, returning just the value. The drop-in for the ternary."""
    return resolve(requested, allowed, default, what=what, severity=severity).value


def substitutions() -> tuple[tuple[str, str], ...]:
    """Every distinct (what, requested) this process substituted, in order.

    This is what makes a run able to contradict a claim about itself. A report
    that says "the deck rendered in brand X" can be checked against it.
    """
    with _lock:
        return tuple(_order)


def reset_substitutions() -> None:
    """Test-only: forget what has been recorded."""
    with _lock:
        _seen.clear()
        _order.clear()


__all__ = [
    "Resolution",
    "coerce",
    "reset_substitutions",
    "resolve",
    "substitutions",
]
