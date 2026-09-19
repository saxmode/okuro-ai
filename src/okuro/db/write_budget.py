# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Fairness budget over okuro's single SQLite writer lock — stops one bulk writer starving every other.
# index: DEFAULT_DUTY | DEFAULT_BURST | class WriteBudget | def budget | def charge | def unpaced
# AGENT_HEADER_END -->
"""A duty-cycle budget over the writer lock, charged in lock-held SECONDS.

THE FAILURE THIS EXISTS TO PREVENT, AND WHY THE OBVIOUS FIX MISSES IT.

``okuro trace index --force`` over the real corpus put a supervised daemon
task into a ``database is locked`` restart loop. The obvious reading is "one
transaction is too long" — and it was wrong. Measured against a sandbox copy
of the real corpus (2126 files, 395k events), with a contending writer doing
one small INSERT per second:

    transactions                1849
    longest SINGLE hold         2.07 s      (busy_timeout is 30 s)
    wall / lock held            777.3 s / 755.7 s
    DUTY CYCLE                  97.2%
    median gap between holds    4.1 ms
    contender                   2 x OperationalError after 30.030 s

No single hold came close to the timeout. The lock was simply never free
long enough to be taken: SQLite's busy handler backs off to 100 ms sleeps,
and the gaps were 4 ms, so a waiter slept straight through thousands of
them and still timed out. Bounding transaction LENGTH does not touch this.
What has to be bounded is the FRACTION of wall time any writer holds.

WHY THE BUDGET LIVES HERE AND NOT IN THE WALKERS.

An earlier version of this fix paced each trace ingester's file loop. Three
things were wrong with that, and all three are structural:

* It measured wall time between files as a PROXY for lock time, because a
  walker cannot see the lock. ``write()`` can: it brackets BEGIN IMMEDIATE
  and COMMIT, so it knows the real number and needs no proxy.
* It does not compose. Four walkers each politely capped at 50% still add
  up to 1 - 0.5**4 = 94% duty between them. One budget at the lock stays at
  its target no matter how many writers are running.
* It had to be remembered. A fifth provider, the cortex indexer
  (``cortex/vectorstore.py``), the vec rebuild (``embed/repair.py``) and the
  distill clusterer (``sense/distill/corpus.py``) all walk large corpora
  through this same lock; every one of them would have had to opt in.

THE BURST IS WHAT MAKES THIS SAFE TO LEAVE ON GLOBALLY.

Pacing every write would tax interactive work that never starves anything.
A token bucket does not: a caller may spend ``DEFAULT_BURST`` seconds of
lock time freely, and only a writer that stays hot long enough to drain that
allowance ever pays. Every interactive okuro write measured here is orders
of magnitude inside it — a bootstrap, a memory write, an MCP tool call
commits in single-digit milliseconds. Sustained bulk work is the only thing
the budget can see, which is the only thing it should see.

``migrate()`` does NOT go through ``write()`` — it manages its own
statements behind the cross-process migrate lock — so deploys are not paced.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time

# Share of wall time a writer may hold the lock once its burst is spent.
DEFAULT_DUTY = 0.5

# Seconds of lock time spendable before the duty cap starts applying. Sized
# well above any interactive write and well below a bulk walk.
DEFAULT_BURST = 5.0

ENV_DUTY = "OKURO_WRITE_DUTY"     # 1.0 disables pacing entirely
ENV_BURST = "OKURO_WRITE_BURST"


def _env_float(name: str, fallback: float, low: float, high: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        return fallback
    # Clamp rather than raise. A typo in an env var must not be able to take
    # out every write in the process, and 0 would mean "wait forever".
    return min(max(value, low), high)


class WriteBudget:
    """Token bucket denominated in seconds of writer-lock time.

    Thread-safe: okuro opens one connection per thread but they contend for
    the same file lock, so the budget has to be shared across them.
    """

    def __init__(
        self,
        duty: float | None = None,
        burst: float | None = None,
        clock=time.monotonic,
    ):
        self.duty = duty if duty is not None else _env_float(
            ENV_DUTY, DEFAULT_DUTY, 0.01, 1.0
        )
        self.burst = burst if burst is not None else _env_float(
            ENV_BURST, DEFAULT_BURST, 0.0, 3600.0
        )
        # Injectable so the steady state can be asserted on a fake clock
        # instead of on a real one, which would make the test a stopwatch.
        self._clock = clock
        self._allowance = self.burst
        self._last = clock()
        self._lock = threading.Lock()
        # Observability: a wrong duty must be tellable from a slow disk.
        self.total_held = 0.0
        self.total_slept = 0.0
        self.charges = 0

    def charge(self, held: float) -> float:
        """Account ``held`` seconds of lock time; return seconds to sleep.

        The caller sleeps AFTER it has committed and released the lock —
        sleeping inside the transaction would be the very starvation this
        is here to prevent, delivered on purpose.
        """
        if self.duty >= 1.0:
            return 0.0
        with self._lock:
            now = self._clock()
            # Refill for the wall time since the last charge, which covers
            # both this hold and whatever sleep preceded it.
            self._allowance = min(
                self.burst, self._allowance + (now - self._last) * self.duty
            )
            self._last = now
            self._allowance -= held
            self.total_held += held
            self.charges += 1
            if self._allowance >= 0:
                return 0.0
            # Deliberately do NOT zero the allowance: sleeping this long
            # refills it back to ~0 through the normal path above, which is
            # what keeps the steady state at exactly `duty`.
            pause = -self._allowance / self.duty
            self.total_slept += pause
            return pause

    def stats(self) -> dict:
        with self._lock:
            elapsed = self._clock() - (self._last - self.total_held)
            return {
                "duty_target": self.duty,
                "burst": self.burst,
                "charges": self.charges,
                "held_s": round(self.total_held, 3),
                "slept_s": round(self.total_slept, 3),
                "effective_duty": round(self.total_held / elapsed, 4)
                if elapsed > 0
                else None,
            }


_budget = WriteBudget()
_budget_lock = threading.Lock()


def budget() -> WriteBudget:
    return _budget


def reset(duty: float | None = None, burst: float | None = None,
          clock=time.monotonic) -> WriteBudget:
    """Replace the process budget. For tests and for re-reading the env."""
    global _budget
    with _budget_lock:
        _budget = WriteBudget(duty=duty, burst=burst, clock=clock)
    return _budget


def charge(held: float) -> float:
    return _budget.charge(held)


@contextlib.contextmanager
def unpaced():
    """Suspend pacing for a block that must not be slowed.

    Nothing in okuro uses this today; it exists so that a future
    latency-critical writer has a sanctioned escape rather than a reason to
    reach past ``write()``.
    """
    b = budget()
    previous, b.duty = b.duty, 1.0
    try:
        yield
    finally:
        b.duty = previous
