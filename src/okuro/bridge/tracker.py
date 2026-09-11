# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Usage tracking: invocation counts, providers, durations.
# index: imports | attribution | def log_invocation | def get_usage
# AGENT_HEADER_END -->
"""Usage tracking: invocation counts, providers, durations."""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
from pathlib import Path
from okuro.db.engine import okuro_home

USAGE_DIR = okuro_home() / "bridge"
USAGE_FILE = USAGE_DIR / "usage.jsonl"

# --- Attribution ------------------------------------------------------------
# Who caused this call. Every row in usage.jsonl used to be an orphan: you could
# see that something spent an opus call at 04:00, but never what. That made the
# daemon registry's declared `kind`/`tier` unverifiable — a task could quietly
# start calling a model and nothing would contradict its "script" label.
#
# The bridge owns this slot rather than the daemon so the dependency points the
# right way (bridge knows nothing about daemon tasks) and so any other caller —
# the orchestrator, the MCP layer — can stamp itself the same way later.
#
# A ContextVar, not os.environ: the daemon runs up to 4 handlers concurrently in
# one process, so a process-global would cross-contaminate. (invoke.py already
# mutates os.environ["MAX_THINKING_TOKENS"] process-wide — a latent race on that
# same pool, untouched here.)
_current_caller: ContextVar[str | None] = ContextVar("okuro_bridge_caller", default=None)


@contextmanager
def attributed_to(caller: str):
    """Stamp every bridge call made in this context with *caller*.

    Must be entered ON the thread that runs the work — a ContextVar set on the
    submitting thread is not copied into a ThreadPoolExecutor worker.
    """
    token = _current_caller.set(caller)
    try:
        yield
    finally:
        # Pool threads are reused; without the reset the next task on this
        # thread would inherit the previous task's name.
        _current_caller.reset(token)


def current_caller() -> str | None:
    """The caller attributed to the current context, if any."""
    return _current_caller.get()


def log_invocation(
    provider: str,
    model: str,
    duration: float,
    success: bool,
    capability: str | None = None,
) -> dict:
    """Log an invocation to the usage file."""
    USAGE_DIR.mkdir(parents=True, exist_ok=True)

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model,
        "capability": capability,
        "duration": duration,
        "success": success,
        # None for calls made outside an attributed context (interactive use,
        # engine subprocesses — which have their own process and so never see
        # this ContextVar). Readers must treat absence as "unknown", never as
        # "nobody".
        "caller": _current_caller.get(),
    }

    with open(USAGE_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")

    return entry


def get_usage(period: str = "today") -> dict:
    """Get usage stats for a period (today, week, month, all)."""
    if not USAGE_FILE.exists():
        return {"period": period, "invocations": 0, "by_provider": {}}

    now = datetime.now(timezone.utc)
    cutoff_map = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "week": now - timedelta(days=7),
        "month": now - timedelta(days=30),
        "all": datetime.min.replace(tzinfo=timezone.utc),
    }
    cutoff = cutoff_map.get(period, cutoff_map["today"])

    entries = []
    with open(USAGE_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts = datetime.fromisoformat(entry["ts"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    entries.append(entry)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

    by_provider = {}
    for e in entries:
        prov = e.get("provider", "unknown")
        if prov not in by_provider:
            by_provider[prov] = {"count": 0, "total_duration": 0.0}
        by_provider[prov]["count"] += 1
        by_provider[prov]["total_duration"] = round(
            by_provider[prov]["total_duration"] + e.get("duration", 0), 2
        )

    return {
        "period": period,
        "invocations": len(entries),
        "by_provider": by_provider,
    }
