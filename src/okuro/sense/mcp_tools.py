# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Sense module tools — bootstrap, memory, thoughts, progress, projects, reminders, people, orchestrator.
# index: imports | def _text | def _parse_when | def _orch_* | def get_tools
# AGENT_HEADER_END -->
"""Sense module tools — bootstrap, memory, thoughts, progress, projects, reminders, people, orchestrator.

Extracted from sense/mcp_server.py for use by the unified okuro.mcp.server.
handle_tool does NOT include compliance middleware (that lives in mcp_middleware).
It DOES include bootstrap session footer logic and date parsing for set_reminder.

Orchestrator wrappers (orchestrator_create / orchestrator_status /
orchestrator_approve / orchestrator_continue) are thin HTTP shims around the
FastAPI handlers in ``okuro.orchestrator.api.main``. We chose in-process HTTP
(stdlib ``urllib.request``) over direct handler imports because:

  * ``create_task`` parses a raw ``Request`` body / multipart form — the
    handler is tightly coupled to FastAPI's ``Request`` shape, so synthesising
    one from the wrapper is brittle.
  * The orchestrator API is the canonical write surface; the same path is
    exercised by the SPA, the CLI, and these MCP tools — bug fixes land once.
  * The daemon owns the spawn lock, rate limit, and bearer-auth middleware —
    bypassing it via direct imports would silently skip those guards.

The cost is one extra TCP round-trip per call. Acceptable given the wrappers
trigger orchestration runs that take seconds-to-minutes anyway.
"""

import json
import logging
import os
import time as _time
from typing import Any

from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)


def _with_dispatch_epoch(arguments: dict) -> dict:
    """Stamp a Stream-B write with the dispatch generation it belongs to (P4.4).

    ``artifact_write`` and ``write_role_handover`` have accepted
    ``dispatch_epoch`` since C12 — it is what lets a straggler write from a
    PRIOR retry be silently superseded instead of reading as current truth to
    the next reviewer. Nothing ever supplied it. The parameter was reachable
    only by a direct Python caller, and every real write comes through this MCP
    handler as ``**arguments`` built from the tool's input schema, which does
    not declare it. So the guard existed and never fired once.

    Injected SERVER-SIDE from the caller's resolved work identity, not added to
    the tool schema, for two reasons. The agent has no way to know its dispatch
    generation — the dispatcher does, and binds it to the session the caller's
    bearer token resolves to. And a schema field would be agent-supplied, which
    makes it a claim rather than a fact: a subagent from a superseded run could
    assert the current epoch and defeat the check it is subject to.

    It used to read this process's environment. Under HTTP MCP that process is
    the shared daemon, which carries no epoch at all, so the fence never fired
    once between C12 and migration 122.

    An explicit value already in ``arguments`` wins, so a direct Python caller
    that knows better is not overridden.
    """
    from okuro.sense.work_identity import resolve_work_identity

    if arguments.get("dispatch_epoch"):
        return arguments
    identity = resolve_work_identity()
    epoch = identity.dispatch_epoch if identity else None
    if not epoch:
        return arguments
    return {**arguments, "dispatch_epoch": epoch}


def _text(data) -> list[TextContent]:
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _project_half_failure_notice(slug: str | None, exc: Exception) -> str:
    """The PROJECT half when assembly raised — a packet, not a stack trace.

    Carries the ``## Role for this session`` heading nothing gates on, and
    deliberately no other section marker: this must never read as a thin but
    normal half. What the agent needs is the three facts below — that project
    context is MISSING, that its own claims are therefore unbacked, and which
    calls recover the pieces one at a time.
    """
    reason = f"{type(exc).__name__}: {exc}"[:400]
    where = f" for project `{slug}`" if slug else ""
    return (
        "# Agent Context — Okuro · PROJECT (FAILED)\n\n"
        "## ⚠ THE PROJECT HALF DID NOT ASSEMBLE\n\n"
        f"Assembling the project half{where} raised:\n\n"
        f"    {reason}\n\n"
        "**Your tools are unlocked anyway.** A gate with no legal move is "
        "worse than a thin briefing — but you are now working WITHOUT project "
        "memory, todos, progress, people, the role assignment and the codebase "
        "map.\n\n"
        "**What this means for what you say.** Anything you would have known "
        "from this half, you do not know. Do not report an absence as a "
        "finding: a memory you cannot see is not a memory that does not "
        "exist.\n\n"
        "**Recover the pieces individually** — each is a separate call, and a "
        "failure in one no longer costs you the others:\n"
        "- `read_memory(query=..., project=...)` — the memory pointers\n"
        "- `todo_list(project=...)` · `get_progress(project=...)`\n"
        "- `roles_match(<your task>)` then `roles_get(<id>)` — the role brief\n"
        "- `cortex_scope()` — the codebase map\n\n"
        "**Report this.** A deterministic failure here means every session on "
        "this project boots blind until it is fixed; the exception is in the "
        "okuro log.\n"
    )


_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}


def _time_of_day(text: str, default_h: int = 9, default_m: int = 0):
    """Parse a clock fragment like '9am', '15:00', '9:30 pm'.

    Returns (hour, minute), or None if the fragment isn't a valid time.
    Empty text returns the supplied default.
    """
    import re
    text = text.strip()
    if not text:
        return default_h, default_m
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return h, mi


def _parse_when(when_str: str):
    """Parse an ISO, absolute, or natural-language date/time string.

    Natural-language forms (matching the set_reminder schema):
      - "in 2 hours" / "in 30 min" / "in 3 days" / "in 1 week"
      - "tomorrow" / "today" / "tonight" [+ time]
      - "next friday" / "monday" [+ time]
      - bare time "9am" / "15:00" (today, or tomorrow if already past)
    Falls back to dateutil / fromisoformat for ISO and absolute dates.
    Naive results are localized to the USER'S timezone, resolved via
    okuro.clock.local_tz (declared profile zone, then detected system zone,
    then UTC). Until 2026-07-28 this constructed a Zurich zone object inline —
    correct on the reference machine and silently wrong on any other, which
    would land every "9am" reminder at the wrong hour. Returns a tz-aware
    datetime or raises ValueError.
    """
    from datetime import datetime, timedelta, timezone  # noqa: F401
    import re

    from okuro.clock import local_tz

    tz = local_tz()
    raw = (when_str or "").strip()
    if not raw:
        raise ValueError("empty 'when' string")
    s = raw.lower()
    now = datetime.now(tz)

    def _localize(dt: datetime) -> datetime:
        return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt

    def _at(date_obj, h: int, mi: int) -> datetime:
        return _localize(datetime(date_obj.year, date_obj.month, date_obj.day, h, mi))

    # "in N <unit>"
    m = re.fullmatch(r"in\s+(\d+)\s*([a-z]+)", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit in ("min", "mins", "minute", "minutes"):
            return now + timedelta(minutes=n)
        if unit in ("h", "hr", "hrs", "hour", "hours"):
            return now + timedelta(hours=n)
        if unit in ("d", "day", "days"):
            return now + timedelta(days=n)
        if unit in ("w", "wk", "wks", "week", "weeks"):
            return now + timedelta(weeks=n)
        # unknown unit — fall through to dateutil

    # "tomorrow" / "today" / "tonight" [+ time]
    for word, day_off in (("tomorrow", 1), ("today", 0), ("tonight", 0)):
        if s == word or s.startswith(word + " "):
            rest = s[len(word):].strip()
            if word == "tonight" and not rest:
                rest = "20:00"
            tod = _time_of_day(rest)
            if tod is not None:
                return _at((now + timedelta(days=day_off)).date(), *tod)
            break  # "tomorrow <garbage>" → let it error below

    # "next <weekday>" / "<weekday>" [+ time]
    m = re.fullmatch(r"(?:(next)\s+)?([a-z]+)(?:\s+(.*))?", s)
    if m and m.group(2) in _WEEKDAYS:
        force_next = bool(m.group(1))
        target_wd = _WEEKDAYS[m.group(2)]
        tod = _time_of_day((m.group(3) or "").strip())
        if tod is not None:
            h, mi = tod
            days_ahead = (target_wd - now.weekday()) % 7
            if days_ahead == 0:
                # Same weekday: jump a week unless the time is still ahead today.
                if force_next or _at(now.date(), h, mi) <= now:
                    days_ahead = 7
            return _at((now + timedelta(days=days_ahead)).date(), h, mi)

    # Bare time of day → next occurrence
    tod = _time_of_day(s)
    if tod is not None:
        cand = _at(now.date(), *tod)
        if cand <= now:
            cand += timedelta(days=1)
        return cand

    # Absolute / ISO fallback
    try:
        from dateutil.parser import parse as parse_dt
        when_due = parse_dt(raw)
    except ImportError:
        when_due = datetime.fromisoformat(raw)
    return _localize(when_due)


# ---------------------------------------------------------------------------
# Orchestrator HTTP shim
# ---------------------------------------------------------------------------
#
# The orchestrator FastAPI lives at $OKURO_ORCHESTRATOR_URL (default
# http://127.0.0.1:13333) and gates every /api/* call behind a bearer token
# stored in the okuro keyring under "okuro/api_token".
#
# These helpers exist so the four MCP tool dispatchers below stay trivial:
# build a request, hit the daemon, return JSON or a friendly error envelope
# explaining how to start the service.

_ORCH_DEFAULT_URL = "http://127.0.0.1:13333"
_ORCH_HEALTH_TIMEOUT_S = 1.5
_ORCH_CALL_TIMEOUT_S = 30.0
_SESSION_REPORT_DEADLINE_S = 3.0


def _run_session_report_with_deadline(reporter, **kwargs) -> str:
    """Run session telemetry behind a short MCP response deadline."""
    import queue
    import threading

    results: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def _target() -> None:
        try:
            results.put_nowait(("ok", reporter(**kwargs)))
        except Exception as exc:
            logger.exception("session_report telemetry write failed")
            results.put_nowait(("error", exc))

    thread = threading.Thread(
        target=_target,
        name="okuro-session-report",
        daemon=True,
    )
    thread.start()
    thread.join(_SESSION_REPORT_DEADLINE_S)

    if thread.is_alive():
        return (
            "Session report accepted. Telemetry write is still running in "
            "the background; MCP response deadline hit after "
            f"{_SESSION_REPORT_DEADLINE_S:.1f}s."
        )

    try:
        status, payload = results.get_nowait()
    except queue.Empty:
        return "Session report accepted, but telemetry returned no result."

    if status == "ok":
        return str(payload)
    return (
        "Session report accepted, but telemetry write failed fast "
        f"({type(payload).__name__}: {payload})."
    )


def _orch_base_url() -> str:
    return os.environ.get("OKURO_ORCHESTRATOR_URL", _ORCH_DEFAULT_URL).rstrip("/")


def _orch_token() -> str | None:
    """Read the API bearer from the keyring (or OKURO_API_TOKEN fallback)."""
    try:
        from okuro.keyring.storage import KeyringStorage
        token = KeyringStorage().get_key("okuro/api_token")
        if token:
            return token
    except Exception:
        pass
    return os.environ.get("OKURO_API_TOKEN")


def _orch_is_running() -> bool:
    """Cheap readiness probe via /api/health (auth-exempt).

    Retries up to 3 times with a short backoff so a uvicorn auto-reload
    window or first-cold-call latency doesn't flip a healthy orchestrator
    to "not running" for the caller. Total wait stays under ~5s.
    """
    import time as _time
    import urllib.request
    import urllib.error
    url = f"{_orch_base_url()}/api/health"
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=_ORCH_HEALTH_TIMEOUT_S) as resp:
                if 200 <= resp.status < 300:
                    return True
                last_exc = RuntimeError(f"health http {resp.status}")
        except (urllib.error.URLError, ConnectionError, OSError, TimeoutError) as e:
            last_exc = e
        if attempt < 2:
            _time.sleep(0.5 * (attempt + 1))
    if last_exc is not None:
        logger.debug("_orch_is_running gave up after retries: %s", last_exc)
    return False


def _orch_not_running_error() -> dict:
    return {
        "error": "orchestrator not running. Run `okuro service start orchestrator`.",
        "url": _orch_base_url(),
    }


def _orch_request(method: str, path: str, body: dict | None = None) -> dict:
    """Issue an authenticated HTTP request to the orchestrator daemon.

    Returns the parsed JSON response on success, or an ``{"error": ...}``
    dict (never raises) on transport / auth / decode failure. Callers must
    surface the error envelope to the agent — silent failure here would
    cause MCP clients to think a task was created when it was not.
    """
    import urllib.request
    import urllib.error

    if not _orch_is_running():
        return _orch_not_running_error()

    token = _orch_token()
    if not token:
        return {
            "error": (
                "no API bearer token available. Run `okuro keys init`, or "
                "set OKURO_API_TOKEN before invoking orchestrator_* tools."
            ),
        }

    url = f"{_orch_base_url()}{path}"
    data = json.dumps(body or {}).encode("utf-8") if method != "GET" else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_ORCH_CALL_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"error": "non-JSON response", "body": raw[:500], "status": resp.status}
    except urllib.error.HTTPError as e:
        try:
            payload = e.read().decode("utf-8")
            parsed = json.loads(payload)
        except Exception:
            parsed = {"detail": payload if "payload" in dir() else str(e)}
        return {"error": f"orchestrator returned HTTP {e.code}", "status": e.code, "detail": parsed}
    except (urllib.error.URLError, ConnectionError, OSError, TimeoutError) as e:
        return {"error": f"orchestrator unreachable: {e}"}


# Mode + intelligence accepted by the orchestrator engine. Listed here so
# the schema strings stay in sync with the FastAPI handler validation.
_ORCH_VALID_MODES = ("plan", "auto-execute", "deliberate")
_ORCH_VALID_INTELLIGENCE = ("low", "balanced", "high", "max")


def _orch_normalize_mode(mode: str) -> str:
    """Map agent-facing ``mode`` onto the engine's mode vocabulary.

    The audit brief uses ``mode="plan"`` to mean "decompose, do not execute"
    — the engine spells that ``deliberate`` (plan + wait for approval at
    every gate). We normalise here so MCP callers don't have to learn the
    legacy vocabulary.
    """
    if mode == "plan":
        return "deliberate"
    return mode


def orchestrator_create(
    description: str,
    mode: str = "plan",
    intelligence: str = "balanced",
    required_roles: list[str] | None = None,
    preferred_cli: str | None = None,
    auto_approve_risk: str = "none",
    verify_command: str | None = None,
    autonomous: bool = True,
    deliberate_continuations: bool | None = None,
    fast_track: bool = False,
    audience: str | None = None,
    delivery_channel: str | None = None,
    delivery_brand_id: str | None = None,
) -> dict:
    """Create a new orchestrator task.

    ``deliberate_continuations`` sets the task-level default for the
    deliberation-on-continuation toggle: when True, a later continuation on
    this (deliberate-mode) task runs a fresh council instead of single-shot
    decompose. None inherits the global config default; a per-continuation
    ``orchestrator_continue(deliberate=...)`` still overrides it.

    ``auto_approve_risk`` is currently a forward-compat shim — the
    daemon's ``auto_approve`` flag is binary today. ``"none"`` keeps every
    gate manual; any other value flips ``auto_approve=True``. This lets
    callers express intent now and get finer-grained behaviour later
    without a tool-signature change.

    ``verify_command`` is the build/test gate. When set, the engine runs
    it in ``project_path`` after all subtasks finalize, before flipping
    the task to "done". Non-zero exit fails the task. Use this when
    "subagents claimed success" is not enough and you want a hard pass/
    fail signal (e.g. ``pytest -q``, ``npm test --silent``).

    ``audience`` is a Stream C recipient — a ``person_id`` from the people
    store. When set, every Stream B artifact a subtask produces is delivered
    to that person as the subtask finalizes. Unset means no delivery, which
    stays the default: most tasks have no recipient, and giving one to a
    scratch task mails it to somebody.

    ``delivery_channel`` is how that delivery is rendered — ``markdown`` |
    ``marp`` | ``microsite`` | ``tts`` | ``podcast``, defaulting to
    ``markdown``. MS formats (PPTX/DOCX/XLSX) are not available.
    ``delivery_brand_id`` overrides the theme. Both are ignored without a
    recipient, since neither describes anything on its own.

    ``fast_track`` opts this task into the fast-track review profile: a phase
    whose parts are ALL risk=LOW and whose deterministic checks pass clean
    skips the critic and scorer LLM stages. Deterministic checks are never
    skipped, MED and HIGH risk are always LLM-reviewed, and every skip is
    logged in the activity feed. Off by default — guessing this wrong ships
    unreviewed work.
    """
    if not description or not description.strip():
        return {"error": "description is required"}
    if mode not in _ORCH_VALID_MODES:
        return {"error": f"mode must be one of {_ORCH_VALID_MODES}"}
    if intelligence not in _ORCH_VALID_INTELLIGENCE:
        return {"error": f"intelligence must be one of {_ORCH_VALID_INTELLIGENCE}"}

    body: dict[str, Any] = {
        "description": description,
        "mode": _orch_normalize_mode(mode),
        "intelligence": intelligence,
        "auto_approve": auto_approve_risk != "none",
        # Agent/MCP calls are autonomous by nature — no human in the loop to
        # answer intake-clarity questions. Tells the API to skip the BLOCKING
        # intake gate (it still logs the clarity score in the background) so
        # the task never dead-ends and the create call returns without waiting
        # on the synchronous clarity LLM (which exceeded this tool's timeout).
        "autonomous": autonomous,
    }
    if required_roles:
        body["required_roles"] = required_roles
    if preferred_cli:
        body["preferred_cli"] = preferred_cli
    if verify_command and verify_command.strip():
        body["verify_command"] = verify_command.strip()
    if deliberate_continuations is not None:
        body["deliberate_continuations"] = bool(deliberate_continuations)
    # Stream C, forwarded only as a complete instruction. The API drops a
    # channel with no recipient anyway; not sending it keeps the two layers
    # from disagreeing about what was requested.
    # P6.7 — fast-track. Only sent when on: the API default is False, and a
    # field that always ships its own default is noise in every request.
    if fast_track:
        body["fast_track"] = True
    if audience and audience.strip():
        body["audience"] = audience.strip()
        if delivery_channel and delivery_channel.strip():
            body["delivery_channel"] = delivery_channel.strip()
        if delivery_brand_id and delivery_brand_id.strip():
            body["delivery_brand_id"] = delivery_brand_id.strip()

    resp = _orch_request("POST", "/api/tasks", body)
    if "error" in resp:
        return resp

    task_id = resp.get("task_id")
    return {
        "task_id": task_id,
        "plan_summary": resp.get("description", description)[:200],
        "requires_approval": mode == "plan" or body["auto_approve"] is False,
        "raw": resp,
    }


def orchestrator_status(task_id: str) -> dict:
    """Return the current state of a task in a stable, agent-friendly shape."""
    if not task_id or not task_id.strip():
        return {"error": "task_id is required"}

    resp = _orch_request("GET", f"/api/tasks/{task_id}/state")
    if "error" in resp:
        return resp

    # Project the dense state document onto the agreed surface.
    phases = resp.get("phases") or []
    current_subtask = None
    # The /state document never sets a top-level blocking_question; the
    # human-facing question lives on the awaiting block (blocked_review /
    # decision_gate / timeout_cap / capability_gap all carry awaiting.message).
    # Derive it so polling agents see WHAT to resolve instead of a null while
    # the task is parked on waiting_user.
    blocking_question = resp.get("blocking_question")
    if not blocking_question:
        _aw = resp.get("awaiting")
        if isinstance(_aw, dict) and _aw.get("kind"):
            blocking_question = _aw.get("message") or None
    artifacts: list[str] = []

    # last_event_ts: prefer the most recent recent_logs[].timestamp (the
    # live signal), then last_updated (always present, refreshed on each
    # read), then created_at. The TaskState API does not surface
    # updated_at/started_at at the top level — those legacy keys returned
    # null so polling agents could not detect progress.
    last_event_ts = None
    recent_logs = resp.get("recent_logs") or []
    if recent_logs:
        try:
            last_event_ts = max(
                (e.get("timestamp") or "" for e in recent_logs if isinstance(e, dict)),
                default="",
            ) or None
        except Exception:
            last_event_ts = None
    if not last_event_ts:
        last_event_ts = resp.get("last_updated") or resp.get("created_at") or None

    # Top-level artifacts is the on-disk inventory (read_task_artifacts).
    # Subtask-level artifacts are not populated by read_task_state, so the
    # legacy phase walk alone always returned []. Walk both for forward
    # compat; dedupe on path/name.
    for art in resp.get("artifacts") or []:
        if isinstance(art, str):
            artifacts.append(art)
        elif isinstance(art, dict):
            path = art.get("path") or art.get("name")
            if path:
                artifacts.append(path)
    for phase in phases:
        for st in phase.get("subtasks") or []:
            status = st.get("status")
            if status in ("running", "waiting_approval") and current_subtask is None:
                current_subtask = {
                    "id": st.get("id"),
                    "role": st.get("role"),
                    "status": status,
                }
            for art in st.get("artifacts") or []:
                if isinstance(art, str) and art not in artifacts:
                    artifacts.append(art)
                elif isinstance(art, dict):
                    path = art.get("path") or art.get("name")
                    if path and path not in artifacts:
                        artifacts.append(path)

    # Umbrella audit fix #11 — surface reconciler discrepancies in status.
    # Pre-fix, reconcile_task computed _Discrepancy records with no
    # upstream consumer in src/ — state/brain desync was detected then
    # silently dropped. Status callers had no way to know that
    # status='done' on disk meant nothing if Stream B was missing.
    # Compute lazily and best-effort: if config/reconciler unavailable,
    # omit the field rather than fail the whole status response.
    discrepancies_payload: list[dict] = []
    try:
        from okuro.orchestrator.config import load_config
        from okuro.orchestrator.reconciler import reconcile_task

        _cfg = load_config()
        for d in reconcile_task(task_id, _cfg.tasks_dir):
            discrepancies_payload.append({
                "subtask_id": d.subtask_id,
                "status": d.status,
                "has_artifact": d.has_artifact,
                "has_handover": d.has_handover,
                "note": d.note,
            })
    except Exception:
        # Best-effort — never fail status because the reconciler choked.
        discrepancies_payload = []

    return {
        "task_id": task_id,
        "status": resp.get("status"),
        "current_subtask": current_subtask,
        "last_event_ts": last_event_ts,
        "artifacts_so_far": artifacts,
        "blocking_question": blocking_question,
        "discrepancies": discrepancies_payload,
        "discrepancies_count": len(discrepancies_payload),
    }


# --- orchestrator_inspect: the read surface -------------------------------
#
# The orchestrator daemon exposes ~14 read-only diagnostics endpoints; MCP
# exposed exactly one of them (orchestrator_status, itself a six-field
# projection of /state) and no way to LIST tasks at all — an agent could not
# find a running task without already knowing its id.
#
# One tool with a view enum rather than fourteen tools: the read surface
# grows as the daemon grows, and a tool per endpoint means a tool-surface
# change every time. The cost is a weaker per-view schema; the mitigation is
# a stable envelope ({view, task_id, data, truncated}) plus the view list in
# the description.
#
# Each entry: (path_template, needs_task_id, limit_default).
# limit_default None = the endpoint takes no limit param.
_ORCH_INSPECT_VIEWS: dict[str, tuple[str, bool, int | None]] = {
    # Discovery — no task_id.
    "tasks":        ("/api/tasks",                          False, 50),
    "system":       ("/api/status",                         False, None),
    "active_roles": ("/api/active-roles",                   False, None),
    "recurring":    ("/api/recurring",                      False, None),
    # No aggregate "suggestions" view: /api/suggestions was deleted with the
    # dead cross-task branch (2026-08-18). Continuation suggestions live in
    # task.yaml and reach agents via view="state"
    # (state_reader continuation_suggestions), which is the live path.
    # Per-task.
    "state":        ("/api/tasks/{tid}/state",              True,  None),
    "snapshot":     ("/api/tasks/{tid}/snapshot",           True,  None),
    "activity":     ("/api/tasks/{tid}/activity",           True,  200),
    "logs":         ("/api/tasks/{tid}/logs",               True,  100),
    "gates":        ("/api/tasks/{tid}/gates",              True,  None),
    "awaiting":     ("/api/tasks/{tid}/awaiting",           True,  None),
    "checkpoints":  ("/api/tasks/{tid}/checkpoints",        True,  None),
    "review":       ("/api/tasks/{tid}/review",             True,  None),
    "artifacts":    ("/api/tasks/{tid}/artifacts",          True,  None),
    "deliveries":   ("/api/tasks/{tid}/deliveries",         True,  None),
}

# Serialized-response ceiling. /activity alone is capped at ~10 MB server
# side (60+ subtask runs produce 4k+ events); an MCP client would choke on
# a fraction of that. Overflow truncates the longest list in the payload and
# says so in the envelope — a silent cap reads as "that's all there is".
_ORCH_INSPECT_MAX_CHARS = 60_000


def _orch_inspect_shrink(data: object) -> tuple[object, dict | None]:
    """Trim the largest list in ``data`` until it serializes under the cap.

    Returns ``(data, truncation_note_or_None)``. Keeps the TAIL of the list:
    for activity, logs and events the recent end is the one being debugged.
    """
    try:
        if len(json.dumps(data, default=str)) <= _ORCH_INSPECT_MAX_CHARS:
            return data, None
    except (TypeError, ValueError):
        return data, None

    if not isinstance(data, dict):
        return data, {"note": "response over cap and not trimmable", "kept": None}

    # Find the longest list-valued key — that is what blew the budget.
    list_keys = [k for k, v in data.items() if isinstance(v, list) and v]
    if not list_keys:
        return data, {"note": "response over cap and not trimmable", "kept": None}
    key = max(list_keys, key=lambda k: len(data[k]))
    original = len(data[key])

    kept = original
    trimmed = dict(data)
    while kept > 1:
        kept = kept // 2
        trimmed[key] = data[key][-kept:]
        try:
            if len(json.dumps(trimmed, default=str)) <= _ORCH_INSPECT_MAX_CHARS:
                break
        except (TypeError, ValueError):
            return data, None

    return trimmed, {
        "field": key,
        "kept": kept,
        "dropped": original - kept,
        "of": original,
        "note": (
            f"kept the newest {kept} of {original} '{key}' entries to stay "
            f"under {_ORCH_INSPECT_MAX_CHARS} chars — raise `limit` and "
            f"narrow the view, or read the endpoint directly, for the rest"
        ),
    }


def orchestrator_inspect(
    view: str = "tasks",
    task_id: str | None = None,
    limit: int | None = None,
) -> dict:
    """Read any orchestrator diagnostic surface.

    ``view="tasks"`` needs no ``task_id`` and is the entry point: it lists
    tasks so a later call can name one. Every other per-task view requires
    ``task_id``.

    Returns ``{view, task_id, data, truncated}``, or an ``{error, ...}``
    envelope listing valid views. ``truncated`` is None unless the payload
    exceeded the size cap, in which case it names the field trimmed and how
    much was dropped.
    """
    spec = _ORCH_INSPECT_VIEWS.get(view)
    if spec is None:
        return {
            "error": f"unknown view {view!r}",
            "valid_views": sorted(_ORCH_INSPECT_VIEWS),
            "hint": "start with view='tasks' to find a task_id",
        }

    path_tmpl, needs_task, limit_default = spec

    if needs_task:
        if not task_id or not task_id.strip():
            return {
                "error": f"view={view!r} requires task_id",
                "hint": "call orchestrator_inspect(view='tasks') to list them",
            }
        path = path_tmpl.format(tid=task_id.strip())
    else:
        if task_id:
            return {
                "error": f"view={view!r} is a global view and takes no task_id",
                "valid_views": sorted(_ORCH_INSPECT_VIEWS),
            }
        path = path_tmpl

    if limit_default is not None:
        effective = limit_default if limit is None else limit
        if effective < 1:
            return {"error": "limit must be >= 1"}
        path = f"{path}?limit={effective}"
    elif limit is not None:
        return {
            "error": f"view={view!r} does not accept a limit",
            "views_accepting_limit": sorted(
                v for v, s in _ORCH_INSPECT_VIEWS.items() if s[2] is not None
            ),
        }

    resp = _orch_request("GET", path)
    if isinstance(resp, dict) and "error" in resp:
        return resp

    data, truncated = _orch_inspect_shrink(resp)
    return {
        "view": view,
        "task_id": task_id,
        "data": data,
        "truncated": truncated,
    }


_BLOCKED_REVIEW_ACTIONS = ("override", "retry", "decide")
_TIMEOUT_CAP_ACTIONS = ("extend_and_retry", "mark_done", "skip", "permanent_fail")


def orchestrator_approve(
    task_id: str,
    subtask_id: str | None = None,
    action: str = "approve",
    roles: list[str] | None = None,
    strategy: str = "parallel",
    decision: str | None = None,
    rationale: str | None = None,
) -> dict:
    """Approve / skip / abort the current gate — including DELIBERATE-mode gates.

    ``action="approve"`` with ``subtask_id=None`` advances whatever gate is
    blocking the task:

    - a plan-approval gate → approve the first ``waiting_approval`` subtask;
    - a ``panel_confirmation`` gate → confirm the proposed deliberation panel
      (override with ``roles`` / ``strategy``; defaults to the proposed panel,
      ``parallel``);
    - a ``discussion_proceed`` gate → resolve the discussion and let execution
      continue;
    - a ``blocked_review`` gate (a reviewer FAIL at the retry cap) → resolve
      with ``action="override"`` (accept the work as-is, default), ``"retry"``
      (reset the capped work and re-dispatch it), or ``"decide"`` (adjudicate
      a NEEDS_USER finding — pass ``decision``, optionally ``rationale``).
      ``action="approve"`` is accepted as an alias for ``"override"``.
    - a ``timeout_cap`` gate (a subtask that exhausted its retry budget on
      timeout) → resolve with ``action="extend_and_retry"`` (give it more
      wall-clock and retry, the recommended default), ``"mark_done"``,
      ``"skip"``, or ``"permanent_fail"``. ``action="approve"`` is accepted
      as an alias for ``"extend_and_retry"``.

    ROCK-SOLID v5 P2.3 — blocked_review and timeout_cap were the two most
    common parks and were HTTP-only: an agent/chat caller could not unblock
    either one. This is the single MCP verb that lets agent/chat callers
    drive ANY gate kind end-to-end. ``action="abort"`` delegates to the
    cancel endpoint.
    """
    if not task_id or not task_id.strip():
        return {"error": "task_id is required"}
    _known_actions = (
        "approve", "skip", "abort", *_BLOCKED_REVIEW_ACTIONS, *_TIMEOUT_CAP_ACTIONS,
    )
    if action not in _known_actions:
        return {"error": f"action must be one of {' | '.join(sorted(set(_known_actions)))}"}

    if action == "abort":
        resp = _orch_request("POST", f"/api/tasks/{task_id}/cancel")
        if "error" in resp:
            return resp
        return {"accepted": True, "next_state": "cancelled", "raw": resp}

    # approve / skip both target a subtask. Resolve it if the caller didn't
    # name one explicitly (the common "approve the plan" path).
    target_subtask = subtask_id
    if target_subtask is None:
        state = _orch_request("GET", f"/api/tasks/{task_id}/state")
        if "error" in state:
            return state
        for phase in state.get("phases") or []:
            for st in phase.get("subtasks") or []:
                if st.get("status") == "waiting_approval":
                    target_subtask = st.get("id")
                    break
            if target_subtask:
                break
        if target_subtask is None:
            # No plan-approval subtask — this may be a deliberate-mode gate.
            # Advance it via the awaiting block (P-COMM-1). Only 'approve' acts.
            aw = state.get("awaiting") or {}
            kind = aw.get("kind")
            if action == "approve" and kind == "panel_confirmation":
                sel = roles
                if not sel:
                    panel = _orch_request("GET", f"/api/tasks/{task_id}/proposed-panel")
                    if isinstance(panel, dict) and "error" in panel:
                        return panel
                    sel = [
                        r.get("role_id")
                        for r in (panel if isinstance(panel, list) else [])
                        if isinstance(r, dict) and r.get("role_id")
                    ]
                if not sel:
                    return {"accepted": False, "error": "no panel roles to confirm"}
                strat = strategy if strategy in ("parallel", "sequential", "debate") else "parallel"
                resp = _orch_request(
                    "POST", f"/api/tasks/{task_id}/panel",
                    {"roles": sel, "strategy": strat},
                )
                if isinstance(resp, dict) and "error" in resp:
                    return resp
                return {
                    "accepted": resp.get("status") == "panel_confirmed",
                    "next_state": "panel_confirmed",
                    "roles": sel,
                    "raw": resp,
                }
            if action == "approve" and kind == "discussion_proceed":
                endpoint = aw.get("endpoint") or ""
                if not endpoint or not endpoint.endswith("/proceed"):
                    return {"accepted": False, "error": "discussion gate exposes no proceed endpoint"}
                # The proceed gate REQUIRES authority assignments first (the UI
                # sets them by dragging roles to lead/contribute). For an MCP
                # "just proceed" we auto-assign the panel: the first position
                # role leads, the rest acknowledge. Skip if already assigned.
                base = endpoint[: -len("/proceed")]
                disc = _orch_request("GET", base)
                if isinstance(disc, dict) and "error" in disc:
                    return disc
                existing = (disc.get("assignments") if isinstance(disc, dict) else None) or []
                if not existing:
                    roles: list[str] = []
                    for p in (disc.get("positions") if isinstance(disc, dict) else None) or []:
                        r = (p or {}).get("role")
                        if r and r not in roles:
                            roles.append(r)
                    if not roles:
                        return {"accepted": False, "error": "discussion has no positions to assign"}
                    built = [{
                        "role": roles[0], "action": "assign",
                        "leads": "the deliverable",
                        "reasoning": "auto-assigned lead (MCP advance)",
                    }] + [{
                        "role": r, "action": "acknowledge",
                        "reasoning": "auto-acknowledged (MCP advance)",
                    } for r in roles[1:]]
                    ar = _orch_request("POST", f"{base}/assign", {"assignments": built})
                    if isinstance(ar, dict) and "error" in ar:
                        return ar
                resp = _orch_request("POST", endpoint, aw.get("payload") or {})
                if isinstance(resp, dict) and "error" in resp:
                    return resp
                return {
                    "accepted": resp.get("status") == "resolved",
                    "next_state": "deliberation_resolved",
                    "raw": resp,
                }
            # ROCK-SOLID v5 P2.3 — blocked_review + timeout_cap, the two
            # gate kinds that were HTTP-only. `aw["endpoint"]` is already
            # the exact right URL (phase- or subtask-scoped, respectively)
            # per set_awaiting's own payload — no need to re-derive it.
            if kind == "blocked_review":
                gate_action = "override" if action == "approve" else action
                if gate_action not in _BLOCKED_REVIEW_ACTIONS:
                    return {
                        "accepted": False,
                        "error": (
                            f"action {action!r} is not valid for a blocked_review "
                            f"gate — use one of {' | '.join(_BLOCKED_REVIEW_ACTIONS)} "
                            "(or 'approve' as an alias for 'override')"
                        ),
                    }
                endpoint = aw.get("endpoint") or ""
                if not endpoint:
                    return {"accepted": False, "error": "blocked_review gate exposes no endpoint"}
                body: dict = {"action": gate_action}
                if gate_action == "decide":
                    if not decision:
                        return {
                            "accepted": False,
                            "error": "action='decide' requires the `decision` argument",
                        }
                    body["decision"] = decision
                    if rationale:
                        body["rationale"] = rationale
                resp = _orch_request("POST", endpoint, body)
                if isinstance(resp, dict) and "error" in resp:
                    return resp
                return {
                    "accepted": True,
                    "next_state": resp.get("status") if isinstance(resp, dict) else None,
                    "raw": resp,
                }
            if kind == "timeout_cap":
                gate_action = "extend_and_retry" if action == "approve" else action
                if gate_action not in _TIMEOUT_CAP_ACTIONS:
                    return {
                        "accepted": False,
                        "error": (
                            f"action {action!r} is not valid for a timeout_cap "
                            f"gate — use one of {' | '.join(_TIMEOUT_CAP_ACTIONS)} "
                            "(or 'approve' as an alias for 'extend_and_retry')"
                        ),
                    }
                endpoint = aw.get("endpoint") or ""
                if not endpoint:
                    return {"accepted": False, "error": "timeout_cap gate exposes no endpoint"}
                resp = _orch_request("POST", endpoint, {"action": gate_action})
                if isinstance(resp, dict) and "error" in resp:
                    return resp
                return {
                    "accepted": True,
                    "next_state": resp.get("status") if isinstance(resp, dict) else None,
                    "raw": resp,
                }
            return {
                "accepted": False,
                "error": "no subtask is waiting_approval — nothing to approve",
            }

    resp = _orch_request(
        "POST",
        f"/api/tasks/{task_id}/approve",
        {"subtask_id": target_subtask, "action": action},
    )
    if "error" in resp:
        return resp
    return {
        "accepted": resp.get("status") == "ok",
        "next_state": "approved" if action == "approve" else "skipped",
        "subtask_id": target_subtask,
        "raw": resp,
    }


def orchestrator_continue(
    task_id: str,
    description: str | None = None,
    suggestion_id: str | None = None,
    deliberate: bool | None = None,
) -> dict:
    """Append continuation phases to an existing task.

    Exactly one of ``description`` or ``suggestion_id`` must be set.
    Both-or-neither is a caller error and is rejected up front so the
    daemon never sees a malformed continue request.

    ``deliberate`` is the per-continuation override for the
    deliberation-on-continuation toggle. Leave ``None`` to inherit the
    task-level default, then the global config default. Set ``True`` to run
    a fresh deliberation council (panel positions → discussion → proceed →
    decompose) for THIS follow-up, or ``False`` to force single-shot
    decomposition. Only effective on deliberate-mode tasks.
    """
    if not task_id or not task_id.strip():
        return {"error": "task_id is required"}

    has_desc = bool(description and description.strip())
    has_sugg = bool(suggestion_id and suggestion_id.strip())
    if has_desc and has_sugg:
        return {"error": "provide exactly one of description or suggestion_id, not both"}
    if not has_desc and not has_sugg:
        return {"error": "provide exactly one of description or suggestion_id"}

    if has_sugg:
        # Suggestions live on the task document under
        # continuation_suggestions[]. Resolve the id → suggestion_text
        # before posting — the engine's continue endpoint only accepts a
        # free-text description today.
        detail = _orch_request("GET", f"/api/tasks/{task_id}")
        if "error" in detail:
            return detail
        suggestions = detail.get("continuation_suggestions") or []
        match = None
        for s in suggestions:
            if isinstance(s, dict) and (s.get("id") == suggestion_id or s.get("suggestion_id") == suggestion_id):
                match = s
                break
        if match is None:
            return {"error": f"suggestion_id {suggestion_id!r} not found on task {task_id}"}
        description = match.get("suggestion_text") or match.get("text") or ""
        if not description:
            return {"error": f"suggestion {suggestion_id!r} has no body"}

    body = {"description": description}
    if deliberate is not None:
        body["deliberate"] = bool(deliberate)
    resp = _orch_request("POST", f"/api/tasks/{task_id}/continue", body)
    if "error" in resp:
        return resp
    # "queued" is success too — the task was still running, so the continuation
    # was enqueued for the live engine to drain (it does NOT spawn a 2nd engine).
    status = resp.get("status")
    return {
        "accepted": status in ("accepted", "queued"),
        "status": status,
        "task_id": task_id,
        "raw": resp,
    }


def _task_event_types() -> list[str]:
    """MCP enum for ``task_events.event_type`` — DERIVED, never hand-copied.

    The hardcoded copy in ``list_task_events`` went stale: migrations 046 /
    048 / 050 / 055 each widened the canonical ``EventType`` (adding
    ``verdict``, the M4/M5 telemetry rows, and ``convergence_telemetry``)
    while the schema enum kept listing only the original six. Those rows
    were being written for weeks and were unreadable through the tool that
    exists to read them. Deriving from the single source of truth closes
    the whole drift class, not just the two missing entries.
    """
    from typing import get_args

    from okuro.sense.task_events import EventType

    return list(get_args(EventType))


def _slider_schema_properties() -> dict:
    """Axis properties for person_update_sliders, from the canonical list.

    Lazy import: peer.cognitive_profile pulls pydantic, and get_tools() is
    called on every MCP handshake.
    """
    from okuro.peer.cognitive_profile import slider_json_schema_properties

    return slider_json_schema_properties()


def get_tools() -> list[Tool]:
    return [
        # --- Bootstrap ---
        Tool(
            name="bootstrap",
            description=(
                "Call this FIRST in every new session. Returns the CORE startup packet: "
                "user profile, behavioural contract, conventions, principles, tool "
                "protocol, hardware, roles and the session close-out contract. "
                "If your task_hint resolves to a project, this is only HALF your "
                "briefing — the packet names the project and `bootstrap_project` "
                "returns everything about it (memory, todos, reminders, progress, "
                "people, your role assignment, the codebase map). Every other okuro "
                "tool stays refused until you make that second call. The packet is "
                "split because both halves together exceed the 50,000-character "
                "result envelope, over which the host discards the entire result."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_hint": {"type": "string", "description": "Brief description of your task"},
                    "provider": {"type": "string", "default": "unknown", "description": "Your agent provider (claude-code, gemini, codex, cursor)"},
                    "budget": {
                        "type": "integer",
                        "description": (
                            "IGNORED — accepted only so existing callers keep working. "
                            "The core is a fixed section set that fits its envelope with "
                            "room to spare, so there is nothing left to budget or rank."
                        ),
                    },
                    "sections": {"type": "array", "items": {"type": "string"}, "default": ["all"], "description": "Core sections to include (diagnostic use)"},
                    "include_digest": {"type": "boolean", "default": False, "description": "Include daily thought digest"},
                },
            },
        ),
        Tool(
            name="bootstrap_project",
            description=(
                "The PROJECT half of your bootstrap packet — REQUIRED after `bootstrap` "
                "whenever your task_hint resolved to a project. Returns the memory "
                "pointers, todos, reminders, intake questions, progress, people, "
                "cross-project tunnels, brand/design/stack, codebase map and role "
                "assignment for that project. Call it with no arguments: the slug and "
                "task hint default to the ones your own bootstrap resolved. Until it "
                "is called, every other okuro tool is refused."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": (
                            "Project slug. Defaults to the one your bootstrap call "
                            "resolved from task_hint — normally leave it unset."
                        ),
                    },
                    "task_hint": {
                        "type": "string",
                        "description": (
                            "Defaults to the hint you passed to bootstrap. Pass a "
                            "different one only to re-aim memory/thought relevance."
                        ),
                    },
                    "provider": {"type": "string", "default": "unknown", "description": "Your agent provider"},
                    "sections": {"type": "array", "items": {"type": "string"}, "default": ["all"], "description": "Project sections to include (diagnostic use)"},
                    "include_digest": {"type": "boolean", "default": False, "description": "Include daily thought digest"},
                },
            },
        ),
        # --- Memory (persistent cross-session learnings — facts about the codebase/system) ---
        Tool(
            name="read_memory",
            description=(
                "Search persistent cross-agent learnings. Returns memories stored by any previous agent session. "
                "Use when you need to remember or recall what a previous session learned, or to check "
                "whether something was already discovered — gotchas, conventions, architecture "
                "decisions. Semantic search (query), topic filtering, or direct lookup by the "
                "`\u2192id` handle printed on every bootstrap memory pointer (memory_id)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": (
                            "Open a specific memory by the 8-char handle shown after the arrow on a "
                            "bootstrap pointer, or by its full id. Use this instead of query when "
                            "you already have a handle — a handle passed as `query` is a semantic "
                            "search on a hex string and returns something unrelated."
                        ),
                    },
                    "query": {"type": "string", "description": "Semantic search query (e.g., 'SQLite concurrency issues')"},
                    "topic": {"type": "string", "enum": ["convention", "gotcha", "decision", "learning", "architecture"]},
                    "project": {"type": "string", "description": "Filter by project slug"},
                    "limit": {"type": "integer", "default": 10},
                    "min_confidence": {"type": "number", "default": 0.3},
                },
            },
        ),
        Tool(
            name="write_memory",
            description=(
                "Persist a FACTUAL learning for future agents. Use for: gotchas (things that broke), "
                "conventions (how things should be done), decisions (why X was chosen over Y), "
                "architecture (how components connect). "
                "NOT for ideas or speculation — use capture_thought for those. "
                "NOT for runtime state or progress — use log_progress for that."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "enum": ["convention", "gotcha", "decision", "learning", "architecture"],
                              "description": "convention=how things should be done, gotcha=things that broke/surprised, decision=why X over Y, learning=reusable pattern, architecture=how components connect"},
                    "content": {"type": "string", "description": "The factual learning to store (be specific — include file paths, error messages, or config values)"},
                    "project": {"type": "string", "description": "Project slug (omit for system-wide learnings)"},
                    "confidence": {"type": "number", "default": 0.7, "description": "How certain you are (0.0-1.0). Unratified agent writes are capped at 0.8 — 1.0 is reserved for human-confirmed fact."},
                    "supersedes": {"type": "string", "description": "UUID of an outdated memory this replaces"},
                    # Provenance. Both were accepted by write_memory() in Python
                    # for a long time but absent from this schema, making them
                    # structurally unreachable — which is why source_agent was
                    # 'unknown' on 96% of rows and role NULL on 100% (measured
                    # 2026-07-19, n=2939). source_agent now defaults to the
                    # session provider server-side; this override exists for
                    # writers that are something more specific.
                    #
                    # `session_id` is NOT exposed on purpose: an agent cannot
                    # know its own telemetry id, and a field the caller must
                    # remember to pass regresses to NULL. It is derived.
                    #
                    # `ratified` is NOT exposed on purpose: it bypasses the
                    # confidence cap, and the cap exists precisely to stop an
                    # agent's own certainty reaching 1.0.
                    "source_agent": {"type": "string", "description": "Who is asserting this, if more specific than the session provider (e.g. a named subagent). Defaults to the current provider."},
                    "role": {"type": "string", "description": "Role scope this learning belongs to, if any (e.g. 'backend-engineer')"},
                },
                "required": ["topic", "content"],
            },
        ),
        # --- Artifacts (long compositions — reports + evidence + plans) ---
        Tool(
            name="artifact_write",
            description=(
                "Persist a long synthesized composition — report, evidence, or plan — "
                "as a first-class artifact. Use for content too long / structured "
                "for agent memory (audit reports, pentest protocols, probe output "
                "cited by a report, pre-execution plans). Memories stay as pointers; "
                "artifacts are the bodies. CODE (shell scripts, Python, SQL, templates) "
                "DOES NOT belong here — commit to the git repo instead (scripts/ or src/). "
                "The `deliverable` kind was removed in migration 027 for that reason."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["report", "evidence", "plan"],
                             "description": "report=post-execution narrative synthesis (audit/pentest/retro), evidence=raw captured output a report cites (probe logs, traces), plan=pre-execution document (migration plan, design spec, ADR)"},
                    "title": {"type": "string", "description": "Short human title (>=3 chars)"},
                    "summary": {"type": "string", "description": "1-2 sentence summary — indexed with title for semantic search"},
                    "body": {"type": "string", "description": "Full markdown/plaintext body"},
                    "media_type": {"type": "string", "description": "e.g. text/markdown, text/plain, application/json, application/x-sh"},
                    "project": {"type": "string", "description": "Project slug"},
                    "parent_id": {"type": "string", "description": "Artifact id this is evidence for (evidence -> report)"},
                    "supersedes": {"type": "string", "description": "Artifact id this replaces"},
                    "memory_refs": {"type": "array", "items": {"type": "string"}, "description": "agent_memory uuids cited"},
                    "confidence": {"type": "number", "default": 0.8},
                    "created_by": {"type": "string", "description": "Agent/user identifier"},
                    "artifact_id": {"type": "string", "description": "Optional explicit id (useful for deterministic porting)"},
                    "task_id": {"type": "string", "description": "Orchestrator task id (Stream B routing — subagent deliverables tagged with their producing task)"},
                    "subtask_id": {"type": "string", "description": "Orchestrator subtask id (Stream B routing — pairs with task_id)"},
                },
                "required": ["kind", "title"],
            },
        ),
        Tool(
            name="artifact_get",
            description="Fetch a single artifact by id. Returns the full body unless include_body=false.",
            inputSchema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "include_body": {"type": "boolean", "default": True},
                },
                "required": ["artifact_id"],
            },
        ),
        Tool(
            name="artifact_search",
            description=(
                "Semantic search over artifacts. Matches against title + summary + "
                "first ~500 chars of body (not full body — keeps relevance signal "
                "concentrated in the intro). Returns row metadata without body; "
                "call artifact_get(id) for the full body."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "kind": {"type": "string", "enum": ["report", "evidence", "plan"]},
                    "project": {"type": "string"},
                    "parent_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                    "min_confidence": {"type": "number", "default": 0.0},
                    "audience": {"type": "string", "enum": ["user", "agent", "process"], "description": "Filter by intended reader. 'user' = human-facing deliverables; 'agent' = machine traces."},
                    "include_superseded": {"type": "boolean", "default": False, "description": "Default skips superseded rows (confidence <= 0.1)."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="artifact_list",
            description=(
                "List artifacts with filters. Does not return body — use artifact_get "
                "for full content. Over half the store is superseded history: pass "
                "include_superseded=False for current truth only, and audience='user' "
                "for human-facing deliverables rather than machine traces. For a "
                "MID-STORE date window use created_after/created_before (+offset to "
                "page); without them paging only reaches the two ends of the sort "
                "order, which is why a 2026-07-29 audit had to report its target "
                "population as unenumerable. artifact_stats gives the aggregate."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["report", "evidence", "plan"]},
                    "project": {"type": "string"},
                    "parent_id": {"type": "string"},
                    "task_id": {"type": "string", "description": "Filter to artifacts produced by a specific orchestrator task (Stream B)"},
                    "subtask_id": {"type": "string", "description": "Filter to artifacts produced by a specific subtask (pairs with task_id)"},
                    "created_after": {"type": "string", "description": "ISO date/datetime, inclusive — lower bound of a creation window"},
                    "created_before": {"type": "string", "description": "ISO date/datetime, exclusive — upper bound of a creation window"},
                    "offset": {"type": "integer", "default": 0, "minimum": 0, "description": "Rows to skip — pages a window instead of only its ends"},
                    "limit": {"type": "integer", "default": 50},
                    "order": {"type": "string", "enum": ["created_at_desc", "created_at_asc", "updated_at_desc", "confidence_desc"], "default": "created_at_desc"},
                    "audience": {"type": "string", "enum": ["user", "agent", "process"], "description": "Filter by intended reader. 'user' = human-facing deliverables; 'agent' = machine traces."},
                    "include_superseded": {"type": "boolean", "default": True, "description": "Superseded rows (confidence <= 0.1) are INCLUDED by default for audit trails. Pass False for current truth only."},
                },
            },
        ),
        Tool(
            name="artifact_supersede",
            description="Point new_id at old_id and drop old_id confidence to 0.1. Preferred over delete when history matters (audit trail).",
            inputSchema={
                "type": "object",
                "properties": {
                    "old_id": {"type": "string"},
                    "new_id": {"type": "string"},
                },
                "required": ["old_id", "new_id"],
            },
        ),
        Tool(
            name="artifact_delete",
            description="Hard-delete an artifact and its vector row. Nulls parent_id/supersedes on rows pointing at it.",
            inputSchema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                },
                "required": ["artifact_id"],
            },
        ),
        # --- Project envelope (enumerate + re-file everything a project owns) ---
        Tool(
            name="project_inventory",
            description=(
                "ENUMERATE every brain row filed under one project slug — compact "
                "listing (id, kind, date, confidence, ~150-char excerpt) plus exact "
                "total/active/superseded counts per table. READ-ONLY. Use this, not "
                "read_memory, to build an index or prove completeness: read_memory "
                "ranks semantically and returns FULL BODIES, so it can neither "
                "enumerate ~100 rows nor prove none were missed. artifact_list pages "
                "one table; this covers every table that carries a project tag and "
                "reports which tables it excluded and why. project_envelope is the "
                "writer that re-files rows into a slug."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Project slug to enumerate"},
                    "include_superseded": {"type": "boolean", "default": False, "description": "Superseded rows are always COUNTED; this includes them in the row listing too."},
                    "tables": {"type": "array", "items": {"type": "string"}, "description": "Restrict to specific tables (default: every tag + binding table)"},
                    "limit_per_table": {"type": "integer", "default": 500},
                    "excerpt_chars": {"type": "integer", "default": 150},
                    "include_bindings": {"type": "boolean", "default": True, "description": "Include tables where project is an FK binding (managed_repos, stack_*) — listed for visibility, never re-filable."},
                },
                "required": ["slug"],
            },
        ),
        Tool(
            name="project_envelope",
            description=(
                "RE-FILE existing brain rows under a project slug — the only way to "
                "change a row's project after creation (write_memory/artifact_write "
                "set it at CREATION only). DRY RUN BY DEFAULT: returns exactly what "
                "would move, with excerpts, and writes nothing. Flow: (1) call with "
                "match= or ids= to get a plan_ref, (2) review, (3) re-call with "
                "plan_ref + exclude_ids + dry_run=False to apply EXACTLY that set "
                "minus your exclusions — not a re-run of a tweaked pattern. "
                "Auto-registers the target project so rows are never stamped with a "
                "slug no project owns. Reversible via the returned backup_ref. Only "
                "the project column is ever written; nothing is deleted. Use "
                "project_inventory first to see what a slug already owns."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "TARGET project slug — rows move INTO this"},
                    "ids": {"type": "array", "items": {"type": "string"}, "description": "Explicit rows: 'agent_memory:<id>' or a bare id / 8-char handle"},
                    "match": {"type": "string", "description": "Substring (or regex with match_regex=true) matched against title+body"},
                    "match_regex": {"type": "boolean", "default": False},
                    "match_fields": {"type": "string", "enum": ["auto", "title", "body"], "default": "auto"},
                    "from_projects": {"type": "array", "items": {"type": "string"}, "description": "Only consider rows currently filed under these projects"},
                    "tables": {"type": "array", "items": {"type": "string"}, "description": "Restrict to specific re-filable tables"},
                    "exclude_ids": {"type": "array", "items": {"type": "string"}, "description": "Drop these rows from a reviewed dry-run result before applying"},
                    "created_after": {"type": "string", "description": "ISO date/datetime, inclusive"},
                    "created_before": {"type": "string", "description": "ISO date/datetime, exclusive"},
                    "include_superseded": {"type": "boolean", "default": True, "description": "Superseded rows move with their project by default — a split history is worse than a complete one."},
                    "dry_run": {"type": "boolean", "default": True, "description": "TRUE by default. Must be explicitly false to write."},
                    "auto_register": {"type": "boolean", "default": True, "description": "Register the target project if missing. False refuses instead, naming the fix."},
                    "plan_ref": {"type": "string", "description": "Replay a dry run's plan file — the reviewed set cannot drift between review and apply"},
                    "excerpt_chars": {"type": "integer", "default": 150},
                },
                "required": ["slug"],
            },
        ),
        Tool(
            name="project_envelope_undo",
            description=(
                "Restore every row in a project_envelope backup to its previous "
                "project. Rows changed again since the apply are reported, not "
                "clobbered. Pass dry_run=true to preview."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "backup_ref": {"type": "string", "description": "The backup_ref returned by an applied project_envelope"},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["backup_ref"],
            },
        ),
        # --- Task events (M2 — append-only typed cross-subtask log) ---
        Tool(
            name="emit_task_event",
            description=(
                "Append one typed event to the task's cross-subtask log "
                "(M2). Use during a subtask whenever you make a "
                "load-bearing decision, supersede a prior one, define a "
                "contract, flag a gap, or raise an open question. "
                "Conflict-detect runs at append: a decision on a topic "
                "already locked by an active decision OR by a user ADR "
                "is REJECTED — emit a 'supersedes' event first, or "
                "surface as 'open_question' when the lock is a user ADR. "
                "Body shape depends on event_type — see schema."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Orchestrator task id"},
                    "subtask_id": {"type": "string", "description": "Producing subtask id"},
                    "event_type": {
                        "type": "string",
                        "enum": ["decision", "supersedes", "contract",
                                 "gap", "open_question", "compression"],
                        "description": "Event kind — drives body shape validation",
                    },
                    "body": {
                        "type": "object",
                        "description": (
                            "Event body. decision={topic,choice,rationale,alternatives?}. "
                            "supersedes={target_event_id,reason,new_choice?}. "
                            "contract={name,kind:api|schema|type|envelope|event|config,schema_body,notes?}. "
                            "gap={summary,affects?}. "
                            "open_question={question,blocking?}. "
                            "compression={artifact_id,covers_seq_from,covers_seq_to,summary} (reserved — compressor uses it)."
                        ),
                    },
                    "from_role": {"type": "string", "description": "Role id emitting the event"},
                    "supersedes": {"type": "string", "description": "Prior event id this event explicitly supersedes (optional — distinct from event_type='supersedes')"},
                    "confidence": {"type": "number", "default": 0.8, "minimum": 0.0, "maximum": 1.0},
                },
                "required": ["task_id", "subtask_id", "event_type", "body"],
            },
        ),
        Tool(
            name="list_task_events",
            description=(
                "Read events for a task in seq order. Use to inspect the "
                "decision log directly when the dispatcher-injected "
                "trace is not enough (drift checkpoint, audit, debug)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "event_type": {
                        "type": "string",
                        "enum": _task_event_types(),
                    },
                    "since_seq": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 500},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="precheck_deliverable",
            description=(
                "Run the reviewer's deterministic text checks against a DRAFT "
                "deliverable BEFORE artifact_write. Same generators and "
                "runners the reviewer uses — not a separate rulebook. Catches "
                "missing acceptance-criteria evidence, superseded terminology, "
                "an unreconciled numbers ledger, and unknown secret names "
                "while they still cost a tool call instead of a full review "
                "round. Disk-bound checks cannot run pre-write and are "
                "reported in checks_skipped."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "subtask_id": {"type": "string"},
                    "body": {
                        "type": "string",
                        "description": "The draft artifact body you are about to write.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["report", "evidence", "plan"],
                        "description": (
                            "The artifact kind you are about to write. "
                            "Resolves the same review profile the reviewer "
                            "uses — plan/report are not held to the "
                            "executable-evidence or secret-name bar. Omit "
                            "for the strict code-shaped default."
                        ),
                    },
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Override the ACs declared in the plan. Omit to "
                            "use the plan's."
                        ),
                    },
                },
                "required": ["task_id", "subtask_id", "body"],
            },
        ),
        Tool(
            name="review_loop_stats",
            description=(
                "Aggregate the orchestrator review loop from its own "
                "telemetry: rounds per subtask, verdict-by-attempt, "
                "inter-round latency, and finding severity. Use to measure "
                "whether the loop is converging before and after a change. "
                "Groups by (task_id, subtask_id) — subtask ids are '1.1'-"
                "style and are NOT unique across tasks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "window_days": {
                        "type": "integer", "default": 30, "minimum": 1,
                        "description": "Look-back window in days.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Restrict to one task (optional).",
                    },
                },
            },
        ),
        # --- Role-handovers (Stream A — agent-to-agent subtask handover) ---
        Tool(
            name="write_role_handover",
            description=(
                "Hand off agent-to-agent context after a subtask. Stream A — "
                "never user-facing. Refs only, no inlined source. Pairs with "
                "the user-facing artifact_write(kind='report', task_id=…) "
                "(Stream B). The next subagent reads brief.summary + decisions, "
                "then resolves cortex_refs via cortex_read_section to inspect "
                "details. Validators V1-V8 reject writes that inline code, "
                "skip cortex_refs for produced files, point at non-existent "
                "paths, or fail outcome=failed → ≥1 open_question."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subtask_id": {"type": "string", "description": "Producing subtask id (e.g. '3.2')"},
                    "from_role":  {"type": "string", "description": "Role id that produced this handover"},
                    "task_id":    {"type": "string", "description": "Orchestrator task id (used for KG lineage + filtering)"},
                    "to_subtask_id": {"type": "string", "description": "Consumer subtask id, if known"},
                    "to_role":    {"type": "string", "description": "Consumer role id, if known"},
                    "brief": {
                        "type": "object",
                        "description": "Structured agent-handover payload — no fenced code, refs only.",
                        "properties": {
                            "summary":        {"type": "string", "maxLength": 600, "description": "1-3 sentences. What I did."},
                            "decisions":      {"type": "array", "items": {
                                "type": "object",
                                "properties": {
                                    "decision":  {"type": "string", "maxLength": 200},
                                    "rationale": {"type": "string", "maxLength": 400},
                                },
                                "required": ["decision", "rationale"],
                            }, "default": []},
                            "outcome":        {"type": "string", "enum": ["success", "partial", "failed"]},
                            "open_questions": {"type": "array", "items": {"type": "string", "maxLength": 240}, "default": []},
                            "next_role_hint": {"type": "string", "description": "Suggested role id for the follow-up subtask"},
                            "produced_files": {
                                "type": "array",
                                "default": [],
                                "items": {"type": "string", "maxLength": 500},
                                "description": (
                                    "Absolute paths to files THIS subtask "
                                    "created or materially modified — including "
                                    "files outside tasks/{id}/artifacts/ (e.g. "
                                    "code in the project repo). The next "
                                    "subagent gets this list rendered in its "
                                    "prompt so it knows what to read or extend."
                                ),
                            },
                            "contracts": {
                                "type": "array",
                                "default": [],
                                "maxItems": 5,
                                "description": (
                                    "Typed contracts THIS subtask defined or "
                                    "extended (API endpoints, DB schemas, "
                                    "interface types, message envelopes). "
                                    "Use when the contract is too important "
                                    "to leave in a cortex_ref alone — "
                                    "downstream subagents inline it verbatim."
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name":   {"type": "string", "maxLength": 120, "description": "e.g. 'POST /auth/register' or 'User table schema'"},
                                        "kind":   {"type": "string", "enum": ["api", "schema", "type", "envelope", "event", "config"]},
                                        "schema": {"type": "string", "maxLength": 4000, "description": "The contract body — fenced code blocks ALLOWED here only"},
                                        "notes":  {"type": "string", "maxLength": 400},
                                    },
                                    "required": ["name", "kind", "schema"],
                                },
                            },
                        },
                        "required": ["summary", "outcome"],
                    },
                    "cortex_refs": {
                        "type": "array",
                        "default": [],
                        "items": {
                            "type": "object",
                            "properties": {
                                "path":       {"type": "string", "description": "Absolute or registered-root-relative"},
                                "start_line": {"type": "integer", "minimum": 1},
                                "end_line":   {"type": "integer", "minimum": 1},
                                "purpose":    {"type": "string", "maxLength": 160},
                                "anchor":     {"type": "string", "description": "AGENT_HEADER section id, if known"},
                            },
                            "required": ["path", "start_line", "end_line", "purpose"],
                        },
                    },
                    "kg_edges": {
                        "type": "array",
                        "default": [],
                        "items": {
                            "type": "object",
                            "properties": {
                                "subject":    {"type": "string"},
                                "predicate":  {"type": "string"},
                                "object":     {"type": "string"},
                                "confidence": {"type": "number", "default": 1.0},
                            },
                            "required": ["subject", "predicate", "object"],
                        },
                    },
                    "artifact_refs": {"type": "array", "items": {"type": "string"}, "default": [], "description": "artifact ids cited (e.g. the Stream B report)"},
                    "memory_refs":   {"type": "array", "items": {"type": "string"}, "default": [], "description": "agent_memory ids cited"},
                    "supersedes":    {"type": "string", "description": "Prior handover id this replaces (re-runs)"},
                    "confidence":    {"type": "number", "default": 0.8},
                    "project":       {"type": "string", "description": "Project slug"},
                },
                "required": ["subtask_id", "from_role", "brief"],
            },
        ),
        Tool(
            name="read_role_handover",
            description=(
                "Fetch the freshest role-handover for a subtask. Used by the "
                "dispatcher to inject upstream subtask context into a "
                "downstream subagent's prompt. Refs only — call "
                "cortex_read_section(path, start_line, end_line) to resolve. "
                "Post-bootstrap only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subtask_id":          {"type": "string", "description": "Producer subtask id"},
                    "task_id":             {"type": "string", "description": "Orchestrator task id (optional disambiguator)"},
                    "handover_id":         {"type": "string", "description": "Explicit handover id (overrides subtask_id lookup)"},
                    "include_superseded":  {"type": "boolean", "default": False, "description": "Surface confidence=0.1 history rows"},
                },
            },
        ),
        Tool(
            name="list_role_handovers",
            description="List role-handovers with filters. Newest first. Default skips superseded rows.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id":            {"type": "string"},
                    "subtask_id":         {"type": "string"},
                    "from_role":          {"type": "string"},
                    "status":             {"type": "string", "enum": ["success", "partial", "failed"]},
                    "limit":              {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
                    "include_superseded": {"type": "boolean", "default": False},
                },
            },
        ),
        # --- Review queue (session-loop subagent retry — PR 2 of 4) ---
        Tool(
            name="await_review",
            description=(
                "Long-poll for an M3 critic+scorer verdict on a "
                "freshly-written artifact. Call this inside a subagent "
                "session immediately after artifact_write to keep the "
                "session OPEN across review rounds — instead of dying "
                "and being respawned cold on FAIL, the same session "
                "wakes when the verdict lands and can patch in place. "
                "Returns one of three shapes: "
                "(a) verdict: {status:'verdict', verdict:'PASS'|'FAIL'|"
                "'CAP'|'NEEDS_USER', findings, implicated_acs, attempt, "
                "max_attempts}; "
                "(b) keep-alive: {status:'still_reviewing', elapsed_s} — "
                "re-call to keep polling; "
                "(c) terminal timeout: {status:'timeout', elapsed_s}. "
                "Idempotent within the 1h TTL — a late caller after a "
                "verdict has landed receives the cached verdict "
                "immediately."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subtask_id":            {"type": "string", "description": "Orchestrator subtask id the verdict is keyed under."},
                    "artifact_id":           {"type": "string", "description": "Artifact id the verdict is keyed under (the artifact the subagent just wrote)."},
                    "timeout_s":             {"type": "number", "default": 1800.0, "minimum": 1.0, "description": "Total long-poll budget in seconds before returning status='timeout'. Defaults to 30 min."},
                    "keep_alive_interval_s": {"type": "number", "default": 30.0, "minimum": 0.1, "description": "Seconds between keep-alive returns. Lower = chattier subagent loop; higher = fewer round-trips."},
                },
                "required": ["subtask_id", "artifact_id"],
            },
        ),
        # --- Deliveries (Stream C — audience-adapted renders of artifacts) ---
        Tool(
            name="delivery_send",
            description=(
                "Render an artifact (Stream B report) for a recipient and store "
                "the result in the deliveries table. Runs the 5-stage pipeline: "
                "SourceDocument -> outline_for_recipient (slider-driven) -> "
                "tokens_to_theme (brand_id) -> channel.render -> persist. "
                "Best-effort: failures persist a row with success=false and "
                "return an error envelope rather than raising. Channels: "
                "markdown (always), marp (PDF if marp-cli present), "
                "microsite (Astro+Tailwind tarball). tts/podcast queued for "
                "P2/P3. PPTX/DOCX/XLSX are explicitly banned — okuro never "
                "emits Microsoft proprietary formats."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "description": "Brain artifact id (Stream B report)."},
                    "person_id":   {"type": "string", "description": "Recipient person_id. Orchestrator engine fills from Task.audience when omitted."},
                    "channel":     {"type": "string", "enum": ["markdown", "marp", "microsite", "tts", "podcast"], "default": "markdown"},
                    "brand_id":    {"type": "string", "description": "Brand for token resolution. Falls back to stack_project_brand for the artifact's project."},
                    "title":       {"type": "string", "description": "Override title (default: artifact.title)."},
                    "context":     {"type": "string", "description": "Optional context passed to person_translate."},
                },
                "required": ["artifact_id"],
            },
        ),
        Tool(
            name="delivery_list",
            description="List deliveries with filters. Body-free (call delivery_get for the rendered payload).",
            inputSchema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "person_id":   {"type": "string"},
                    "channel":     {"type": "string", "enum": ["markdown", "marp", "microsite", "tts", "podcast"]},
                    "brand_id":    {"type": "string"},
                    "success":     {"type": "boolean"},
                    "limit":       {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
                    "order":       {"type": "string", "enum": ["created_at_desc", "created_at_asc"], "default": "created_at_desc"},
                },
            },
        ),
        Tool(
            name="delivery_get",
            description="Fetch a delivery by id. include_body=true returns the rendered text payload; include_blob=true returns the binary (PDF, audio).",
            inputSchema={
                "type": "object",
                "properties": {
                    "delivery_id":  {"type": "string"},
                    "include_body": {"type": "boolean", "default": True},
                    "include_blob": {"type": "boolean", "default": False},
                },
                "required": ["delivery_id"],
            },
        ),
        Tool(
            name="delivery_delete",
            description="Hard-delete a delivery row + its vec entry.",
            inputSchema={
                "type": "object",
                "properties": {
                    "delivery_id": {"type": "string"},
                },
                "required": ["delivery_id"],
            },
        ),
        # --- Thoughts (ephemeral ideas, observations, questions — things to revisit later) ---
        Tool(
            name="capture_thought",
            description=(
                "Save an idea, observation, or open question for the USER to revisit later. "
                "Unlike write_memory (facts for agents), thoughts are for humans: "
                "'what if we refactored X?', 'should we consider Y?', 'this pattern feels wrong'. "
                "Thoughts surface in future bootstrap packets when relevant to the task."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The idea, observation, or question"},
                    "category": {"type": "string", "description": "Category tag (e.g., 'idea', 'concern', 'question', 'observation')"},
                    "project": {"type": "string", "description": "Project slug for context"},
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="search_thoughts",
            description="Search through captured thoughts (ideas, observations, questions). Returns matching thoughts with status and timestamps.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "default": 5},
                    "category": {"type": "string", "description": "Filter by category"},
                    "status": {"type": "string", "description": "Filter by status"},
                    "since": {"type": "string", "description": "ISO date string"},
                },
            },
        ),
        Tool(
            name="update_thought",
            description="Update a thought's status or content.",
            inputSchema={
                "type": "object",
                "properties": {
                    "thought_id": {"type": "string"},
                    "status": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["thought_id"],
            },
        ),
        Tool(
            name="daily_digest",
            description="Get unresolved thoughts, action items, forgotten ideas.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 10},
                    "include_dismissed": {"type": "boolean", "default": False},
                },
            },
        ),
        # --- Progress ---
        Tool(
            name="log_progress",
            description="Record a work milestone. Call after completing a meaningful unit of work — not every tiny step. Stored per-project, shown to future agents in bootstrap.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project slug"},
                    "status": {"type": "string", "enum": ["exploring", "implementing", "testing", "blocked", "waiting_for_user"]},
                    "summary": {"type": "string", "description": "One-line summary"},
                    "files_touched": {"type": "array", "items": {"type": "string"}},
                    "next_steps": {"type": "string"},
                    "blockers": {"type": "string"},
                    "memory_keys": {"type": "array", "items": {"type": "string"}},
                    "phase": {"type": "string", "description": "Key of a phase declared via project_phases_set. Passing it promotes a pending phase to active, so 'how far along' updates without a second call. Optional."},
                },
                "required": ["project", "status", "summary"],
            },
        ),
        Tool(
            name="get_progress",
            description="Get recent progress for a project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "agent": {"type": "string"},
                    "include_history": {"type": "boolean", "default": False, "description": "Every past entry, full text. Can be thousands of tokens."},
                    "history_limit": {"type": "integer", "description": "Render at most N past entries, compact (summary and next_steps clipped). Implies history. Use this instead of include_history when the answer has to fit a budget."},
                },
                "required": ["project"],
            },
        ),
        # --- Project status (where does this project stand) ---
        Tool(
            name="project_status",
            description=(
                "WHERE DOES THIS PROJECT STAND — one read answering what "
                "get_project + get_progress + session_history + "
                "project_inventory + todo_list each answer a slice of, plus the "
                "one none of them can: how far along it is. Returns charter and "
                "registration state, the DECLARED phase plan with a done/total "
                "ratio, the current progress row, history merged across ALL "
                "agents (progress is upserted per (project, agent), so "
                "get_progress reads one stack of possibly several), inventory "
                "counts, open todos, recent decisions, sessions with compliance "
                "scores — and a STALENESS verdict comparing the age of the "
                "declared status against git HEAD and the newest brain row. "
                "Read the staleness block first: it prices everything else. "
                "Declare phases with project_phases_set."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Project slug"},
                    "history_limit": {"type": "integer", "default": 8, "description": "Past progress entries to merge in"},
                    "session_limit": {"type": "integer", "default": 10},
                    "include_charter": {"type": "boolean", "default": False, "description": "Inline the full charter text (can be a few KB); off by default, presence and size are always reported."},
                },
                "required": ["slug"],
            },
        ),
        Tool(
            name="project_phases_set",
            description=(
                "DECLARE or ADVANCE a project's phase plan — the queryable "
                "answer to 'what is done and what is left', instead of it living "
                "in prose nobody re-reads. UPSERT by key, so one tool does both "
                "jobs: declare a plan with phases=[{key,title}, ...] "
                "(replace=true to drop phases you omitted), advance one with "
                "phases=[{'key':'p2','state':'done'}]. Only the fields you pass "
                "are written. States: pending | active | done | dropped. "
                "'active' is normally DERIVED — log_progress(phase='p2') "
                "promotes a pending phase — so you rarely set it by hand; "
                "'done' is never derived and must be declared here. Read the "
                "result with project_status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project slug"},
                    "phases": {
                        "type": "array",
                        "description": "Phase objects. `key` required; `title`, `seq`, `state`, `note` optional. seq defaults to list position.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "title": {"type": "string"},
                                "seq": {"type": "integer"},
                                "state": {"type": "string", "enum": ["pending", "active", "done", "dropped"]},
                                "note": {"type": "string"},
                            },
                            "required": ["key"],
                        },
                    },
                    "replace": {"type": "boolean", "default": False, "description": "Delete declared phases absent from this list. The only way to remove one."},
                },
                "required": ["project", "phases"],
            },
        ),
        # --- Projects ---
        Tool(
            name="list_projects",
            description="List active projects.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_project",
            description="Get project details.",
            inputSchema={
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        ),
        Tool(
            name="projects_overview",
            description=(
                "WHERE DOES EVERYTHING STAND — one compact row per project, in "
                "ONE read. This is the cross-project answer project_status "
                "cannot give: that tool returns ~4 KB for a SINGLE slug, so "
                "asking it of every project is ~100 calls, and the absence of "
                "this read is why no projects list exists and why charter/phase "
                "coverage was unmeasurable.\n\n"
                "Per project: charter presence, the DECLARED phase plan as "
                "done/total (percent is null when no plan is declared — 'no "
                "plan' and '0% done' are different facts), the newest progress "
                "row across ALL agents with its next_steps as a RESUME line, "
                "blockers, open + URGENT todo counts (urgent = stored priority "
                "4-5, which the UI renders as P1/P0 — the stored number is "
                "INVERTED by the display, higher stored = more urgent), brain "
                "row count, whether the "
                "path is real rather than an `unknown/` placeholder, and a "
                "staleness verdict WITH the ages it was computed from.\n\n"
                "Use touched_within_days=14 to collapse a ~100-project registry "
                "to the handful actually live; the payload reports how many it "
                "dropped. Costs are flat in project count (aggregates, never "
                "per-project enumeration) — measured 80 ms over 97 projects "
                "with git, 12 ms without. For row-level enumeration of ONE "
                "project use project_inventory; for its full detail, "
                "project_status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "touched_within_days": {
                        "type": "number",
                        "description": (
                            "Keep only projects whose newest signal (progress, "
                            "brain row, or git when included) is this recent. "
                            "14 is the useful default for 'what am I working on'."
                        ),
                    },
                    "include_repo": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Fold git HEAD age into the verdict. True by "
                            "default (80ms vs 12ms over 97 projects). False "
                            "yields a brain-only verdict and says so."
                        ),
                    },
                    "active_only": {"type": "boolean", "default": True},
                    "slugs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Restrict to these slugs; bypasses the filters.",
                    },
                },
            },
        ),
        Tool(
            name="register_project",
            description=(
                "CREATE a project row for a slug. IDEMPOTENT — re-registering "
                "an existing slug writes nothing and reports created=false.\n\n"
                "This is the only way to register a PATHLESS slug, and until it "
                "existed there was none: update_project only updates, "
                "scan_projects walks directories, and `okuro cortex add "
                "--slug` needs a path. project_envelope auto-registers its "
                "TARGET, but only inside its apply block — a slug whose rows "
                "are already tagged (exactly the orphan case) never reaches "
                "it. Use this when rows carry a slug no project owns, or to "
                "declare a brain-only project that has no repo.\n\n"
                "Rows are created provisional=1 (quarantine, migration 093). "
                "You cannot clear that here; it lifts when a human-meaningful "
                "`path` is set via update_project. `path`/`observes_path` are "
                "deliberately NOT settable at creation — `path` is "
                "UNIQUE-indexed and a guess would steal another project's "
                "session resolution. `charter` belongs to set_project_charter."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Project id to create. Lower-kebab by convention.",
                    },
                    "name":        {"type": "string", "description": "Display name. Defaults to the slug."},
                    "description": {"type": "string"},
                    "kind":        {"type": "string", "description": "code / hardware / domain / client / product."},
                    "stack":       {"type": "array", "items": {"type": "string"}},
                    "port_range":  {"type": "string"},
                    "url":         {"type": "string", "description": "Display only."},
                },
                "required": ["slug"],
            },
        ),
        Tool(
            name="update_project",
            description=(
                "Update a project's descriptive fields. DELIBERATELY NARROW — "
                "it exposes the fields that are read for DISPLAY, plus the one "
                "field that changes behaviour safely.\n\n"
                "observes_path is the interesting one: it says 'this project's "
                "work HAPPENS IN that tree' (many-to-one) as opposed to path, "
                "which says 'this project IS that tree' (one-to-one). Set it on "
                "a brain project whose decisions and artifacts are its own but "
                "whose code lives in another repo — project_status then reports "
                "real git activity (repo_activity.source becomes 'observes_path' "
                "and staleness.repo_age_days stops being null) WITHOUT making "
                "the project a cortex root and WITHOUT capturing that tree's "
                "sessions. It is read by exactly one consumer, "
                "sense/status.py::_repo_activity.\n\n"
                "NOT settable here, on purpose — the tool raises with the "
                "reason if you try: `path` (OWNERSHIP; re-pointing it moves a "
                "cortex root or steals another project's session resolution — "
                "use observes_path), `roles` (it selects the expert role "
                "injected into every future session — an instruction channel, "
                "not data), `charter` (use set_project_charter), `active` / "
                "`indexed` / `provisional` (lifecycle and quarantine flags owned "
                "by cortex root registration and the repo/corpus lifecycles), "
                "`design_profile` (a validated ref into the design registry; a "
                "raw setter would write a dangling ref)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Project id."},
                    "observes_path": {
                        "type": "string",
                        "description": (
                            "Absolute path of the tree whose git history "
                            "describes this project's work. Many projects may "
                            "observe one tree. Does NOT claim ownership."
                        ),
                    },
                    "name":        {"type": "string", "description": "Display name (not the id)."},
                    "description": {"type": "string"},
                    "kind":        {"type": "string", "description": "code / hardware / domain / client / product."},
                    "stack":       {"type": "array", "items": {"type": "string"}},
                    "port_range":  {"type": "string"},
                    "url":         {
                        "type": "string",
                        "description": (
                            "Display only. On a managed repo this is rewritten "
                            "by repo_sync from the clone url."
                        ),
                    },
                },
                "required": ["slug"],
            },
        ),
        Tool(
            name="get_project_charter",
            description=(
                "Get a project's RAW charter text (the per-project "
                "sub-bootstrap knowledge payload), or empty if unset. Use "
                "this to fetch-edit-set a charter without scraping it out of "
                "get_project's rendered markdown."
            ),
            inputSchema={
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        ),
        Tool(
            name="set_project_charter",
            description=(
                "Set/replace a project's charter: the compact, human-approved "
                "doctrine (what it's for, core job, architecture, principles) "
                "that build_project injects on project resolution so agents "
                "stop re-learning fundamentals. Safe — touches only the "
                "charter, never path/active/roles. Authoring flow: draft "
                "(charter-scribe role) -> human approves -> set here."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "slug":    {"type": "string"},
                    "charter": {"type": "string"},
                },
                "required": ["slug", "charter"],
            },
        ),
        # --- Managed repos (clone + code-graph ingest) ---
        Tool(
            name="repo_add",
            description=(
                "Clone a git repository under okuro's data dir, register it as a "
                "project (→ cortex root), and code-graph ingest it. Makes the repo "
                "queryable via cortex_* and kg_* tools."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "git clone url (https or ssh)"},
                    "workspace": {"type": "string", "description": "grouping bucket (default 'default')"},
                    "tier": {"type": "string", "enum": ["air", "advanced", "pro"], "description": "retrieval depth (default air = tree-sitter graph)"},
                    "name": {"type": "string", "description": "override repo dir name (defaults to url tail)"},
                    "token_key": {"type": "string", "description": "keyring entry name holding a git token (https only)"},
                    "username": {"type": "string", "description": "GIT Basic-auth username for the token. Bitbucket Atlassian API token = your Bitbucket USERNAME (the account handle) — NOT the email, which git rejects; the email belongs in pr_username. Auto per-host if omitted. Persisted so sync/push reuse it."},
                    "pr_username": {"type": "string", "description": "PR/REST Basic-auth user for opening pull requests on push — Bitbucket = your Atlassian ACCOUNT EMAIL (differs from the git username, which 401s on the REST API). Optional; only needed if you'll push+PR."},
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="repo_push",
            description=(
                "Push local commits in a managed clone to a NEW branch and open a "
                "pull request into the default branch. Never pushes to the default "
                "branch directly and never force-pushes; a no-op when there are no "
                "local commits, and refused if the local branch has diverged (sync "
                "first). Reuses the stored git credential; the PR uses pr_username "
                "(Bitbucket = Atlassian email). Does NOT re-ingest."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "description": "managed repo id (<workspace>__<name>)"},
                    "branch": {"type": "string", "description": "source branch name to push to (default okuro/<utc-stamp>)"},
                    "title": {"type": "string", "description": "PR title (default = last commit subject)"},
                    "open_pr": {"type": "boolean", "description": "open a PR after pushing (default true)"},
                    "pr_username": {"type": "string", "description": "override the stored PR/REST user (Bitbucket = Atlassian email)"},
                    "token_key": {"type": "string", "description": "override the stored keyring token name"},
                    "username": {"type": "string", "description": "override the stored git username"},
                },
                "required": ["repo_id"],
            },
        ),
        Tool(
            name="repo_discover",
            description=(
                "List every repository the stored git credentials can actually SEE on "
                "their forge, flagging which are already managed. Use before repo_add "
                "when you do not know the exact clone url, or to answer 'what am I not "
                "indexing yet'. Read-only — adding stays an explicit repo_add. With no "
                "arguments it sweeps every host+workspace already in the registry. "
                "NOTE Bitbucket: the global repository listing is deprecated "
                "(CHANGE-2770) and workspaces are not enumerable with an API token, so "
                "a Bitbucket workspace must already be known or passed explicitly; "
                "GitHub and GitLab list everything the token reaches in one call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace": {"type": "string", "description": "forge workspace/org to list (required for a Bitbucket workspace not already in the registry)"},
                    "host": {"type": "string", "description": "restrict to one forge host, e.g. bitbucket.org"},
                    "token_key": {"type": "string", "description": "keyring entry to authenticate with (defaults to the one already used for that host/workspace)"},
                },
            },
        ),
        Tool(
            name="repo_update",
            description=(
                "Update a managed repo's stored credential or metadata IN PLACE — the "
                "credential-rotation path. Use when a token is rotated, a forge "
                "username changes, or a repo moves; the alternative (repo_remove + "
                "repo_add) re-clones and re-ingests thousands of files to change three "
                "columns. The new identity is PROVEN against the live forge before it "
                "is written (git ls-remote + a REST whoami when pr_username changes) "
                "and the update is refused if it fails — a saved-but-broken credential "
                "looks green and breaks at the next sync. To fix every repo sharing one "
                "identity at once, use repo_credentials instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "description": "managed repo id (<workspace>__<name>)"},
                    "url": {"type": "string", "description": "new clone url (e.g. the repo moved workspace)"},
                    "tier": {"type": "string", "description": "air | advanced | pro"},
                    "default_branch": {"type": "string"},
                    "token_key": {"type": "string", "description": "keyring entry name holding the git token"},
                    "username": {"type": "string", "description": "GIT Basic-auth username. Bitbucket API token = the Bitbucket USERNAME, not the email."},
                    "pr_username": {"type": "string", "description": "REST/PR user. Bitbucket = the Atlassian ACCOUNT EMAIL. Verified separately from the git username."},
                    "verify": {"type": "boolean", "description": "probe the new identity before writing (default true)"},
                    "force": {"type": "boolean", "description": "write even if verification fails (default false)"},
                },
                "required": ["repo_id"],
            },
        ),
        Tool(
            name="repo_credentials",
            description=(
                "Re-point EVERY repo sharing one identity in a single verified call — "
                "the class-scope repair. Repos are grouped by (host, token_key, "
                "username), the same grouping the auth probe uses, because repos "
                "behind one credential share its fate. Select by the OLD identity, set "
                "the new one; one member is probed and the whole group is written only "
                "if that probe passes. Prefer this over repo_update when an account or "
                "token changed — fixing N repos one at a time is the instance fix "
                "applied N times."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "token_key": {"type": "string", "description": "select repos whose CURRENT token_key is this"},
                    "username": {"type": "string", "description": "select repos whose CURRENT username is this"},
                    "host": {"type": "string", "description": "select repos on this host (e.g. bitbucket.org)"},
                    "new_token_key": {"type": "string"},
                    "new_username": {"type": "string", "description": "new GIT username (Bitbucket = the username, not the email)"},
                    "new_pr_username": {"type": "string", "description": "new REST/PR user (Bitbucket = the Atlassian email)"},
                    "verify": {"type": "boolean", "description": "probe before writing (default true)"},
                    "force": {"type": "boolean", "description": "write even if verification fails (default false)"},
                },
            },
        ),
        Tool(
            name="repo_sync",
            description="git pull a managed repo and re-run incremental code-graph ingest.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "description": "managed repo id (<workspace>__<name>)"},
                    "token_key": {"type": "string"},
                },
                "required": ["repo_id"],
            },
        ),
        Tool(
            name="repo_list",
            description="List managed repositories (optionally filtered by workspace).",
            inputSchema={
                "type": "object",
                "properties": {"workspace": {"type": "string"}},
            },
        ),
        Tool(
            name="repo_remove",
            description=(
                "Remove a managed repo from the registry (deactivates its project). "
                "Files on disk are kept unless delete_files=true is explicitly passed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string"},
                    "delete_files": {"type": "boolean", "description": "also delete the clone from disk (default false)"},
                },
                "required": ["repo_id"],
            },
        ),
        # --- Managed corpora (non-git document sources → cortex) ---
        Tool(
            name="corpus_add",
            description=(
                "Register a NON-GIT document source (Confluence space, local folder) "
                "as a project → cortex root, so it becomes searchable with the normal "
                "cortex_* tools. Use for wikis, docs folders, note vaults — anything "
                "repo_add cannot take because it has no git clone url. There is no "
                "separate wiki/notes search tool by design: after this, query it with "
                "cortex_search(query, project='<workspace>__<name>'). "
                "SIMPLEST USE: pass url= with a pasted Confluence link or a folder path "
                "and everything else is derived. Confluence works anonymously on public "
                "spaces; local_folder indexes the folder in place without copying it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "a pasted source url or folder path — e.g. https://site.atlassian.net/wiki/spaces/KEY/... or ~/docs. source_type and config are derived from it."},
                    "source_type": {"type": "string", "enum": ["confluence", "local_folder"], "description": "adapter id — only needed when not passing url"},
                    "config": {"type": "object", "description": "adapter config — confluence: {base_url, space_key, include_archived?}; local_folder: {path, include?, exclude?, extensions?}. Overrides anything derived from url."},
                    "name": {"type": "string", "description": "corpus name (default: space key / folder name)"},
                    "workspace": {"type": "string", "description": "grouping bucket (default 'default')"},
                    "token_key": {"type": "string", "description": "keyring entry name holding an API token — omit for anonymously readable sources. NEVER pass a raw token."},
                    "username": {"type": "string", "description": "Atlassian account EMAIL paired with the token (Confluence Cloud rejects the display name)"},
                },
            },
        ),
        Tool(
            name="corpus_sync",
            description=(
                "Re-enumerate a managed corpus and refresh only what changed, then "
                "re-index into cortex. Cheap by design — unchanged items are never "
                "re-fetched. full=true re-fetches everything and forces a cortex "
                "re-index (needed after a chunking/embedding config change)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "corpus_id": {"type": "string", "description": "managed corpus id (<workspace>__<name>)"},
                    "full": {"type": "boolean", "description": "re-fetch every item and force re-index (default false)"},
                },
                "required": ["corpus_id"],
            },
        ),
        Tool(
            name="corpus_list",
            description=(
                "List managed corpora with status, item counts, and last-sync deltas. "
                "Also reports the registered source types and the config keys each needs."
            ),
            inputSchema={
                "type": "object",
                "properties": {"workspace": {"type": "string"}},
            },
        ),
        Tool(
            name="corpus_remove",
            description=(
                "Remove a corpus from the registry and deactivate its project. Files are "
                "kept unless delete_files=true, which is honoured ONLY for corpora okuro "
                "materialized itself — a local_folder corpus points at your own directory "
                "and the request is refused there rather than silently ignored."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "corpus_id": {"type": "string"},
                    "delete_files": {"type": "boolean", "description": "also delete okuro's materialized copy (default false)"},
                },
                "required": ["corpus_id"],
            },
        ),
        Tool(
            name="codegraph_insights",
            description=(
                "Code-graph insights for a project: 'god nodes' (the most-connected "
                "hub symbols/files by degree centrality) and detected subsystems "
                "(Louvain communities). Use to orient in an unfamiliar repo — jump "
                "to the hubs everything flows through instead of scanning blindly."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "project slug / managed repo id"},
                    "top_n": {"type": "integer", "description": "number of god nodes (default 15)"},
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="codegraph_audit",
            description=(
                "Index self-audit for a project's code graph — okuro's GRAPH_REPORT. "
                "Reports provenance tiers (extracted=directly parsed / import-proven, "
                "inferred=name-match guess, ambiguous=>1 candidate, external=stdlib), a "
                "determinism ratio, the ambiguous edges needing review with their "
                "candidate targets, per-language coverage (symbol-extracted vs sha-only), "
                "and god nodes. Use before trusting blast-radius / callers answers so an "
                "approximate edge is never mistaken for a precise one."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "project slug / managed repo id"},
                    "top_n": {"type": "integer", "description": "god nodes + ambiguous edges to list (default 15)"},
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="cross_repo_build",
            description=(
                "(Re)build the workspace cross-repo graph over ALL ingested (managed) "
                "repos. Links files across repo boundaries via shared anchors — symbol "
                "names, REST/route paths, tool-name literals — regardless of language "
                "(TS/Java/Python/…). Runs automatically on repo_add/repo_sync; call "
                "manually to force a refresh. Fast (sub-second on a handful of repos)."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="cross_repo_connections",
            description=(
                "Find connections that cross repository boundaries. Give an 'anchor' "
                "(e.g. 'route:/knowledge', 'sym:ErrorResponse', 'tool:load_agent') to "
                "list every repo/file linked to it; or give a 'node' "
                "('<project>|<relpath>' or a bare relpath) to see which files in OTHER "
                "repos share its symbols/routes/tools. Use when tracing how a change in "
                "one repo ripples into the others."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "anchor": {"type": "string", "description": "anchor id: sym:/route:/tool: prefixed"},
                    "node": {"type": "string", "description": "file node: '<project>|<relpath>' or bare relpath"},
                    "limit": {"type": "integer", "description": "max results (default 50)"},
                },
            },
        ),
        Tool(
            name="cross_repo_search",
            description=(
                "Search the cross-repo graph for shared structural elements — symbols, "
                "routes, tool names — that tie repos together. Matches spanning >=2 "
                "repos rank first (cross_repo=true). Complements cortex_search (prose/"
                "semantic) with exact shared-contract lookup across all ingested repos."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "substring to match against anchor names"},
                    "limit": {"type": "integer", "description": "max results (default 25)"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="cross_repo_insights",
            description=(
                "Workspace bridge hubs: the anchors (shared types, routes, tools) that "
                "tie the most repositories together — the cross-repo analogue of "
                "per-repo god-nodes. Surfaces the shared contracts whose change ripples "
                "widest across the ingested repo set. Orient here before a cross-cutting change."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "top_n": {"type": "integer", "description": "number of bridge hubs (default 20)"},
                },
            },
        ),
        # --- Brain ---
        Tool(
            name="brain_advise",
            description="Get a task briefing from a local LLM (free, no API cost). Returns context-aware advice about how to approach the task based on project history and codebase.",
            inputSchema={
                "type": "object",
                "properties": {"task_hint": {"type": "string"}},
                "required": ["task_hint"],
            },
        ),
        Tool(
            name="get_principles",
            description="Get working principles.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        # --- Profile ---
        Tool(
            name="get_profile",
            description="Get the user profile.",
            inputSchema={
                "type": "object",
                "properties": {"section": {"type": "string"}},
            },
        ),
        Tool(
            name="update_profile",
            description="Update user profile.",
            inputSchema={
                "type": "object",
                "properties": {
                    "section": {"type": "string"},
                    "path": {"type": "string"},
                    "action": {"type": "string"},
                    "value": {},
                },
                "required": ["section", "path", "action", "value"],
            },
        ),
        # --- Session ---
        Tool(
            name="session_report",
            description="MANDATORY end-of-session call. Rate the tools you used (useful/not useful) so the system improves. This is the single most-missed compliance step — call it before ending.",
            inputSchema={
                "type": "object",
                "properties": {
                    "feedback": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool_name": {"type": "string"},
                                "used": {"type": "boolean"},
                                "useful": {"type": "boolean"},
                                "comment": {"type": "string"},
                            },
                            "required": ["tool_name", "used"],
                        },
                    },
                },
            },
        ),
        Tool(
            name="session_history",
            description="Get recent session history with compliance scores.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Filter by project slug"},
                    "provider": {"type": "string", "description": "Filter by provider"},
                    "limit": {"type": "integer", "default": 10},
                },
            },
        ),
        Tool(
            name="session_score",
            description="Check current session compliance status.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="tool_performance",
            description="Query tool performance aggregated from telemetry.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "Specific tool name"},
                    "server": {"type": "string", "description": "Filter by MCP server"},
                    "days": {"type": "integer", "default": 30},
                },
            },
        ),
        Tool(
            name="compliance_scorecard",
            description="Show provider compliance scorecard.",
            inputSchema={
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "description": "Specific provider or all"},
                    "days": {"type": "integer", "default": 30},
                },
            },
        ),
        Tool(
            name="run_maintenance",
            description="Run system hygiene cycle: decay low-confidence memories, clean stale thoughts, aggregate telemetry, detect patterns. Usually run by scheduled daemon — manual invocation is fine.",
            inputSchema={"type": "object", "properties": {}},
        ),
        # --- People ---
        Tool(
            name="person_add",
            description="Add a communication partner.",
            inputSchema={
                "type": "object",
                "properties": {
                    "display_name": {"type": "string"},
                    "organization": {"type": "string"},
                    "role": {"type": "string"},
                    "relation_to_user": {
                        "type": "string",
                        "description": "How this person relates to you, freeform "
                        "(e.g. 'customer', 'co-shareholder', 'CEO of a prospect').",
                    },
                    "relation_type": {
                        "type": "string",
                        "description": "colleague | friend | family | professional | "
                        "acquaintance | other (default: professional).",
                    },
                    "notes": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["display_name"],
            },
        ),
        Tool(
            name="person_get",
            description="Get person details.",
            inputSchema={
                "type": "object",
                "properties": {"person_id": {"type": "string"}},
                "required": ["person_id"],
            },
        ),
        Tool(
            name="person_list",
            description="List known people.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "organization": {"type": "string"},
                },
            },
        ),
        Tool(
            name="person_update",
            description="Update person info.",
            inputSchema={
                "type": "object",
                "properties": {
                    "person_id": {"type": "string"},
                    "field": {"type": "string"},
                    "value": {},
                    "action": {"type": "string", "default": "set"},
                },
                "required": ["person_id", "field", "value"],
            },
        ),
        Tool(
            name="person_update_sliders",
            description=(
                "Patch the 17-axis cognitive slider vector on a person. "
                "Mirrors PUT /api/people/{id}/sliders so agents can adjust "
                "recipient sliders without the web UI. Partial: omitted "
                "axes preserve their stored value. Each axis is an int 1..5."
            ),
            inputSchema={
                "type": "object",
                # Generated from SLIDER_NAMES. This schema hand-listed eight
                # axes while the function validated seventeen, so the nine
                # newer ones worked but no agent could discover them — the
                # direct cause of their zero coverage across every person.
                "properties": {
                    "person_id": {"type": "string"},
                    **_slider_schema_properties(),
                },
                "required": ["person_id"],
            },
        ),
        Tool(
            name="person_lens",
            description="Get tailored communication guidance for a person — tone, formality, language preferences, relationship context. Use before drafting messages to someone.",
            inputSchema={
                "type": "object",
                "properties": {
                    "person_id": {"type": "string"},
                    "context": {"type": "string"},
                },
                "required": ["person_id"],
            },
        ),
        Tool(
            name="person_match",
            description="Find people matching a query.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 3},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="person_translate",
            description=(
                "Rewrite text so the named person will receive it well. "
                "Composes sender (user) cognitive profile + recipient communication "
                "profile, routes via bridge (capability='translate'), logs the "
                "attempt to translation_log (powers /people edge stats)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "person_id": {"type": "string"},
                    "source": {"type": "string", "description": "Raw message from the user."},
                    "context": {"type": "string", "description": "Optional channel/situation, e.g. 'slack DM' or 'all-hands email'."},
                    "provider": {"type": "string", "description": "Optional provider override."},
                },
                "required": ["person_id", "source"],
            },
        ),
        # --- People CRM (relational: companies / hats / connections / engagements) ---
        Tool(
            name="person_merge",
            description="Merge a duplicate person into a canonical one. Re-points affiliations/connections/engagements, unions tags, deactivates the duplicate (soft).",
            inputSchema={
                "type": "object",
                "properties": {
                    "keep_id": {"type": "string", "description": "Canonical person to keep."},
                    "merge_id": {"type": "string", "description": "Duplicate person to fold in and deactivate."},
                },
                "required": ["keep_id", "merge_id"],
            },
        ),
        Tool(
            name="company_add",
            description="Add or update a company — the entity that owns a design identity (brand). brand_id may be omitted until brands mature.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "brand_id": {"type": "string", "description": "Optional brand/design-profile id."},
                    "domain": {"type": "string"},
                    "notes": {"type": "string"},
                    "company_id": {"type": "string", "description": "Custom slug; auto from name if omitted."},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="company_get",
            description="Get a company profile plus its people (active affiliations).",
            inputSchema={
                "type": "object",
                "properties": {"company_id": {"type": "string"}},
                "required": ["company_id"],
            },
        ),
        Tool(
            name="company_list",
            description="List all companies with member counts.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="company_set_brand",
            description="Bind a company to a design profile (brand). Empty string clears it.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company_id": {"type": "string"},
                    "brand_id": {"type": "string"},
                },
                "required": ["company_id", "brand_id"],
            },
        ),
        Tool(
            name="affiliation_add",
            description="Add a hat: a person holds a role at a company. is_primary marks the headline hat. role_class (ceo/cto/engineer/client/…) seeds slider priors.",
            inputSchema={
                "type": "object",
                "properties": {
                    "person_id": {"type": "string"},
                    "company_id": {"type": "string"},
                    "role": {"type": "string", "description": "e.g. CEO, Board member, Stakeholder."},
                    "role_class": {"type": "string", "description": "ROLE_DEFAULT_SLIDERS key for slider seeding."},
                    "is_primary": {"type": "boolean", "default": False},
                    "status": {"type": "string", "enum": ["active", "past"], "default": "active"},
                    "started_at": {"type": "string"},
                    "ended_at": {"type": "string"},
                },
                "required": ["person_id", "company_id"],
            },
        ),
        Tool(
            name="affiliation_list",
            description="List hats (person→company roles), filtered by person and/or company.",
            inputSchema={
                "type": "object",
                "properties": {
                    "person_id": {"type": "string"},
                    "company_id": {"type": "string"},
                },
            },
        ),
        Tool(
            name="affiliation_set_primary",
            description="Make a hat the person's headline hat; demotes any other primary.",
            inputSchema={
                "type": "object",
                "properties": {"affiliation_id": {"type": "string"}},
                "required": ["affiliation_id"],
            },
        ),
        Tool(
            name="affiliation_remove",
            description="Delete a hat (affiliation).",
            inputSchema={
                "type": "object",
                "properties": {"affiliation_id": {"type": "string"}},
                "required": ["affiliation_id"],
            },
        ),
        Tool(
            name="connection_add",
            description="Record YOUR relationship to a person (co-shareholder, customer, vendor, board-peer…), optionally scoped to a context/company.",
            inputSchema={
                "type": "object",
                "properties": {
                    "person_id": {"type": "string"},
                    "connection_type": {"type": "string"},
                    "context": {"type": "string", "description": "e.g. 'the co-op', 'home install'."},
                    "company_id": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["person_id", "connection_type"],
            },
        ),
        Tool(
            name="connection_list",
            description="List your connection edges, optionally for one person.",
            inputSchema={
                "type": "object",
                "properties": {"person_id": {"type": "string"}},
            },
        ),
        Tool(
            name="connection_remove",
            description="Delete a connection edge.",
            inputSchema={
                "type": "object",
                "properties": {"connection_id": {"type": "string"}},
                "required": ["connection_id"],
            },
        ),
        Tool(
            name="engagement_add",
            description="Bind a person (wearing a hat) to a project under a company's design. company inferred from the affiliation if omitted. One per (project, person).",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_slug": {"type": "string"},
                    "person_id": {"type": "string"},
                    "affiliation_id": {"type": "string", "description": "Which hat applies on this project."},
                    "company_id": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["project_slug", "person_id"],
            },
        ),
        Tool(
            name="engagement_list",
            description="List engagements (project × person × hat × company), filtered by project and/or person.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_slug": {"type": "string"},
                    "person_id": {"type": "string"},
                },
            },
        ),
        Tool(
            name="engagement_resolve",
            description="For a project + person, resolve the delivery context: information architecture from the PERSON's cognitive profile + visual design from the COMPANY's brand + which hat is in play. Call before producing output for someone on a project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_slug": {"type": "string"},
                    "person_id": {"type": "string"},
                },
                "required": ["project_slug", "person_id"],
            },
        ),
        # --- Target groups (audience registry) ---
        Tool(
            name="target_group_add",
            description="Add or update a target group — the audience a deck is tailored for (e.g. board-of-mobiliar). role_class (board/top_management/engineer/designer/…) seeds the audience lens. Omit company_id for a reusable template; set it for a concrete group.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role_class": {"type": "string", "description": "ROLE_DEFAULT_SLIDERS key seeding the audience lens."},
                    "company_id": {"type": "string", "description": "Optional; NULL makes a reusable template."},
                    "kind": {"type": "string", "enum": ["standard", "custom"], "default": "custom"},
                    "notes": {"type": "string"},
                    "group_id": {"type": "string", "description": "Custom slug; auto from name if omitted."},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="target_group_get",
            description="Get a target group: resolved members (affiliation-derived + explicit) plus the merged audience lens, or a generic-deck note when there are no profiled members yet.",
            inputSchema={
                "type": "object",
                "properties": {"group_id": {"type": "string"}},
                "required": ["group_id"],
            },
        ),
        Tool(
            name="target_group_list",
            description="List all target groups with resolved member counts.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="target_group_match",
            description=(
                "Find AUDIENCES semantically — by how they want to be addressed, "
                "not just by name. Each group is embedded with its merged cognitive "
                "shape, so queries like 'who wants a short exec summary with a "
                "recommendation' or 'audience that needs deep technical detail' "
                "work. Use target_group_list when you just want the names."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="target_group_add_member",
            description="Explicitly pull a person into a group (override, beyond affiliation-derived membership).",
            inputSchema={
                "type": "object",
                "properties": {
                    "group_id": {"type": "string"},
                    "person_id": {"type": "string"},
                },
                "required": ["group_id", "person_id"],
            },
        ),
        Tool(
            name="target_group_remove_member",
            description="Remove an explicit member override from a group.",
            inputSchema={
                "type": "object",
                "properties": {
                    "group_id": {"type": "string"},
                    "person_id": {"type": "string"},
                },
                "required": ["group_id", "person_id"],
            },
        ),
        Tool(
            name="seed_standard_groups",
            description="Idempotently create the standard template groups (board, top-management, engineers, designers, product, sales, marketing).",
            inputSchema={"type": "object", "properties": {}},
        ),
        # --- Person provenance (sourced, confidence-scored claims) ---
        Tool(
            name="person_source_add",
            description="Record ONE sourced, confidence-scored claim about a person, so a user can verify WHY a fact was registered (e.g. 'understands numbers' ← conf 0.8 ← cv @ xy.com). Used by the audience-research flow to enrich people from the web with provenance. Merges into the person's communication/cognitive profile.",
            inputSchema={
                "type": "object",
                "properties": {
                    "person_id": {"type": "string"},
                    "field_path": {"type": "string", "description": "communication.<key> or cognitive.<key>, e.g. cognitive.knowledge_areas, cognitive.topic_interests."},
                    "value": {"type": "string", "description": "Claim value. JSON is parsed (use a JSON array/object for lists/structs); otherwise stored as a string."},
                    "source_url": {"type": "string", "description": "The proof link (stored as source_ref)."},
                    "source_type": {"type": "string", "default": "web", "description": "web | url | manual | inferred | …"},
                    "confidence": {"type": "number", "default": 0.8, "description": "0.0-1.0 certainty of the claim."},
                },
                "required": ["person_id", "field_path", "value"],
            },
        ),
        Tool(
            name="person_sources_list",
            description="List the provenance chain for a person — every registered claim with its value, confidence, source type, and source URL. The verification view behind 'is this actually true?'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "person_id": {"type": "string"},
                    "applied_only": {"type": "boolean", "default": True},
                },
                "required": ["person_id"],
            },
        ),
        # --- Resonance (communication compiler: context+goal → audience-fitted media) ---
        Tool(
            name="resonance_ingest",
            description="Resonance INTAKE. Land a document in the brain: stores it as an evidence artifact + extracts atomic claims to the KG with source + confidence. Returns an artifact_id to pass into analyze/render as source_artifact_ids.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The document body (plain text)."},
                    "title": {"type": "string"},
                    "source_ref": {"type": "string", "description": "Origin: filename or URL."},
                    "project": {"type": "string"},
                },
                "required": ["text", "title"],
            },
        ),
        Tool(
            name="resonance_analyze",
            description="Resonance ANALYZE (the epistemic gate). Given a goal + the ingested documents, decompose the goal into requirements, score whether the context suffices, and route each gap: research (factual/external) vs interview (tacit/internal). Returns completeness + requirements. Call before rendering.",
            inputSchema={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "source_artifact_ids": {"type": "array", "items": {"type": "string"}, "description": "artifact_ids from resonance_ingest — the docs for this goal."},
                    "project": {"type": "string"},
                },
                "required": ["goal"],
            },
        ),
        Tool(
            name="resonance_interview",
            description="Resonance ENRICH. Returns the next interview questions — the tacit/internal gaps the user must fill (research-route gaps are handled separately). Ask these, then feed answers to resonance_answer.",
            inputSchema={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "source_artifact_ids": {"type": "array", "items": {"type": "string"}},
                    "max_q": {"type": "integer", "default": 3},
                    "project": {"type": "string"},
                },
                "required": ["goal"],
            },
        ),
        Tool(
            name="resonance_answer",
            description="Resonance ENRICH. Record one interview Q&A — ingested as evidence so the next resonance_analyze sees higher completeness. Returns an artifact_id to add to source_artifact_ids.",
            inputSchema={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                    "project": {"type": "string"},
                },
                "required": ["goal", "question", "answer"],
            },
        ),
        Tool(
            name="resonance_research",
            description="Resonance RESEARCH (autonomous enrich). Web-research the goal's research-route gaps (factual/external) and ingest the cited findings as evidence — the machine-side counterpart to the interview. Returns new artifact_ids to add to source_artifact_ids; re-analyze and research gaps close. Slow (web).",
            inputSchema={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "source_artifact_ids": {"type": "array", "items": {"type": "string"}},
                    "max_gaps": {"type": "integer", "default": 3},
                    "project": {"type": "string"},
                },
                "required": ["goal"],
            },
        ),
        Tool(
            name="resonance_render",
            description="Resonance PREPARE + RENDER. Build a provenance-tagged PCO from the ingested docs and render it for an audience into a medium (prism | slides). audience is a person_id or target_group id (e.g. 'board'); omit for no audience lens. Returns the PCO digest + the media doc.",
            inputSchema={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "source_artifact_ids": {"type": "array", "items": {"type": "string"}},
                    "audience": {"type": "string", "description": "person_id or target_group id (e.g. 'board')."},
                    "media": {"type": "string", "enum": ["prism", "slides", "website"], "default": "prism"},
                    "brand_id": {"type": "string", "default": "okuro"},
                    "project": {"type": "string"},
                },
                "required": ["goal"],
            },
        ),
        Tool(
            name="resonance_session_create",
            description="Resonance ENRICH. Open a DURABLE, resumable interview session for a goal — owns the growing evidence set + Q/A log so you thread only a session_id (not the whole artifact list) and can resume later. Seed with any already-ingested doc ids.",
            inputSchema={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "project": {"type": "string"},
                    "person_id": {"type": "string", "description": "optional recipient/audience"},
                    "brand_id": {"type": "string"},
                    "source_artifact_ids": {"type": "array", "items": {"type": "string"}, "description": "already-ingested doc ids to seed the session"},
                },
                "required": ["goal"],
            },
        ),
        Tool(
            name="resonance_session_next",
            description="Resonance ENRICH. Compute + persist the next interview questions for a session (wraps resonance_interview over the session's evidence set). Updates cached completeness/ready. Ask the returned questions, then resonance_session_answer.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "max_q": {"type": "integer", "default": 3},
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="resonance_session_answer",
            description="Resonance ENRICH. Record one Q&A into a session — ingests it as evidence, attaches it, and recomputes completeness. No need to track artifact_ids yourself.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                },
                "required": ["session_id", "question", "answer"],
            },
        ),
        Tool(
            name="resonance_session_get",
            description="Resonance ENRICH. Load a durable interview session — goal, evidence set, cached completeness/questions, and the full turn log (for resume / UI).",
            inputSchema={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
        ),
        Tool(
            name="resonance_session_list",
            description="Resonance ENRICH. List interview sessions (newest first), optionally filtered by status (open|ready|closed) or project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["open", "ready", "closed"]},
                    "project": {"type": "string"},
                },
            },
        ),
        Tool(
            name="resonance_actuality_check",
            description="Resonance ACTUALITY. Is the grounding evidence still current? Flags stale source artifacts by age × volatility (web >30d, doc >180d, interview never). The freshness counterpart to resonance_analyze (which checks coverage). Run before rendering a deck on older sources.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_artifact_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["source_artifact_ids"],
            },
        ),
        Tool(
            name="resonance_actuality_refresh",
            description="Resonance ACTUALITY. Re-research the STALE web sources (their question is recovered from the source_ref) and ingest fresh findings — returns new artifact_ids to swap in. doc/interview staleness is reported but needs a human (re-upload / re-interview). Slow (web).",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_artifact_ids": {"type": "array", "items": {"type": "string"}},
                    "project": {"type": "string"},
                },
                "required": ["source_artifact_ids"],
            },
        ),
        # --- Reviews (contextual feedback on any okuro surface) ---
        Tool(
            name="review_add",
            description=(
                "Record what the user dislikes (or likes) about a specific "
                "okuro surface, IN CONTEXT. Use when the user comments on the "
                "product itself — a page, a component, a behaviour — rather "
                "than on the work being done. Append-only: the same surface "
                "can be reviewed many times and every verdict is kept, "
                "timestamped. blocker/annoyance auto-creates a todo; "
                "idea/praise is recorded without adding to the backlog."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "surface_id": {
                        "type": "string",
                        "description": (
                            "DECLARED identity of what was reviewed, e.g. "
                            "'task.flow-feedback-card'. Not the URL — routes "
                            "get renamed and identity must survive that."
                        ),
                    },
                    "comment": {"type": "string"},
                    "rating": {"type": "integer", "minimum": 1, "maximum": 5},
                    "severity": {
                        "type": "string",
                        "enum": ["blocker", "annoyance", "idea", "praise"],
                        "default": "annoyance",
                    },
                    "target_type": {
                        "type": "string",
                        "enum": [
                            "task", "subtask", "note", "person", "project",
                            "thought", "artifact", "role", "deck", "kg",
                            "unresolved",
                        ],
                        "default": "unresolved",
                    },
                    "target_id": {"type": "string"},
                    "route": {"type": "string"},
                    "project": {"type": "string"},
                    "make_todo": {"type": "boolean"},
                },
                "required": ["surface_id"],
            },
        ),
        Tool(
            name="review_list",
            description=(
                "Read reviews, newest first. Filter by surface_id to see what "
                "the user thinks of one thing over time, or by target to see "
                "everything said about a specific task/note/person."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "surface_id": {"type": "string"},
                    "target_type": {"type": "string"},
                    "target_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
        Tool(
            name="review_surfaces",
            description=(
                "Per-surface rollup: review count, latest verdict, average "
                "rating, open blockers. Which parts of okuro the user keeps "
                "complaining about."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "include_orphaned": {"type": "boolean", "default": False},
                },
            },
        ),
        # --- Redline (comment on an HTML document, per version; migration 145) ---
        Tool(
            name="redline_open",
            description=(
                "Open an HTML document for commenting and pin its bytes as a "
                "VERSION. Use before listing or resolving comments, and again "
                "after you regenerate the document — changed bytes create a "
                "new version whose comment list starts EMPTY, and the previous "
                "version's open comments are yours to resolve by hand. "
                "Returns a served URL carrying a per-document token (8h) and "
                "the /redline web route. Three sources: an html FILE under "
                "redline.roots in ~/.okuro/config.yaml (fails closed when "
                "empty), an okuro html ARTIFACT (every link of its supersede "
                "chain is one version), or a PRISM deck (the deck JSON's hash "
                "is the version; it renders in the SPA, so url is null and "
                "deck_url carries the snapshot)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["file", "artifact", "prism"],
                        "description": (
                            "file = html on disk under redline.roots; "
                            "artifact = okuro html artifact id, one version per "
                            "supersede; prism = deck2 deck id"
                        ),
                    },
                    "ref": {
                        "type": "string",
                        "description": "absolute path | artifact id | deck id",
                    },
                    "title": {"type": "string"},
                    "project": {"type": "string"},
                    "reload": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "accepted for callers that know the source changed; "
                            "the source is read on every open regardless"
                        ),
                    },
                    "version_seq": {
                        "type": "integer",
                        "description": (
                            "open an EXISTING version read-only instead of "
                            "pinning the current bytes. Only artifact and prism "
                            "versions can be rendered this way — a past file "
                            "version kept its hash and not its content, and "
                            "answers with renderable=false and a reason"
                        ),
                    },
                    "shots": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "also crop a PNG per anchored comment, so "
                            "redline_list returns a screenshot path. Launches a "
                            "headless browser and needs the optional 'browser' "
                            "extra; when it cannot, the answer says why instead "
                            "of failing the open"
                        ),
                    },
                },
                "required": ["kind", "ref"],
            },
        ),
        Tool(
            name="redline_list",
            description=(
                "The comments on ONE version of a document, each with the "
                "element excerpt, its text, an absolute file:line:col and WHY "
                "it can be trusted (identified_by). Comments never carry over "
                "between versions — act on the excerpt, not on a live lookup. "
                "Use format='markdown' to get one paste-ready block instead of "
                "the rows: that is the shape to put in a prompt when you are "
                "about to act on the owner's comments."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "version_id": {
                        "type": "string",
                        "description": "defaults to the newest version",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["open", "done", "all"],
                        "default": "open",
                    },
                    "include_replies": {"type": "boolean", "default": True},
                    "format": {
                        "type": "string",
                        "enum": ["json", "markdown"],
                        "default": "json",
                        "description": (
                            "markdown returns one paste-into-prompt block and "
                            "no rows — same facts, one shape, no second export "
                            "path to keep in step"
                        ),
                    },
                },
                "required": ["document_id"],
            },
        ),
        Tool(
            name="redline_resolve",
            description=(
                "Mark one comment done. The NOTE is the evidence and is "
                "required — an empty one is refused by the schema, not only by "
                "this tool. after_excerpt is the element's outerHTML after "
                "your fix; it is a hint beside the note, never a proof, "
                "because a cloned element is byte-identical to its original."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "comment_id": {"type": "string"},
                    "note": {
                        "type": "string",
                        "description": "what you changed. Non-empty, required.",
                    },
                    "after_excerpt": {"type": "string"},
                    "done_by": {"type": "string"},
                },
                "required": ["comment_id", "note"],
            },
        ),
        Tool(
            name="redline_versions",
            description=(
                "Every version of one document, newest first, with the open "
                "and done counts on each. Use it to find the version whose "
                "comments you still owe a resolution."
            ),
            inputSchema={
                "type": "object",
                "properties": {"document_id": {"type": "string"}},
                "required": ["document_id"],
            },
        ),
        # --- Todos ---
        Tool(
            name="todo_add",
            description=(
                "Register an actionable item that needs the user's attention "
                "later. Use this when you discover work to be done — NOT a "
                "persistent learning (write_memory), NOT an ephemeral idea "
                "(capture_thought), NOT a time-scheduled nudge (set_reminder). "
                "Surfaces on the Now page."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "One-line imperative"},
                    "detail": {"type": "string", "description": "Multi-line context"},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
                    "project": {"type": "string", "description": "Project slug"},
                    "due_at": {"type": "string", "description": "ISO-8601 timestamp"},
                    "source": {
                        "type": "string",
                        "enum": ["user", "agent", "system", "ingress"],
                        "default": "agent",
                    },
                    "source_event_id": {"type": "string"},
                    "context": {"type": "object"},
                    "reminder_id": {"type": "string"},
                },
                "required": ["title"],
            },
        ),
        Tool(
            name="todo_list",
            description=(
                "List todos. Default: active set (open+doing), ordered by "
                "priority+due_at. Returns a compact view — identity, state, "
                "and a truncated detail preview. Drill into a single todo "
                "with todo_get(todo_id) for full detail and provenance."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["open", "doing", "done", "dropped"],
                    },
                    "project": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                    "verbose": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Return full rows including the ingress provenance "
                            "blob. Large — a single project can exceed the "
                            "token ceiling. Prefer todo_get for one todo."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="todo_get",
            description="Get one todo by id — full detail and provenance.",
            inputSchema={
                "type": "object",
                "properties": {"todo_id": {"type": "string"}},
                "required": ["todo_id"],
            },
        ),
        Tool(
            name="todo_update",
            description=(
                "Patch a todo. Only non-null fields are applied. Setting "
                "status='done' stamps completed_at."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "todo_id": {"type": "string"},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["open", "doing", "done", "dropped"],
                    },
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                    "project": {"type": "string"},
                    "due_at": {"type": "string"},
                    "reminder_id": {"type": "string"},
                    "context": {"type": "object"},
                },
                "required": ["todo_id"],
            },
        ),
        Tool(
            name="todo_done",
            description="Mark a todo done. Shorthand for todo_update(id, status='done').",
            inputSchema={
                "type": "object",
                "properties": {"todo_id": {"type": "string"}},
                "required": ["todo_id"],
            },
        ),
        # --- Reminders ---
        Tool(
            name="set_reminder",
            description="Create a reminder with auto-generated escalation cascade (timing adapted to user's neurotype). Supports one-time or recurring (cron/interval).",
            inputSchema={
                "type": "object",
                "properties": {
                    "what": {"type": "string", "description": "What to be reminded about"},
                    "when": {"type": "string", "description": "ISO datetime or natural description ('tomorrow 9am', '2026-03-28T10:00')"},
                    "urgency": {"type": "integer", "default": 3, "minimum": 1, "maximum": 5, "description": "1=low, 3=normal, 5=critical"},
                    "channels": {"type": "array", "items": {"type": "string"}, "description": "Override channels (omit for profile-driven)"},
                    "repeat": {"type": "object", "description": "Recurrence: {every: '1w'} or {cron: '0 9 * * 1'}"},
                    "project": {"type": "string", "description": "Project slug for context"},
                    "context": {"type": "object", "description": "Extra context"},
                },
                "required": ["what", "when"],
            },
        ),
        Tool(
            name="list_reminders",
            description="List reminders, optionally filtered. Shows upcoming cascade steps and pending suggestions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["pending", "active", "snoozed", "done", "dismissed"]},
                    "upcoming_hours": {"type": "integer", "description": "Only show reminders due within N hours"},
                    "include_suggestions": {"type": "boolean", "default": False, "description": "Include pending auto-suggestions"},
                },
            },
        ),
        Tool(
            name="snooze_reminder",
            description="Snooze a reminder. Duration defaults to profile-driven value (ADHD=15min, neurotypical=60min).",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Reminder UUID"},
                    "duration_min": {"type": "integer", "description": "Snooze duration in minutes (omit for profile default)"},
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="dismiss_reminder",
            description="Dismiss a reminder permanently.",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Reminder UUID"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="acknowledge_reminder",
            description="Acknowledge a reminder (mark as seen).",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Reminder UUID"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="accept_suggestion",
            description="Accept a system-proposed reminder suggestion \u2014 creates the reminder.",
            inputSchema={
                "type": "object",
                "properties": {
                    "suggestion_id": {"type": "string"},
                    "override_when": {"type": "string", "description": "Override suggested due time"},
                    "override_urgency": {"type": "integer", "description": "Override suggested urgency"},
                },
                "required": ["suggestion_id"],
            },
        ),
        Tool(
            name="reject_suggestion",
            description="Reject a system-proposed reminder suggestion.",
            inputSchema={
                "type": "object",
                "properties": {"suggestion_id": {"type": "string"}},
                "required": ["suggestion_id"],
            },
        ),
        # --- Session retros (Meta-Harness P5) ---
        Tool(
            name="retro_list",
            description=(
                "List behavioral patterns discovered by session retros (causal post-mortems "
                "over low-score sessions). Use to see what prior agents got wrong and what "
                "the remedies were."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "batch_id": {"type": "string", "description": "Restrict to one retro batch"},
                    "limit": {"type": "integer", "default": 5, "maximum": 50},
                },
            },
        ),
        Tool(
            name="retro_run",
            description=(
                "Run a retro batch on-demand: compare recent low-score sessions against "
                "high-score ones and persist behavioral findings as gotcha memories. Normally "
                "runs weekly via the daemon."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "window_days": {"type": "integer", "default": 14},
                    "max_per_cohort": {"type": "integer", "default": 10, "maximum": 25},
                    "provider": {"type": "string", "description": "bridge provider id (default: claude)"},
                },
            },
        ),
        # --- Memory utility (Meta-Harness P8) ---
        Tool(
            name="memory_utility",
            description=(
                "Objective per-memory utility: average compliance score of sessions where a "
                "memory was surfaced vs the global baseline. Positive delta = memories that "
                "help. Negative delta = memories that may hurt or be noise. Use to decide "
                "what to supersede."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "Restrict to one memory"},
                    "min_surfacings": {"type": "integer", "default": 5, "description": "Ignore memories with fewer surfacings than this"},
                    "limit": {"type": "integer", "default": 20, "maximum": 200},
                },
            },
        ),
        Tool(
            name="memory_stale",
            description=(
                "List memories the CODE now contradicts — the retire-on-fix check. "
                "'contradicted_literal' (memory says X = a, code says X = b) and "
                "'dangling_path' (cites a file that no longer exists) are PROOF; "
                "'file_changed_since' is only a hint. Reports, never demotes: review "
                "each, then retire a stale one with write_memory(supersedes=<id>), "
                "keeping whatever is still true. Run after landing a change that "
                "invalidates documented behaviour. Complements memory_audit, which "
                "measures a memory's USEFULNESS, not its truth."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "min_confidence": {"type": "number", "default": 0.3, "description": "skip already-superseded rows"},
                    "proof_only": {"type": "boolean", "default": True, "description": "hide 'file changed since' hints"},
                    "limit": {"type": "integer", "default": 20},
                },
            },
        ),
        Tool(
            name="memory_audit",
            description=(
                "List memories whose surfacing correlates with lower compliance scores — "
                "candidates for review or supersede. Requires at least 20 surfacings for "
                "statistical weight."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "min_surfacings": {"type": "integer", "default": 20},
                    "flag_threshold": {"type": "number", "default": -0.10, "description": "delta ≤ this flags the memory"},
                },
            },
        ),
        Tool(
            name="memory_stats",
            description=(
                "Memory CREATION COUNTS over a date window, per day/week/month, "
                "split active vs superseded. READ-ONLY. This is the tool to reach "
                "for when a question is 'how many memories were written between X "
                "and Y, and how many were later retracted' — read_memory ranks by "
                "relevance and cannot answer it, and a 2026-07-29 audit of a "
                "nine-week window reported six of eight measurements unreachable "
                "for exactly this reason. `since` inclusive, `until` exclusive, "
                "ISO dates."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "group_by": {"type": "string", "enum": ["day", "week", "month"], "default": "week"},
                    "since": {"type": "string", "description": "ISO date/datetime, inclusive"},
                    "until": {"type": "string", "description": "ISO date/datetime, exclusive"},
                    "project": {"type": "string", "description": "Project slug filter"},
                    "topic": {"type": "string", "description": "Topic filter"},
                },
            },
        ),
        Tool(
            name="memory_census",
            description=(
                "Memory row counts over a date window grouped by topic, project or "
                "source_agent, with average confidence and a count of legacy "
                "confidence-1.0 rows. READ-ONLY. Pairs with memory_stats: that one "
                "answers 'how many per period', this one 'of what kind, and whose'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "since": {"type": "string", "description": "ISO date/datetime, inclusive"},
                    "until": {"type": "string", "description": "ISO date/datetime, exclusive"},
                    "group_by": {"type": "string", "enum": ["topic", "project", "source_agent"], "default": "topic"},
                },
            },
        ),
        Tool(
            name="artifact_stats",
            description=(
                "Artifact CREATION COUNTS over a date window, per day/week/month, "
                "split active vs superseded. READ-ONLY. artifact_list pages from "
                "either end of a sort order; before offset + created_after/"
                "created_before existed, a mid-store window was structurally "
                "unreachable and an audit had to report its largest population as "
                "unenumerated. Use this for the aggregate and artifact_list("
                "created_after=..., created_before=..., offset=...) for the rows."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "group_by": {"type": "string", "enum": ["day", "week", "month"], "default": "week"},
                    "since": {"type": "string", "description": "ISO date/datetime, inclusive"},
                    "until": {"type": "string", "description": "ISO date/datetime, exclusive"},
                    "project": {"type": "string"},
                    "kind": {"type": "string", "enum": ["report", "evidence", "plan"]},
                },
            },
        ),
        Tool(
            name="memory_recall_eval",
            description=(
                "Measure whether memory recall actually RETRIEVES — okuro's own "
                "quality gate, the same one the daily daemon task alarms on. "
                "Returns MRR, recall@k, top-1 and junk-rejection for the shipped "
                "hybrid read path, plus the pure-vector arm as a _SIM_FLOOR "
                "diagnostic, plus a pass/fail verdict against the catastrophe "
                "floors. Run this BEFORE claiming anything about recall quality "
                "or 'fixing' retrieval — a 2026-07 investigation concluded recall "
                "was broken from a measurement of the raw vector arm, which is "
                "not the path agents call, and nearly sent a session to repair a "
                "subsystem meeting every target. Queries are drawn from the "
                "store's own memories, never hand-written. COST: embeds one "
                "query per sample, so sample=400 is slow; sample<100 is flagged "
                "`noisy` and must not be quoted as the system's state."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sample": {"type": "integer", "default": 120, "minimum": 10, "maximum": 800,
                               "description": "memories to round-trip; <100 is noise, 400 for a number worth recording"},
                    "k": {"type": "integer", "default": 10, "description": "recall@k cutoff"},
                    "seed": {"type": "integer", "default": 7, "description": "sampling seed — vary it to see run-to-run spread"},
                },
            },
        ),
        # --- Behavioral rules (Meta-Harness P10) ---
        Tool(
            name="behavioral_rules",
            description=(
                "Return behavioral rules as structured {rule, rationale, evidence} triples. "
                "Use when you need to understand WHY a directive in the bootstrap contract "
                "exists before deciding how to apply it in an edge case. Without arguments "
                "returns every section; pass `section` to filter or `query` to fuzzy-match a "
                "specific rule."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "description": "Section id — implications / patterns / pet_peeves / avoid / preferred / hates / loves / strengths",
                    },
                    "query": {"type": "string", "description": "Fuzzy-match against rule text"},
                    "limit": {"type": "integer", "default": 10, "maximum": 50},
                },
            },
        ),

        # ── Principle sets ───────────────────────────────────────────────
        # Named bundles of decision-principle IDs. Where the atomic DPs
        # (DP01..DP10) live on the user profile, principle_sets compose
        # them into *project-level* constraint bundles referenced by a
        # brand's `principles` slot.
        Tool(
            name="principle_set_list",
            description=(
                "List principle sets (project-level constraint bundles). "
                "Each set references 1..N principle IDs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string",
                               "enum": ["active", "draft", "archived"]},
                },
            },
        ),
        Tool(
            name="principle_set_get",
            description=(
                "Get a principle set with its members expanded to full "
                "principle rows (title + description + priority)."
            ),
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="principle_set_upsert",
            description=(
                "Create or update a principle set. `principles` is a list "
                "of principle IDs (e.g. ['DP01','DP03']). Passing "
                "principles=null leaves existing members untouched."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id":          {"type": "string"},
                    "name":        {"type": "string"},
                    "description": {"type": "string"},
                    "status":      {"type": "string",
                                    "enum": ["active", "draft", "archived"]},
                    "principles":  {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                    },
                },
                "required": ["id", "name"],
            },
        ),
        Tool(
            name="principle_set_delete",
            description=(
                "Delete a principle set. Cascades to members via FK."
            ),
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        # --- Orchestrator (drive okuro orchestrator runs from inside an agent session) ---
        Tool(
            name="orchestrator_create",
            description=(
                "Create a new orchestrator task. Use when the agent needs to "
                "kick off a multi-step okuro run (decompose → execute → "
                "verify) on the user's behalf rather than doing the work "
                "inline. Returns {task_id, plan_summary, requires_approval}. "
                "If the orchestrator daemon is not running, returns an "
                "{error: ...} envelope explaining how to start it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "What the orchestrator should achieve."},
                    "mode": {
                        "type": "string",
                        "enum": list(_ORCH_VALID_MODES),
                        "default": "plan",
                        "description": "plan = decompose + wait for approval at every gate; auto-execute = run end-to-end; deliberate = synonym for plan.",
                    },
                    "intelligence": {
                        "type": "string",
                        "enum": list(_ORCH_VALID_INTELLIGENCE),
                        "default": "balanced",
                    },
                    "required_roles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional role IDs the decomposer MUST include.",
                    },
                    "preferred_cli": {"type": "string", "description": "claude | codex | antigravity — pin the executor CLI."},
                    "auto_approve_risk": {
                        "type": "string",
                        "default": "none",
                        "description": "'none' keeps every gate manual. Anything else flips auto_approve.",
                    },
                    "verify_command": {
                        "type": "string",
                        "description": (
                            "Build/test gate. When set, runs in project_path "
                            "after all subtasks finalize, before flipping the "
                            "task to 'done'. Non-zero exit fails the task. "
                            "Examples: 'pytest -q', 'npm test --silent', "
                            "'cargo test'. Use this when 'subagents claimed "
                            "success' is not enough."
                        ),
                    },
                    "autonomous": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Agent/MCP calls are autonomous (no human to answer "
                            "intake-clarity questions). True (default) skips the "
                            "blocking intake gate — the task never dead-ends and "
                            "the call returns without waiting on the clarity LLM. "
                            "The clarity score is still logged in the background. "
                            "Set False only to force the interactive intake gate."
                        ),
                    },
                    # ROCK-SOLID v5 P5.4 / P6.7. These MUST be declared here,
                    # not only on the Python signature: this inputSchema is
                    # hand-written and is the ONLY thing an MCP caller can see.
                    # A parameter present on the function and absent here is
                    # unreachable from every agent — the same shape that kept
                    # C12's dispatch_epoch guard from ever firing.
                    "fast_track": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Fast-track review profile. A step whose parts are "
                            "ALL risk=LOW and whose deterministic checks pass "
                            "clean skips the critic+scorer LLM stages. "
                            "Deterministic checks are never skipped, MED and "
                            "HIGH risk are always LLM-reviewed, and every skip "
                            "is logged in the activity feed. Off by default — "
                            "guessing this wrong ships unreviewed work."
                        ),
                    },
                    "audience": {
                        "type": "string",
                        "description": (
                            "Stream C recipient — a person_id from the people "
                            "store. When set, every deliverable a part produces "
                            "is delivered to that person as the part finishes. "
                            "Unset means no delivery, which is the default."
                        ),
                    },
                    "delivery_channel": {
                        "type": "string",
                        "enum": ["markdown", "marp", "microsite", "tts", "podcast"],
                        "description": (
                            "How the delivery is rendered. Defaults to markdown "
                            "when a recipient is set; ignored without one. MS "
                            "formats (PPTX/DOCX/XLSX) are not available."
                        ),
                    },
                    "delivery_brand_id": {
                        "type": "string",
                        "description": (
                            "Brand override for the delivery theme. Ignored "
                            "without a recipient."
                        ),
                    },
                },
                "required": ["description"],
            },
        ),
        Tool(
            name="orchestrator_status",
            description=(
                "Return the current state of an orchestrator task: "
                "{status, current_subtask, last_event_ts, "
                "artifacts_so_far, blocking_question}. Cheap — safe to poll."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="orchestrator_inspect",
            description=(
                "Read any orchestrator diagnostic surface. START HERE when "
                "investigating a run: view='tasks' lists tasks (no task_id "
                "needed) so you can find the one to inspect. "
                "Global views: tasks · system · active_roles · recurring · "
                "suggestions. Per-task views (need task_id): state · "
                "snapshot · activity · logs · gates · awaiting · "
                "checkpoints · review · artifacts · deliveries. "
                "Returns {view, task_id, data, truncated}; oversized "
                "payloads keep the newest entries and say what was dropped. "
                "For the append-only decision log use list_task_events."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "view": {
                        "type": "string",
                        "enum": sorted(_ORCH_INSPECT_VIEWS),
                        "default": "tasks",
                        "description": "Which surface to read. 'tasks' is the entry point.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Required for per-task views; rejected for global views.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Only for tasks (default 50), activity (200) and "
                            "logs (100). Rejected on views that take none."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="orchestrator_approve",
            description=(
                "Advance the current gate. action=approve | skip | abort. "
                "With subtask_id omitted, approve advances whatever blocks the "
                "task: a plan-approval gate (first waiting_approval subtask), a "
                "deliberate-mode panel_confirmation gate (confirms the proposed "
                "panel; override with roles/strategy), or a discussion_proceed "
                "gate (resolves it so execution continues). Returns "
                "{accepted, next_state}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "subtask_id": {"type": "string", "description": "Optional — defaults to first waiting_approval subtask."},
                    "action": {
                        "type": "string",
                        "enum": ["approve", "skip", "abort"],
                        "default": "approve",
                    },
                    "roles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional — role ids to confirm at a panel_confirmation gate. Defaults to the proposed panel.",
                    },
                    "strategy": {
                        "type": "string",
                        "enum": ["parallel", "sequential", "debate"],
                        "default": "parallel",
                        "description": "Panel deliberation strategy (panel_confirmation gate only).",
                    },
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="orchestrator_continue",
            description=(
                "Append continuation phases to an existing task. Provide "
                "EXACTLY ONE of description (free text) or suggestion_id "
                "(references a saved continuation_suggestions[] entry). "
                "Returns {accepted, task_id}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "description": {"type": "string", "description": "Free-text continuation instructions."},
                    "suggestion_id": {"type": "string", "description": "ID of a saved continuation suggestion."},
                },
                "required": ["task_id"],
            },
        ),
        # --- Knowledge Graph (temporal triples) ---
        Tool(
            name="kg_add",
            description=(
                "Add a temporal triple to the knowledge graph. Distinguishes "
                "'X was true Q1, Y is true Q2' from flat replacement (which "
                "agent_memory's supersedes does). NULL valid_to = still true. "
                "Idempotent on (subject, predicate, object, valid_from). "
                "Use for facts that have a timeline: people↔projects, stack "
                "decisions per period, ownership changes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subject":            {"type": "string"},
                    "predicate":          {"type": "string", "description": "e.g. owns, uses, depends_on, succeeded_by, member_of"},
                    "object":             {"type": "string"},
                    "valid_from":         {"type": "string", "description": "ISO date YYYY-MM-DD; partials accepted"},
                    "valid_to":           {"type": "string", "description": "Optional close date"},
                    "subject_type":       {"type": "string", "default": "concept", "description": "person | project | tool | concept | brand | other"},
                    "object_type":        {"type": "string", "default": "concept"},
                    "project":            {"type": "string", "description": "Project slug context"},
                    "source_memory_id":   {"type": "string", "description": "agent_memory.id that asserted this fact"},
                    "source_artifact_id": {"type": "string", "description": "artifacts.id that asserted this fact"},
                    "confidence":         {"type": "number", "default": 0.7},
                },
                "required": ["subject", "predicate", "object"],
            },
        ),
        Tool(
            name="kg_query",
            description=(
                "Query triples touching an entity. ``as_of`` filters to facts "
                "valid on a specific date (timeline reconstruction). "
                "direction = outgoing | incoming | both."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity":    {"type": "string"},
                    "as_of":     {"type": "string", "description": "ISO date — what was true on this day"},
                    "direction": {"type": "string", "enum": ["outgoing", "incoming", "both"], "default": "both"},
                    "project":   {"type": "string"},
                    "limit":     {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
                    "fuzzy":     {"type": "boolean", "default": True, "description": "On a lexical miss, resolve the term to the closest canonical entity name and retry (rows tagged resolved_from/resolved_to). Off = strict exact match."},
                },
                "required": ["entity"],
            },
        ),
        Tool(
            name="kg_resolve",
            description=(
                "Resolve an approximate term to canonical KG entity names "
                "(fuzzy/alias/normalized), ranked. Use to disambiguate before "
                "kg_query, or to see 'did you mean' candidates when a lookup "
                "came back empty. Returns [{name, entity_id, type, score, tier, matched}]."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "term":      {"type": "string"},
                    "type":      {"type": "string", "description": "Restrict to an entity type (person, concept, task, artifact, …)"},
                    "project":   {"type": "string"},
                    "limit":     {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
                    "threshold": {"type": "number", "default": 0.55, "description": "Minimum fuzzy score to include (0-1)"},
                },
                "required": ["term"],
            },
        ),
        Tool(
            name="kg_invalidate",
            description=(
                "Mark a triple's validity window as ending on ``ended`` "
                "(default: today). Only triples with NULL valid_to are "
                "affected. Use when a fact stops being true (project ended, "
                "stack swapped, role changed)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subject":   {"type": "string"},
                    "predicate": {"type": "string"},
                    "object":    {"type": "string"},
                    "ended":     {"type": "string", "description": "ISO date; defaults to today"},
                },
                "required": ["subject", "predicate", "object"],
            },
        ),
        Tool(
            name="kg_timeline",
            description=(
                "Chronological story for an entity (or all triples when "
                "``entity`` is omitted). Ordered by valid_from DESC, falls "
                "back to created_at."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity":  {"type": "string"},
                    "project": {"type": "string"},
                    "limit":   {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
                },
            },
        ),
        Tool(
            name="kg_stats",
            description="Knowledge-graph overview: entity count, triples (active/closed), entity types, top predicates.",
            inputSchema={"type": "object", "properties": {}},
        ),
        # --- Role / agent diary streams ---
        Tool(
            name="role_diary_write",
            description=(
                "Append a chronological narrative entry to a role/agent diary. "
                "Distinct from roles_learn (curated/typed insights) — diary is "
                "low-curation observation log: 'what I noticed this session'. "
                "At least one of role_id or agent must be set. Diary entries can "
                "later crystallize into role_knowledge via roles_learn."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entry":      {"type": "string", "description": "The verbatim observation/finding/decision"},
                    "role_id":    {"type": "string", "description": "Formal role from roles catalog (optional)"},
                    "agent":      {"type": "string", "description": "Free-form agent label (e.g. 'reviewer', 'research-bot')"},
                    "project":    {"type": "string"},
                    "topic":      {"type": "string", "description": "Short tag (e.g. 'auth-pr-review')"},
                    "session_id": {"type": "string", "description": "Link to session telemetry"},
                    "embed":      {"type": "boolean", "default": True},
                },
                "required": ["entry"],
            },
        ),
        Tool(
            name="role_diary_read",
            description=(
                "Last-N diary entries for a role/agent stream, newest first. "
                "Filter by role_id, agent, project, topic. At least one filter "
                "should be provided to scope the read."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "role_id": {"type": "string"},
                    "agent":   {"type": "string"},
                    "project": {"type": "string"},
                    "topic":   {"type": "string"},
                    "last_n":  {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
                },
            },
        ),
        Tool(
            name="role_diary_search",
            description=(
                "Semantic search over diary entries. Filter by role_id, agent, "
                "or project to scope the corpus."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query":   {"type": "string"},
                    "role_id": {"type": "string"},
                    "agent":   {"type": "string"},
                    "project": {"type": "string"},
                    "limit":   {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="role_diary_stats",
            description="Diary stream stats: total entries, distinct sessions, per-project counts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "role_id": {"type": "string"},
                    "agent":   {"type": "string"},
                },
            },
        ),
        # --- Memory tunnels (cross-project memory sharing) ---
        Tool(
            name="tunnel_link",
            description=(
                "Link a memory to a shared concept tag so it surfaces across "
                "every project that uses the same concept. Memories are "
                "project-scoped today — tunnels are the sideband that lets "
                "a learning written under project A be visible to agents "
                "working in project B. Idempotent on (concept, memory_id). "
                "Concepts are slug-normalized (lowercase, alphanumeric+hyphen)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "agent_memory.id to link"},
                    "concept":   {"type": "string", "description": "Free-form tag (e.g. 'mcp-middleware', 'sqlite-concurrency')"},
                    "note":      {"type": "string", "description": "Optional connector note explaining the link"},
                },
                "required": ["memory_id", "concept"],
            },
        ),
        Tool(
            name="tunnel_find",
            description=(
                "Memories linked to a concept. Use ``exclude_project`` to "
                "surface ONLY cross-project hits ('what does everyone else "
                "know about this?'). Returns full memory bodies."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "concept":         {"type": "string"},
                    "project":         {"type": "string", "description": "Restrict to this project plus system-wide"},
                    "exclude_project": {"type": "string", "description": "Omit memories from this project (useful for cross-project surfacing)"},
                    "limit":           {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                },
                "required": ["concept"],
            },
        ),
        Tool(
            name="tunnel_bridge",
            description=(
                "Concepts present in BOTH projects' tunnels — the intersection "
                "of two projects' learnings. Returns concept + per-project counts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_a": {"type": "string"},
                    "project_b": {"type": "string"},
                    "limit":     {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
                },
                "required": ["project_a", "project_b"],
            },
        ),
        Tool(
            name="tunnel_concepts",
            description=(
                "List concepts ranked by linked-memory and project count. "
                "Filter ``min_count`` to surface concepts that bridge ≥ N "
                "memories. Default min_count=2 hides single-memory concepts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "min_count": {"type": "integer", "default": 2, "minimum": 1},
                    "project":   {"type": "string"},
                    "limit":     {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
                },
            },
        ),
        Tool(
            name="kg_assert",
            description=(
                "Validate a claim against the knowledge graph. Detects: "
                "attribution conflict (different subject owns this p+o), "
                "superseded (different object active for this s+p), "
                "stale (matching triple was invalidated), "
                "temporal (claim as_of precedes recorded valid_from). "
                "Returns {verdict: ok|warn|conflict|unknown, confirms, conflicts, stale, notes}. "
                "Call BEFORE asserting facts you're not 100% sure of."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subject":        {"type": "string"},
                    "predicate":      {"type": "string"},
                    "object":         {"type": "string"},
                    "as_of":          {"type": "string", "description": "ISO date of the claim; defaults to today"},
                    "project":        {"type": "string"},
                    "min_confidence": {"type": "number", "default": 0.5, "description": "Skip KG triples below this confidence"},
                },
                "required": ["subject", "predicate", "object"],
            },
        ),
        # --- Transcripts (verbatim per-message ingest) ---
        Tool(
            name="transcript_sweep",
            description=(
                "Verbatim per-message ingest from a CLI transcript file or directory. "
                "Idempotent on deterministic ID = sha256(source:session_id:message_uuid); "
                "re-running on a partially-ingested transcript only writes new rows. "
                "Use this when you need true 'vague recall' over past sessions — "
                "what was said about X 3 weeks ago — that paraphrased memory cannot answer. "
                "Returns counts: {scanned, written, skipped, files}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Transcript file or directory (recursive)."},
                    "source": {
                        "type": "string",
                        "enum": ["claude-jsonl", "gemini", "codex"],
                        "default": "claude-jsonl",
                        "description": "Adapter key. claude-jsonl is the only fully implemented adapter today.",
                    },
                    "project": {"type": "string", "description": "Optional project slug to associate."},
                    "embed": {"type": "boolean", "default": True, "description": "Embed each message for semantic search."},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="transcript_adapters",
            description=(
                "List registered transcript source adapters with their name, "
                "default file extension, and description. Use to discover "
                "what `transcript_sweep` can ingest right now."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="transcript_search",
            description=(
                "Semantic search over verbatim transcript messages. Returns ranked rows "
                "with full original content (no paraphrasing). Filterable by project, "
                "source, role, or session_id. Falls back to chronological listing when "
                "embeddings are unavailable."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language query."},
                    "project": {"type": "string", "description": "Filter by project slug."},
                    "source": {"type": "string", "description": "Filter by adapter key (claude-jsonl, gemini, codex)."},
                    "role": {"type": "string", "description": "Filter by role (user, assistant, system, tool)."},
                    "session_id": {"type": "string", "description": "Filter to one session."},
                    "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
            },
        ),
        # --- ComfyUI generation (local image/video engine) ---
        Tool(
            name="comfy_generate",
            description=(
                "Generate an image on okuro's OWN coupled ComfyUI. Gates on edition (air=off) and "
                "the model's commercial licence, optimises the prompt for the model's family (booru "
                "vs natural-language), resolves a registered workflow (else a built-in family graph), "
                "runs it, and saves the PNG to the generations dir. Pass EITHER model_id (an installed "
                "okuro model — okuro resolves its checkpoint, family, and licence) OR ckpt_name + "
                "family explicitly. okuro launches/leases its own ComfyUI; it does not reuse a foreign "
                "instance unless OKURO_COMFYUI_ENDPOINTS is explicitly set."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "What to generate."},
                    "model_id": {"type": "string", "description": "Installed okuro model id — resolves checkpoint + family + licence. Preferred over the explicit fields."},
                    "ckpt_name": {"type": "string", "description": "Checkpoint filename as ComfyUI addresses it (use instead of model_id)."},
                    "family": {"type": "string", "enum": ["sdxl", "pony", "illustrious", "sd15", "flux"], "description": "Model family — sets prompt dialect + graph topology (required if no model_id)."},
                    "task": {"type": "string", "default": "text-to-image"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                    "seed": {"type": "integer"},
                    "license_id": {"type": "string", "description": "Licence id for the commercial gate (e.g. openrail, apache-2.0, stability-ai-community, flux-1-dev-non-commercial-license). Auto-resolved from model_id."},
                    "org_revenue_usd": {"type": "number", "description": "Org annual revenue (USD) for the community-licence <$1M gate."},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="comfy_workflow_status",
            description="Check whether a ComfyUI workflow is registered for a model+task — the 'no workflow yet, create one?' readiness check.",
            inputSchema={
                "type": "object",
                "properties": {
                    "model_key": {"type": "string"},
                    "task": {"type": "string", "default": "text-to-image"},
                },
                "required": ["model_key"],
            },
        ),
        Tool(
            name="comfy_install",
            description=(
                "Provision okuro's OWN coupled ComfyUI side-service (a pinned ComfyUI cloned into an "
                "okuro-owned home with its own venv, model dirs wired to okuro's store). Idempotent. "
                "Without consent=true it only REPORTS status; consent=true performs the install "
                "(clones GPL-3.0 ComfyUI as a separate process — okuro never imports/vendors it). "
                "advanced/pro editions only. The install is long-running (clone + torch)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "consent": {"type": "boolean", "default": False, "description": "true = install now (you agree to installing ComfyUI); false = report status only."},
                    "ref": {"type": "string", "description": "ComfyUI git ref to pin (default from OKURO_COMFYUI_REF)."},
                },
            },
        ),
        Tool(
            name="comfy_install_nodes",
            description=(
                "Install custom-node repos into okuro's coupled ComfyUI (consent-gated). A "
                "researched workflow may need node classes a stock ComfyUI lacks; the workflow-"
                "designer researches which repos provide them, then this installs them (git clone "
                "into custom_nodes + their requirements). Restart okuro's ComfyUI afterwards to load."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repos": {"type": "array", "items": {"type": "string"}, "description": "Git URLs of the custom-node repos to install."},
                    "consent": {"type": "boolean", "default": False, "description": "true = install (you agree to cloning third-party node code into okuro's ComfyUI)."},
                },
                "required": ["repos"],
            },
        ),
        # --- Distill: the lesson review surface ---
        # Mined lessons are CANDIDATES. Corroboration across a cluster proves
        # the defect is real; it does not prove the proposed remedy is right,
        # and an active lesson becomes a rule the user lives under. So the
        # candidate -> active step is a human decision, and these are how it
        # gets made. Migration 143 makes an unapproved active row
        # unrepresentable, so there is no path around them.
        Tool(
            name="distill_lessons_review",
            description=(
                "List mined lessons awaiting your decision — the review queue "
                "for okuro's self-improvement loop. Shows the claim, its class, "
                "the okuro surface and location it would change, how many "
                "DISTINCT sessions corroborated the defect, and bounded "
                "evidence snippets from those sessions' facets. Nothing here "
                "is in force: a lesson only becomes a proposal after "
                "distill_lesson_approve AND the corroboration threshold AND the "
                "dwell clock. Pass status='active' to see what IS in force, or "
                "'retired' to see what was rejected or retired on evidence."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["candidate", "active", "retired"], "default": "candidate"},
                    "limit": {"type": "integer", "default": 20},
                },
            },
        ),
        Tool(
            name="distill_lesson_approve",
            description=(
                "Approve a mined lesson — the second key on candidate -> active. "
                "Records WHO decided and when; it does NOT activate on its own. "
                "The next weekly maintenance pass promotes the lesson only if it "
                "also clears the corroboration threshold and the dwell clock, so "
                "approving an under-corroborated lesson does not force it "
                "through. Once active it emits an interaction_improvements row "
                "and is verified against a marker rate like any other proposal. "
                "approved_by must name a person — automation-shaped approvers "
                "('daemon', 'cron', 'auto') are refused."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lesson_id": {"type": "integer"},
                    "approved_by": {"type": "string", "description": "The person approving. Not a daemon."},
                },
                "required": ["lesson_id", "approved_by"],
            },
        ),
        Tool(
            name="distill_lesson_reject",
            description=(
                "Reject a mined lesson — retires it with the reason recorded. "
                "Needs no approval and no thresholds: rejecting REMOVES a "
                "candidate rule from consideration, which is the safe direction, "
                "so it is deliberately cheap. A reason is required — without one "
                "the row cannot be told apart from a lesson the lifecycle "
                "retired on evidence. Retirement is terminal; a defect that "
                "recurs is re-learned by mining as a new row with its own "
                "evidence."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lesson_id": {"type": "integer"},
                    "reason": {"type": "string", "description": "Why this should not become a rule."},
                    "rejected_by": {"type": "string", "default": ""},
                },
                "required": ["lesson_id", "reason"],
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch sense tools. No compliance middleware here — that's in the unified server."""
    from okuro.sense.mcp_middleware import (
        get_session_state, _mark_bootstrapped, _build_compliance_nudges,
    )
    _session_state = get_session_state()

    # --- ComfyUI generation ---
    if name == "comfy_generate":
        from okuro.inference import gen_tools
        from okuro.inference.comfy import ComfyError
        try:
            r = gen_tools.run_generation(**arguments)
        except ComfyError as exc:
            return _text(f"⚠️ generation blocked: {exc}")
        lines = [f"**Generated {r['count']} image(s)** (seed {r['seed']})"]
        lines.append(f"- licence: {r['license'].get('reason', 'n/a')}")
        if r.get("workflow_id"):
            lines.append(f"- workflow: {r['workflow_id']}")
        lines.append(f"- prompt used: {r['prompt_used']}")
        for p in r["paths"]:
            lines.append(f"- {p}")
        return _text("\n".join(lines))

    if name == "comfy_workflow_status":
        from okuro.inference import gen_tools
        r = gen_tools.workflow_status(**arguments)
        state = "✅ ready" if r["ready"] else "❌ none"
        line = f"**{r['model']}** / {r['task']}: {state} — {r['reason']}"
        if r.get("workflow_id"):
            line += f" ({r['workflow_id']})"
        return _text(line)

    if name == "comfy_install":
        from okuro.inference import comfy_install
        if not arguments.get("consent"):
            st = comfy_install.status()
            return _text(
                f"**okuro ComfyUI** — installed: {st['installed']} at `{st['home']}`. "
                f"Pass consent=true to install (clones GPL-3.0 ComfyUI as okuro's coupled "
                f"side-service; advanced/pro only; long-running).")
        try:
            st = comfy_install.install(consent=True, ref=arguments.get("ref", comfy_install.DEFAULT_REF))
        except comfy_install.ComfyInstallError as exc:
            return _text(f"⚠️ install blocked: {exc}")
        return _text(
            f"✅ okuro ComfyUI ready at `{st['home']}` "
            f"(venv `{st['venv_python']}`, model paths wired: {st['model_paths_wired']}).")

    if name == "comfy_install_nodes":
        from okuro.inference import comfy_nodes
        try:
            r = comfy_nodes.install_custom_nodes(
                arguments.get("repos", []), consent=arguments.get("consent", False))
        except comfy_nodes.ComfyNodeError as exc:
            return _text(f"⚠️ {exc}")
        msg = f"nodes — installed: {r['installed'] or 'none'} · skipped: {r['skipped'] or 'none'}"
        if r["restart_required"]:
            msg += " · restart okuro's ComfyUI to load them"
        return _text(msg)

    # --- Bootstrap ---
    if name == "bootstrap":
        from okuro.sense.bootstrap.assembler import assemble
        packet, project_slug, session_id = assemble(
            budget=arguments.get("budget"),
            sections_filter=arguments.get("sections"),
            task_hint=arguments.get("task_hint"),
            provider=arguments.get("provider", "unknown"),
            include_digest=arguments.get("include_digest", False),
        )
        # A packet missing a mandatory section is not a bootstrap. It was
        # possible to get one: a typo in `sections` yielded 418 bytes with no
        # contract, no principles, no identity — and _mark_bootstrapped ran
        # unconditionally, flipping the gate and instructing the agent to quote
        # "claude-code is bootstrapped" verbatim to the user. The system
        # asserted its own correctness inside the artifact that was empty, and
        # the agent then behaved like a stock model while reporting success.
        #
        # Behavioral is required on EVERY path. On the default (unfiltered) path
        # — the one every real session uses — `tools` and `session` are required
        # too: the budget allocator claims section minimums greedily and can
        # silently drop a mandatory section whose minimum does not fit (measured
        # for `tools`/`session` below ~1840 tok — see tests/sense/
        # test_bootstrap_budget_floor.py). Without those, an agent boots with no
        # toolbox reference and no close-out contract and still reports success.
        # A FILTERED call is a diagnostic special-case and keeps only the
        # behavioral requirement, so intentional single-section fetches are not
        # broken.
        # Markers sourced from the bootstrap CONTRACT (single source of truth) —
        # the allocator reserves exactly these sections first, so the gate and
        # the budget floor cannot drift apart. A filtered bootstrap is a
        # diagnostic special-case that keeps only the behavioral requirement.
        from okuro.sense.bootstrap.contract import verify_required_markers
        _missing = verify_required_markers(
            packet, filtered=bool(arguments.get("sections"))
        )
        if _missing:
            logging.getLogger(__name__).error(
                "bootstrap: assembled packet missing mandatory section(s) %r "
                "(sections=%r, %d chars) — refusing to mark the session "
                "bootstrapped", _missing, arguments.get("sections"), len(packet),
            )
            # Strip the greeting before returning. Prefixing a refusal to a
            # packet that still carries "## Bootstrap greeting (quote verbatim
            # ...)" and the line "claude-code is bootstrapped" hands the agent
            # two contradictory instructions and lets the more specific one
            # win. The remainder is kept for diagnosis.
            # `(?=\n## |\Z)` — the greeting is often the LAST surviving section
            # in a degenerate packet, so a lookahead requiring a following
            # header silently matches nothing. \Z covers end-of-string.
            import re as _re
            stripped = _re.sub(
                r"\n## Bootstrap greeting.*?(?=\n## |\Z)", "\n", packet, flags=_re.S
            )
            # Local import: _registry imports this module's siblings, so a
            # module-level import would risk a cycle.
            from okuro.mcp._registry import _error as _tool_error
            return _tool_error(
                "BOOTSTRAP INCOMPLETE — the assembled packet is missing "
                f"mandatory section(s): {', '.join(_missing)}. This session is "
                "NOT bootstrapped. No greeting is included below and none "
                "should be quoted.\n\n"
                f"Requested sections: {arguments.get('sections')!r}\n"
                "Call bootstrap() again without a `sections` filter.\n\n"
                "--- partial packet, for diagnosis only ---\n"
                + stripped
            )

        # B1: persist session_id in in-process state so session_report can
        # resolve the right session without reading a telemetry marker
        # that external processes' bootstraps may have overwritten.
        _mark_bootstrapped(
            provider=arguments.get("provider", "unknown"),
            task_hint=arguments.get("task_hint", ""),
            project=project_slug,
            session_id=session_id,
        )

        # Convention drift watch: if any supported provider's CLI version
        # changed since last bootstrap, fire a background probe to refresh
        # the observed-conventions cache. Cheap (3 `--version` calls) on
        # the steady state; no-op when no CLI bumped. Wrapped because a
        # cache fault must never break bootstrap itself.
        try:
            from okuro.sense.providers.probe import maybe_probe_async
            maybe_probe_async()
        except Exception:
            pass

        # The core is HALF a briefing when a project resolved. Say so IN the
        # packet, not only in the refusal the agent gets when it tries to work:
        # a directive the agent reads while still in the bootstrap turn costs it
        # nothing, and the refusal is the belt under it. Appended here rather
        # than in the assembler because `assemble_full` (chat.py) has no second
        # call to make and must not be told to make one.
        if project_slug:
            packet += (
                "\n\n## ⛔ THIS IS HALF YOUR PACKET — NEXT CALL IS MANDATORY\n\n"
                f"Your task hint resolved to project **{project_slug}**. Everything "
                "about it — memory, todos, reminders, progress, people, your role "
                "assignment, the codebase map — is in the PROJECT half, which you "
                "do not have yet.\n\n"
                ">> CALL THIS NOW, BEFORE ANY OTHER TOOL:\n"
                f"   mcp__okuro__bootstrap_project(slug='{project_slug}')\n\n"
                "Every other okuro tool is REFUSED until you do. The packet is split "
                "because both halves together exceed the 50,000-character result "
                "envelope, and over that line your host discards the whole result."
            )

        # Session footer is now a budget-protected section (build_session in sections.py)
        # — no more unbudgeted append that could get lost
        return _text(packet)

    if name == "bootstrap_project":
        from okuro.sense.bootstrap.assembler import assemble_project
        from okuro.sense.session_state import mark_project_half_fetched

        state = get_session_state()
        # Default BOTH from the session's own bootstrap. An agent that calls
        # this with no arguments gets exactly the half it was gated for, which
        # is the point: the cheapest correct call must also be the obvious one.
        slug = arguments.get("slug") or state.get("project") or state.get(
            "project_half_pending"
        )
        hint = arguments.get("task_hint") or state.get("task_hint") or ""
        provider = arguments.get("provider") or state.get("provider") or "unknown"

        try:
            packet, resolved = assemble_project(
                slug,
                task_hint=hint or None,
                provider=provider,
                include_digest=arguments.get("include_digest", False),
                sections_filter=arguments.get("sections"),
            )
        except Exception as exc:  # noqa: BLE001 — the gate must terminate
            # A GATE MUST HAVE A LEGAL MOVE. Clearing only on success was the
            # right instinct — a half that raised is a half not delivered — but
            # it left one path with no exit: a DETERMINISTIC assembly failure
            # (a malformed project row, a builder crash outside the per-section
            # _safe wrapper) refuses every other okuro tool, and the retry the
            # refusal asks for fails identically. The session then loops on the
            # only call it is allowed to make.
            #
            # So: degrade, announce, and terminate. The agent gets a half that
            # says exactly what is missing and why, in the packet where it
            # cannot be overlooked, and its tools come back. A thin half is
            # recoverable; a session with no legal move is not.
            logger.exception("bootstrap_project: assembly failed for %r", slug)
            packet = _project_half_failure_notice(slug, exc)
        mark_project_half_fetched()
        return _text(packet)

    # --- Memory ---
    if name == "read_memory":
        from okuro.sense.memory import read_memory
        # Agent-facing reads are session-ful: tag them 'read_memory' so their
        # surfacings carry the bootstrap-minted telemetry sid and feed
        # memory_utility. Internal RAG callers keep the 'system_read' default
        # (session-less) and stay out of the utility signal — G6.
        return _text(read_memory(**arguments, _surface_context="read_memory"))
    if name == "write_memory":
        from okuro.sense.memory import write_memory
        return _text(write_memory(**arguments))

    # --- Artifacts ---
    if name == "artifact_write":
        from okuro.sense.artifacts import artifact_write
        return _text(artifact_write(**_with_dispatch_epoch(arguments)))
    if name == "artifact_get":
        from okuro.sense.artifacts import artifact_get
        result = artifact_get(
            artifact_id=arguments["artifact_id"],
            include_body=arguments.get("include_body", True),
        )
        return _text(result) if result else _text(
            f"Artifact not found: {arguments['artifact_id']}"
        )
    if name == "artifact_search":
        from okuro.sense.artifacts import artifact_search
        return _text(artifact_search(
            query=arguments["query"],
            kind=arguments.get("kind"),
            project=arguments.get("project"),
            parent_id=arguments.get("parent_id"),
            limit=int(arguments.get("limit", 10)),
            min_confidence=float(arguments.get("min_confidence", 0.0)),
            audience=arguments.get("audience"),
            include_superseded=bool(arguments.get("include_superseded", False)),
        ))
    if name == "artifact_list":
        from okuro.sense.artifacts import artifact_list
        return _text(artifact_list(
            kind=arguments.get("kind"),
            project=arguments.get("project"),
            parent_id=arguments.get("parent_id"),
            task_id=arguments.get("task_id"),
            subtask_id=arguments.get("subtask_id"),
            limit=int(arguments.get("limit", 50)),
            offset=int(arguments.get("offset", 0)),
            order=arguments.get("order", "created_at_desc"),
            audience=arguments.get("audience"),
            include_superseded=bool(arguments.get("include_superseded", True)),
            created_after=arguments.get("created_after"),
            created_before=arguments.get("created_before"),
        ))
    if name == "artifact_supersede":
        from okuro.sense.artifacts import artifact_supersede
        return _text(artifact_supersede(
            old_id=arguments["old_id"],
            new_id=arguments["new_id"],
        ))
    if name == "artifact_delete":
        from okuro.sense.artifacts import artifact_delete
        return _text(artifact_delete(artifact_id=arguments["artifact_id"]))

    # --- Project envelope ---
    if name == "project_inventory":
        from okuro.sense.envelope import project_inventory
        return _text(project_inventory(
            slug=arguments["slug"],
            include_superseded=bool(arguments.get("include_superseded", False)),
            tables=arguments.get("tables"),
            limit_per_table=int(arguments.get("limit_per_table", 500)),
            excerpt_chars=int(arguments.get("excerpt_chars", 150)),
            include_bindings=bool(arguments.get("include_bindings", True)),
        ))
    if name == "project_envelope":
        from okuro.sense.envelope import project_envelope
        return _text(project_envelope(
            slug=arguments["slug"],
            ids=arguments.get("ids"),
            match=arguments.get("match"),
            match_regex=bool(arguments.get("match_regex", False)),
            match_fields=arguments.get("match_fields", "auto"),
            from_projects=arguments.get("from_projects"),
            tables=arguments.get("tables"),
            exclude_ids=arguments.get("exclude_ids"),
            created_after=arguments.get("created_after"),
            created_before=arguments.get("created_before"),
            include_superseded=bool(arguments.get("include_superseded", True)),
            dry_run=bool(arguments.get("dry_run", True)),
            auto_register=bool(arguments.get("auto_register", True)),
            plan_ref=arguments.get("plan_ref"),
            excerpt_chars=int(arguments.get("excerpt_chars", 150)),
        ))
    if name == "project_envelope_undo":
        from okuro.sense.envelope import project_envelope_undo
        return _text(project_envelope_undo(
            backup_ref=arguments["backup_ref"],
            dry_run=bool(arguments.get("dry_run", False)),
        ))

    # --- Task events (M2) ---
    if name == "emit_task_event":
        from okuro.sense.task_events import append_event, EventConflictError
        # Pull task.adrs to make ADR conflict-detect work from MCP path.
        task_adrs: list[dict] = []
        try:
            from okuro.orchestrator.config import Config
            from okuro.orchestrator.state import load_task
            cfg = Config.load() if hasattr(Config, "load") else Config()
            tdir = getattr(cfg, "tasks_dir", None)
            if tdir:
                t = load_task(arguments["task_id"], tdir)
                task_adrs = list(getattr(t, "adrs", []) or [])
        except Exception:
            task_adrs = []
        try:
            eid = append_event(
                task_id=arguments["task_id"],
                subtask_id=arguments["subtask_id"],
                event_type=arguments["event_type"],
                body=arguments["body"],
                from_role=arguments.get("from_role", ""),
                supersedes=arguments.get("supersedes"),
                confidence=float(arguments.get("confidence", 0.8)),
                created_by=arguments.get("from_role", ""),
                adrs=task_adrs,
            )
            return _text({"ok": True, "event_id": eid})
        except EventConflictError as exc:
            return _text({"ok": False, "rejected": True, "error": str(exc)})
        except ValueError as exc:
            return _text({"ok": False, "rejected": True, "error": f"REJECTED: {exc}"})
    if name == "list_task_events":
        from okuro.sense.task_events import list_events
        return _text(list_events(
            task_id=arguments["task_id"],
            event_type=arguments.get("event_type"),
            since_seq=int(arguments.get("since_seq", 0)),
            limit=int(arguments.get("limit", 500)),
        ))
    if name == "precheck_deliverable":
        from okuro.orchestrator.reviewer.precheck import precheck_deliverable
        return _text(precheck_deliverable(
            task_id=str(arguments.get("task_id") or ""),
            subtask_id=str(arguments.get("subtask_id") or ""),
            body=str(arguments.get("body") or ""),
            kind=arguments.get("kind"),
            acceptance_criteria=arguments.get("acceptance_criteria"),
        ))
    if name == "review_loop_stats":
        from okuro.sense.review_stats import review_loop_stats
        return _text(review_loop_stats(
            window_days=int(arguments.get("window_days", 30)),
            task_id=arguments.get("task_id"),
        ))

    # --- Role-handovers (Stream A) ---
    if name == "write_role_handover":
        from okuro.sense.role_handover import write_role_handover
        return _text(write_role_handover(
            subtask_id=arguments["subtask_id"],
            from_role=arguments["from_role"],
            brief=arguments["brief"],
            task_id=arguments.get("task_id"),
            to_subtask_id=arguments.get("to_subtask_id"),
            to_role=arguments.get("to_role"),
            cortex_refs=arguments.get("cortex_refs"),
            kg_edges=arguments.get("kg_edges"),
            artifact_refs=arguments.get("artifact_refs"),
            memory_refs=arguments.get("memory_refs"),
            supersedes=arguments.get("supersedes"),
            confidence=float(arguments.get("confidence", 0.8)),
            project=arguments.get("project"),
            # P4.4 — same server-side stamp as artifact_write. Stream A has
            # the identical straggler problem: a handover from a superseded
            # dispatch is what the NEXT subagent reads as its context.
            dispatch_epoch=_with_dispatch_epoch({}).get("dispatch_epoch"),
        ))
    if name == "read_role_handover":
        from okuro.sense.role_handover import read_role_handover
        result = read_role_handover(
            subtask_id=arguments.get("subtask_id"),
            handover_id=arguments.get("handover_id"),
            task_id=arguments.get("task_id"),
            include_superseded=bool(arguments.get("include_superseded", False)),
        )
        return _text(result) if result else _text(
            "No role-handover found for the given filters."
        )
    if name == "list_role_handovers":
        from okuro.sense.role_handover import list_role_handovers
        return _text(list_role_handovers(
            task_id=arguments.get("task_id"),
            subtask_id=arguments.get("subtask_id"),
            from_role=arguments.get("from_role"),
            status=arguments.get("status"),
            limit=int(arguments.get("limit", 50)),
            include_superseded=bool(arguments.get("include_superseded", False)),
        ))

    # --- Review queue (session-loop subagent retry — PR 2 of 4) ---
    if name == "await_review":
        from okuro.sense.review_queue import await_review as _await_review
        subtask_id = arguments.get("subtask_id")
        artifact_id = arguments.get("artifact_id")
        if not subtask_id or not str(subtask_id).strip():
            return _text({"error": "subtask_id is required (non-empty string)"})
        if not artifact_id or not str(artifact_id).strip():
            return _text({"error": "artifact_id is required (non-empty string)"})
        try:
            result = await _await_review(
                subtask_id=str(subtask_id),
                artifact_id=str(artifact_id),
                timeout_s=float(arguments.get("timeout_s", 1800.0)),
                keep_alive_interval_s=float(arguments.get(
                    "keep_alive_interval_s", 30.0,
                )),
            )
        except ValueError as exc:
            # Defensive — the explicit checks above should catch empties,
            # but the primitive also validates and we surface its message
            # verbatim rather than raise out of an MCP handler.
            return _text({"error": str(exc)})
        return _text(result)

    # --- Deliveries (Stream C) ---
    if name == "delivery_send":
        from okuro.peer.delivery.pipeline import send as _delivery_send
        return _text(_delivery_send(
            arguments["artifact_id"],
            person_id=arguments.get("person_id"),
            channel=arguments.get("channel", "markdown"),
            brand_id=arguments.get("brand_id"),
            title=arguments.get("title"),
            context=arguments.get("context"),
        ))
    if name == "delivery_list":
        from okuro.peer.delivery.store import delivery_list
        return _text(delivery_list(
            artifact_id=arguments.get("artifact_id"),
            person_id=arguments.get("person_id"),
            channel=arguments.get("channel"),
            brand_id=arguments.get("brand_id"),
            success=arguments.get("success"),
            limit=int(arguments.get("limit", 50)),
            order=arguments.get("order", "created_at_desc"),
        ))
    if name == "delivery_get":
        from okuro.peer.delivery.store import delivery_get
        result = delivery_get(
            arguments["delivery_id"],
            include_body=bool(arguments.get("include_body", True)),
            include_blob=bool(arguments.get("include_blob", False)),
        )
        return _text(result) if result else _text(
            f"Delivery not found: {arguments['delivery_id']}"
        )
    if name == "delivery_delete":
        from okuro.peer.delivery.store import delivery_delete
        return _text(delivery_delete(arguments["delivery_id"]))

    # --- Thoughts ---
    if name == "capture_thought":
        from okuro.sense.thoughts import capture_thought
        return _text(capture_thought(**arguments))
    if name == "search_thoughts":
        from okuro.sense.thoughts import search_thoughts
        return _text(search_thoughts(**arguments))
    if name == "update_thought":
        from okuro.sense.thoughts import update_thought
        return _text(update_thought(**arguments))
    if name == "daily_digest":
        from okuro.sense.thoughts import daily_digest
        return _text(daily_digest(**arguments))

    # --- Progress ---
    if name == "log_progress":
        from okuro.sense.progress import log_progress
        return _text(log_progress(**arguments))
    if name == "get_progress":
        from okuro.sense.progress import get_progress
        return _text(get_progress(**arguments))

    # --- Projects ---
    if name == "list_projects":
        from okuro.sense.projects import list_projects
        return _text(list_projects())
    if name == "get_project":
        from okuro.sense.projects import get_project
        return _text(get_project(arguments["slug"]))
    if name == "projects_overview":
        from okuro.sense.overview import projects_overview
        return _text(projects_overview(
            include_repo=arguments.get("include_repo", True),
            active_only=arguments.get("active_only", True),
            touched_within_days=arguments.get("touched_within_days"),
            slugs=arguments.get("slugs"),
        ))
    if name == "register_project":
        from okuro.sense.projects import register_project
        args = {k: v for k, v in arguments.items() if k != "slug"}
        return _text(register_project(arguments["slug"], **args))
    if name == "update_project":
        from okuro.sense.projects import update_project_agent
        args = {k: v for k, v in arguments.items() if k != "slug"}
        return _text(update_project_agent(arguments["slug"], **args))
    if name == "get_project_charter":
        from okuro.sense.projects import get_project_charter
        return _text(get_project_charter(arguments["slug"]) or "(no charter set)")
    if name == "set_project_charter":
        from okuro.sense.projects import set_project_charter
        return _text(set_project_charter(arguments["slug"], arguments["charter"]))
    if name == "project_status":
        from okuro.sense.status import project_status
        return _text(project_status(
            slug=arguments["slug"],
            history_limit=int(arguments.get("history_limit", 8)),
            session_limit=int(arguments.get("session_limit", 10)),
            include_charter=bool(arguments.get("include_charter", False)),
        ))
    if name == "project_phases_set":
        from okuro.sense.phases import project_phases_set
        return _text(project_phases_set(
            project=arguments["project"],
            phases=arguments["phases"],
            replace=bool(arguments.get("replace", False)),
        ))

    # --- Managed repos ---
    if name == "repo_add":
        import json as _json
        from okuro.repos import add_repo
        rec = add_repo(
            url=arguments["url"],
            workspace=arguments.get("workspace", "default"),
            tier=arguments.get("tier", "air"),
            name=arguments.get("name"),
            token_key=arguments.get("token_key"),
            username=arguments.get("username"),
            pr_username=arguments.get("pr_username"),
        )
        return _text(_json.dumps(rec, indent=2))
    if name == "repo_push":
        import json as _json
        from okuro.repos import push_repo
        rec = push_repo(
            arguments["repo_id"],
            token_key=arguments.get("token_key"),
            username=arguments.get("username"),
            pr_username=arguments.get("pr_username"),
            branch=arguments.get("branch"),
            title=arguments.get("title"),
            open_pr=arguments.get("open_pr", True),
        )
        return _text(_json.dumps(rec, indent=2))
    if name == "repo_discover":
        import json as _json
        from okuro.repos.discovery import discover_repos
        rec = discover_repos(
            workspace=arguments.get("workspace"),
            host=arguments.get("host"),
            token_key=arguments.get("token_key"),
        )
        return _text(_json.dumps(rec, indent=2))
    if name == "repo_update":
        import json as _json
        from okuro.repos import update_repo
        _fields = {
            k: arguments[k]
            for k in ("url", "tier", "default_branch", "token_key", "username", "pr_username")
            if k in arguments
        }
        rec = update_repo(
            arguments["repo_id"],
            verify=arguments.get("verify", True),
            force=arguments.get("force", False),
            **_fields,
        )
        return _text(_json.dumps(rec, indent=2))
    if name == "repo_credentials":
        import json as _json
        from okuro.repos import update_credential
        rec = update_credential(
            token_key=arguments.get("token_key"),
            username=arguments.get("username"),
            host=arguments.get("host"),
            new_token_key=arguments.get("new_token_key"),
            new_username=arguments.get("new_username"),
            new_pr_username=arguments.get("new_pr_username"),
            verify=arguments.get("verify", True),
            force=arguments.get("force", False),
        )
        return _text(_json.dumps(rec, indent=2))
    if name == "repo_sync":
        import json as _json
        from okuro.repos import sync_repo
        rec = sync_repo(arguments["repo_id"], token_key=arguments.get("token_key"))
        return _text(_json.dumps(rec, indent=2))
    if name == "repo_list":
        import json as _json
        from okuro.repos import list_repos
        return _text(_json.dumps(list_repos(arguments.get("workspace")), indent=2))
    if name == "repo_remove":
        import json as _json
        from okuro.repos import remove_repo
        rec = remove_repo(arguments["repo_id"], delete_files=arguments.get("delete_files", False))
        return _text(_json.dumps(rec, indent=2))
    # --- Managed corpora ---
    if name == "corpus_add":
        import json as _json
        from okuro.corpus import add_corpus
        rec = add_corpus(
            url=arguments.get("url"),
            source_type=arguments.get("source_type"),
            config=arguments.get("config") or {},
            name=arguments.get("name"),
            workspace=arguments.get("workspace", "default"),
            token_key=arguments.get("token_key"),
            username=arguments.get("username"),
        )
        return _text(_json.dumps(rec, indent=2))
    if name == "corpus_sync":
        import json as _json
        from okuro.corpus import sync_corpus
        rec = sync_corpus(arguments["corpus_id"], full=arguments.get("full", False))
        return _text(_json.dumps(rec, indent=2))
    if name == "corpus_list":
        import json as _json
        from okuro.corpus import describe_types, list_corpora
        return _text(_json.dumps({
            "corpora": list_corpora(arguments.get("workspace")),
            "source_types": describe_types(),
        }, indent=2))
    if name == "corpus_remove":
        import json as _json
        from okuro.corpus import remove_corpus
        rec = remove_corpus(arguments["corpus_id"], delete_files=arguments.get("delete_files", False))
        return _text(_json.dumps(rec, indent=2))
    if name == "codegraph_insights":
        import json as _json
        from okuro.cortex.codegraph import insights_summary
        return _text(_json.dumps(insights_summary(arguments["project"], top_n=arguments.get("top_n", 15)), indent=2))
    if name == "codegraph_audit":
        import json as _json
        from okuro.cortex.codegraph import index_audit
        return _text(_json.dumps(index_audit(arguments["project"], top_n=arguments.get("top_n", 15)), indent=2))
    if name == "cross_repo_build":
        import json as _json
        from okuro.cortex.codegraph import build_cross_repo_graph
        return _text(_json.dumps(build_cross_repo_graph(), indent=2))
    if name == "cross_repo_connections":
        import json as _json
        from okuro.cortex.codegraph import cross_repo_connections
        return _text(_json.dumps(cross_repo_connections(
            node=arguments.get("node"), anchor=arguments.get("anchor"),
            limit=arguments.get("limit", 50)), indent=2))
    if name == "cross_repo_search":
        import json as _json
        from okuro.cortex.codegraph import cross_repo_search
        return _text(_json.dumps(cross_repo_search(
            arguments["query"], limit=arguments.get("limit", 25)), indent=2))
    if name == "cross_repo_insights":
        import json as _json
        from okuro.cortex.codegraph import cross_repo_insights
        return _text(_json.dumps(cross_repo_insights(top_n=arguments.get("top_n", 20)), indent=2))

    # --- Brain ---
    if name == "brain_advise":
        from okuro.sense.advisor import advise
        return _text(advise(arguments["task_hint"]))
    if name == "get_principles":
        from okuro.sense.principles import get_principles
        return _text(get_principles(ids=arguments.get("ids")))

    # --- Principle sets ---
    if name == "principle_set_list":
        from okuro.sense.principle_sets import list_principle_sets
        return _text(list_principle_sets(status=arguments.get("status")))
    if name == "principle_set_get":
        from okuro.sense.principle_sets import get_principle_set
        result = get_principle_set(arguments["id"])
        return _text(result) if result else _text(
            f"Principle set not found: {arguments['id']}"
        )
    if name == "principle_set_upsert":
        from okuro.sense.principle_sets import upsert_principle_set
        return _text(upsert_principle_set(
            arguments["id"],
            name=arguments["name"],
            description=arguments.get("description", ""),
            status=arguments.get("status", "active"),
            principles=arguments.get("principles"),
        ))
    if name == "principle_set_delete":
        from okuro.sense.principle_sets import delete_principle_set
        ok = delete_principle_set(arguments["id"])
        return _text({"ok": ok, "id": arguments["id"]})

    # --- Profile ---
    if name == "get_profile":
        from okuro.yu.profile import get_profile
        return _text(get_profile(section=arguments.get("section")))
    if name == "update_profile":
        from okuro.yu.profile import update_profile
        return _text(update_profile(**arguments))

    # --- Session ---
    if name == "session_report":
        from okuro.sense.telemetry import session_report as _session_report
        from okuro.telemetry.logger import mark_session_reported
        # B1: resolve session_id from in-process state (set at bootstrap),
        # NOT from the telemetry marker. External processes' bootstraps
        # overwrite the marker; relying on it mis-attributed reports.
        sid = _session_state.get("session_id") or "unknown"
        _session_state["session_reported"] = True
        mark_session_reported()
        result = _run_session_report_with_deadline(
            _session_report,
            session_id=sid,
            provider=_session_state.get("provider", "unknown"),
            task_hint=_session_state.get("task_hint", ""),
            project=_session_state.get("project"),
            feedback=arguments.get("feedback"),
        )
        return _text(result)
    if name == "session_history":
        from okuro.sense.telemetry import session_history
        return _text(session_history(**arguments))
    if name == "session_score":
        return _text({
            "bootstrap_called": _session_state["bootstrapped"],
            "memory_written": _session_state["memory_written"],
            "progress_logged": _session_state["progress_logged"],
            "cortex_used": _session_state["cortex_used"],
            "session_reported": _session_state["session_reported"],
            "tool_calls": _session_state["tool_calls"],
            "provider": _session_state["provider"],
        })
    if name == "tool_performance":
        from okuro.sense.telemetry import tool_performance
        return _text(tool_performance(**arguments))
    if name == "compliance_scorecard":
        from okuro.sense.telemetry import compliance_scorecard
        return _text(compliance_scorecard(**arguments))
    if name == "run_maintenance":
        from okuro.sense.improvement import run_maintenance
        return _text(run_maintenance())

    # --- People ---
    if name == "person_add":
        from okuro.peer.persons import person_add
        args = dict(arguments)
        # back-compat: the schema once exposed `relationship` (no matching param,
        # raised InputValidationError). Map it to the real freeform field.
        if "relationship" in args:
            args.setdefault("relation_to_user", args.pop("relationship"))
        return _text(person_add(**args))
    if name == "person_get":
        from okuro.peer.persons import person_get
        return _text(person_get(arguments["person_id"]))
    if name == "person_list":
        from okuro.peer.persons import person_list
        return _text(person_list(**arguments))
    if name == "person_update":
        from okuro.peer.persons import person_update
        return _text(person_update(**arguments))
    if name == "person_update_sliders":
        from okuro.peer.persons import person_update_sliders
        try:
            return _text(person_update_sliders(**arguments))
        except ValueError as e:
            return _text(f"Error: {e}")
    if name == "person_lens":
        from okuro.peer.persons import person_lens
        return _text(person_lens(**arguments))
    if name == "person_match":
        from okuro.peer.persons import person_match
        return _text(person_match(**arguments))
    if name == "person_translate":
        from okuro.peer.translate import person_translate
        import json as _json
        result = person_translate(**arguments)
        if result.get("success"):
            header = f"**{result['person']['display_name']}** via {result.get('provider')}/{result.get('model')} in {result.get('duration_ms')}ms"
            return _text(f"{header}\n\n{result.get('translated') or ''}")
        return _text(
            f"Translation failed for `{result.get('person_id') or arguments.get('person_id')}`: {result.get('error')}\n\n"
            f"raw={_json.dumps(result, indent=2, default=str)}"
        )

    # --- People CRM (relational) ---
    if name == "person_merge":
        from okuro.peer.persons import person_merge
        return _text(person_merge(**arguments))
    if name == "company_add":
        from okuro.peer.crm import company_add
        return _text(company_add(**arguments))
    if name == "company_get":
        from okuro.peer.crm import company_get
        return _text(company_get(arguments["company_id"]))
    if name == "company_list":
        from okuro.peer.crm import company_list
        return _text(company_list())
    if name == "company_set_brand":
        from okuro.peer.crm import company_set_brand
        return _text(company_set_brand(**arguments))
    if name == "affiliation_add":
        from okuro.peer.crm import affiliation_add
        return _text(affiliation_add(**arguments))
    if name == "affiliation_list":
        from okuro.peer.crm import affiliation_list
        return _text(affiliation_list(**arguments))
    if name == "affiliation_set_primary":
        from okuro.peer.crm import affiliation_set_primary
        return _text(affiliation_set_primary(**arguments))
    if name == "affiliation_remove":
        from okuro.peer.crm import affiliation_remove
        return _text(affiliation_remove(**arguments))
    if name == "connection_add":
        from okuro.peer.crm import connection_add
        return _text(connection_add(**arguments))
    if name == "connection_list":
        from okuro.peer.crm import connection_list
        return _text(connection_list(**arguments))
    if name == "connection_remove":
        from okuro.peer.crm import connection_remove
        return _text(connection_remove(**arguments))
    if name == "engagement_add":
        from okuro.peer.crm import engagement_add
        return _text(engagement_add(**arguments))
    if name == "engagement_list":
        from okuro.peer.crm import engagement_list
        return _text(engagement_list(**arguments))
    if name == "engagement_resolve":
        from okuro.peer.crm import engagement_resolve
        return _text(engagement_resolve(**arguments))

    # --- Target groups (audience registry) ---
    if name == "target_group_add":
        from okuro.peer.target_groups import target_group_add
        return _text(target_group_add(**arguments))
    if name == "target_group_get":
        from okuro.peer.target_groups import target_group_get
        return _text(target_group_get(arguments["group_id"]))
    if name == "target_group_list":
        from okuro.peer.target_groups import target_group_list
        return _text(target_group_list())
    if name == "target_group_match":
        from okuro.peer.target_groups import match_groups
        hits = match_groups(**arguments)
        if not hits:
            return _text("No matching audience. Try target_group_list, or the "
                         "group may have no profiled members yet.")
        lines = ["| Match | Group | Members | Similarity |", "|---|---|---|---|"]
        for i, h in enumerate(hits, 1):
            lines.append(
                f"| {i} | {h['name']} (`{h['id']}`) | {h['member_count']} "
                f"| {h['similarity']:.2f} |"
            )
        return _text("\n".join(lines))
    if name == "target_group_add_member":
        from okuro.peer.target_groups import target_group_add_member
        return _text(target_group_add_member(**arguments))
    if name == "target_group_remove_member":
        from okuro.peer.target_groups import target_group_remove_member
        return _text(target_group_remove_member(**arguments))
    if name == "seed_standard_groups":
        from okuro.peer.target_groups import seed_standard_groups
        return _text(seed_standard_groups())

    # --- Person provenance (sourced, confidence-scored claims) ---
    if name == "person_source_add":
        from okuro.peer.sources import person_source_add
        args = dict(arguments)
        raw = args.pop("value")
        try:
            import json as _json
            value = _json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            value = raw
        res = person_source_add(value=value, **args)
        return _text(
            f"Claim recorded for `{args['person_id']}` · {args['field_path']} "
            f"= {value!r} (confidence {args.get('confidence', 0.8)}"
            + (f", source {args['source_url']}" if args.get("source_url") else "")
            + f") — {res['sources_written']} source row(s)."
        )
    if name == "person_sources_list":
        from okuro.peer.sources import list_sources
        rows = list_sources(arguments["person_id"],
                            applied_only=arguments.get("applied_only", True))
        if not rows:
            return _text("No sources recorded for this person.")
        lines = ["| Field | Value | Conf | Type | Source |",
                 "|---|---|---|---|---|"]
        for r in rows:
            val = str(r.get("extracted_value"))
            if len(val) > 60:
                val = val[:57] + "…"
            lines.append(
                f"| {r['field_path']} | {val} | {r['confidence']} "
                f"| {r['source_type']} | {r.get('source_ref') or '-'} |")
        return _text("\n".join(lines))

    # --- Resonance (communication compiler) ---
    if name == "resonance_ingest":
        from okuro.resonance import ingest_document
        m = ingest_document(**arguments)
        return _text(
            f"Ingested **{arguments.get('title')}** → artifact `{m['artifact_id']}` "
            f"· {m['claims_added']} claim(s) to KG. "
            f"Pass this artifact_id in `source_artifact_ids` to analyze/render.")
    if name == "resonance_analyze":
        from okuro.resonance import analyze_gaps
        g = analyze_gaps(**arguments)
        lines = [f"**Completeness: {g['completeness']}** · ready: {g['ready']}"]
        if g.get("note"):
            lines.append(f"_{g['note']}_")
        if g.get("requirements"):
            lines.append("\n| Requirement | Status | Route | Question |")
            lines.append("|---|---|---|---|")
            for r in g["requirements"]:
                lines.append(
                    f"| {r['requirement'][:44]} | {r['status']} | {r['route']} "
                    f"| {(r['question'] or '-')[:44]} |")
        return _text("\n".join(lines))
    if name == "resonance_interview":
        from okuro.resonance import next_questions
        q = next_questions(**arguments)
        lines = [f"**Completeness: {q['completeness']}** · ready: {q['ready']}",
                 "\n**Ask the user:**"]
        lines += [f"{i+1}. {x}" for i, x in enumerate(q["questions"])] or ["_(no interview gaps)_"]
        if q["research_gaps"]:
            lines.append(f"\n_Research-route gaps (auto-enrich, not user): {len(q['research_gaps'])}_")
        return _text("\n".join(lines))
    if name == "resonance_answer":
        from okuro.resonance import record_answer
        m = record_answer(**arguments)
        return _text(
            f"Recorded → artifact `{m['artifact_id']}` · {m['claims_added']} claim(s). "
            f"Add it to source_artifact_ids and re-analyze — completeness should rise.")
    if name == "resonance_research":
        from okuro.resonance import resolve_research_gaps
        r = resolve_research_gaps(**arguments)
        lines = [
            f"Auto-researched {len(r['researched'])} gap(s); "
            f"{r['remaining_research']} research gap(s) remain.",
        ]
        for x in r["researched"]:
            lines.append(f"- {x['question'][:60]} → artifact `{x['artifact_id']}` "
                         f"({x['claims_added']} claims)")
        if r["new_artifact_ids"]:
            lines.append("\nAdd these artifact_ids to source_artifact_ids and re-analyze — "
                         "research gaps should close.")
        return _text("\n".join(lines))
    if name == "resonance_render":
        from okuro.resonance import render_from_brain
        r = render_from_brain(**arguments)
        pco = r["pco"]
        rend = r["render"]
        doc = (rend or {}).get("result", {})
        lines = [
            f"Built PCO **{pco.get('title')}** — {pco.get('beat_count')} beat(s), "
            f"min confidence {pco.get('min_confidence')}.",
            f"Rendered **{rend.get('media')}** → `{doc.get('id') or doc.get('url') or 'ok'}`.",
            "\n| Beat | Depth | Claims |", "|---|---|---|",
        ]
        for b in pco.get("beats", []):
            lines.append(f"| {b['intent'][:44]} | {b['depth_hint']} | {len(b['claims'])} |")
        return _text("\n".join(lines))
    if name == "resonance_session_create":
        from okuro.resonance import session_create
        s = session_create(**arguments)
        return _text(
            f"Session `{s['id']}` opened for goal: _{s['goal']}_ "
            f"({len(s['artifact_ids'])} seed doc(s)). "
            f"Call resonance_session_next to get the first questions.")
    if name == "resonance_session_next":
        from okuro.resonance import session_next
        q = session_next(**arguments)
        lines = [f"Session `{q['session_id']}` · **Completeness: {q['completeness']}** · ready: {q['ready']}",
                 "\n**Ask the user:**"]
        lines += [f"{i+1}. {x}" for i, x in enumerate(q["questions"])] or ["_(no interview gaps — ready to render)_"]
        if q.get("research_gaps"):
            lines.append(f"\n_Research-route gaps (auto-enrich via resonance_research): {len(q['research_gaps'])}_")
        return _text("\n".join(lines))
    if name == "resonance_session_answer":
        from okuro.resonance import session_answer
        r = session_answer(**arguments)
        lines = [f"Recorded → artifact `{r['artifact_id']}` · {r['claims_added']} claim(s). "
                 f"**Completeness: {r['completeness']}** · ready: {r['ready']}"]
        if r.get("questions"):
            lines.append("\n**Next:**")
            lines += [f"{i+1}. {x}" for i, x in enumerate(r["questions"])]
        return _text("\n".join(lines))
    if name == "resonance_session_get":
        from okuro.resonance import session_get
        s = session_get(arguments["session_id"])
        if not s:
            return _text(f"Session not found: {arguments['session_id']}")
        lines = [f"**{s['id']}** · {s['status']} · completeness {s['completeness']} · ready {s['ready']}",
                 f"Goal: _{s['goal']}_ · {len(s['artifact_ids'])} evidence artifact(s)"]
        if s["turns"]:
            lines.append("\n| # | Kind | Q | A |"); lines.append("|---|---|---|---|")
            for i, t in enumerate(s["turns"], 1):
                lines.append(f"| {i} | {t['kind']} | {(t.get('question') or '-')[:36]} | {(t.get('answer') or '-')[:36]} |")
        return _text("\n".join(lines))
    if name == "resonance_session_list":
        from okuro.resonance import session_list
        rows = session_list(**arguments)
        if not rows:
            return _text("No interview sessions.")
        lines = ["| Session | Status | Complete | Goal |", "|---|---|---|---|"]
        for s in rows:
            lines.append(f"| `{s['id']}` | {s['status']} | {s['completeness'] or '-'} | {s['goal'][:44]} |")
        return _text("\n".join(lines))
    if name == "resonance_actuality_check":
        from okuro.resonance import check_actuality
        r = check_actuality(**arguments)
        lines = [f"Checked {r['checked']} source(s): {r['fresh']} fresh · {len(r['stale'])} stale — _{r['note']}_"]
        if r["stale"]:
            lines.append("\n| Source | Class | Age (d) | Limit | artifact |")
            lines.append("|---|---|---|---|---|")
            for s in r["stale"]:
                lines.append(f"| {(s['source_ref'] or s['title'] or '-')[:40]} | {s['class']} | {s['age_days']} | {s['threshold_days']} | `{s['artifact_id']}` |")
            lines.append("\nRun resonance_actuality_refresh to re-research the stale web sources.")
        return _text("\n".join(lines))
    if name == "resonance_actuality_refresh":
        from okuro.resonance import refresh_stale
        r = refresh_stale(**arguments)
        lines = [f"Refreshed {len(r['refreshed'])} web source(s); {r['skipped']} skipped (doc/interview → needs a human)."]
        for x in r["refreshed"]:
            lines.append(f"- {x['question'][:56]} → `{x['new_artifact_id']}` ({x['claims_added']} claims), replaces `{x['old_artifact_id']}`")
        if r["new_artifact_ids"]:
            lines.append("\nSwap the new artifact_ids into source_artifact_ids (drop the old) and re-render.")
        return _text("\n".join(lines))

    # --- Reviews ---
    if name == "review_add":
        from okuro.sense.reviews import review_add
        try:
            row = review_add(
                arguments.get("surface_id", ""),
                comment=arguments.get("comment"),
                rating=arguments.get("rating"),
                severity=arguments.get("severity", "annoyance"),
                target_type=arguments.get("target_type", "unresolved"),
                target_id=arguments.get("target_id"),
                route=arguments.get("route"),
                project=arguments.get("project"),
                make_todo=arguments.get("make_todo"),
            )
        except ValueError as exc:
            return _text(f"REJECTED: {exc}")
        out = [f"Review recorded on `{row.get('surface_id')}`."]
        out.append(
            f"Todo created: {row['todo_id']}" if row.get("todo_id")
            else "No todo created (severity gate)."
        )
        return _text("\n".join(out))

    if name == "review_list":
        from okuro.sense.reviews import review_list
        rows = review_list(
            arguments.get("surface_id"),
            target_type=arguments.get("target_type"),
            target_id=arguments.get("target_id"),
            limit=int(arguments.get("limit", 50)),
        )
        if not rows:
            return _text("No reviews match.")
        lines = [f"{len(rows)} review(s), newest first:"]
        for r in rows:
            score = f" {r['rating']}/5" if r.get("rating") else ""
            lines.append(
                f"- [{r.get('severity')}]{score} {r.get('surface_id')} "
                f"({str(r.get('created_at'))[:16]}): "
                f"{str(r.get('comment') or '')[:160]}"
            )
        return _text("\n".join(lines))

    if name == "review_surfaces":
        from okuro.sense.reviews import surface_summary
        rows = surface_summary(
            include_orphaned=bool(arguments.get("include_orphaned", False))
        )
        if not rows:
            return _text("No surfaces registered yet.")
        lines = ["surface · reviews · blockers · avg · last"]
        for r in rows:
            avg = f"{r['avg_rating']:.1f}" if r.get("avg_rating") else "-"
            gone = " (orphaned)" if r.get("orphaned_at") else ""
            lines.append(
                f"- {r['surface_id']}{gone} · {r.get('review_count', 0)} · "
                f"{r.get('blockers', 0)} · {avg} · "
                f"{str(r.get('last_review_at') or '-')[:16]}"
            )
        return _text("\n".join(lines))

    # --- Redline ---
    if name == "redline_open":
        # Minted where it is SERVED. The token lives in the API process's
        # memory, never in the DB, so opening through the daemon is what makes
        # the returned URL work at all — an MCP process minting its own would
        # hand back a credential the server has never heard of.
        from okuro.sense import redline as redline_svc

        body = {
            "kind": arguments.get("kind", "file"),
            "ref": arguments.get("ref", ""),
            "title": arguments.get("title"),
            "project": arguments.get("project"),
            "reload": bool(arguments.get("reload", False)),
            "version_seq": arguments.get("version_seq"),
            "shots": bool(arguments.get("shots", False)),
        }
        result = _orch_request("POST", "/api/redline/open", body)
        if isinstance(result, dict) and result.get("error"):
            if result.get("status") == 422:
                detail = result.get("detail") or {}
                return _text(f"REJECTED: {detail.get('detail', result['error'])}")
            # No API reachable: the version still lands in the shared DB, but
            # nothing can serve it, so say that instead of returning a URL
            # that would 401.
            try:
                local = redline_svc.open_document(
                    body["kind"], body["ref"],
                    title=body["title"], project=body["project"],
                    reload=body["reload"], version_seq=body["version_seq"],
                    shots=body["shots"],
                )
            except redline_svc.RedlineError as exc:
                return _text(f"REJECTED: {exc}")
            local["url"] = None
            local["warning"] = (
                "the orchestrator API is not reachable, so no serve URL was "
                f"minted ({result['error']}). The version is recorded; open it "
                "again once the API is up."
            )
            return _text(local)
        return _text(result)

    if name == "redline_list":
        from okuro.sense import redline as redline_svc
        fmt = arguments.get("format", "json")
        try:
            listing = redline_svc.list_comments(
                arguments.get("document_id", ""),
                version_id=arguments.get("version_id"),
                status=arguments.get("status", "open"),
                include_replies=bool(arguments.get("include_replies", True)),
                format=fmt,
            )
        except redline_svc.RedlineError as exc:
            return _text(f"REJECTED: {exc}")
        # The markdown block is meant to be PASTED, so it arrives as the text
        # it is. Wrapping it in a JSON envelope would make the caller unwrap
        # and re-escape the one thing it asked for.
        if fmt == "markdown":
            return _text(listing.get("markdown") or "")
        return _text(listing)

    if name == "redline_resolve":
        from okuro.sense import redline as redline_svc
        try:
            return _text(redline_svc.resolve_comment(
                arguments.get("comment_id", ""),
                arguments.get("note", ""),
                after_excerpt=arguments.get("after_excerpt"),
                done_by=arguments.get("done_by"),
            ))
        except redline_svc.RedlineError as exc:
            return _text(f"REJECTED: {exc}")

    if name == "redline_versions":
        from okuro.sense import redline as redline_svc
        try:
            return _text(redline_svc.versions(arguments.get("document_id", "")))
        except redline_svc.RedlineError as exc:
            return _text(f"REJECTED: {exc}")

    # --- Todos ---
    if name == "todo_add":
        from okuro.sense.todos import todo_add
        return _text(todo_add(**arguments))
    if name == "todo_list":
        from okuro.sense.todos import todo_list
        return _text(todo_list(**arguments))
    if name == "todo_get":
        from okuro.sense.todos import todo_get
        return _text(todo_get(arguments["todo_id"]))
    if name == "todo_update":
        from okuro.sense.todos import todo_update
        return _text(todo_update(**arguments))
    if name == "todo_done":
        from okuro.sense.todos import todo_done
        return _text(todo_done(arguments["todo_id"]))

    # --- Reminders ---
    if name == "set_reminder":
        from okuro.sense.reminders.engine import create_reminder
        when_str = arguments["when"]
        try:
            when_due = _parse_when(when_str)
        except Exception:
            return _text(f"Cannot parse date: {when_str}. Use ISO format (2026-03-28T10:00) or clear description.")
        ctx = arguments.get("context", {})
        if arguments.get("project"):
            ctx["project"] = arguments["project"]
        result = create_reminder(
            what=arguments["what"],
            when_due=when_due,
            urgency=arguments.get("urgency", 3),
            channels=arguments.get("channels"),
            repeat=arguments.get("repeat"),
            context=ctx,
            source="user",
        )
        return _text(result)
    if name == "list_reminders":
        from okuro.sense.reminders.engine import list_reminders
        return _text(list_reminders(
            status=arguments.get("status"),
            upcoming_hours=arguments.get("upcoming_hours"),
            include_suggestions=arguments.get("include_suggestions", False),
        ))
    if name == "snooze_reminder":
        from okuro.sense.reminders.engine import snooze_reminder
        return _text(snooze_reminder(
            reminder_id=arguments["id"],
            duration_min=arguments.get("duration_min"),
        ))
    if name == "dismiss_reminder":
        from okuro.sense.reminders.engine import dismiss_reminder
        return _text(dismiss_reminder(reminder_id=arguments["id"]))
    if name == "acknowledge_reminder":
        from okuro.sense.reminders.engine import acknowledge_reminder
        return _text(acknowledge_reminder(reminder_id=arguments["id"]))
    if name == "accept_suggestion":
        from okuro.sense.reminders.suggestions import accept_suggestion
        return _text(accept_suggestion(
            suggestion_id=arguments["suggestion_id"],
            override_when=arguments.get("override_when"),
            override_urgency=arguments.get("override_urgency"),
        ))
    if name == "reject_suggestion":
        from okuro.sense.reminders.suggestions import reject_suggestion
        return _text(reject_suggestion(arguments["suggestion_id"]))

    # --- Session retros ---
    if name == "retro_list":
        from okuro.db import get_db
        db = get_db()
        limit = min(int(arguments.get("limit", 5)), 50)
        batch_id = arguments.get("batch_id")
        if batch_id:
            batches = db.fetchall(
                "SELECT * FROM session_retro_batches WHERE batch_id = ?", (batch_id,),
            )
        else:
            batches = db.fetchall(
                "SELECT * FROM session_retro_batches ORDER BY ran_at DESC LIMIT ?", (limit,),
            )
        for b in batches:
            try:
                b["patterns"] = json.loads(b.pop("patterns_json") or "[]")
                b["memory_ids"] = json.loads(b.get("memory_ids") or "[]")
            except (TypeError, ValueError):
                pass
        return _text({"count": len(batches), "batches": batches})

    if name == "retro_run":
        from okuro.sense.retros import run_retros
        return _text(run_retros(
            window_days=int(arguments.get("window_days", 14)),
            max_per_cohort=int(arguments.get("max_per_cohort", 10)),
            provider=arguments.get("provider"),
        ))

    # --- Memory utility ---
    if name == "memory_utility":
        from okuro.sense.memory_utility import compute_utility
        return _text(compute_utility(
            memory_id=arguments.get("memory_id"),
            min_surfacings=int(arguments.get("min_surfacings", 5)),
            limit=int(arguments.get("limit", 20)),
        ))
    if name == "memory_stale":
        from okuro.sense.memory_staleness import scan
        res = scan(
            min_confidence=float(arguments.get("min_confidence", 0.3)),
            limit=int(arguments.get("limit", 20)),
        )
        if arguments.get("proof_only", True):
            res["findings"] = [f for f in res["findings"]
                               if f["strength"] == "proof"]
        return _text(res)
    if name == "memory_audit":
        from okuro.sense.memory_utility import audit
        return _text(audit(
            min_surfacings=int(arguments.get("min_surfacings", 20)),
            flag_threshold=float(arguments.get("flag_threshold", -0.10)),
        ))
    if name == "memory_stats":
        from okuro.sense.memory import memory_stats
        return _text(memory_stats(
            group_by=arguments.get("group_by", "week"),
            since=arguments.get("since"),
            until=arguments.get("until"),
            project=arguments.get("project"),
            topic=arguments.get("topic"),
        ))
    if name == "memory_census":
        from okuro.sense.memory import memory_census
        return _text(memory_census(
            since=arguments.get("since"),
            until=arguments.get("until"),
            group_by=arguments.get("group_by", "topic"),
        ))
    if name == "artifact_stats":
        from okuro.sense.artifacts import artifact_stats
        return _text(artifact_stats(
            group_by=arguments.get("group_by", "week"),
            since=arguments.get("since"),
            until=arguments.get("until"),
            project=arguments.get("project"),
            kind=arguments.get("kind"),
        ))
    if name == "memory_recall_eval":
        from okuro.sense.memory_eval import evaluate_recall
        return _text(evaluate_recall(
            sample=int(arguments.get("sample", 120)),
            k=int(arguments.get("k", 10)),
            seed=int(arguments.get("seed", 7)),
        ))

    # --- Orchestrator (drive runs from inside an agent session) ---
    if name == "orchestrator_create":
        return _text(orchestrator_create(
            description=arguments["description"],
            mode=arguments.get("mode", "plan"),
            intelligence=arguments.get("intelligence", "balanced"),
            required_roles=arguments.get("required_roles"),
            preferred_cli=arguments.get("preferred_cli"),
            auto_approve_risk=arguments.get("auto_approve_risk", "none"),
            verify_command=arguments.get("verify_command"),
            autonomous=arguments.get("autonomous", True),
            fast_track=bool(arguments.get("fast_track", False)),
            audience=arguments.get("audience"),
            delivery_channel=arguments.get("delivery_channel"),
            delivery_brand_id=arguments.get("delivery_brand_id"),
        ))
    if name == "orchestrator_status":
        return _text(orchestrator_status(task_id=arguments["task_id"]))
    if name == "orchestrator_inspect":
        return _text(orchestrator_inspect(
            view=arguments.get("view", "tasks"),
            task_id=arguments.get("task_id"),
            limit=arguments.get("limit"),
        ))
    if name == "orchestrator_approve":
        return _text(orchestrator_approve(
            task_id=arguments["task_id"],
            subtask_id=arguments.get("subtask_id"),
            action=arguments.get("action", "approve"),
            roles=arguments.get("roles"),
            strategy=arguments.get("strategy", "parallel"),
        ))
    if name == "orchestrator_continue":
        return _text(orchestrator_continue(
            task_id=arguments["task_id"],
            description=arguments.get("description"),
            suggestion_id=arguments.get("suggestion_id"),
        ))

    # --- Behavioral rules ---
    if name == "behavioral_rules":
        from okuro.sense.rules import load_all, load_section, find_rule
        query = arguments.get("query")
        section = arguments.get("section")
        limit = min(int(arguments.get("limit", 10)), 50)
        if query:
            return _text({"query": query, "matches": find_rule(query, limit=limit)})
        if section:
            return _text({"section": section, "rules": load_section(section)[:limit]})
        bundles = load_all()
        return _text({
            "sections": [
                {"id": sid, "label": b["label"], "path": b["path"], "count": len(b["rules"]), "rules": b["rules"]}
                for sid, b in bundles.items()
            ],
        })

    # --- Transcripts ---
    if name == "transcript_sweep":
        from okuro.sense.transcripts import sweep
        return _text(sweep(**arguments))
    if name == "transcript_search":
        from okuro.sense.transcripts import transcript_search
        return _text(transcript_search(**arguments))
    if name == "transcript_adapters":
        from okuro.sense.transcripts import list_adapters
        return _text(list_adapters())

    # --- Knowledge graph ---
    if name == "kg_add":
        from okuro.sense.kg import kg_add
        return _text(kg_add(**arguments))
    if name == "kg_query":
        from okuro.sense.kg import kg_query
        return _text(kg_query(**arguments))
    if name == "kg_resolve":
        from okuro.sense.kg_resolve import resolve_entity
        return _text(resolve_entity(**arguments))
    if name == "kg_invalidate":
        from okuro.sense.kg import kg_invalidate
        return _text(kg_invalidate(**arguments))
    if name == "kg_timeline":
        from okuro.sense.kg import kg_timeline
        return _text(kg_timeline(**arguments))
    if name == "kg_stats":
        from okuro.sense.kg import kg_stats
        return _text(kg_stats(**arguments))
    if name == "kg_assert":
        from okuro.sense.kg_check import kg_assert
        return _text(kg_assert(**arguments))

    # --- Role diary ---
    if name == "role_diary_write":
        from okuro.sense.role_diary import role_diary_write
        return _text(role_diary_write(**arguments))
    if name == "role_diary_read":
        from okuro.sense.role_diary import role_diary_read
        return _text(role_diary_read(**arguments))
    if name == "role_diary_search":
        from okuro.sense.role_diary import role_diary_search
        return _text(role_diary_search(**arguments))
    if name == "role_diary_stats":
        from okuro.sense.role_diary import role_diary_stats
        return _text(role_diary_stats(**arguments))

    # --- Memory tunnels ---
    if name == "tunnel_link":
        from okuro.sense.tunnels import tunnel_link
        return _text(tunnel_link(**arguments))
    if name == "tunnel_find":
        from okuro.sense.tunnels import tunnel_find
        return _text(tunnel_find(**arguments))
    if name == "tunnel_bridge":
        from okuro.sense.tunnels import tunnel_bridge
        return _text(tunnel_bridge(**arguments))
    if name == "tunnel_concepts":
        from okuro.sense.tunnels import tunnel_concepts
        return _text(tunnel_concepts(**arguments))

    if name == "distill_lessons_review":
        from okuro.sense.distill.lessons import lessons_for_review
        return _text(lessons_for_review(
            status=arguments.get("status", "candidate"),
            limit=int(arguments.get("limit", 20)),
        ))
    if name == "distill_lesson_approve":
        from okuro.sense.distill.lessons import approve_lesson
        # ValueError carries the refusal text (retired lesson, automation
        # approver) and is surfaced rather than swallowed — a silent no-op here
        # would read as "approved" to whoever called it.
        try:
            return _text(approve_lesson(
                int(arguments["lesson_id"]),
                approved_by=str(arguments["approved_by"]),
            ))
        except ValueError as exc:
            return _text({"ok": False, "error": str(exc)})
    if name == "distill_lesson_reject":
        from okuro.sense.distill.lessons import reject_lesson
        try:
            return _text(reject_lesson(
                int(arguments["lesson_id"]),
                reason=str(arguments["reason"]),
                rejected_by=str(arguments.get("rejected_by", "")),
            ))
        except ValueError as exc:
            return _text({"ok": False, "error": str(exc)})

    return _text(f"Unknown tool: {name}")
