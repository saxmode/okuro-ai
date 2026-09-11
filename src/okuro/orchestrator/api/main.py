# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Okuro Orchestrator API Server
# index:
#   imports
#   def _validate_task_id
#   def _validate_artifact_name
#   def _load_api_token
#   class BearerAuthMiddleware
#   class _RateLimiter
#   def _get_spawn_lock
#   class ConnectionManager
#   class LogFileHandler
#   class EventWatcher
#   def _require_loopback
#   def _allow_configured_lan
#   class TaskCreateRequest
#   class TaskContinueRequest
#   class TaskRenameRequest
#   class TaskPreviewRequest
#   def spawn_orchestrator
#   def _secure_filename
#   def _is_orchestrator_running
#   def _cancel_task
#   class TaskIntelligenceRequest
#   class SubtaskModelOverrideRequest
#   class ApprovalRequest
# AGENT_HEADER_END -->
"""
Okuro Orchestrator API Server

FastAPI server providing REST endpoints and WebSocket events
for the Okuro autonomous workforce orchestrator.
Reads state from file system (task.yaml, plan.yaml, log.jsonl).
"""

import asyncio
import hashlib as _hashlib
import json
import logging
import os
import re as _re
import secrets as _secrets
import signal
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request, UploadFile, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse, FileResponse, Response
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from okuro import __version__
from okuro.orchestrator.yamlfast import yload
from okuro.orchestrator.api.models import SystemStatus, WSLogEvent
from okuro.orchestrator.api.proposals import router as proposals_router
from okuro.orchestrator.api.deliberation import router as deliberation_router
from okuro.orchestrator.api.onboarding import router as onboarding_router
from okuro.orchestrator.api.profile_sources import router as profile_sources_router
from okuro.orchestrator.api.keyring import router as keyring_router
from okuro.design_engine.api import router as design_engine_router
from okuro.design_engine.api import public_router as design_engine_public_router
from okuro.orchestrator.api.cortex import router as cortex_router
from okuro.orchestrator.api.embed import router as embed_router
from okuro.orchestrator.api.voice_tier import router as voice_tier_router
from okuro.orchestrator.api.voice_tts import router as voice_tts_router
from okuro.orchestrator.api.services import router as services_router
from okuro.orchestrator.api.people import router as people_router
from okuro.orchestrator.api.media import router as media_router
from okuro.orchestrator.api.slides import router as slides_router
from okuro.orchestrator.api.prism import router as prism_router
from okuro.orchestrator.api.sparring import router as sparring_router
from okuro.orchestrator.api.handover import router as handover_router
from okuro.orchestrator.api.target_groups import router as target_groups_router
from okuro.orchestrator.api.resonance import router as resonance_router
from okuro.orchestrator.api.studio import router as studio_router
from okuro.orchestrator.api.brand_assets import router as brand_assets_router
from okuro.orchestrator.api.crm import router as crm_router
from okuro.orchestrator.api.questionnaires import router as questionnaires_router
from okuro.orchestrator.api.reminders import router as reminders_router
from okuro.orchestrator.api.lessons import router as lessons_router
from okuro.orchestrator.api.integrations import router as integrations_router
from okuro.orchestrator.api.bridge import router as bridge_router
from okuro.orchestrator.api.models_api import router as models_api_router
from okuro.orchestrator.api.digest import router as digest_router
from okuro.orchestrator.api.knowledge_graph import router as knowledge_graph_router
from okuro.orchestrator.api.repos import router as repos_router
from okuro.orchestrator.api.corpora import router as corpora_router
from okuro.orchestrator.api.todos import router as todos_router
from okuro.orchestrator.api.todos_solve import router as todos_solve_router
from okuro.orchestrator.api.feedback import router as feedback_router
from okuro.orchestrator.api.reviews import router as reviews_router
from okuro.orchestrator.api.redline import router as redline_router
from okuro.orchestrator.api.chat import router as chat_router
from okuro.orchestrator.api.signals import router as signals_router
from okuro.orchestrator.api.inbox import router as inbox_router
from okuro.orchestrator.api.sessions import router as sessions_router
from okuro.orchestrator.api.roles import router as roles_router
from okuro.orchestrator.api.preview import router as preview_router
from okuro.orchestrator.api.state_reader import (
    list_all_tasks, read_task_detail, read_task_logs,
    read_task_artifacts, read_task_state, read_task_snapshot,
)
from okuro.web.app import router as web_router, mount_spa
from okuro.db.engine import okuro_home

# -- Configuration --

# Resolve okuro orchestrator root from env or default ~/.okuro/orchestrator
OKURO_ROOT = Path(os.environ.get("OKURO_ROOT", okuro_home() / "orchestrator"))
TASKS_DIR = OKURO_ROOT / "tasks"
TOOLS_REGISTRY = OKURO_ROOT / "tools" / "registry.yaml"

HOST = os.environ.get("OKURO_HOST", "127.0.0.1")
# Port comes from port_registry — single source of truth across the codebase.
from okuro.system.port_registry import orchestrator_port as _orchestrator_port  # noqa: E402

PORT = _orchestrator_port()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("okuro.orchestrator.api")

if HOST != "127.0.0.1":
    logger.warning(
        "DANGER: okuro API bound to %s — reachable on LAN. Set OKURO_HOST=127.0.0.1 to close.",
        HOST,
    )

app_state = {"start_time": datetime.utcnow()}


# ---------------------------------------------------------------------------
# C7 — closed state-change event allowlist.
#
# Every event_type that the FE state pool (/ws) must invalidate caches on
# is listed here ONCE. Module level so the Gate 4 harness can extract the
# literal by regex; ``state.emit_event(..., pool="state_change")`` reads
# this set at runtime and refuses unknown event types — adding a new state
# changing emitter without updating this set is now a loud ValueError, not
# a silent state-invisible mutation (Gate 2 §C7 / V4 + D15).
#
# Telemetry-only events (engine_version, verify_passed, gap, ...) live in
# state.TELEMETRY_ONLY_EVENTS and intentionally do not appear here. WS
# protocol envelope types (connected, pong, subscribed, snapshot,
# agent_activity, ...) are hoisted to constants below so they no longer
# masquerade as log event types.
# ---------------------------------------------------------------------------

_STATE_CHANGE_EVENTS = frozenset({
    # Task lifecycle.
    "task_created", "task_done", "task_failed",
    "task_completed_partial",
    "task_halted_on_exit", "task_halted_reconciled",
    "task_renamed", "task_cancelled", "task_retry",
    "status_changed",
    # Subtask lifecycle.
    "subtask_running", "subtask_done", "subtask_failed",
    "subtask_failed_permanently", "subtask_skipped",
    # A shipped subtask whose review could not settle. Terminal for the review
    # axis while the work axis stays `done` — emitted instead of
    # subtask_failed_permanently so the log stops asserting a work failure that
    # did not happen.
    "subtask_review_unresolved",
    "subtask_waiting_approval", "subtask_approved",
    "subtask_pending", "subtask_retry",
    # Legacy cascade-skip events. The mechanism is gone, but task logs written
    # before its removal still carry these rows — keep them renderable so
    # historical tasks replay intact.
    "subtask_cascade_skipped", "subtask_cascade_skipped_fallback",
    "subtask_kill_after_ship",
    "subtasks_superseded",
    "cascade_skip_summary", "cascade_skip_fallback_summary",
    # Replacement: dependents held pending behind a permanent failure. Derived
    # per loop, so this is an observability row, not a state change.
    "subtasks_blocked_upstream",
    # Graph node lifecycle.
    "node_running", "node_done", "node_failed",
    "node_pending", "node_resolved", "node_awaiting_user",
    # Phase lifecycle.
    "phases_appended",
    # A drawn workflow's fan-out could not expand when its phase closed. It IS a
    # state change from the user's side: the run stops short of the phases that
    # fan-out would have added, and the deck (or whatever) is silently smaller
    # than the workflow describes. Surfaced rather than buried in the log.
    "workflow_fanout_failed",
    "phase_status_changed",  # canonical for engine.py:705/2064/2535
    "phase_blocked_by_review_cap",
    "phase_blocked_review",  # Theme F — session-loop CAP/NEEDS_USER → blocked_review
    "phase_override_blocked_review",
    # C9 — retry/decide resolutions were LOGGED but absent from this allowlist,
    # so passive viewers / other tabs never refetched after a Retry or Decide.
    "phase_retry_blocked_review",
    "phase_decide_blocked_review",
    # Decide action (state.lock_blocked_review_decision) — adds an ADR + resets
    # the decided subtask to pending. Consumer-visible state change; was missing
    # from the allowlist (pre-existing C9 gap caught by test_c7_events).
    "blocked_review_decided",
    # Retries-cap timeout flow (timeout_cap awaiting).
    "subtask_timeout_cap", "subtask_timeout_cap_resolved",
    # Planning / decomposition.
    "decompose_started", "decompose_failed",
    "discussion_resolved",
    "recouncil_started",
    "continuation_council_seeded",
    "role_added_to_discussion", "role_removed_from_discussion",
    # User-gated flows.
    "awaiting_user", "awaiting_user_cleared",
    # ROCK-SOLID v5 P2.4 — a parked gate's card was re-authored in place. The
    # gate did not open or close, but the words on screen changed, and the
    # open page is the ONLY consumer that matters here: a refresh that no
    # tab picks up is the stale copy it set out to replace.
    "awaiting_refreshed",
    # ROCK-SOLID v5 P2.2 — a recurring task hit a gate and the run answered it
    # deterministically rather than stopping the schedule on a human who is
    # not watching. Visible by design: "speed is never silent" applies to
    # unattended decisions most of all.
    "gate_auto_resolved",
    # ROCK-SOLID v5 P3.1 — the next-steps panel was permanently empty on a
    # HEALTHY socket. Suggestions are written to task.yaml and logged, but
    # the event was classed as telemetry, and the EventWatcher filters the
    # log tail through THIS set (main.py, handle_log_change) — so the one
    # row that says "there are next steps now" was dropped on the floor and
    # the panel only filled on a manual reload.
    "continuation_suggested",
    # P3.1 — the review transport recovering from a lost closeout sentinel.
    # It was emitted to .activity.jsonl only, which no state consumer
    # tails; the user saw a stall with no explanation and then work
    # resuming with no explanation either.
    "review_sentinel_lost",
    # ROCK-SOLID v5 P3.3 — Stream C actually reached a recipient. The
    # fan-out logged at INFO and emitted nothing, so the deliveries tab
    # (a mount-only fetch) could not learn a delivery had happened
    # without a page reload.
    "delivery_sent",
    # ROCK-SOLID v5 P3.4 — a queued continuation prompt was retracted.
    # delete_intervention wrote task.yaml and logged NOTHING, so no
    # state_change frame existed and no other tab learned the prompt was
    # gone. (The acting tab looked fine only because its own mutation
    # handler invalidated locally.)
    "intervention_deleted",
    # ROCK-SOLID v5 P4.2 — dispatch refused to spawn a second subagent for a
    # subtask that already has a live one. Visible because a refusal that only
    # appears in the engine log looks, from the page, like a subtask that
    # simply never started.
    "dispatch_refused_lease",
    # P4.2/P4.3 — a successor engine cancelled what its predecessor left
    # running. Visible because a subagent being killed underneath a user is
    # otherwise indistinguishable from one that silently stopped.
    "predecessor_sessions_reaped",
    "capability_gap",
    "await_user_decision", "await_user_decision_resolved",
    "await_user_decision_skipped",
    "approval_batch",
    # Single-subtask approvals — POST /api/tasks/:id/approve. Measured
    # 2026-07-27 against a live /ws probe: an allowlisted event reaches the
    # client as state_change + snapshot in ~16 ms, while "approval_approved"
    # produced ONLY a `log` frame and NO cache invalidation, ever. That is a
    # stuck banner rather than a slow one, because task-detail.tsx pauses the
    # taskState query while the WS is connected and use-tasks.ts then turns
    # the 3 s poll OFF — that poll used to mask exactly this omission.
    #
    # NOTE for future editors: comments inside this literal must not contain
    # a curly brace. test_c7_events extracts the set with a regex that stops
    # at the first closing brace, so one stray brace silently empties the
    # parsed allowlist and the guard then reports every event as unregistered.
    "approval_approved", "approval_skipped", "approval_rejected",
    # Plan persistence + engine crash.
    "plan_created",
    "orchestrator_crashed",
    # PR A — reviewer pipeline state-changing emissions (Stage A canonical
    # vocab). Producer side now emits these from reviewer/pipeline.py so
    # the FE state pool can refetch when critic / scorer transition.
    # review_starting / review_complete are activity-feed markers
    # written to .activity.jsonl, not log.jsonl — they are NOT allowlisted
    # here (see _append_activity_row at engine.py:1029).
    "review_started", "review_complete_event",
    "critic_finding", "scorer_decision", "verdict_published",
    # PR A — drafter side: role-designer subagent persists a draft into the
    # roles table. Stage A spec lists role_drafted as a state-changing event
    # so the FE can pop a draft-review card without polling /api/roles.
    "role_drafted",
    # PR C — restart-resilience surface. engine_resumed is emitted on warm
    # start (post-restart) so the FE can show a "engine resumed" banner.
    # subagent_wedged fires after a session goes silent past the wedge TTL
    # so the FE can surface a "stuck — kill the scope?" affordance.
    "engine_resumed", "subagent_wedged",
    # ROCK-SOLID v5 P1.4 — engine_parked is emitted by every park_engine_exit
    # call (the single choke point for a human-blocking gate releasing its
    # process) but was never allowlisted, so a live viewer never learned why
    # the task went quiet without a manual refresh.
    "engine_parked",
    # autopilot_gate (the single choke point for every autopilot-answered
    # gate) already logs this with chosen_option + rationale + confidence —
    # a real structured signal, just never allowlisted, so an autopilot
    # answer was invisible to a live viewer (evidence inventory: "Autopilot
    # answers gates on the user's behalf and never says it did").
    "autopilot_decision",
    # Theme E — source-change self-restart. engine_restart_requested is
    # emitted by the source-watcher daemon thread immediately before
    # sys.exit(0); the launcher's respawn loop then re-execs against
    # fresh modules. FE consumers can surface a "engine restarting…"
    # banner without polling the task dir.
    "engine_restart_requested",
    # Configuration mutations that change task surface.
    "intelligence_changed", "project_path_changed",
    "model_override", "checkpoint_restored",
    # Theme C — continuation forces the engine onto the phase-dispatch
    # path. task.mode flipping deliberate→auto-execute changes which
    # loop runs and which terminal-check applies, so it is a true
    # state change the FE should reflect (badge / activity row).
    "mode_flipped_on_continuation",
    # Resume drains queued continuations into new phases + flips status to
    # active (engine.py _drain_pending_continuations). Consumer-visible
    # state change (new phases appear, status changes) — was emitted but
    # missing from the allowlist (C9 gap caught by test_c7_events).
    "continuation_drained_on_resume",
    # C12 — brain/cortex index write consistency. Pre-fix, exceptions
    # raised inside _index_task_artifacts were swallowed with a
    # logger.warning — reviewer's next pass ran against a partial index
    # and the FE never learned. cortex_index_failed surfaces it as a
    # state-change so FE consumers can refetch and operators can act
    # without tailing .cortex-index.log.
    "cortex_index_failed",
})


# WS protocol / envelope types — hoisted out of inline string literals so
# scanners can distinguish broadcast envelope wrappers from log event types
# that belong in _STATE_CHANGE_EVENTS. These names appear in WS messages
# only; nothing writes them to log.jsonl.
_WS_ENVELOPE_CONNECTED = "connected"
_WS_ENVELOPE_SUBSCRIBED = "subscribed"
_WS_ENVELOPE_PONG = "pong"
_WS_ENVELOPE_LOG = "log"
_WS_ENVELOPE_STATE_CHANGE = "state_change"
_WS_ENVELOPE_SNAPSHOT = "snapshot"
_WS_ENVELOPE_AGENT_ACTIVITY = "agent_activity"
_WS_INNER_MCP_TOOL_CALL = "mcp_tool_call"

# HTTP response discriminator for recurring-task creation; not a log event.
_HTTP_RESPONSE_RECURRING = "recurring"


# -- Security Utilities --

_TASK_ID_RE = _re.compile(r"^task(?:-[a-z]+)?-\d{8}-\d{6}(?:-[A-Za-z0-9_-]+)?$")


def _validate_task_id(task_id: str) -> None:
    """Validate task_id format and path containment. Raises HTTPException on failure."""
    if not _TASK_ID_RE.match(task_id):
        raise HTTPException(400, f"Invalid task_id format: {task_id}")
    resolved = (TASKS_DIR / task_id).resolve()
    if not str(resolved).startswith(str(TASKS_DIR.resolve())):
        raise HTTPException(400, f"Invalid task_id: path traversal detected")


def _validate_artifact_name(artifact_name: str, task_id: str) -> Path:
    """Validate artifact_name and return safe resolved path. Raises HTTPException on failure."""
    if ".." in artifact_name or "/" in artifact_name:
        raise HTTPException(400, f"Invalid artifact name: {artifact_name}")
    artifacts_dir = (TASKS_DIR / task_id / "artifacts").resolve()
    artifact_path = (TASKS_DIR / task_id / "artifacts" / artifact_name).resolve()
    if not str(artifact_path).startswith(str(artifacts_dir)):
        raise HTTPException(400, "Invalid artifact name: path traversal detected")
    return artifact_path


def _load_api_token() -> str:
    """Load or generate the API bearer token from okuro.keyring."""
    key_name = "okuro/api_token"
    try:
        from okuro.keyring.storage import KeyringStorage
        ks = KeyringStorage()
        token = ks.get_key(key_name)
        if token:
            return token
        # Generate and store a new token
        token = _secrets.token_urlsafe(32)
        ks.set_key(key_name, token)
        logger.info("Generated new Okuro API bearer token (stored in keyring as '%s')", key_name)
        return token
    except Exception as e:
        logger.warning("okuro.keyring unavailable (%s), falling back to env var OKURO_API_TOKEN", e)
        token = os.environ.get("OKURO_API_TOKEN")
        if token:
            return token
        # Keyring unavailable (first-run / headless, before the wizard's
        # keyring step). Persist a token to a 0600 file under ~/.okuro so it
        # SURVIVES restarts. installability audit 2026-06-04 (S1): the prior
        # behaviour fell straight through to the ephemeral mint below, which
        # re-issued a fresh bearer on every service restart — forcing the SPA
        # to re-auth and breaking anything holding a token across a restart.
        # The keyring stays the primary store; this only covers the
        # not-yet-initialized window. Loopback binding + peer-UID gating
        # already scope the bearer to the local user, and the value is never
        # logged.
        try:
            token_file = okuro_home() / "api_token"
            if token_file.exists():
                existing = token_file.read_text().strip()
                if existing:
                    return existing
            token = _secrets.token_urlsafe(32)
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(token)
            token_file.chmod(0o600)
            logger.info(
                "Persisted API bearer token to %s (keyring not initialized; "
                "value not logged). Run `okuro keys init` to migrate it into "
                "the keyring.",
                token_file,
            )
            return token
        except Exception as fe:  # noqa: BLE001
            logger.warning(
                "api_token file fallback failed (%s) — using an ephemeral "
                "in-memory token for THIS process (value NOT logged). Finish "
                "onboarding's keyring step or run `okuro keys init`.",
                fe,
            )
        # Last resort: ephemeral in-memory token. FIRST-RUN's auth-exempt
        # /api/onboarding/* routes don't need the bearer, so the wizard still
        # runs; the keyring step then persists a permanent token.
        token = _secrets.token_urlsafe(32)
        return token


_API_TOKEN: str = _load_api_token()


def _log_peer_uid_mode() -> None:
    """One-time startup log so operators see which peer-UID mode is active.

    Three states:
      strict    — lookup failures DENY access. Linux default + macOS-as-root +
                  explicit OKURO_PEER_UID_STRICT=1.
      permissive — lookup failures GRANT access. macOS-non-root default +
                  explicit OKURO_PEER_UID_STRICT=0. Co-tenant attack surface
                  exists ONLY if a different-UID user can reach loopback.
                  On a single-user laptop this is impossible.
    """
    explicit = os.environ.get("OKURO_PEER_UID_STRICT")
    if explicit is not None:
        mode = "strict" if explicit != "0" else "permissive"
        why = f"explicit OKURO_PEER_UID_STRICT={explicit!r}"
    elif sys.platform.startswith("linux"):
        mode = "strict"
        why = "linux default (/proc/net/tcp works as non-root)"
    elif hasattr(os, "geteuid") and os.geteuid() == 0:
        mode = "strict"
        why = "running as root (psutil works)"
    else:
        mode = "permissive"
        why = (
            f"{sys.platform} as non-root (psutil.net_connections() requires "
            f"root on Darwin); single-user laptop assumption. Set "
            f"OKURO_PEER_UID_STRICT=1 to override (and accept that "
            f"/api/auth/token will deny on every request)."
        )
    logger.info("peer-UID enforcement: %s — %s", mode, why)


_log_peer_uid_mode()


# Paths exempt from bearer auth:
#   /api/auth/token — bootstrap; protected instead by _require_loopback
#   /api/health     — public readiness probe; returns no sensitive data
#
# The rule that holds for everything else is: /api/* = auth-gated JSON
# data. Any resource that must be loaded via a browser-native mechanism
# (``<link rel="stylesheet">``, ``<script src>``, ``<img src>``, ``<link
# rel="manifest">``) CANNOT attach an Authorization header and therefore
# must NOT live under /api/*. See /tokens.css (okuro.web.app) for the
# historical scar — gating tokens behind this middleware broke the whole
# SPA twice because stylesheets can't carry bearers.
#
# /ws/* bypasses this middleware because WebSockets authenticate inside
# the endpoint handler via the handshake query token.
_AUTH_EXEMPT_PATHS = {
    "/api/auth/token",
    "/api/health",
}

# Prefix-match exemptions. Anything starting with one of these strings is
# served without requiring a bearer. Use sparingly — every entry expands
# the unauthenticated surface. /api/q/ carries per-token questionnaires
# whose tokens are themselves the capability check.
_AUTH_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/q/",
    # Inline-session HTTP MCP carries its own per-session bearer
    # (issued at bridge_stream_start, validated in
    # okuro.mcp.inline_http.InlineMcpBearerMiddleware). The global
    # /api/* bearer would reject CLI subprocess requests using the
    # session token instead of the per-user API token.
    "/mcp/",
    # Wave 3c — inline session SSE + control routes use the same
    # per-session bearer as /mcp/v1 (verified via verify_session_bearer
    # in okuro.web.session_sse._SessionBearerMiddleware). The global
    # API bearer would shadow this check.
    "/api/sessions/",
)

# Path suffixes that look exempt by prefix but must STILL pass global
# bearer auth. Wave 5: ``/reissue-bearer`` is the recovery endpoint —
# the caller has just lost the per-session bearer, so it presents the
# global API token to mint a fresh one. Without this override the
# /api/sessions/ exemption would let unauthenticated callers reissue
# arbitrary sessions.
_AUTH_NOT_EXEMPT_SUFFIXES: tuple[str, ...] = ("/reissue-bearer",)

# Regex exemptions for routes whose capability lives in a PATH segment
# rather than a header/query — the global bearer can't gate them because
# the credential isn't where this middleware looks. ``/api/preview/{id}/
# serve/{token}/...`` serves a doc deliverable's files (incl. relative
# sub-resources that carry no header or ?token= of their own); the handler
# validates the in-path token (see preview.serve_asset). Same capability-
# in-path model as the /api/q/ prefix.
# ``/api/redline/doc/{id}/v/{seq}/serve/{token}/...`` serves one VERSION of one
# commented document plus its relative sub-resources. Same reason as preview —
# a browser resolving ./style.css or a CORS-gated @font-face attaches no header
# — but a DIFFERENT credential: redline mints a per-document token that expires
# (see sense/redline.check_token), because the framed document can read the
# in-path token out of location.pathname and the global bearer must never be
# what it reads.
_AUTH_EXEMPT_REGEXES = (
    _re.compile(r"^/api/preview/[^/]+/serve/"),
    _re.compile(r"^/api/redline/doc/[^/]+/v/[^/]+/serve/"),
)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require Bearer token on every /api/* route (excluding the bootstrap + health probe).

    Accepts the bearer in EITHER the ``Authorization: Bearer <tok>`` header
    OR a ``?token=<tok>`` query param. Header is the default for every
    fetch() call from the SPA. The query param exists so that browser-
    native cross-process opens (``window.open`` / ``<a target="_blank">``,
    pywebview's ``open_external`` bridge → ``webbrowser.open``) can reach
    bearer-gated download endpoints, since those mechanisms can't attach
    custom headers. Mirrors the existing WebSocket pattern in
    ``_require_ws_auth``. Tradeoff: the query token leaks into browser
    history; acceptable because the bearer scopes only to localhost API
    access on this user's machine.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Non-API paths are the SPA + static assets — serve without bearer so
        # the shell can load /api/auth/token over loopback and then auth.
        if not path.startswith("/api/"):
            return await call_next(request)
        if path in _AUTH_EXEMPT_PATHS:
            return await call_next(request)
        if any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
            # An exempt prefix can carry a suffix that must still pass
            # global bearer auth (e.g. /api/sessions/{id}/reissue-bearer
            # — the per-session bearer is what the caller is missing).
            if not any(path.endswith(s) for s in _AUTH_NOT_EXEMPT_SUFFIXES):
                return await call_next(request)
        # Capability-in-path routes validate their own token in the handler.
        if any(rx.match(path) for rx in _AUTH_EXEMPT_REGEXES):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        header_ok = auth.startswith("Bearer ") and auth[7:] == _API_TOKEN
        query_ok = request.query_params.get("token") == _API_TOKEN
        if not (header_ok or query_ok):
            return StarletteResponse(
                content=json.dumps({"detail": "Unauthorized — Bearer token required"}),
                status_code=401,
                media_type="application/json",
            )
        return await call_next(request)


def _require_ws_auth(websocket: WebSocket) -> bool:
    """Validate `?token=<bearer>` on a WebSocket handshake.

    Returns True if the handshake carried a valid bearer. Callers must
    close the socket with policy-violation when False — do not accept the
    connection and then reject, to avoid leaking a "connected" event.
    """
    token = websocket.query_params.get("token") or ""
    if not token:
        # Also accept Bearer via the Authorization header (some clients).
        auth = websocket.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    return bool(token) and token == _API_TOKEN


# -- Rate Limiting --

class _RateLimiter:
    """Simple in-memory rate limiter using sliding window timestamps."""

    def __init__(self):
        self._buckets: Dict[str, list[float]] = defaultdict(list)

    def check(self, bucket: str, max_calls: int, window_seconds: float) -> bool:
        """Return True if request is allowed, False if rate-limited."""
        now = time.time()
        cutoff = now - window_seconds
        timestamps = self._buckets[bucket]
        # Prune old entries
        self._buckets[bucket] = [t for t in timestamps if t > cutoff]
        if len(self._buckets[bucket]) >= max_calls:
            return False
        self._buckets[bucket].append(now)
        return True

    def peek(self, bucket: str, max_calls: int, window_seconds: float) -> bool:
        """Return whether a call WOULD be allowed, without recording it.

        Pair with ``record`` so a slot is consumed only on success — a failed
        or no-op request (clarity-gate intake round, fast spawn failure) must
        not throttle the user's legitimate retry.
        """
        now = time.time()
        cutoff = now - window_seconds
        self._buckets[bucket] = [t for t in self._buckets[bucket] if t > cutoff]
        return len(self._buckets[bucket]) < max_calls

    def record(self, bucket: str) -> None:
        """Record one successful call against the bucket's sliding window."""
        self._buckets[bucket].append(time.time())


_rate_limiter = _RateLimiter()


# -- Spawn Locks (per-task asyncio lock for race condition prevention) --

_spawn_locks: Dict[str, asyncio.Lock] = {}


def _get_spawn_lock(task_id: str) -> asyncio.Lock:
    """Get or create a per-task asyncio lock for spawn operations.

    LOCKED BODIES MUST NOT RUN SYNCHRONOUS WORK DIRECTLY (2026-07-30).

    An ``asyncio.Lock`` can only be held from a coroutine, so every handler in
    this cohort is necessarily ``async def`` — and Starlette runs ``async def``
    path-ops ON the event loop. Until 2026-07-30 all five holders did their
    entire job inside ``async with lock``: yaml read-modify-write, an
    ``atomic_dump_yaml``, and a ``spawn_orchestrator`` process launch, none of
    it awaited. So each one stalled the same loop that serves the /ws push and
    the inline /mcp/v1 mount subagents call.

    The earlier sweep that converted the write endpoints to sync ``def``
    (approve_subtask, resolve_gate, override_blocked_review, …) could not reach
    this cohort: ``async with`` inside a ``def`` is a SyntaxError, and dropping
    the lock would reintroduce the double-spawn race it was added to prevent
    (two concurrent POSTs decomposing the same task into divergent plans —
    audit-07 §B spawns #3+#4).

    The shape that works, and the one every holder now uses::

        lock = _get_spawn_lock(task_id)
        async with lock:
            result = await asyncio.to_thread(_the_sync_body, ...)

    The lock is still held for the whole operation, so the race stays closed,
    but the loop is free while the body runs. Extracted helpers are named
    ``_<endpoint>_locked``. ``asyncio.to_thread`` propagates HTTPException
    unchanged, so the helpers raise exactly as the inline code did.

    ``tests/orchestrator/api/test_spawn_lock_off_loop.py`` re-derives this
    cohort from the AST, so a sixth holder cannot reintroduce the pattern.
    """
    if task_id not in _spawn_locks:
        _spawn_locks[task_id] = asyncio.Lock()
    return _spawn_locks[task_id]


def _create_task_locked(cmd: list[str], task_id: str) -> int | None:
    """Synchronous body of POST /api/tasks. Runs in a worker thread.

    See :func:`_get_spawn_lock` for why this is split out.
    """
    pid = spawn_orchestrator(cmd, task_id)
    if not pid:
        # A non-zero/fast launcher exit is NOT always a failure: in
        # deliberate mode the engine proposes a role panel, persists an
        # `awaiting` state, then parks (fast exit). The task IS created.
        # Only surface 500 when no task state was written (real crash).
        if not _task_created_ok(task_id):
            raise HTTPException(500, "Failed to spawn task orchestrator")
    return pid


# -- WebSocket Manager --

class ConnectionManager:
    """Manages WebSocket connections and task subscriptions."""

    def __init__(self):
        # /ws clients (subscribe by task_id). Receive task-scoped events via
        # broadcast_to_task, plus cross-task state_change via broadcast_state.
        # They must NOT receive agent_activity from other tasks.
        self.state_subs: Set[WebSocket] = set()
        # /ws/activity clients. Receive every global agent_activity / pulse
        # event via broadcast_activity. No per-task subscription.
        self.activity_subs: Set[WebSocket] = set()
        self.task_subs: Dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()
        # Most recent orchestrator_state envelope PER TASK, replayed to every
        # new /ws/activity client on connect. Without this the OkuroThinker
        # hook only sees state events that fire *after* the WS opens — page
        # refresh / navigation = miss the current state = idle widget even
        # when a subtask is actively running.
        #
        # Phase 0: keyed by task_id (was a single global slot). A single slot
        # let task B's "running" evict task A's last state, so a task-A viewer
        # reloading mid-run replayed B's state (or nothing) and saw idle —
        # cross-task leakage. Per-task keying isolates each task's last frame.
        self._last_orchestrator_state_msg: Dict[str, dict] = {}

    async def connect_state(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.state_subs.add(ws)

    async def connect_activity(self, ws: WebSocket):
        await ws.accept()
        # Snapshot the per-task last-state map; replay each task's last frame
        # oldest-seq first so a global (unscoped) consumer ends on the
        # freshest, while task-scoped consumers (the inline pill) filter to
        # their own task_id and seq-guard the rest.
        replay = sorted(
            self._last_orchestrator_state_msg.values(),
            key=lambda m: m.get("seq") or 0,
        )
        async with self._lock:
            self.activity_subs.add(ws)
        # Replay outside the lock so a slow client send can't block other
        # broadcasts. Best-effort — if the client drops mid-send the regular
        # send-failure path will collect it.
        for msg in replay:
            try:
                await ws.send_text(json.dumps(msg, default=str))
            except Exception as e:
                logger.debug(f"orchestrator_state snapshot send failed: {e}")
                break

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self.state_subs.discard(ws)
            self.activity_subs.discard(ws)
            for task_id in list(self.task_subs):
                self.task_subs[task_id].discard(ws)
                if not self.task_subs[task_id]:
                    del self.task_subs[task_id]

    async def subscribe(self, ws: WebSocket, task_id: str):
        async with self._lock:
            self.task_subs.setdefault(task_id, set()).add(ws)
            # Belt-and-suspenders — re-arm state_subs membership on
            # every subscribe. _broadcast_pool removes sockets from
            # state_subs on transient send failures (back-pressure
            # during multi-minute LLM calls); without this re-add the
            # socket received task-scoped log/agent_activity events
            # but missed cross-task state_change + snapshot broadcasts,
            # which is the "FE state stale after phase 1 review" class.
            self.state_subs.add(ws)

    async def broadcast_to_task(self, task_id: str, data: dict):
        subs = self.task_subs.get(task_id, set()).copy()
        dead = set()
        msg = json.dumps(data, default=str)
        for ws in subs:
            try:
                await ws.send_text(msg)
            except Exception as e:
                logger.debug(f"WebSocket send failed for task {task_id}, removing client: {e}")
                dead.add(ws)
        if dead:
            async with self._lock:
                if task_id in self.task_subs:
                    self.task_subs[task_id] -= dead

    async def _broadcast_pool(self, pool: Set[WebSocket], data: dict):
        dead = set()
        msg = json.dumps(data, default=str)
        for ws in pool.copy():
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        if dead:
            async with self._lock:
                pool -= dead

    async def broadcast_state(self, data: dict):
        """Send to every /ws client (task-detail pages, sidebar list, etc.).

        Used for cross-task state_change events that all clients need to
        refresh their caches on — NOT for per-task agent_activity.
        """
        await self._broadcast_pool(self.state_subs, data)

    async def broadcast_activity(self, data: dict):
        """Send to every /ws/activity client (pulse, shell).

        Used for the global agent-activity stream. /ws clients are NOT in
        this pool, so task-detail pages cannot receive events from other
        tasks through this channel.

        Side effect: when the broadcast is an orchestrator_state event,
        record it as the snapshot served to future client connects (see
        connect_activity). This is the cheapest way to fix the "page
        refreshed after a state change → chip stuck idle" bug; the WS
        stream becomes "snapshot + tail" instead of "tail only".
        """
        if (
            data.get("type") == "agent_activity"
            and isinstance(data.get("event"), dict)
            and data["event"].get("type") == "orchestrator_state"
        ):
            tid = data["event"].get("task_id") or data.get("task_id")
            if tid:
                self._last_orchestrator_state_msg[tid] = data
        await self._broadcast_pool(self.activity_subs, data)


ws_manager = ConnectionManager()


def broadcast_agent_event(event_type: str, payload: dict) -> None:
    """Schedule a global broadcast of an agent event to /ws/activity only.

    Fire-and-forget helper for subsystems (bridge, sense, hashi, …) that want
    to surface activity into the pulse without needing task scope. Safe to call
    from sync code — no-ops if no running loop.

    Goes to the activity pool only: task-detail /ws clients must not receive
    untagged global events that would pollute their per-task feed.
    """
    loop = getattr(event_watcher, "loop", None)
    if loop is None or not loop.is_running():
        return
    msg = {
        "type": _WS_ENVELOPE_AGENT_ACTIVITY,
        "task_id": None,
        "event": {"type": event_type, **payload},
    }
    asyncio.run_coroutine_threadsafe(ws_manager.broadcast_activity(msg), loop)


def broadcast_orchestrator_state(
    state: str,
    label: str,
    task_id: str | None = None,
) -> None:
    """Broadcast an orchestrator lifecycle event to the OkuroThinker widget.

    Emits into the same /ws/activity stream as other agent events, tagged as
    ``event.type == "orchestrator_state"`` so the thinker hook can filter
    without affecting pulse-rate calculations.

    states:
        idle       — nothing running; widget can hide
        thinking   — request received, preparing the call
        planning   — decomposer is running
        running    — a subtask is executing (include subtask_id in payload)
        generating — post-completion work (continuation suggestions, capability harvest)
        complete   — terminal state for a task; transitions to idle after
        error      — something failed; widget surfaces the label

    label: short human string (≤60 chars) shown next to the animation.
        Leads with a verb in present-continuous: "Planning phase 2…",
        "Running backend-engineer…", "Generating 3 next steps…".
    """
    payload = {"state": state, "label": label}
    if task_id:
        payload["task_id"] = task_id
    broadcast_agent_event("orchestrator_state", payload)


# -- EventWatcher (watchdog on tasks/ directory) --


class EventWatcherOffsetStore:
    """C9 — persisted byte-offset store for tailed log files.

    The pre-fix EventWatcher held ``_file_positions: Dict[str, int]`` in
    memory only. On process restart the dict was empty; the bootstrap path
    then seeked every existing ``.activity.jsonl`` to ``stat().st_size`` so
    a restart wouldn't replay history — but that also meant **events
    written during the downtime window were never broadcast** (audit
    `02-invariants.md` §C9 "restart loss class").

    Contract:
        store = EventWatcherOffsetStore(tasks_dir)
        store.set_offset(path, 42)
        # process restarts ...
        EventWatcherOffsetStore(tasks_dir).get_offset(path)  -> 42

    Persistence layout: a single SQLite file at
    ``tasks_dir / ".watcher-offsets.db"`` with one row per watched path.
    SQLite is chosen over per-file ``.offset`` markers because the watcher
    tails ~30+ files concurrently and the bulk of writes hit a single
    table — one fsync per commit beats per-file inode churn.

    Thread-safety: each call opens its own short-lived connection so
    callers from the asyncio loop, the watchdog thread, and tests don't
    share state. SQLite handles serialization at the DB level.
    """

    def __init__(self, tasks_dir: Path):
        self.tasks_dir = Path(tasks_dir)
        # Place the offsets DB beside the tasks tree so it travels with
        # the orchestrator root (OKURO_ROOT). The parent always exists in
        # production (tasks/ is created at startup); in unit-test land
        # the fixture creates it. Be defensive anyway.
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.tasks_dir / ".watcher-offsets.db"
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS offsets ("
                " path TEXT PRIMARY KEY,"
                " offset INTEGER NOT NULL,"
                " updated_at TEXT NOT NULL"
                ")"
            )
            conn.commit()

    def get_offset(self, path: Path) -> int:
        """Return the persisted offset for ``path``, or 0 if unknown."""
        import sqlite3
        key = str(path)
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "SELECT offset FROM offsets WHERE path = ?", (key,)
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error as exc:
            logger.warning("offset read failed for %s: %r", key, exc)
            return 0

    def set_offset(self, path: Path, offset: int) -> None:
        """Persist ``offset`` for ``path``. Upserts on path."""
        import sqlite3
        key = str(path)
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO offsets (path, offset, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET "
                    " offset = excluded.offset,"
                    " updated_at = excluded.updated_at",
                    (key, int(offset), datetime.utcnow().isoformat()),
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.warning("offset write failed for %s: %r", key, exc)


class LogFileHandler(FileSystemEventHandler):
    """Watches for log.jsonl changes and dispatches new events.

    Note: .activity.jsonl is intentionally NOT handled here. The dispatcher
    holds that file open for the duration of a subagent run with periodic
    flushes; macOS FSEvents coalesces on_modified events for long-held-open
    file descriptors and only delivers them when the FD is closed (verified
    live: 50 writes over 5s with flush after each → 0 FSEvents until close,
    then all 50 fire at once). EventWatcher._poll_activity_files() handles
    .activity.jsonl via direct polling instead.
    """

    def __init__(self, watcher: "EventWatcher"):
        self.watcher = watcher
        self._last_sizes: Dict[str, int] = {}
        self._debounce: Dict[str, float] = {}

    def on_modified(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.name != "log.jsonl":
            return

        # Debounce
        now = time.time()
        key = str(path)
        if key in self._debounce and now - self._debounce[key] < 0.3:
            return
        self._debounce[key] = now

        # Extract task_id from path: tasks/{task_id}/log.jsonl
        parts = path.parts
        if "tasks" in parts:
            idx = parts.index("tasks")
            if idx + 1 < len(parts):
                task_id = parts[idx + 1]
                if self.watcher.loop:
                    asyncio.run_coroutine_threadsafe(
                        self.watcher.handle_log_change(task_id, path),
                        self.watcher.loop,
                    )


class EventWatcher:
    """Watches log.jsonl files and broadcasts new events via WebSocket.

    Also tails ~/.okuro/telemetry/usage.jsonl to surface MCP tool calls
    from every okuro-infra agent (not just orchestrator task subagents).
    """

    TELEMETRY_FILE = okuro_home() / "telemetry" / "usage.jsonl"
    _TELEMETRY_POLL_SECONDS = 1.0
    # Activity tail polls per-task .activity.jsonl files. Required because
    # macOS FSEvents coalesces on_modified events for files held open with
    # periodic flushes (the dispatcher's exact write pattern), only firing
    # when the FD closes — by which time the subagent has finished and the
    # "live" stream is moot. 250ms gives sub-second perceived latency.
    _ACTIVITY_POLL_SECONDS = 0.25

    def __init__(self, tasks_dir: Path):
        self.tasks_dir = tasks_dir
        self.observer: Observer | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        # C9 — persisted offset store; survives process restart so events
        # written during downtime are still tailed on next boot. The
        # ``_file_positions`` dict is now a write-through in-memory cache
        # in front of the SQLite store, kept so the hot tail loop doesn't
        # round-trip to disk on every iteration.
        self._offset_store = EventWatcherOffsetStore(tasks_dir)
        self._file_positions: Dict[str, int] = {}
        self._telemetry_task: asyncio.Task | None = None
        self._activity_task: asyncio.Task | None = None
        # Per-session cache so we don't hit the sessions table for every
        # tool call. Populated lazily from sessions.pid the first time we
        # see a given session_id on the telemetry tail. Sized loosely —
        # session_ids are long-lived, the cache is bounded by concurrent
        # agents, not by call volume.
        self._session_pid_cache: Dict[str, tuple[int | None, str | None]] = {}

    def _get_pos(self, path: Path) -> int:
        """Look up the current read offset, preferring in-memory cache."""
        key = str(path)
        if key in self._file_positions:
            return self._file_positions[key]
        # Lazy hydration from the persisted store on first lookup.
        persisted = self._offset_store.get_offset(path)
        self._file_positions[key] = persisted
        return persisted

    def _set_pos(self, path: Path, offset: int) -> None:
        """Update the in-memory cache AND the persisted store."""
        key = str(path)
        self._file_positions[key] = offset
        self._offset_store.set_offset(path, offset)

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.observer = Observer()
        handler = LogFileHandler(self)
        if self.tasks_dir.exists():
            self.observer.schedule(handler, str(self.tasks_dir), recursive=True)
            self.observer.start()
            logger.info(f"EventWatcher started on {self.tasks_dir}")

        # C9 — On startup, hydrate the in-memory cache from the persisted
        # store. If a file has no persisted offset (first-ever start, or a
        # freshly created task), seek to EOF so we don't replay history.
        # The persisted offset is the SoT — never overwrite a known offset
        # with EOF.
        try:
            if self.TELEMETRY_FILE.exists():
                persisted = self._offset_store.get_offset(self.TELEMETRY_FILE)
                if persisted > 0:
                    self._file_positions[str(self.TELEMETRY_FILE)] = persisted
                else:
                    eof = self.TELEMETRY_FILE.stat().st_size
                    self._set_pos(self.TELEMETRY_FILE, eof)
        except OSError:
            pass
        if self.tasks_dir.exists():
            # Same shape as the tail loop: d_type instead of an is_dir stat,
            # and one stat on the FILE instead of exists+stat.
            with os.scandir(self.tasks_dir) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    act = Path(entry.path) / ".activity.jsonl"
                    try:
                        eof = os.stat(act).st_size
                    except OSError:
                        continue
                    try:
                        persisted = self._offset_store.get_offset(act)
                        if persisted > 0:
                            self._file_positions[str(act)] = persisted
                        else:
                            self._set_pos(act, eof)
                    except OSError:
                        pass
        self._telemetry_task = asyncio.create_task(self._tail_telemetry())
        self._activity_task = asyncio.create_task(self._tail_activity_files())

    def is_alive(self) -> bool:
        """Check if the observer thread is still running."""
        if self.observer and not self.observer.is_alive():
            logger.error("EventWatcher observer thread died — real-time updates are stopped")
            return False
        return True

    async def stop(self):
        for task in (self._telemetry_task, self._activity_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self.observer:
            self.observer.stop()
            self.observer.join()
            logger.info("EventWatcher stopped")

    async def _tail_activity_files(self):
        """Poll every task's .activity.jsonl and broadcast new lines.

        FSEvents-bypass: the dispatcher holds .activity.jsonl open for the
        whole subagent run with periodic flush; macOS coalesces watchdog
        on_modified events until the FD is closed. Polling sees flushed
        bytes immediately. handle_activity_change does the actual byte
        read + broadcast and dedupes via self._file_positions[pos_key],
        so a future watchdog fire on close becomes a no-op.
        """
        while True:
            try:
                if self.tasks_dir.exists():
                    seen = 0
                    with os.scandir(self.tasks_dir) as entries:
                        for entry in entries:
                            # d_type from getdents — no stat syscall on ext4.
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                            # Stat THE FILE, never entry.stat(): entry is the
                            # TASK DIRECTORY, so entry.stat().st_size is 4096.
                            # Compared against a byte offset that makes
                            # ``size <= last_pos`` true for every task whose
                            # activity log passed 4 KB, and the live stream
                            # stops broadcasting with no exception and no log
                            # line — while every latency metric improves,
                            # because the work genuinely disappeared.
                            activity_path = Path(entry.path) / ".activity.jsonl"
                            try:
                                size = os.stat(activity_path).st_size
                            except OSError:
                                continue
                            seen += 1
                            last_pos = self._get_pos(activity_path)
                            if size > last_pos:
                                await self.handle_activity_change(
                                    entry.name, activity_path
                                )
                            # Hand the loop back periodically. Without this the
                            # whole scan is one uninterruptible block: the
                            # profile measured 3.8ms per cycle alone but 473ms
                            # with two CPU-bound threads competing for the GIL,
                            # and for that whole window the loop can neither
                            # accept nor answer anything. Safe at this level —
                            # handle_activity_change's read-modify-write of the
                            # offset has no await inside it, so a yield here
                            # cannot interleave into it.
                            if seen % 50 == 0:
                                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"activity tail error: {e}")
            await asyncio.sleep(self._ACTIVITY_POLL_SECONDS)

    def _resolve_session_pid(
        self, session_id: str, fallback_provider: str | None
    ) -> tuple[int | None, str | None]:
        """Return (pid, provider) for a session_id, cached.

        The telemetry line carries provider but not pid — pid is only in
        the sessions table (populated by create_session at bootstrap).
        Cache the lookup so the tail stays fast at steady state.
        """
        cached = self._session_pid_cache.get(session_id)
        if cached is not None:
            return cached
        pid: int | None = None
        provider: str | None = fallback_provider
        try:
            from okuro.db import get_db
            row = get_db().fetchone(
                "SELECT pid, provider FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            if row is not None:
                pid = row["pid"]
                provider = row["provider"] or fallback_provider
        except Exception as e:
            logger.debug(f"session pid lookup failed for {session_id}: {e}")
        self._session_pid_cache[session_id] = (pid, provider)
        return pid, provider

    async def _stamp_agent_heartbeat(self, entry: dict) -> None:
        """Make every telemetry line stamp a heartbeat for its agent.

        This is the systematic presence feed: any process that writes to
        usage.jsonl (regardless of which version of the MCP middleware it
        loaded) gets its agent row refreshed here. Also auto-links the
        session row to its agent the first time we see it, so historical
        sessions created before the agents migration pick up agent_id
        retroactively without a backfill script.

        Attribution priority:
          1. `entry.pid` — set by the writer itself via os.getpid() inside
             log_call. Authoritative: each subprocess knows its own pid,
             immune to shared-marker collisions between parallel claude-code
             CLIs.
          2. Session→pid lookup in the sessions table. Fallback for entries
             emitted before log_call started stamping pid (transitional).
        """
        session_id = entry.get("session")
        provider = entry.get("provider")
        if not provider or provider == "unknown":
            return

        pid = entry.get("pid")
        resolved_provider: str | None = provider
        if not pid and session_id:
            pid, resolved_provider = self._resolve_session_pid(session_id, provider)
        if pid is None:
            return
        try:
            from okuro.sense.agents import upsert_heartbeat
            agent_id = upsert_heartbeat(
                provider=resolved_provider or provider, pid=pid,
            )
            if agent_id is None:
                return
            # Opportunistic link: fill sessions.agent_id for rows that
            # predate the agents layer. Guarded so an existing link is
            # never overwritten.
            from okuro.db import get_db
            db = get_db()
            db.execute(
                "UPDATE sessions SET agent_id = ? "
                "WHERE session_id = ? AND agent_id IS NULL",
                (agent_id, session_id),
            )
            db.conn.commit()
        except Exception as e:
            logger.debug(f"heartbeat stamp failed for {session_id}: {e}")

    async def _tail_telemetry(self):
        """Poll ~/.okuro/telemetry/usage.jsonl and broadcast new MCP tool calls.

        Every okuro agent (claude-code / gemini / codex / cursor) writes to this
        file via okuro.telemetry.logger.log_call. Tailing it makes the pulse
        reflect *all* okuro-infra activity, not just orchestrator task subagents,
        AND serves as the primary heartbeat source for the agents layer —
        existing MCP subprocesses that loaded the pre-agents middleware still
        appear as "live" as long as they keep writing telemetry.
        """
        pos_key = str(self.TELEMETRY_FILE)
        while True:
            try:
                if self.TELEMETRY_FILE.exists():
                    last_pos = self._get_pos(self.TELEMETRY_FILE)
                    try:
                        size = self.TELEMETRY_FILE.stat().st_size
                    except OSError:
                        size = last_pos
                    if size < last_pos:
                        last_pos = 0  # file rotated
                    if size > last_pos:
                        with open(self.TELEMETRY_FILE) as f:
                            f.seek(last_pos)
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    entry = json.loads(line)
                                except json.JSONDecodeError:
                                    continue
                                # Skip synthetic rows the system writes to
                                # itself (e.g., doctor's telemetry_write_health
                                # probe). They have no provider/pid so the
                                # heartbeat path already drops them, but the
                                # broadcast path used to forward them as
                                # mcp_tool_call — inflating CALLS in the pulse
                                # view while AGENTS stayed at 0.
                                if entry.get("type") == "doctor_probe":
                                    continue
                                # Stamp heartbeat FIRST so presence reflects
                                # activity even if the WS broadcast errors.
                                await self._stamp_agent_heartbeat(entry)
                                # Propagate task_id + subtask_id from the
                                # telemetry entry into BOTH the envelope (so
                                # the per-task WS filter keeps them on the
                                # right task) AND the inner event (so the
                                # activity-feed component's subtask scope
                                # filter can match them). Pre-fix, both
                                # fields were dropped → role-researcher
                                # Phase 0 subagent appeared to do nothing in
                                # the panel while it was actively spawning
                                # tool calls. The dispatcher already stamps
                                # these onto every subagent's child env via
                                # OKURO_TASK_ID / OKURO_SUBTASK_ID; the MCP
                                # client adds them to each telemetry row.
                                _ent_task = entry.get("task_id")
                                _ent_sub = entry.get("subtask_id")
                                await ws_manager.broadcast_activity({
                                    "type": _WS_ENVELOPE_AGENT_ACTIVITY,
                                    "task_id": _ent_task,
                                    "event": {
                                        "type": _WS_INNER_MCP_TOOL_CALL,
                                        "server": entry.get("server"),
                                        "tool": entry.get("tool"),
                                        "provider": entry.get("provider"),
                                        "session": entry.get("session"),
                                        "task_id": _ent_task,
                                        "subtask_id": _ent_sub,
                                        "role": entry.get("subtask_role"),
                                        "ok": entry.get("ok", True),
                                        "latency_ms": entry.get("latency_ms"),
                                        "preview": entry.get("preview"),
                                        "ts": entry.get("ts"),
                                    },
                                })
                            self._set_pos(self.TELEMETRY_FILE, f.tell())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"telemetry tail error: {e}")
            await asyncio.sleep(self._TELEMETRY_POLL_SECONDS)

    async def handle_log_change(self, task_id: str, log_path: Path):
        """Read new lines from log.jsonl and broadcast them.

        COALESCING CONTRACT (2026-07-30) — read this before changing the loop.

        The watchdog debounce collapses a write burst into ONE call to this
        method that then iterates over every new line. Until this fix the loop
        body did, per allowlisted line: invalidate the snapshot cache, run the
        *sync* ``read_task_snapshot`` (191 ms at the p99 108 KB plan, because
        the invalidate one line earlier guarantees the cold path), and emit a
        ``state_change`` frame. A review round emits five allowlisted events
        back to back; the busiest observed second held 26. Measured on the p99
        plan with ``scripts/streamline_latency_bench.py``: 26 ``state_change``
        + 26 ``snapshot`` frames, last frame 5.3 s after the append, and 52
        frontend GETs — because ``use-websocket.ts`` blanket-invalidates 8
        query keys on *every* ``state_change`` and never reads ``event_type``.

        So the per-line work was 26 renders of one semantic change. Now:

        - ``log`` frames stay PER LINE. The log view needs every entry.
        - ``orchestrator_state`` forwarding stays PER LINE. It carries ``seq``
          and drives the thinker pill's ordering guard.
        - ``state_change`` + ``snapshot`` fire ONCE per task per batch, after
          the loop, carrying the full list of triggering types so no consumer
          loses information (see :meth:`_push_coalesced_state`).
        """
        try:
            last_pos = self._get_pos(log_path)

            with open(log_path) as f:
                f.seek(last_pos)
                new_lines = f.readlines()
                self._set_pos(log_path, f.tell())

            # Allowlisted types seen in THIS batch, in order. Non-empty ⇒ emit
            # exactly one state_change + one snapshot once the loop is done.
            state_change_types: list[str] = []

            for line in new_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    await ws_manager.broadcast_to_task(task_id, {
                        "type": _WS_ENVELOPE_LOG,
                        "task_id": task_id,
                        "entry": event,
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                    # Task state changes go to every /ws client so
                    # sidebars/task lists invalidate their caches. Stays in
                    # the state pool — does NOT hit /ws/activity pulse
                    # clients. Deferred to the batch tail below.
                    #
                    # C7 — the allowlist lives at module level (top of file).
                    # Adding a new state-change emitter without registering it
                    # there raises ValueError at the state.emit_event boundary.
                    if event.get("type") in _STATE_CHANGE_EVENTS:
                        state_change_types.append(event.get("type"))

                    # Forward orchestrator_state events from the engine
                    # subprocess into the global activity stream so the
                    # OkuroThinker widget picks them up without needing a
                    # second IPC path.
                    if event.get("type") == "orchestrator_state":
                        # Carry the append_log seq through so the FE pill can
                        # drop out-of-order / stale-replay frames (Phase 0).
                        _seq = event.get("seq")
                        await ws_manager.broadcast_activity({
                            "type": _WS_ENVELOPE_AGENT_ACTIVITY,
                            "task_id": task_id,
                            "seq": _seq,
                            "event": {
                                "type": "orchestrator_state",
                                "state": event.get("state", "idle"),
                                "label": event.get("label", ""),
                                "task_id": task_id,
                                "seq": _seq,
                            },
                        })
                except json.JSONDecodeError:
                    continue

            # Batch tail — one state_change + one snapshot for the whole burst.
            if state_change_types:
                await self._push_coalesced_state(task_id, state_change_types)
        except Exception as e:
            logger.error(f"Error handling log change for {task_id}: {e}")

    async def _push_coalesced_state(self, task_id: str, triggers: list[str]) -> None:
        """Emit ONE ``state_change`` + ONE ``snapshot`` for a batch of events.

        Split out of :meth:`handle_log_change` so the coalescing contract has a
        single call site and a single test surface.

        Two things here are deliberate and load-bearing:

        **The invalidate stays.** It looks redundant next to the mtime cache,
        but it is the only freshness mechanism the snapshot has for log-derived
        fields: ``read_task_snapshot`` derives from ``log.jsonl`` too
        (``state_reader.py`` "Every /api/tasks/{id}/snapshot GET used to
        re-read task.yaml + plan.yaml + log.jsonl"), while the cache signature
        is only the ``(task.yaml, plan.yaml)`` mtime pair. An event appended to
        ``log.jsonl`` alone moves neither mtime, so without the explicit
        invalidate every log-derived field goes permanently stale. Batching it
        keeps that correctness — the read that follows is still cold — while
        paying for the cold read once per burst instead of once per line.

        **The read runs off the event loop.** ``read_task_snapshot`` is a sync
        ``def``; called bare inside a coroutine it holds the loop for its full
        duration. ``asyncio.to_thread`` hands it to a worker. Consequence worth
        stating: ``state_reader``'s module-level ``_SNAPSHOT_CACHE`` /
        ``_SUMMARY_CACHE`` dicts are now written from a worker thread
        concurrently with the sync read endpoints that Starlette already
        threadpools. Both writers only ever rebind whole dict entries, and dict
        item assignment is atomic under the GIL, so a reader sees either the
        old or the new entry — never a torn one. Do not add read-modify-write
        logic to those caches without a lock.
        """
        # event_type keeps the single-value shape every existing consumer was
        # written against (the frontend's state_change case does not read it at
        # all — it blanket-invalidates); event_types carries the full batch so a
        # future consumer that DOES care loses nothing to coalescing.
        await ws_manager.broadcast_state({
            "type": _WS_ENVELOPE_STATE_CHANGE,
            "task_id": task_id,
            "event_type": triggers[-1],
            "event_types": triggers,
            "coalesced": len(triggers),
            "timestamp": datetime.utcnow().isoformat(),
        })

        # Push the freshly-rendered snapshot so subscribers update without a
        # /snapshot round-trip. The FE applies the payload directly.
        try:
            from okuro.orchestrator.api.state_reader import (
                read_task_snapshot,
                invalidate_snapshot_cache,
            )
            from pathlib import Path as _Path
            # C9 — belt-and-suspenders invalidation: direct append_log writers
            # that bypass state.emit_event land here when the EventWatcher
            # reads the line off disk. Invalidate before re-reading so the WS
            # push and any downstream GET serve the fresh post-mutation
            # snapshot from cache.
            invalidate_snapshot_cache(task_id)
            _snap = await asyncio.to_thread(
                read_task_snapshot, _Path(TASKS_DIR) / task_id
            )
            if _snap is not None:
                await ws_manager.broadcast_state({
                    "type": _WS_ENVELOPE_SNAPSHOT,
                    "task_id": task_id,
                    "snapshot": _snap,
                    "trigger": triggers[-1],
                    "triggers": triggers,
                    "timestamp": datetime.utcnow().isoformat(),
                })
        except Exception as _exc:
            logger.warning(f"snapshot push failed for {task_id}: {_exc}")

    async def handle_activity_change(self, task_id: str, activity_path: Path):
        """Read new lines from .activity.jsonl and broadcast as agent_activity events."""
        try:
            last_pos = self._get_pos(activity_path)

            # Safety: if file somehow got smaller, reset position
            try:
                file_size = activity_path.stat().st_size
                if file_size < last_pos:
                    last_pos = 0
            except OSError:
                return

            with open(activity_path) as f:
                f.seek(last_pos)
                new_lines = f.readlines()
                self._set_pos(activity_path, f.tell())

            for line in new_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # C4 — drop phantom subagent emissions before broadcast.
                # When a duplicate-dispatch (LF1/LF2) or a 600 s claude-cli
                # watchdog timeout writes events for a subtask the engine
                # already marked done, is_phantom_event detects the
                # terminal precedence and we skip the broadcast entirely.
                # Fail-open: any error falls through to broadcast.
                ev_sub = event.get("subtask_id")
                ev_ts = event.get("ts") or ""
                if ev_sub and ev_ts:
                    try:
                        if is_phantom_event(
                            task_id, ev_sub, ev_ts,
                            tasks_dir=self.tasks_dir,
                        ):
                            logger.debug(
                                "phantom event dropped for %s/%s @ %s",
                                task_id, ev_sub, ev_ts,
                            )
                            continue
                    except Exception:
                        pass

                msg = {
                    "type": _WS_ENVELOPE_AGENT_ACTIVITY,
                    "task_id": task_id,
                    "event": event,
                }
                await ws_manager.broadcast_to_task(task_id, msg)
                # tool_use / text also feed the global pulse (/ws/activity).
                # Do NOT hit /ws state-pool clients — they'd see another
                # task's activity in their task-detail feed.
                if event.get("type") in ("tool_use", "text"):
                    await ws_manager.broadcast_activity(msg)
        except Exception as e:
            logger.error(f"Error handling activity change for {task_id}: {e}")


event_watcher = EventWatcher(TASKS_DIR)


# -- App Lifespan --

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting Okuro Orchestrator API Server")
    logger.info(f"Okuro root: {OKURO_ROOT}")
    logger.info(f"Tasks dir: {TASKS_DIR}")
    TASKS_DIR.mkdir(parents=True, exist_ok=True)

    # Auto-sync new role catalog YAMLs into the live DB. seed_if_empty
    # only triggers on a fresh install, so adding a new role to the
    # repo (e.g. workforce-reviewer) wouldn't reach existing users
    # without a manual seeder call. seed_from_catalog with overwrite=False
    # only inserts roles whose role_id isn't already present, so user
    # edits to existing roles aren't clobbered.
    try:
        from okuro.db import get_db
        from okuro.roles.seed import seed_from_catalog

        inserted, _ = seed_from_catalog(get_db(), overwrite=False)
        if inserted:
            logger.info(f"Auto-seeded {inserted} new role(s) from catalog")
    except Exception as exc:
        logger.warning(f"Role catalog auto-sync failed: {exc}")

    # Hydrate the orchestrator-state snapshot from disk so a server restart
    # doesn't reset the OkuroThinker to "no current state". Scans every task
    # log.jsonl, finds the most recent orchestrator_state event globally,
    # and seeds ws_manager._last_orchestrator_state_msg. New /ws/activity
    # connects then receive that snapshot immediately on connect.
    try:
        _hydrate_orchestrator_state_snapshot()
    except Exception as exc:
        logger.warning(f"orchestrator_state snapshot hydration failed: {exc}")

    await event_watcher.start()
    # Theme H — periodic scope reaper. Old http.server / engine scopes
    # accumulated over a 2-hour session (0 → 4) when engine death didn't
    # cascade to nested scopes; this reaper backstops the launcher's
    # EXIT trap by sweeping orphans every 60 s. Gate via env so tests
    # can disable. Disabled when systemd-run is unavailable (macOS,
    # non-systemd Linux).
    reaper_enabled = os.environ.get("OKURO_SCOPE_REAPER_ENABLED", "1") != "0"
    scope_reaper_task = None
    if reaper_enabled and _can_use_systemd_run():
        scope_reaper_task = asyncio.create_task(_scope_reaper_loop())

    # Inline-MCP sub-app lifespan is NOT propagated by Starlette mount —
    # enter it explicitly so StreamableHTTPSessionManager.run() is active.
    from okuro.mcp.inline_http import inline_mcp_lifespan

    async with inline_mcp_lifespan():
        yield

    if scope_reaper_task is not None:
        scope_reaper_task.cancel()
        try:
            await scope_reaper_task
        except (asyncio.CancelledError, Exception):
            pass
    await event_watcher.stop()
    logger.info("Okuro Orchestrator API Server stopped")


# ---------------------------------------------------------------------------
# Theme H — scope reaper.
#
# When the engine subprocess dies abnormally (SIGKILL, OOM, crash before
# trap installs), the launcher's EXIT trap doesn't fire and nested
# scopes (engine's http.server children, etc.) survive as orphans.
# Observed in a long 2026-05-29 session: scope_count grew
# 0 → 4 over 2 hours on a single task.
#
# The reaper sweeps every 60 s:
#   1. ``systemctl --user list-units --type=scope --no-legend --plain``
#   2. Match scope names against ``okuro-task-<id>-<digits>.scope``.
#   3. For each, load task.yaml — if status ∈ {done, failed, halted}
#      AND scope is running, ``systemctl --user stop <scope>``.
#
# All subprocess calls have a 5 s timeout so a hung systemctl can never
# block the loop.
# ---------------------------------------------------------------------------


_SCOPE_RE = _re.compile(r"^(okuro-(task-[^\s.]+)-\d+\.scope)\b")
_REAPER_TERMINAL_STATUSES = {"done", "failed", "halted"}


def _list_okuro_scopes() -> list[tuple[str, str]]:
    """Return ``[(scope_name, task_id), ...]`` for every active okuro scope.

    Empty list on subprocess error / missing systemctl / unparseable line.
    Each ``scope_name`` includes the ``.scope`` suffix so it round-trips
    through ``systemctl stop`` without re-formatting.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-units", "--type=scope",
             "--no-legend", "--plain"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("scope_reaper: list-units failed: %s", exc)
        return []
    if result.returncode != 0:
        logger.warning("scope_reaper: list-units rc=%s stderr=%s",
                       result.returncode, (result.stderr or "")[:200])
        return []
    out: list[tuple[str, str]] = []
    for line in (result.stdout or "").splitlines():
        # ``--plain`` strips column padding but the unit name still leads.
        line = line.strip()
        if not line:
            continue
        first = line.split(None, 1)[0]
        m = _SCOPE_RE.match(first)
        if not m:
            continue
        scope_name = m.group(1)
        task_id = m.group(2)
        out.append((scope_name, task_id))
    return out


def _task_status_terminal(task_id: str) -> bool:
    """True iff task.yaml's status field is in {done, failed, halted}.

    Defensive: missing task dir, missing task.yaml, or YAML parse error
    all return False so the reaper never stops a scope whose status
    cannot be confirmed.
    """
    try:
        task_yaml = TASKS_DIR / task_id / "task.yaml"
        if not task_yaml.is_file():
            return False
        with open(task_yaml) as fh:
            data = yload(fh) or {}
        status = str(data.get("status") or "").strip().lower()
        return status in _REAPER_TERMINAL_STATUSES
    except Exception as exc:
        logger.warning("scope_reaper: cannot read status for %s: %s",
                       task_id, exc)
        return False


def _stop_scope(scope_name: str) -> bool:
    """``systemctl --user stop <scope>``. True iff the stop call succeeded."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "stop", scope_name],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("scope_reaper: stop %s failed: %s", scope_name, exc)
        return False
    if result.returncode != 0:
        logger.warning("scope_reaper: stop %s rc=%s stderr=%s",
                       scope_name, result.returncode,
                       (result.stderr or "")[:200])
        return False
    return True


def _reaper_sweep_once() -> dict:
    """One reaper pass — list scopes, stop terminal ones, return counts.

    Pure function (no asyncio) — drives the periodic loop AND the unit
    tests directly.
    """
    scopes = _list_okuro_scopes()
    reaped: list[str] = []
    for scope_name, task_id in scopes:
        if _task_status_terminal(task_id):
            if _stop_scope(scope_name):
                reaped.append(scope_name)
                logger.info("scope_reaper: reaped %s (task %s terminal)",
                            scope_name, task_id)
    return {"scanned": len(scopes), "reaped": reaped}


async def _scope_reaper_loop(interval_s: float = 60.0) -> None:
    """Periodic reaper task. Cancellable from lifespan teardown."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            # Run the sweep in a thread so the synchronous subprocess
            # calls don't block the event loop even with their 5s caps.
            await asyncio.to_thread(_reaper_sweep_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("scope_reaper loop error: %s", exc)


def _hydrate_orchestrator_state_snapshot() -> None:
    """Find the latest orchestrator_state event across all task log.jsonl files.

    Scans only the bottom of each log (last ~16 KB) — orchestrator_state is
    appended frequently enough that the latest one is always near the tail.
    Sets ``ws_manager._last_orchestrator_state_msg`` if any found so the
    next /ws/activity connect serves it as a snapshot.
    """
    if not TASKS_DIR.exists():
        return
    hydrated = 0
    for task_dir in TASKS_DIR.iterdir():
        if not task_dir.is_dir():
            continue
        log_path = task_dir / "log.jsonl"
        if not log_path.exists():
            continue
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 16384))
                tail = f.read().decode(errors="ignore").splitlines()
        except OSError:
            continue
        for line in reversed(tail):
            line = line.strip()
            if not line or "orchestrator_state" not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "orchestrator_state":
                continue
            # Only resurrect genuinely-active tasks. An idle/complete/empty
            # last-frame carries no signal — the FE pill defaults to idle —
            # and hydrating one per historical task would flood every fresh
            # /ws/activity connect with hundreds of idle replays. We still
            # break after the first orchestrator_state per file (the latest);
            # if that latest is non-active this task simply isn't hydrated.
            if (ev.get("state") or "idle") in ("idle", "complete"):
                break
            # Per-task: store each task's own latest frame so a restart
            # replays the correct state to each task's viewers (was: only the
            # single globally-latest task got a replay — every other task's
            # viewer saw idle after a server restart).
            _seq = ev.get("seq")
            ws_manager._last_orchestrator_state_msg[task_dir.name] = {
                "type": _WS_ENVELOPE_AGENT_ACTIVITY,
                "task_id": task_dir.name,
                "seq": _seq,
                "event": {
                    "type": "orchestrator_state",
                    "state": ev.get("state", "idle"),
                    "label": ev.get("label", ""),
                    "task_id": task_dir.name,
                    "seq": _seq,
                },
            }
            hydrated += 1
            break  # only the most recent in this file matters
    if hydrated:
        logger.info(f"orchestrator_state snapshot hydrated for {hydrated} task(s)")


# audit(NF-5): /docs, /redoc, /openapi.json are publicly reachable on loopback
# by default in FastAPI. On multi-user boxes or malware-compromised laptops any
# local reader gets a 167-path API map. Gate the three endpoints on OKURO_DEV=1;
# in prod they 404. Overrideable via OKURO_EXPOSE_DOCS=1 for ops who want them.
def _should_expose_docs() -> bool:
    """Return True when FastAPI's auto-docs (/docs, /redoc, /openapi.json) should
    be reachable. Defaults to False — opt in via OKURO_DEV=1 (general dev mode)
    or OKURO_EXPOSE_DOCS=1 (docs-only override for ops who need the schema)."""
    return (
        os.environ.get("OKURO_DEV") == "1"
        or os.environ.get("OKURO_EXPOSE_DOCS") == "1"
    )


_expose_docs = _should_expose_docs()

app = FastAPI(
    title="Okuro Orchestrator API",
    description="REST API and WebSocket events for Okuro autonomous workforce",
    version=__version__,  # derived: pyproject is the SSOT, see okuro.release.version
    lifespan=lifespan,
    docs_url="/docs" if _expose_docs else None,
    redoc_url="/redoc" if _expose_docs else None,
    openapi_url="/openapi.json" if _expose_docs else None,
)

from okuro.orchestrator.api.security_headers import SecurityHeadersMiddleware

# Auth middleware must be added before CORS so CORS preflight (OPTIONS) passes through.
# Security headers are added AFTER the bearer auth middleware so 401 responses also
# carry the hardening headers (Starlette wraps in reverse-add order — last-added is
# outermost, so SecurityHeadersMiddleware wraps BearerAuthMiddleware's direct 401 replies).
app.add_middleware(BearerAuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

def _build_cors_regex() -> str:
    """Build CORS origin regex: always localhost, plus user's LAN domain if configured.

    audit fix #21: previously matched `[\\w.-]+\\.<host.domain>` which
    permitted any subdomain (e.g. `evil.example.lan`) to make
    credentialed cross-origin requests once the user had set
    `host.domain`. Now: only the bare host.domain plus an explicit allow-list
    of subdomains is permitted. The allow-list is read from convention
    `host.allowed_subdomains` (list of strings); if absent, defaults to
    `["okuro"]` so `okuro.<domain>` keeps working without configuration.
    """
    base = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
    try:
        from okuro.yu.conventions import get_convention
        domain = get_convention("host.domain")
        if domain:
            escaped_domain = _re.escape(domain)
            # Bare domain, e.g. https://example.lan
            base += rf"|^https?://{escaped_domain}(:\d+)?$"
            # Explicit subdomain allow-list (defaults to ["okuro"]).
            allowed = get_convention("host.allowed_subdomains") or ["okuro"]
            if isinstance(allowed, str):
                allowed = [allowed]
            for sub in allowed:
                if not isinstance(sub, str) or not sub:
                    continue
                escaped_sub = _re.escape(sub)
                base += rf"|^https?://{escaped_sub}\.{escaped_domain}(:\d+)?$"
    except Exception:
        pass
    return base


app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=_build_cors_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -- Proposals Router (signal proposals/opportunities CRUD) --
app.include_router(proposals_router)

# -- Deliberation Router (DAG-based deliberation) --
app.include_router(deliberation_router)

# -- Onboarding Router (9-step dependency-gated wizard) --
app.include_router(onboarding_router)
app.include_router(profile_sources_router)

# -- Keyring Router (localhost-only; password-gated secret CRUD) --
app.include_router(reviews_router)
# -- Redline Router (comment on an HTML document, per version; migration 145).
# The serve sub-route is exempt from the global bearer (see
# _AUTH_EXEMPT_REGEXES) and validates its own per-document token instead.
app.include_router(redline_router)
app.include_router(keyring_router)

# -- Cortex Router (codebase semantic + literal search, stats, reindex) --
app.include_router(cortex_router)

# -- Design-engine Router (the v2 engine: kits, resolve, sheet, growth).
#
# THE v1 ROUTER USED TO BE MOUNTED ABOVE THIS, and its import line was the
# single most dangerous thing about retiring that package. `from
# okuro.design_systems.api import router` sat at indent 0 in THIS module — the
# one that mounts ~40 routers including prism, slides, media, preview, cortex
# and the SPA catch-all. No try/except. Deleting the package without deleting
# that line does not degrade a page: the orchestrator process does not start.
#
# It is gone because the v2 engine is now the only design layer the app reads.
# /ds-tokens.css went with it; it was never linked from index.html.
app.include_router(design_engine_router)
# /engine.css — the emitted sheet the SPA links. OUTSIDE /api/* for the reason
# stated at _AUTH_EXEMPT_PATHS: a stylesheet fetch carries no bearer, and
# gating one behind this middleware broke the whole SPA twice. Same shape as
# v1's /ds-tokens.css, and a separate router rather than an exemption prefix,
# because an exemption widens the unauthenticated surface for every future
# /api/design-engine route while a prefix-less router widens nothing.
app.include_router(design_engine_public_router)

# -- Embed Router (embedding tier config + hardware detection + tier switch) --
app.include_router(embed_router)

# -- Voice STT tier Router (dictation tier catalogue + user selection) --
app.include_router(voice_tier_router)

# -- Voice TTS Router (delivery voices: podcast/summary/brief + previews) --
app.include_router(voice_tts_router)

# -- Services Router (localhost-only mutations; start/stop/restart/install) --
app.include_router(services_router)

# -- People Router (communication partners — CRUD + match + lens) --
app.include_router(people_router)
# -- Media Router (podcast-on-a-topic for a person, optional recurring) --
app.include_router(media_router)
app.include_router(slides_router)
app.include_router(prism_router)
app.include_router(sparring_router)
# -- Handover Router (content-interchange: selection → any target tool) --
app.include_router(handover_router)
# -- Target-groups Router (JSON group list for the handover recipient picker) --
app.include_router(target_groups_router)
app.include_router(resonance_router)
app.include_router(studio_router)
app.include_router(brand_assets_router)
# -- CRM Router (companies + hats + connections + engagements, mig 056) --
app.include_router(crm_router)
# Public questionnaire routes at /q/{token} — deliberately NOT under /api/*
# so the bearer middleware lets them through. Registered before mount_spa
# so the SPA catch-all doesn't eat them.
app.include_router(questionnaires_router)

# -- Reminders Router (CRUD + snooze/dismiss/ack + suggestions) --
app.include_router(reminders_router)

# -- Lessons Router (mined-lesson review queue + approve/reject) --
app.include_router(lessons_router)

# -- Integrations Router (ingress channel toggle + chat_id approval) --
app.include_router(integrations_router)

# -- Bridge Router (provider status + routing table + try-it-out invoke) --
app.include_router(bridge_router)

# -- Models Router — registered LAST (see near web_router) so its greedy
#    /api/models/{model_id:path} detail route stops shadowing web_router's
#    literal siblings (/search, /edition, /gpu, /pull/status, /discoveries).

# -- Digest Router (home-page daily digest: thoughts + action items + forgotten) --
app.include_router(digest_router)

# -- Knowledge-graph Router (/api/knowledge/graph — unified memory+thoughts+artifacts+progress+kg) --
app.include_router(knowledge_graph_router)

# -- Managed-repos Router (/api/repos — clone remote repos + code-graph ingest, mig 081) --
app.include_router(repos_router)

# -- Managed-corpora Router (/api/corpora — non-git document sources → cortex, mig 129) --
app.include_router(corpora_router)

# -- Todos Router (dedicated actionable-items registry; Now-page feeder) --
app.include_router(todos_router)

# -- Solve Router (POST /api/todos/{id}/solve — mint session or kick orchestrator) --
app.include_router(todos_solve_router)
# -- Flow Feedback (F3) — post-flow rating + autopilot-decision learning join --
app.include_router(feedback_router)
app.include_router(chat_router)

# -- Signals Router (proactive proposals → todos pipeline; wave 4b) --
app.include_router(signals_router)

# -- Inbox Router (unified inbox overlay; read-only ranked list + reduce trigger) --
app.include_router(inbox_router)

# -- Sessions Router (POST /api/sessions/{id}/reissue-bearer — wave 5) --
# Registered BEFORE the /api/sessions sub-app mount (further down) so the
# explicit reissue-bearer route matches first. The sub-app's per-session
# bearer middleware never sees this path — auth is the global API bearer
# enforced by BearerAuthMiddleware (which has a hard-coded exception for
# the reissue-bearer suffix).
app.include_router(sessions_router)

# -- Roles Router (list/detail/CRUD + knowledge + maintenance surface) --
# Registered BEFORE web_router because both expose /api/roles — the roles
# router is the source of truth now (extended with stale + knowledge_count).
app.include_router(roles_router)

# -- Preview Router (launch the visible result of an orchestration task) --
app.include_router(preview_router)

# -- Web utility router (system, design, doctor, bridge, models discover/search/gpu, ...) --
# SPA static mount is added at the end of this file.
app.include_router(web_router)

# -- Models Router (AI model registry: local GGUFs + subscription models + the
#    /api/models/{id} detail route). Registered AFTER web_router on purpose: its
#    catch-all /{model_id:path} would otherwise shadow web_router's literal
#    /api/models/* GET routes (search, edition, gpu, pull/status, discoveries).
app.include_router(models_api_router)


# -- Auth Token Endpoint (localhost-only) --

def _require_loopback(request: Request):
    """Raise 403 if request is not from the loopback interface.

    Strict: no RFC1918, no Tailscale CGNAT, no same-host reverse proxy.
    Named routes that want LAN exposure must call `_allow_configured_lan`
    instead (off by default, opt-in via `OKURO_LAN_ALLOW=1`).
    """
    import ipaddress
    client = request.client
    if not client:
        raise HTTPException(403, "This endpoint is loopback-only")
    try:
        addr = ipaddress.ip_address(client.host)
    except ValueError:
        if client.host == "localhost":
            return
        raise HTTPException(403, "This endpoint is loopback-only")
    if addr.is_loopback:
        return
    raise HTTPException(403, "This endpoint is loopback-only")


# audit(NF-4): `_require_loopback` only checks the SOURCE IP of the TCP
# connection. On a multi-user box, user Eve (not Alice, who runs okuro) can
# connect to 127.0.0.1:13333, look like loopback, and hit /api/auth/token —
# leaking Alice's bearer to any local UID. Defense-in-depth: verify the peer
# socket's owning process belongs to the SAME UID as the orchestrator.
#
# On Linux we parse /proc/net/tcp + /proc/net/tcp6 directly — those files
# expose the socket-owner UID in column 8 for EVERY socket, regardless of
# ownership, and are readable by unprivileged users. We cannot use psutil
# alone because, as a non-root user on Linux, psutil masks out the PID of
# other users' sockets, so we never learn the peer UID.
#
# On macOS/other Unix we fall back to psutil.net_connections(). On error or
# when the peer's socket can't be located (TIME_WAIT between lookup and
# request handling, non-Linux without psutil) we fail OPEN unless
# OKURO_PEER_UID_STRICT=1 — the loopback check remains the primary defense
# and the attack still requires same-box local execution.
def _peer_uid_from_proc(peer_host: str, peer_port: int, our_port: int) -> int | None:
    """Linux: find the peer-side socket in /proc/net/tcp[6] and return its
    owning UID, or None if no match. Non-root readable — the UID column is
    always populated for ESTABLISHED sockets."""
    import ipaddress

    try:
        peer_addr = ipaddress.ip_address(peer_host)
    except ValueError:
        return None

    # /proc/net/tcp encodes addr+port as hex (little-endian for IPv4 dotted-quad).
    # Target row: local_address = peer_host:peer_port, rem_address = loopback:our_port
    # We scan both tcp and tcp6 because the peer may connect via IPv4 or IPv6.
    def _hex_v4(ip: str, port: int) -> str:
        octets = [int(x) for x in ip.split(".")]
        # reversed octets (little-endian) then ":PORT" big-endian hex
        return "{:02X}{:02X}{:02X}{:02X}:{:04X}".format(
            octets[3], octets[2], octets[1], octets[0], port
        )

    targets = []
    our_loopbacks = ["127.0.0.1"]
    if peer_addr.version == 4:
        peer_hex = _hex_v4(peer_host, peer_port)
        rem_hexes = [_hex_v4(lb, our_port) for lb in our_loopbacks]
        targets.append(("/proc/net/tcp", peer_hex, rem_hexes))

    # IPv6 could be added — skipped for v1 since okuro binds 127.0.0.1 only.

    for path, peer_hex, rem_hexes in targets:
        try:
            with open(path, "r") as fh:
                next(fh, None)  # skip header
                for line in fh:
                    parts = line.split()
                    if len(parts) < 8:
                        continue
                    local = parts[1]
                    remote = parts[2]
                    state = parts[3]
                    if state != "01":  # 01 = TCP_ESTABLISHED
                        continue
                    if local != peer_hex:
                        continue
                    if remote not in rem_hexes:
                        continue
                    try:
                        return int(parts[7])
                    except ValueError:
                        return None
        except (FileNotFoundError, PermissionError, OSError):
            return None
    return None


def _peer_uid_matches_us(request: Request) -> bool:
    """Return True if the TCP peer's owning process shares our UID.

    Linux:        /proc/net/tcp direct parse (works as non-root).
    macOS root:   psutil.net_connections() works.
    macOS non-root: net_connections() raises AccessDenied — psutil docs say
                  this call requires root on Darwin. We can't enumerate the
                  peer's UID at all. Treat the peer as same-UID by default
                  (loopback only, single-user laptop reality). Set
                  OKURO_PEER_UID_STRICT=1 to opt back into deny-on-failure.

    audit fix #2 (revised after macOS install regression on 2026-04-27):
    the original Sprint-1 fix made strict the default everywhere. That broke
    every macOS install where okuro runs as the user (which is every install).
    Symptom: /api/auth/token always 403, frontend gets "Failed to fetch auth
    token", every wizard step is unusable. The race attack the strict default
    was guarding against requires (a) a different-UID co-tenant on the same
    machine AND (b) Linux. So strict default is right ONLY where the lookup
    actually works. Where it doesn't, default-strict is a denial-of-service.
    """
    peer = request.client
    if peer is None:
        return True

    # Windows: no /proc, and psutil.Process.uids() is POSIX-only, so peer-UID
    # matching can't run. On a single-user Windows box reaching us over
    # loopback (already enforced upstream) the peer is us — trust it. Also
    # avoids os.getuid()/os.geteuid(), which don't exist on Windows.
    if not sys.platform.startswith(("linux", "darwin")):
        return True

    my_uid = os.getuid()
    # Trusted reverse-proxy UIDs. A same-box proxy (e.g. Caddy running as the
    # `caddy` user) terminates the LAN connection and re-connects to us over
    # loopback, so its socket is owned by the proxy's UID, not ours — the
    # bare UID==my_uid check would 403 every proxied request ("Failed to fetch
    # auth token" on okuro.<lan-domain>). OKURO_PROXY_UID is a comma-separated
    # allowlist of UIDs we accept as trusted front-ends. Trusting a proxy means
    # trusting whoever can reach it (the LAN) — opt-in, off by default.
    allowed_uids = {my_uid}
    _proxy_env = os.environ.get("OKURO_PROXY_UID", "").strip()
    if _proxy_env:
        for _u in _proxy_env.split(","):
            _u = _u.strip()
            if _u.isdigit():
                allowed_uids.add(int(_u))
    peer_host, peer_port = peer.host, peer.port
    # The port this request actually arrived on — NOT the orchestrator port.
    # _peer_uid_from_proc finds the peer's socket by matching its remote
    # address against ours, so guessing the wrong port means the row can never
    # match: the lookup returns None, Linux defaults to strict, and we 403.
    #
    # We do not serve only the orchestrator port. `okuro init` binds the
    # transient wizard port (13335, or 13336-13399 when busy), so on Linux
    # EVERY onboarding request was denied — "Failed to fetch auth token" on
    # each step, Next greyed out, wizard unusable on a fresh install. macOS
    # hid it: non-root there is permissive by default, so the same failed
    # lookup fell through to allow.
    #
    # scope["server"] is set by uvicorn from the real listening socket. Unlike
    # request.url.port it is not derived from the Host header, so a client
    # cannot steer the lookup by spoofing it. getattr keeps this tolerant of
    # request-shaped stubs that carry no scope — falling back to the old
    # assumption is correct there, since a stub has no real socket anyway.
    _scope = getattr(request, "scope", None) or {}
    _server = _scope.get("server")
    our_port = _server[1] if _server and len(_server) > 1 and _server[1] else None
    if our_port is None:
        try:
            our_port = _orchestrator_port()
        except ValueError:
            from okuro.system.port_registry import ORCHESTRATOR_PORT_DEFAULT
            our_port = ORCHESTRATOR_PORT_DEFAULT

    # ──────────────────────────────────────────────────────────────────
    # Strict-mode resolution. The default differs by platform because the
    # underlying lookup mechanism differs in reliability:
    #
    #   - Linux:        /proc/net/tcp works as non-root → strict by default.
    #   - macOS root:   psutil.net_connections() works → strict.
    #   - macOS non-root: net_connections() raises AccessDenied → permissive
    #                   by default (the alternative is to deny every request).
    #
    # OKURO_PEER_UID_STRICT explicitly overrides the per-platform default
    # to "1" (always strict) or "0" (always permissive).
    # ──────────────────────────────────────────────────────────────────
    explicit = os.environ.get("OKURO_PEER_UID_STRICT")
    if explicit is not None:
        strict = explicit != "0"
    else:
        strict = sys.platform.startswith("linux") or os.geteuid() == 0

    # Linux path: /proc/net/tcp
    if sys.platform.startswith("linux"):
        peer_uid = _peer_uid_from_proc(peer_host, peer_port, our_port)
        if peer_uid is not None:
            if peer_uid in allowed_uids:
                return True
            logger.warning(
                "Peer UID mismatch on %s: peer_uid=%s, allowed=%s — denying",
                request.url.path, peer_uid, sorted(allowed_uids),
            )
            return False
        # Lookup failed (socket not in table, race with close)
        logger.debug("peer UID: /proc/net/tcp had no match for %s:%s", peer_host, peer_port)
        return not strict

    # Non-Linux fallback via psutil
    try:
        import psutil
    except ImportError:
        logger.debug("psutil unavailable on %s — skipping peer UID check", sys.platform)
        return not strict

    try:
        for conn in psutil.net_connections(kind="inet"):
            if not conn.laddr or not conn.raddr:
                continue
            if conn.laddr.port != peer_port:
                continue
            if conn.raddr.port != our_port:
                continue
            if conn.raddr.ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                continue
            if conn.pid is None:
                continue
            try:
                p = psutil.Process(conn.pid)
                peer_uid = p.uids().real
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if peer_uid in allowed_uids:
                return True
            logger.warning(
                "Peer UID mismatch on %s: peer_uid=%s, allowed=%s (pid=%s) — denying",
                request.url.path, peer_uid, sorted(allowed_uids), conn.pid,
            )
            return False
    except (psutil.AccessDenied, PermissionError) as e:
        logger.debug("psutil access denied for peer UID lookup: %s", e)
        return not strict
    except Exception as e:
        logger.debug("peer UID lookup failed: %s", e)
        return not strict

    logger.debug("peer UID: no matching socket found for %s:%s", peer_host, peer_port)
    return not strict


def _allow_configured_lan(request: Request):
    """Loopback, plus RFC1918 / Tailscale CGNAT *only* when OKURO_LAN_ALLOW=1.

    Not wired into any route in this commit — added as the separate opt-in
    path for future LAN-enabled workflows. Callers that use this instead of
    `_require_loopback` are accepting LAN exposure deliberately.
    """
    import ipaddress
    client = request.client
    if not client:
        raise HTTPException(403, "This endpoint is loopback-only")
    try:
        addr = ipaddress.ip_address(client.host)
    except ValueError:
        if client.host == "localhost":
            return
        raise HTTPException(403, "This endpoint is loopback-only")
    if addr.is_loopback:
        return
    if os.environ.get("OKURO_LAN_ALLOW") == "1":
        _TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
        if addr.is_private or addr in _TAILSCALE_CGNAT:
            return
    raise HTTPException(403, "This endpoint is loopback-only")


@app.get("/api/auth/token")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_auth_token(request: Request):
    """Return the API bearer token. Restricted to localhost clients.

    audit(NF-4): additional peer-UID check — the requester's process must
    belong to the same UID as the orchestrator. Blocks cross-user PrivEsc
    on shared boxes (user Eve cannot grab Alice's bearer just by reaching
    127.0.0.1:13333).
    """
    _require_loopback(request)
    if not _peer_uid_matches_us(request):
        raise HTTPException(403, "Peer UID does not match orchestrator UID")
    return {"token": _API_TOKEN}


@app.post("/api/auth/token/rotate")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def rotate_auth_token(request: Request):
    """Generate a new API token, invalidating the old one. Localhost-only."""
    global _API_TOKEN
    _require_loopback(request)
    if not _peer_uid_matches_us(request):
        raise HTTPException(403, "Peer UID does not match orchestrator UID")

    # Require current token to authorize rotation
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != _API_TOKEN:
        raise HTTPException(401, "Current bearer token required to rotate")

    new_token = _secrets.token_urlsafe(32)

    # Persist to okuro.keyring if available
    try:
        from okuro.keyring.storage import KeyringStorage
        KeyringStorage().set_key("okuro/api_token", new_token)
    except Exception as e:
        logger.warning("Could not persist rotated token to keyring: %s", e)

    old_prefix = _API_TOKEN[:8]
    _API_TOKEN = new_token
    logger.info("API token rotated (old prefix: %s...)", old_prefix)
    return {"token": _API_TOKEN}


# -- Health & Status --

@app.get("/api/health")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def health():
    """Health check — under /api so the SPA can own /health as a route.

    Public (see the auth-exemption note above), so this stays counts-only: how
    many agents are attached and how many turns are streaming — never who they
    are or what they said.

    These two exist so a restart can be gated on evidence instead of hope.
    Everything else here survives or self-heals: engines run in their own
    systemd scopes, uvicorn waits out in-flight HTTP, browsers re-establish SSE.
    What doesn't:
      * mcp_sessions — inline MCP at /mcp/v1 runs with no event store, so a
        restart 404s attached agents and each must re-initialize;
      * chat_turns_in_flight — chat CLIs are plain subprocess children (no
        systemd-run --scope), so a streaming reply dies mid-sentence. Idle
        sessions are excluded: they respawn transparently.
    """
    from okuro.mcp.inline_http import live_session_count
    from okuro.orchestrator.api.chat_session import turns_in_flight

    return {
        "status": "healthy",
        # null = the SDK's internals moved; callers must treat it as unknown,
        # never as zero.
        "mcp_sessions": live_session_count(),
        "chat_turns_in_flight": turns_in_flight(),
    }


@app.get("/api/status", response_model=SystemStatus)
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_status():
    tasks = list_all_tasks(TASKS_DIR)
    active = sum(1 for t in tasks if t.status in ("active", "planning"))
    tools_count = 0
    if TOOLS_REGISTRY.exists():
        with open(TOOLS_REGISTRY) as f:
            reg = yload(f)
        for cat in reg.get("tools", {}).values():
            tools_count += len(cat)

    uptime = (datetime.utcnow() - app_state["start_time"]).total_seconds()
    return SystemStatus(
        okuro_root=str(OKURO_ROOT),
        tasks_count=len(tasks),
        active_tasks=active,
        tools_count=tools_count,
        uptime_seconds=uptime,
    )


# -- Task Request Models --

# ROCK-SOLID v5 P5.4 — the delivery channels Stream C can actually render.
#
# The banned list is not stylistic. `Task.delivery_channel` has carried the
# note "PPTX/DOCX/XLSX are banned (no MS formats)" since Stream C landed, and
# with no writer there was nothing to enforce it. Adding the writer without
# the check would make the first thing a caller can do the thing the field
# explicitly forbids.
DELIVERY_CHANNELS = ("markdown", "marp", "microsite", "tts", "podcast")


def _normalize_delivery_channel(raw: str | None) -> str:
    """Lower-cased channel, or "" for unset/unknown.

    Unknown falls back to "" rather than raising: `create_task` resolves an
    empty channel to "markdown" whenever a recipient is set, so a typo
    degrades to the default delivery instead of failing task creation
    outright. The value is logged by the CLI it is forwarded to.
    """
    value = (raw or "").strip().lower()
    return value if value in DELIVERY_CHANNELS else ""


class TaskCreateRequest(BaseModel):
    description: str
    # WP4a smart gate: default is None (caller left mode unspecified) so the
    # intake gate can distinguish "not chosen — okuro may auto-route" from an
    # explicit caller choice ("auto-execute" or "deliberate"), which is always
    # honored. Resolved to a concrete mode before spawn.
    mode: str | None = None  # None | "auto-execute" | "deliberate"
    dry_run: bool = False
    auto_approve: bool = False
    preferred_cli: str | None = None
    intelligence: str | None = None  # "" or "max"
    # "" (inherit the global default) | "per_artifact" | "closeout".
    # Per-task so a closeout experiment is scoped to ONE run.
    review_trigger: str | None = None
    required_roles: list[str] | None = None  # user-specified roles the decomposer MUST use
    recurring: bool = False
    recurring_title: str | None = None
    recurring_role: str | None = None
    recurring_schedule: str | None = None  # cron expression
    # If the task was spawned from a registered todo (clicked from the
    # Now-page Todos column), the todo id flows through here so the engine
    # can mark the todo done when this task reaches a terminal state.
    source_todo_id: str | None = None
    # Same idea for an action-item rendered inside a parent thought
    # (Now-page Action Items column). On task done the engine calls
    # update_thought(id, status="resolved").
    source_thought_id: str | None = None
    # P5.4 — Stream C. `audience` is a person_id from the people store; the
    # other two only mean anything alongside it.
    # P6.7 — fast-track profile, per-task opt-in.
    fast_track: bool = False
    audience: str | None = None
    delivery_channel: str | None = None
    delivery_brand_id: str | None = None
    # Audit finding #23 — batched HIGH-risk approvals. Lets the caller
    # short-circuit per-subtask wait_for_approval prompts up to a chosen
    # risk ceiling. "none" (default) preserves the current behavior.
    auto_approve_risk: str = "none"  # "none" | "medium" | "high"
    # Sprint 3C — intake gate. By default, the API runs assess_task_clarity()
    # before spawning the orchestrator and refuses vague tasks with an
    # `intake_required` response. Set true to bypass when the user is sure.
    skip_intake: bool = False
    # Autonomous (agent/MCP) caller — there is no human in the loop to answer
    # intake questions, so the BLOCKING gate is skipped (same rationale as
    # recurring tasks). The clarity score is still assessed in the background
    # and logged (intake_assessment) for observability; it never blocks.
    # UI callers leave this False to keep the interactive intake_required flow.
    autonomous: bool = False
    # Optional project slug context for the clarity assessor.
    project: str | None = None
    # Build/test gate. When set, runs in project_path after all subtasks
    # finalize, before flipping the task to "done". Non-zero exit fails
    # the task. Closes the "subagents claimed success but does the code
    # actually work?" gap left by the structural-only reconciler.
    verify_command: str | None = None
    # Task-level default for the deliberation-on-continuation toggle. None =
    # inherit the global config default; True/False sets the default for
    # every continuation on this task (a per-continuation `deliberate` still
    # overrides). Only effective for deliberate-mode tasks.
    deliberate_continuations: bool | None = None
    # F2 — task-level autopilot override. None = inherit the global
    # orchestrator.autopilot config default; True/False forces autopilot on/off
    # for this task. When on, every human gate (including high-risk approvals +
    # capability-gap) is auto-answered from the profile — the confidence/abstain
    # fallback is the only safeguard.
    autopilot: bool | None = None


class TaskContinueRequest(BaseModel):
    description: str
    dry_run: bool = False
    auto_approve: bool = False
    preferred_cli: str | None = None
    intelligence: str | None = None
    # Per-continuation override for the deliberation-on-continuation toggle.
    # None → inherit task-level default → global config default. True/False
    # forces a fresh council / single-shot decompose for this continuation.
    deliberate: bool | None = None


class TaskRenameRequest(BaseModel):
    description: str


# -- Task Endpoints --

def _can_use_systemd_run() -> bool:
    """True only when the host can actually run ``systemd-run --user --scope``.

    systemd-run is the cgroup-escape trick on Linux — it puts the engine in
    its own transient unit so the API's KillMode=control-group doesn't reap
    it on a service restart. macOS, WSL1, Alpine without systemd, Devuan,
    and any non-systemd box has no systemd-run binary; trying anyway raises
    FileNotFoundError and the spawn fails (caught on macOS Tahoe install
    report 2026-04-28). On those hosts we fall back to plain Popen with
    start_new_session=True — the child becomes a session leader and
    reparents to PID 1 (launchd / init) on parent exit, giving the same
    survive-API-restart guarantee.
    """
    if sys.platform != "linux":
        return False
    return shutil.which("systemd-run") is not None


def spawn_orchestrator(cmd: list[str], task_id: str):
    """Spawn the okuro orchestrator engine so it survives API restarts.

    Two supervision strategies, picked by host:

    * **systemd Linux** — ``systemd-run --user --scope`` puts the engine in
      its own transient cgroup so KillMode=control-group on the API unit
      can't reach it.
    * **Everywhere else** (macOS, non-systemd Linux) — plain
      ``Popen(start_new_session=True)``. The child becomes a session
      leader; when the API exits, the child reparents to PID 1 (launchd
      on macOS, init on systemd-less Linux) and keeps running.

    Both paths share the same launcher.sh that handles cwd, env, and
    stdout/stderr redirection to ``startup.log``.
    """
    try:
        # Pre-create task directory for the startup log
        task_path = TASKS_DIR / task_id
        task_path.mkdir(parents=True, exist_ok=True)
        startup_log = task_path / "startup.log"
        launcher_script = task_path / ".launcher.sh"

        # Clear the engine-exit sentinel before launching — a new spawn must
        # not be treated as a cleanly-exited engine by the liveness check.
        # Idempotent; companion to state.mark_engine_exited.
        try:
            (task_path / ".exited").unlink(missing_ok=True)
        except Exception:
            pass

        # Write spawn marker
        with open(startup_log, "a") as f:
            f.write(f"\n--- {datetime.utcnow().isoformat()} - SPAWNED ---\n")

        # Strip CLAUDECODE env var to avoid nested-session detection.
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        # Refresh PATH at SPAWN time, not at install time. The plist's
        # frozen PATH is fine for the orchestrator API itself, but each
        # per-task engine should see CLIs the user installed since the
        # plist was rendered. _user_path_dirs() probes nvm-newest, fnm,
        # asdf, Homebrew, ~/.local/bin, etc. dynamically.
        from okuro.system.service_manager import _user_path_dirs
        existing_path = env.get("PATH", "")
        seen: set[str] = set()
        merged: list[str] = []
        for d in (existing_path.split(":") if existing_path else []) + _user_path_dirs():
            if d and d not in seen:
                seen.add(d)
                merged.append(d)
        env["PATH"] = ":".join(merged)

        # Write launcher script to avoid shell quoting issues.
        #
        # Theme E + H — the launcher wraps the engine in:
        #
        #   * an EXIT/INT/TERM trap that sends SIGTERM to every nested
        #     child process (Theme H — http.server scopes spawned inside
        #     the launcher's process group get reaped on engine death
        #     instead of orphaning into the scope) — fires before the
        #     respawn loop's `exit` so a clean engine exit doesn't reap
        #     a still-pending engine respawn;
        #   * a respawn-on-clean-exit loop (Theme E — engine writes
        #     ``.exited`` sentinel on terminal task states (done /
        #     failed / cancelled / verify_failed / panel_return /
        #     capability_gap_return); absence means "source-change
        #     restart requested", respawn against fresh modules). The
        #     loop hard-caps at 5 respawns/min to prevent runaway crash
        #     loops if the engine fails-fast on every start.
        import shlex
        task_dir_q = shlex.quote(str(task_path))
        startup_log_q = shlex.quote(str(startup_log))
        engine_cmd_q = shlex.join(cmd)
        okuro_root_q = shlex.quote(str(OKURO_ROOT))
        with open(launcher_script, "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f"cd {okuro_root_q}\n")
            # Theme H — propagate signals to nested children on engine
            # exit. ``pkill -P $$`` SIGTERMs every process whose parent
            # is this launcher (the engine's nested http.server scopes,
            # subagent CLIs not already in their own cgroup). We do NOT
            # trap EXIT — the EXIT trap on bash fires for the script's
            # own clean exit and would kill respawns mid-flight. We trap
            # INT and TERM only, so external signals propagate but
            # ``exit 0`` returns cleanly.
            f.write("trap 'pkill -P $$ 2>/dev/null; exit 130' INT\n")
            f.write("trap 'pkill -P $$ 2>/dev/null; exit 143' TERM\n")
            # Theme E — respawn loop. The `.exited` sentinel under the
            # task directory is the contract:
            #   present → engine exited intentionally; do NOT respawn.
            #   absent  → engine exited for restart (e.g. source-change
            #             watcher) or crashed; respawn.
            f.write("RESPAWN_COUNT=0\n")
            f.write("RESPAWN_WINDOW_START=$(date +%s)\n")
            f.write("while true; do\n")
            f.write(f"  {engine_cmd_q} >>{startup_log_q} 2>&1\n")
            f.write("  rc=$?\n")
            f.write(f"  if [ -f {task_dir_q}/.exited ]; then\n")
            f.write("    exit 0\n")
            f.write("  fi\n")
            # Rate-limit respawns: max 5 per 60 seconds.
            f.write("  NOW=$(date +%s)\n")
            f.write("  if [ $((NOW - RESPAWN_WINDOW_START)) -ge 60 ]; then\n")
            f.write("    RESPAWN_COUNT=0\n")
            f.write("    RESPAWN_WINDOW_START=$NOW\n")
            f.write("  fi\n")
            f.write("  RESPAWN_COUNT=$((RESPAWN_COUNT + 1))\n")
            f.write("  if [ $RESPAWN_COUNT -gt 5 ]; then\n")
            f.write(f"    echo \"[LAUNCHER] respawn rate-limited (>5/min). Last exit $rc — aborting.\" >>{startup_log_q}\n")
            f.write("    exit $rc\n")
            f.write("  fi\n")
            f.write(f"  echo \"[LAUNCHER] engine exited rc=$rc, no .exited sentinel — respawning (#$RESPAWN_COUNT)\" >>{startup_log_q}\n")
            f.write("  sleep 1\n")
            f.write("done\n")
        launcher_script.chmod(0o755)

        from datetime import datetime as _dt
        scope_ts = _dt.now().strftime("%H%M%S")
        scope_name = f"okuro-{task_id}-{scope_ts}"
        use_systemd = _can_use_systemd_run()
        if use_systemd:
            spawn_cmd = [
                "systemd-run", "--user", "--scope",
                f"--unit={scope_name}",
                "--", str(launcher_script),
            ]
            supervisor = f"systemd scope '{scope_name}'"
        else:
            spawn_cmd = [str(launcher_script)]
            supervisor = "Popen+setsid"

        proc = subprocess.Popen(
            spawn_cmd,
            cwd=str(OKURO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
        )
        # Wait briefly to catch immediate failures. systemd-run exits ~0s
        # after bootstrapping the scope (we observe its exit code); the
        # plain launcher.sh exec's into a long-running engine, so
        # TimeoutExpired here is the success signal. When the launcher's
        # exec fails fast (e.g. CLI binary missing, config.yaml unreadable)
        # the engine writes its traceback to startup.log via the launcher's
        # 2>&1 redirect — the API's stderr pipe is empty, which is why we
        # point operators there instead of repeating "unknown error".
        try:
            _, stderr = proc.communicate(timeout=5)
            if proc.returncode and proc.returncode != 0:
                # A non-zero exit within the window is NOT necessarily a spawn
                # failure. The scope/launcher may have started fine and the
                # engine then parked fast BY DESIGN: a deliberate-mode task
                # proposes a panel + persists an awaiting state then exits
                # non-zero; a --resume with no ready work (e.g. everything
                # already terminal, or task parked at an awaiting gate)
                # likewise exits fast. _task_created_ok confirms valid task
                # state was persisted — that is a successful run, not a
                # failed spawn. Without this the systemd branch logged
                # systemd-run's benign "Running as unit: …" stderr as an
                # error and returned None → the caller 500'd on a healthy park.
                if _task_created_ok(task_id):
                    logger.info(
                        f"Orchestrator for {task_id} parked fast (rc={proc.returncode}, "
                        f"task state persisted) via {supervisor} — treated as success, "
                        "not a spawn failure."
                    )
                    return proc.pid
                err_msg = stderr.decode().strip() if stderr else ""
                if use_systemd:
                    detail = err_msg or "unknown error"
                    logger.error(
                        f"orchestrator spawn failed for {task_id} via {supervisor}: {detail}"
                    )
                else:
                    # Popen path: returncode is the engine's exit code. The
                    # actual failure is in startup.log, not on our pipes.
                    log_hint = task_path / "startup.log"
                    detail = (
                        f"engine exited with code {proc.returncode} immediately "
                        f"after launch — check {log_hint} for the underlying "
                        "error (e.g. CLI binary missing, config.yaml unreadable, "
                        "import error)."
                    )
                    if err_msg:
                        detail += f" stderr: {err_msg}"
                    logger.error(
                        f"orchestrator engine failed fast for {task_id}: {detail}"
                    )
                return None
        except subprocess.TimeoutExpired:
            pass  # Process is running — expected for long-lived orchestrator
        logger.info(
            f"Spawned orchestrator via {supervisor} (PID {proc.pid}) for task {task_id}"
        )
        return proc.pid
    except Exception as e:
        logger.error(f"Failed to spawn orchestrator for {task_id}: {e}")
        return None


def _task_created_ok(task_id: str) -> bool:
    """True if the engine persisted a valid task state, even when the launcher
    process exited fast. Deliberate-mode tasks propose a role panel, persist an
    ``awaiting`` state, then park (fast non-zero exit) — that is a successful
    creation, not a spawn failure. A real fast-crash writes no task.yaml/status,
    so this cleanly distinguishes the two.
    """
    try:
        p = TASKS_DIR / task_id / "task.yaml"
        if not p.exists():
            return False
        data = yload(p.read_text()) or {}
        return bool(data.get("status"))
    except Exception:
        return False


_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB per file

def _secure_filename(filename: str) -> str:
    """Sanitize a filename: strip path separators and dangerous sequences."""
    # Remove directory components
    filename = filename.replace("\\", "/")
    filename = filename.split("/")[-1]
    # Remove leading dots (hidden files), strip dangerous chars
    filename = _re.sub(r"[^\w.\-]", "_", filename).lstrip(".")
    return filename or "upload"


class TaskPreviewRequest(BaseModel):
    description: str
    required_roles: list[str] | None = None


@app.post("/api/tasks/preview")
async def preview_task(body: TaskPreviewRequest):
    """
    Decompose a task without spawning the orchestrator.

    Returns the same `phases[]` shape used by /api/tasks/{id}/state, so the
    frontend's PipelineView renders it unchanged. Does not write any files.
    Subtask statuses are left at their dataclass default ("pending") — nothing
    has executed.
    """
    if not _rate_limiter.check("task_preview", max_calls=3, window_seconds=30):
        raise HTTPException(429, "Rate limited — max 3 previews per 30 seconds")

    description = (body.description or "").strip()
    if not description:
        raise HTTPException(400, "description is required")

    try:
        from okuro.orchestrator.config import load_config
        from okuro.orchestrator.decomposer import decompose_task
        from dataclasses import asdict

        config = load_config(OKURO_ROOT / "config.yaml")
        phases = await asyncio.to_thread(
            decompose_task, description, config, body.required_roles or None,
        )
    except Exception as e:
        logger.exception("Preview decompose failed")
        raise HTTPException(500, f"Decompose failed: {e}")

    phases_data = [
        {
            "id": phase.id,
            "name": phase.name,
            "status": phase.status,
            "subtasks": [asdict(st) for st in phase.subtasks],
        }
        for phase in phases
    ]
    return {
        "phases": phases_data,
        "required_roles": body.required_roles or [],
        "description": description,
    }


def _spawn_background_clarity_assessment(
    task_id: str, description: str, project_slug: str | None
) -> None:
    """Assess task clarity OFF the request path and log the score.

    Autonomous (agent/MCP) callers skip the blocking intake gate, but the
    clarity signal is still useful for observability. Runs in a daemon thread
    so the create response — and the MCP tool's 30s call budget — never waits
    on the clarity LLM. Best-effort: any failure is logged and swallowed.
    """
    import threading

    def _run():
        try:
            from okuro.orchestrator.clarity import assess_task_clarity
            project_context = {"project": project_slug} if project_slug else None
            clarity = assess_task_clarity(description, project_context)
            task_dir = TASKS_DIR / task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            with open(task_dir / "log.jsonl", "a") as f:
                f.write(json.dumps({
                    "type": "intake_assessment",
                    "autonomous": True,
                    "confidence": clarity.get("confidence"),
                    "missing": clarity.get("missing"),
                    "ts": datetime.utcnow().isoformat(),
                }) + "\n")
        except Exception as exc:
            logger.warning(
                "[intake_assessment] background assess failed for %s: %r",
                task_id, exc,
            )

    threading.Thread(
        target=_run, name=f"intake-assess-{task_id}", daemon=True,
    ).start()


@app.post("/api/tasks", status_code=202)
async def create_task(request: Request):
    """
    Create and execute a new task.
    Accepts application/json (original) or multipart/form-data (with file uploads).
    """
    # Rate limit: max 1 task creation per 30 seconds. Peek only — the slot is
    # consumed via _rate_limiter.record() on SUCCESS, so a failed spawn or a
    # clarity-gate intake round does not throttle the user's next attempt.
    if not _rate_limiter.peek("task_create", max_calls=1, window_seconds=30):
        raise HTTPException(429, "Rate limited — max 1 task creation per 30 seconds")

    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        description = form.get("description", "")
        if not description:
            raise HTTPException(400, "description is required")
        # WP4a: preserve "unspecified" (None) vs explicit so the smart gate
        # can auto-route only when the caller left mode at its default. An
        # explicit-but-invalid value falls back to auto-execute (explicit).
        _raw_mode = form.get("mode")
        if _raw_mode is None or _raw_mode == "":
            mode = None
        elif _raw_mode in ("auto-execute", "deliberate"):
            mode = _raw_mode
        else:
            mode = "auto-execute"
        dry_run = form.get("dry_run", "false").lower() in ("true", "1", "yes")
        auto_approve = form.get("auto_approve", "false").lower() in ("true", "1", "yes")
        preferred_cli = form.get("preferred_cli") or None
        intelligence = form.get("intelligence") or None
        review_trigger = form.get("review_trigger") or None
        _roles_raw = form.get("required_roles") or None
        required_roles = [r.strip() for r in _roles_raw.split(",")] if _roles_raw else None
        source_todo_id = form.get("source_todo_id") or None
        source_thought_id = form.get("source_thought_id") or None
        auto_approve_risk = (form.get("auto_approve_risk") or "none").lower()
        if auto_approve_risk not in ("none", "medium", "high"):
            auto_approve_risk = "none"
        skip_intake = form.get("skip_intake", "false").lower() in ("true", "1", "yes")
        autonomous = form.get("autonomous", "false").lower() in ("true", "1", "yes")
        project_slug = form.get("project") or None
        verify_command = (form.get("verify_command") or "").strip()
        fast_track = (form.get("fast_track") or "").lower() in ("true", "1", "yes")
        audience = (form.get("audience") or "").strip()
        delivery_channel = _normalize_delivery_channel(form.get("delivery_channel"))
        delivery_brand_id = (form.get("delivery_brand_id") or "").strip()
        _dc = form.get("deliberate_continuations")
        deliberate_continuations = (
            None if _dc in (None, "")
            else str(_dc).lower() in ("true", "1", "yes")
        )
        _ap = form.get("autopilot")
        autopilot = (
            None if _ap in (None, "")
            else str(_ap).lower() in ("true", "1", "yes")
        )

        # Generate task ID and save uploaded files
        task_id = f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        inputs_dir = TASKS_DIR / task_id / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)

        saved_files = []
        for key in form:
            item = form[key]
            if hasattr(item, "filename") and item.filename:
                safe_name = _secure_filename(item.filename)
                content = await item.read()
                if len(content) > _MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"File '{safe_name}' exceeds 10MB limit")
                dest = inputs_dir / safe_name
                dest.write_bytes(content)
                saved_files.append(str(dest))
                logger.info(f"Saved upload: {dest}")

        # Append file context to description
        if saved_files:
            file_list = "\n".join(f"  - {p}" for p in saved_files)
            description = f"{description}\n\nAttached files:\n{file_list}"
    else:
        body = TaskCreateRequest(**(await request.json()))
        description = body.description
        # WP4a: None = unspecified (smart gate may auto-route). An explicit
        # value is honored; an explicit-but-invalid value falls back to
        # auto-execute (still treated as an explicit choice).
        if body.mode is None:
            mode = None
        elif body.mode in ("auto-execute", "deliberate"):
            mode = body.mode
        else:
            mode = "auto-execute"
        dry_run = body.dry_run
        auto_approve = body.auto_approve
        preferred_cli = body.preferred_cli
        intelligence = body.intelligence
        review_trigger = body.review_trigger
        required_roles = body.required_roles
        source_todo_id = body.source_todo_id
        source_thought_id = body.source_thought_id
        auto_approve_risk = (body.auto_approve_risk or "none").lower()
        if auto_approve_risk not in ("none", "medium", "high"):
            auto_approve_risk = "none"
        skip_intake = body.skip_intake
        autonomous = body.autonomous
        project_slug = body.project
        verify_command = (body.verify_command or "").strip()
        fast_track = bool(body.fast_track)
        audience = (body.audience or "").strip()
        delivery_channel = _normalize_delivery_channel(body.delivery_channel)
        delivery_brand_id = (body.delivery_brand_id or "").strip()
        deliberate_continuations = body.deliberate_continuations
        autopilot = body.autopilot
        task_id = f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        saved_files = []

        # Handle recurring task creation
        if body.recurring:
            return await _create_recurring_def(body)

    # -- Sprint 3C: intake gate --
    # Bridge a fast LLM check before spawning the orchestrator. If the task
    # description is too vague to plan well, return targeted questions so the
    # user can resubmit with richer context — instead of silently producing a
    # low-quality plan and burning a strategic-tier LLM call.
    #
    # FAIL-OPEN: any clarity-assessor error returns confidence=1.0, so an LLM
    # outage never blocks a real task. Recurring tasks bypass the gate (the
    # cron-template runs daily — no human in the loop to answer questions).
    #
    # AUTONOMOUS (agent/MCP) callers bypass the BLOCKING gate for the same
    # reason recurring tasks do — there is no human to answer intake questions,
    # so blocking would dead-end the task. Two problems this closes:
    #   1. agent-created tasks could never get past a low-confidence score;
    #   2. the synchronous clarity LLM exceeded the MCP tool's 30s call timeout.
    # The score is still assessed in the background and logged for
    # observability (intake_assessment event) — it just never blocks dispatch.
    if autonomous and not skip_intake:
        _spawn_background_clarity_assessment(task_id, description, project_slug)
    if not skip_intake and not autonomous:
        try:
            from okuro.orchestrator.clarity import (
                CONFIDENCE_THRESHOLD,
                DELIBERATE_CONFIDENCE_CEILING,
                assess_task_clarity,
            )
            project_context = {"project": project_slug} if project_slug else None
            clarity = await asyncio.to_thread(
                assess_task_clarity, description, project_context,
            )
            confidence = clarity["confidence"]
            if confidence < CONFIDENCE_THRESHOLD:
                logger.info(
                    "[intake_gate] blocking task — confidence=%.2f, missing=%s",
                    confidence, clarity["missing"],
                )
                return {
                    "status": "intake_required",
                    "confidence": confidence,
                    "missing": clarity["missing"],
                    "questions": clarity["questions"],
                    "hint": (
                        "Resubmit with skip_intake=true to force, or refine "
                        "the description to address the questions above."
                    ),
                }
            # WP4a smart gate — CLARITY band.
            # [CONFIDENCE_THRESHOLD, CEILING): clear enough to proceed but
            # ambiguous enough to deserve options. Auto-route into deliberation
            # (research → options → user decides) ONLY when the caller left mode
            # unspecified (mode is None). An explicit caller choice is never
            # overridden.
            if (
                mode is None
                and CONFIDENCE_THRESHOLD <= confidence < DELIBERATE_CONFIDENCE_CEILING
            ):
                mode = "deliberate"
                logger.info(
                    "[smart_gate] auto-routing to deliberation — "
                    "confidence=%.2f band=[%.2f,%.2f)",
                    confidence, CONFIDENCE_THRESHOLD, DELIBERATE_CONFIDENCE_CEILING,
                )
                # Observability: make the auto-route visible in the global
                # activity/pulse stream so the smart gate is never "magic".
                broadcast_agent_event(
                    "smart_gate_deliberation",
                    {
                        "task_id": task_id,
                        "confidence": round(confidence, 4),
                        "band": [CONFIDENCE_THRESHOLD, DELIBERATE_CONFIDENCE_CEILING],
                        "reason": (
                            "clarity in deliberation band — auto-routed to "
                            "research→options→user-decide"
                        ),
                    },
                )
        except Exception as exc:
            # Defensive: never let the gate itself crash task creation
            logger.warning("[intake_gate] assessor crashed (%s) — proceeding", exc)

    # WP4a: resolve any still-unspecified mode to the default auto-execute.
    # This covers skip_intake=true, an assessor crash, and confidence ≥ CEILING.
    if mode is None:
        mode = "auto-execute"

    cmd = [sys.executable, "-m", "okuro.orchestrator.engine", "--task-id", task_id]

    if mode == "deliberate":
        cmd.extend(["--mode", "deliberate"])
    if auto_approve:
        cmd.append("--yes")
    if dry_run:
        cmd.append("--dry-run")
    if preferred_cli:
        cmd.extend(["--preferred-cli", preferred_cli])
    if intelligence:
        cmd.extend(["--intelligence", intelligence])
    if review_trigger:
        cmd.extend(["--review-trigger", review_trigger])
    if required_roles:
        cmd.extend(["--required-roles", ",".join(required_roles)])
    if source_todo_id:
        cmd.extend(["--source-todo-id", source_todo_id])
    if source_thought_id:
        cmd.extend(["--source-thought-id", source_thought_id])
    if auto_approve_risk and auto_approve_risk != "none":
        cmd.extend(["--auto-approve-risk", auto_approve_risk])
    if verify_command:
        cmd.extend(["--verify-command", verify_command])
    # P5.4 — the channel and brand are only forwarded WITH a recipient. A
    # channel alone configures a delivery that will never be sent, and it
    # would sit in task.yaml looking like the feature was on.
    if fast_track:
        cmd.append("--fast-track")
    if audience:
        cmd.extend(["--audience", audience])
        if delivery_channel:
            cmd.extend(["--delivery-channel", delivery_channel])
        if delivery_brand_id:
            cmd.extend(["--delivery-brand", delivery_brand_id])
    if deliberate_continuations is not None:
        cmd.extend([
            "--deliberate-continuations",
            "true" if deliberate_continuations else "false",
        ])
    if autopilot is not None:
        cmd.extend(["--autopilot", "true" if autopilot else "false"])

    cmd.append(description)

    # C4 — create_task joins the spawn-lock cohort (LF1 root). Two
    # concurrent POSTs to /api/tasks for the SAME task id used to race
    # past spawn_orchestrator unguarded → two engines decomposed the same
    # task in parallel and produced divergent plans (audit-07 §B spawns
    # #3+#4). continue_task / resume_task / retry_task already acquire
    # the same lock; create_task now joins the cohort.
    lock = _get_spawn_lock(task_id)
    async with lock:
        pid = await asyncio.to_thread(_create_task_locked, cmd, task_id)

    # Success (live engine or intentionally parked) — consume the rate slot.
    _rate_limiter.record("task_create")
    return {
        "status": "accepted",
        "task_id": task_id,
        "pid": pid,
        "description": description,
        "mode": mode,
        "dry_run": dry_run,
        "files": [Path(f).name for f in saved_files],
    }


async def _create_recurring_def(body: TaskCreateRequest):
    """Create a recurring task definition in recurring/*.yaml."""
    import re
    role = body.recurring_role or ""
    schedule = body.recurring_schedule or "0 6 * * *"  # default: daily 6am
    title = body.recurring_title or body.description[:60]

    if not role:
        raise HTTPException(400, "recurring_role is required for recurring tasks")

    # Sanitize role to filename
    def_id = re.sub(r'[^a-z0-9-]', '-', role.lower()).strip('-')
    recurring_dir = OKURO_ROOT / "recurring"
    recurring_dir.mkdir(exist_ok=True)
    def_path = recurring_dir / f"{def_id}.yaml"

    if def_path.exists():
        raise HTTPException(409, f"Recurring definition '{def_id}' already exists")

    def_data = {
        "id": def_id,
        "title": title,
        "description": body.description,
        "role": role,
        "scheduler": schedule,
        "status": "scheduled",
        "last_run_at": None,
        "next_run_at": None,
        "run_count": 0,
    }

    import yaml
    def_path.write_text(yaml.dump(def_data, default_flow_style=False))
    logger.info(f"Created recurring definition: {def_id} (schedule: {schedule})")

    return {
        "status": "created",
        "type": _HTTP_RESPONSE_RECURRING,
        "def_id": def_id,
        "role": role,
        "schedule": schedule,
        "title": title,
    }


# Bounds for the two O(N) request-path corpus walks below (/api/recent-files
# and /api/active-roles). Both are polled by the SPA, so their cost lands on
# every dashboard tick, and both were unbounded over a corpus that only grows.
_RECENT_FILES_SCAN_LIMIT = 60
_RECENT_FILES_MAX_AGE_S = 3 * 86400
# 256 KiB comfortably holds the last 50 activity lines (they are ~100-400 B)
# without reading multi-megabyte logs from byte zero.
_ACTIVITY_TAIL_BYTES = 256 * 1024

_ACTIVE_ROLES_SCAN_LIMIT = 200
# A dir with no fresh liveness marker cannot host a running engine, so the
# expensive _is_orchestrator_running (lease read, several stats, psutil cmdline
# scan) never has to run for it. The window is deliberately ~60x the largest
# TTL that check honours (15s engine lease, 15s spawn grace, ~1 Hz heartbeat),
# so this pre-filter can only ever admit MORE dirs than the real check accepts.
_ACTIVE_MARKER_FILES = (".engine.lease", ".heartbeat", ".orchestrator.pid",
                        ".launcher.sh", ".exited")
_ACTIVE_MARKER_MAX_AGE_S = 900


def _has_fresh_liveness_marker(task_dir: Path, now: float) -> bool:
    """Cheap negative filter for 'could this task's engine be alive?'."""
    for marker in _ACTIVE_MARKER_FILES:
        try:
            if now - os.stat(task_dir / marker).st_mtime < _ACTIVE_MARKER_MAX_AGE_S:
                return True
        except OSError:
            continue
    return False


@app.get("/api/recent-files")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def recent_files(limit: int = 20):
    """Return recently touched filenames from active task activity logs.

    BOUNDED. The old version sorted every task dir, then read each
    ``.activity.jsonl`` IN FULL only to keep its last 50 lines — over a corpus
    that grows by at least a directory a day. Measured at 1.19s per request
    before the tracemalloc removal, and the SPA polls it.

    Three bounds, all of which sharpen the endpoint's own meaning ("recently
    touched"): activity logs older than ``_RECENT_FILES_MAX_AGE_S`` cannot hold
    recent files; only the ``_RECENT_FILES_SCAN_LIMIT`` most recently written
    are read; and each is read from the TAIL, not from the start.
    """
    import json as _json
    files = []
    seen = set()
    cutoff = time.time() - _RECENT_FILES_MAX_AGE_S

    # One scandir with d_type, one stat on the FILE (never entry.stat(), which
    # would return the directory's 4096), and sort by real recency.
    candidates: list[tuple[float, Path]] = []
    with os.scandir(TASKS_DIR) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            activity = Path(entry.path) / ".activity.jsonl"
            try:
                mtime = os.stat(activity).st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            candidates.append((mtime, activity))
    candidates.sort(key=lambda pair: pair[0], reverse=True)

    for _mtime, activity in candidates[:_RECENT_FILES_SCAN_LIMIT]:
        try:
            with open(activity, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - _ACTIVITY_TAIL_BYTES))
                chunk = fh.read()
            lines = chunk.decode("utf-8", errors="ignore").strip().split("\n")
            # A mid-line seek makes the first line a fragment; json.loads would
            # reject it anyway, but dropping it keeps the parse loop honest.
            if size > _ACTIVITY_TAIL_BYTES and len(lines) > 1:
                lines = lines[1:]
            for line in reversed(lines[-50:]):  # last 50 events
                evt = _json.loads(line)
                if evt.get("type") == "tool_use" and evt.get("preview"):
                    name = evt.get("name", "")
                    preview = evt["preview"]
                    # File reads show filename in preview
                    if name in ("Read", "cortex_read_header", "cortex_read_section",
                                "cortex_read_file", "Edit", "Write", "Grep"):
                        if preview not in seen and len(preview) > 2:
                            seen.add(preview)
                            files.append({"file": preview, "tool": name, "ts": evt.get("ts", "")})
                            if len(files) >= limit:
                                return {"files": files}
        except Exception:
            continue
    return {"files": files}


@app.get("/api/active-roles")
def active_roles():
    """Return currently running subtask roles across all active tasks.

    Sync `def` (NOT async) on purpose: the body is pure blocking I/O —
    iterates task dirs, yaml-parses plans, and calls _is_orchestrator_running
    (file stats + a psutil scan). As an `async def` it ran entirely on the
    event loop and starved the inline /mcp/v1 mount that subagents call for
    artifact_write (→ "subagent never wrote an artifact"). Starlette runs sync
    path-ops in its threadpool, so the loop stays free for MCP + SSE under
    heavy dashboard polling.

    BUT the threadpool only moves work off the loop's STACK, not off the GIL:
    for CPU-bound pure-Python work it converts a clean block into a convoy,
    which is worse for tail latency. The real fix is doing less work, so the
    walk is bounded: newest-first (task dir names are timestamps, so a reverse
    name sort is chronological and needs no stat), capped, and gated behind a
    cheap liveness-marker check so the expensive per-task liveness probe runs
    only for dirs that could plausibly be alive. Measured at 8.81s per request
    before the tracemalloc removal, 0.20s after — and the SPA polls it.
    """
    roles = []
    now = time.time()

    names: list[str] = []
    with os.scandir(TASKS_DIR) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                names.append(entry.name)
    names.sort(reverse=True)

    for name in names[:_ACTIVE_ROLES_SCAN_LIMIT]:
        task_dir = TASKS_DIR / name
        if not _has_fresh_liveness_marker(task_dir, now):
            continue
        plan_path = task_dir / "plan.yaml"
        if not plan_path.exists():
            continue
        # Quick check: is orchestrator running?
        if not _is_orchestrator_running(task_dir.name):
            continue
        try:
            with open(plan_path) as f:
                plan = yload(f)
            for phase in plan.get("phases", []):
                for st in phase.get("subtasks", []):
                    if st.get("status") == "running":
                        roles.append({
                            "task_id": task_dir.name,
                            "subtask": st.get("id", ""),
                            "role": st.get("role", ""),
                            "description": (st.get("description", ""))[:80],
                        })
        except Exception:
            continue
    return {"active": len(roles) > 0, "roles": roles}


@app.get("/api/recurring")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def list_recurring_defs():
    """List all recurring task definitions with schedule info."""
    from okuro.orchestrator.recurring import load_recurring_defs
    defs = load_recurring_defs(OKURO_ROOT / "recurring")
    now = datetime.now()
    result = []
    for d in defs:
        next_seconds = None
        if d.next_run_at:
            try:
                next_dt = datetime.fromisoformat(d.next_run_at.replace("Z", "+00:00"))
                if next_dt.tzinfo is not None:
                    next_dt = next_dt.replace(tzinfo=None)
                next_seconds = max(0, int((next_dt - now).total_seconds()))
            except ValueError:
                pass
        result.append({
            "id": d.id,
            "title": d.title,
            "role": d.role,
            "roles": d.roles,
            "schedule": d.scheduler,
            "status": d.status,
            "last_run_at": d.last_run_at,
            "next_run_at": d.next_run_at,
            "next_run_seconds": next_seconds,
            "run_count": d.run_count,
            "adaptive": d.adaptive,
            "description_template": d.description_template,
            # Every recurring def spawns an engine — the kind is structural, not
            # declared, so it is stated here rather than stored per-def.
            "kind": "orchestrator",
            "tier": d.tier,
        })
    return {"definitions": result}


class RecurringCreate(BaseModel):
    title: str
    description: str            # the prompt the scheduled run executes
    role: str | None = None
    roles: list[str] | None = None
    schedule: str = "0 6 * * *"


@app.post("/api/recurring")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def create_recurring_def(body: RecurringCreate):
    """Create a recurring orchestrator run — role(s) + prompt on a cron.

    This is the dedicated create path for the Scheduled page (the older
    ``POST /api/tasks {recurring:true}`` branch derives the id from the role;
    this one derives it from the title so multiple runs can share a role).
    """
    import re
    import yaml
    from okuro.orchestrator.recurring import load_recurring_defs

    roles = [r for r in (body.roles or []) if r] or (
        [body.role] if body.role else []
    )
    if not roles:
        raise HTTPException(400, "at least one role is required")
    if not body.description.strip():
        raise HTTPException(400, "description (the prompt to run) is required")
    _validate_cron_5field(body.schedule)

    title = body.title.strip() or body.description[:60]
    def_id = re.sub(r"[^a-z0-9-]", "-", title.lower()).strip("-") or "scheduled-run"

    recurring_dir = OKURO_ROOT / "recurring"
    recurring_dir.mkdir(exist_ok=True)
    def_path = recurring_dir / f"{def_id}.yaml"
    if def_path.exists():
        raise HTTPException(409, f"A scheduled run named '{def_id}' already exists")

    def_data = {
        "id": def_id,
        "title": title,
        "description": body.description,
        "role": roles[0],
        "roles": roles,
        "scheduler": body.schedule,
        "status": "scheduled",
        "last_run_at": None,
        "next_run_at": None,
        "run_count": 0,
    }
    def_path.write_text(
        yaml.dump(def_data, default_flow_style=False, sort_keys=False)
    )
    logger.info(f"Created recurring def via /api/recurring: {def_id} ({body.schedule})")
    return {"status": "created", "id": def_id, "title": title, "schedule": body.schedule}


@app.delete("/api/recurring/{def_id}")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def delete_recurring_def(def_id: str):
    """Delete a recurring task definition (removes its YAML file)."""
    import re
    if not re.fullmatch(r"[a-z0-9-]+", def_id):
        raise HTTPException(400, f"invalid definition id: {def_id!r}")
    def_path = OKURO_ROOT / "recurring" / f"{def_id}.yaml"
    if not def_path.exists():
        raise HTTPException(404, f"Recurring definition '{def_id}' not found")
    def_path.unlink()
    logger.info(f"Deleted recurring def: {def_id}")
    return {"status": "deleted", "id": def_id}


@app.get("/api/recurring/{def_id}/history")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_recurring_history(def_id: str):
    """Get run outcome history for a recurring task definition."""
    from okuro.orchestrator.recurring import load_recurring_defs
    defs = load_recurring_defs(OKURO_ROOT / "recurring")
    for d in defs:
        if d.id == def_id:
            return {
                "id": d.id,
                "outcomes": [
                    {
                        "run_id": o.run_id,
                        "outcome": o.outcome,
                        "summary": o.summary,
                        "completed_at": o.completed_at,
                    }
                    for o in d.outcome_history
                ],
            }
    raise HTTPException(404, f"Recurring definition '{def_id}' not found")


class RecurringDefPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    description_template: str | None = None
    role: str | None = None
    roles: list[str] | None = None
    scheduler: str | None = None
    schedule: str | None = None   # UI-friendly alias for `scheduler` (cron expr)
    status: str | None = None
    paused: bool | None = None    # convenience boolean → maps to status scheduled|paused
    adaptive: bool | None = None
    tier: str | None = None       # standard | quality — validated below


def _validate_recurring_tier(value: str) -> None:
    """Reject an unknown tier at the API edge.

    The loader falls back to standard on a bad value so one typo can't stop the
    schedule, but that silence is wrong for an interactive edit — a user who
    asks for `quaility` should be told, not quietly given standard forever.
    """
    from okuro.orchestrator.recurring import RECURRING_TIERS
    if value not in RECURRING_TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"tier must be one of {sorted(RECURRING_TIERS)} (got {value!r})",
        )


def _validate_cron_5field(expr: str) -> None:
    """Raise HTTPException(400) if ``expr`` is not a well-formed 5-field cron.

    Uses croniter to parse; mirrors how the scheduler itself validates at
    run time, so a PATCH failure is authoritative.
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        raise HTTPException(
            400,
            f"Invalid cron expression: expected 5 fields, got {len(parts)} — '{expr}'",
        )
    try:
        from croniter import croniter as _croniter  # type: ignore

        _croniter(expr)
    except Exception as exc:
        raise HTTPException(400, f"Invalid cron expression '{expr}': {exc}")


@app.patch("/api/recurring/{def_id}")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def patch_recurring_def(def_id: str, body: RecurringDefPatch):
    """Update fields on a recurring task definition.

    Accepts UI-friendly aliases:
    - ``schedule`` → written as ``scheduler`` in yaml (+ validated as 5-field cron)
    - ``paused`` boolean → written as ``status: paused|scheduled``
    - ``status`` direct ("scheduled"|"paused") still accepted as-is.

    On a cron change, the scheduler picks it up on its next poll (mtime-based).
    """
    from okuro.orchestrator.recurring import (
        compute_next_run,
        load_recurring_defs,
        update_recurring_def,
    )

    defs = load_recurring_defs(OKURO_ROOT / "recurring")
    for d in defs:
        if d.id == def_id:
            raw = body.model_dump()
            updates: dict = {}

            # schedule alias → scheduler
            if raw.get("schedule") is not None and raw.get("scheduler") is None:
                raw["scheduler"] = raw["schedule"]
            raw.pop("schedule", None)

            # paused alias → status
            if raw.get("paused") is not None:
                if raw.get("status") is None:
                    raw["status"] = "paused" if raw["paused"] else "scheduled"
            raw.pop("paused", None)

            for k, v in raw.items():
                if v is not None:
                    updates[k] = v
            if not updates:
                raise HTTPException(400, "No fields to update")

            # Validate cron + recompute next_run_at when scheduler changes.
            if "scheduler" in updates:
                _validate_cron_5field(updates["scheduler"])
                try:
                    updates["next_run_at"] = compute_next_run(updates["scheduler"])
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"next_run_at recompute failed for {def_id}: {exc}"
                    )

            # Guard status values
            if "status" in updates and updates["status"] not in (
                "scheduled",
                "paused",
                "disabled",
            ):
                raise HTTPException(
                    400,
                    f"Invalid status '{updates['status']}' — "
                    "use scheduled|paused|disabled",
                )

            # Guard tier — this one moves money, so a typo must not pass.
            if "tier" in updates:
                updates["tier"] = str(updates["tier"]).strip().lower()
                _validate_recurring_tier(updates["tier"])

            ok = update_recurring_def(d, updates)
            if not ok:
                raise HTTPException(500, "Failed to update definition")
            return {"status": "updated", "id": def_id, "fields": list(updates.keys())}
    raise HTTPException(404, f"Recurring definition '{def_id}' not found")


@app.post("/api/tasks/{task_id}/continue", status_code=202)
async def continue_task(task_id: str, request: Request):
    """Continue an existing task with new instructions.

    Accepts application/json (original) or multipart/form-data (with file
    uploads). Uploaded files land under ``tasks/{id}/inputs/`` — the same
    place create_task writes them — and an "Attached files:" block is appended
    to the continuation prompt so the continued subagent can read them
    (artifacts-to-disk convention)."""
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    # Parse multipart-or-JSON into a uniform ContinueTaskRequest, mirroring
    # create_task. Files are saved BEFORE the spawn lock so a slow upload does
    # not hold the lock.
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        _desc = (form.get("description") or "").strip()
        if not _desc:
            raise HTTPException(400, "description is required")
        inputs_dir = task_dir / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        saved_files: list[str] = []
        for key in form:
            item = form[key]
            if hasattr(item, "filename") and item.filename:
                safe_name = _secure_filename(item.filename)
                content = await item.read()
                if len(content) > _MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"File '{safe_name}' exceeds 10MB limit")
                dest = inputs_dir / safe_name
                dest.write_bytes(content)
                saved_files.append(str(dest))
                logger.info(f"Saved continuation upload: {dest}")
        if saved_files:
            file_list = "\n".join(f"  - {p}" for p in saved_files)
            _desc = f"{_desc}\n\nAttached files:\n{file_list}"
        request = TaskContinueRequest(
            description=_desc,
            dry_run=(form.get("dry_run", "false").lower() in ("true", "1", "yes")),
            auto_approve=(form.get("auto_approve", "false").lower() in ("true", "1", "yes")),
            preferred_cli=(form.get("preferred_cli") or None),
            intelligence=(form.get("intelligence") or None),
            deliberate=(
                None if form.get("deliberate") in (None, "")
                else str(form.get("deliberate")).lower() in ("true", "1", "yes")
            ),
        )
    else:
        request = TaskContinueRequest(**(await request.json()))

    lock = _get_spawn_lock(task_id)
    async with lock:
        return await asyncio.to_thread(
            _continue_task_locked, task_id, task_dir, request
        )


def _continue_task_locked(
    task_id: str, task_dir: Path, request: "TaskContinueRequest"
) -> dict:
    """Synchronous body of POST /continue. Runs in a worker thread.

    See :func:`_get_spawn_lock` for why this is split out. The multipart/JSON
    parse stays in the coroutine — it genuinely awaits, and it deliberately
    happens BEFORE the lock so a slow upload does not hold it.
    """
    task_yaml = task_dir / "task.yaml"
    try:
        with open(task_yaml) as f:
            task_data = yload(f)
    except Exception as e:
        logger.warning(f"Failed to read task.yaml for continue of {task_id}: {e}")
        raise HTTPException(500, f"Failed to read task state: {e}")

    is_running = (
        task_data.get("status") in ("active", "planning")
        and _is_orchestrator_running(task_id)
    )

    # Persist the user's continuation prompt BEFORE spawning the
    # orchestrator subprocess. If the subprocess never starts, or the
    # decomposer crashes (E2BIG, etc.), the prompt still survives so
    # the UI can render it as a node in the flow chart. before_phase_id
    # defaults to "next phase", so this is a QUEUED, not-yet-decomposed
    # continuation — exactly what a live engine drains. Detect when the
    # description matches a registered continuation_suggestion — the user
    # clicked-to-accept rather than typing, so source="suggestion_accepted"
    # hides it from the default chart view (where only "MY prompts" show).
    try:
        from okuro.orchestrator.state import record_intervention

        source = "user_typed"
        try:
            suggestions = task_data.get("continuation_suggestions") or []
            if isinstance(suggestions, list):
                for sugg in suggestions:
                    if (
                        isinstance(sugg, dict)
                        and sugg.get("suggestion_text") == request.description
                    ):
                        source = "suggestion_accepted"
                        break
        except Exception:
            pass

        record_intervention(
            task_id,
            request.description,
            TASKS_DIR,
            kind="continuation",
            source=source,
            deliberate=request.deliberate,
        )
    except Exception as exc:
        logger.warning(f"Failed to record continuation intervention for {task_id}: {exc}")

    # P-CONT-1 — task still running → QUEUE the continuation instead of
    # 409ing. The continuation is recorded above as a pending (not-yet-
    # decomposed) intervention; the live engine's work loop drains it at
    # its next idle checkpoint (engine._drain_pending_continuations, before
    # the completion check), so a second --continue engine would only race
    # the running one. Re-check liveness so a continuation does not strand
    # if the engine exited while we were recording — then fall through to
    # spawn a --continue engine as before.
    if is_running and _is_orchestrator_running(task_id):
        return {
            "status": "queued",
            "task_id": task_id,
            "pid": None,
            "description": request.description,
            "dry_run": request.dry_run,
        }

    cmd = [sys.executable, "-m", "okuro.orchestrator.engine", "--continue", task_id]

    if request.auto_approve:
        cmd.append("--yes")
    if request.dry_run:
        cmd.append("--dry-run")
    if request.preferred_cli:
        cmd.extend(["--preferred-cli", request.preferred_cli])
    if request.intelligence:
        cmd.extend(["--intelligence", request.intelligence])

    cmd.append(request.description)

    pid = spawn_orchestrator(cmd, task_id)
    if not pid:
        raise HTTPException(500, "Failed to spawn task orchestrator")

    return {
        "status": "accepted",
        "task_id": task_id,
        "pid": pid,
        "description": request.description,
        "dry_run": request.dry_run,
    }


def _resume_task_locked(task_id: str, task_dir: Path) -> int:
    """Synchronous body of POST /resume. Runs in a worker thread.

    See :func:`_get_spawn_lock` for why this is split out.
    """
    task_yaml = task_dir / "task.yaml"
    try:
        with open(task_yaml) as f:
            task_data = yload(f)
        current_status = (task_data.get("status") or "").lower()

        # Terminal statuses never resume — caller must spawn a new
        # task instead.
        if current_status in ("done", "cancelled"):
            raise HTTPException(
                409,
                f"Task {task_id} is {current_status}; resume is not "
                "applicable to terminal tasks.",
            )

        # Any status + a live engine → refuse. The live engine
        # owns the task; a second engine would race over plan.yaml
        # writes. Covers the prior 'active/planning + alive' guard
        # plus the rare 'blocked + alive' case (engine flipped
        # status but hasn't exited yet).
        if _is_orchestrator_running(task_id):
            raise HTTPException(
                409,
                f"Task {task_id} is currently {current_status or 'unknown'} "
                "with a running orchestrator. Wait for it to exit or "
                "cancel the task first.",
            )

        # All other statuses (halted, blocked, failed, active,
        # planning, pending) are accepted — the caller is
        # responsible for having resolved any blockers (FE flow:
        # /override-blocked-review or /retry before /resume for
        # blocked, or just /resume directly for halted/failed).
        task_data["status"] = "active"
        # The status flip MUST clear any awaiting block. get_ready_subtasks
        # short-circuits to [] whenever task.awaiting is a halt-kind (via
        # deliberation._task_halt_reason), so leaving a stale awaiting here
        # makes the respawned engine re-park on no_subtasks_ready instead of
        # dispatching — resume becomes a silent no-op. Mirrors the
        # canonical writer invariant (status != waiting_user ⇒ awaiting None).
        task_data.pop("awaiting", None)
        from okuro.orchestrator.state import atomic_dump_yaml
        atomic_dump_yaml(task_yaml, task_data, default_flow_style=False)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Failed to read/update task.yaml for resume of {task_id}: {e}")
        raise HTTPException(500, f"Failed to read task state: {e}")

    cmd = [sys.executable, "-m", "okuro.orchestrator.engine", "--resume", task_id]

    pid = spawn_orchestrator(cmd, task_id)
    if not pid:
        raise HTTPException(500, "Failed to spawn task orchestrator")
    return pid


@app.post("/api/tasks/{task_id}/resume", status_code=202)
async def resume_task(task_id: str):
    """Resume execution of an existing task.

    Theme G — accept ``current_status ∈ {halted, blocked, failed,
    active, planning}`` as long as no orchestrator process is alive
    for the task. Pre-fix the endpoint only short-circuited the
    ``active/planning + alive`` case explicitly; an orphan ``active``
    status with a dead engine + the user's intent to resume worked,
    BUT the FE flow for ``blocked`` (post override-blocked-review)
    required a manual ``task.yaml status='halted'`` flip first because
    operators had been trained that 'blocked' meant "needs reset
    first". Making the contract explicit removes that workaround.

    Rejection criteria:
      - status='done' or 'cancelled' → 409 (terminal, use a new task).
      - any status + engine alive → 409 (let the live engine finish).
      - missing task → 404.
    """
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    lock = _get_spawn_lock(task_id)
    async with lock:
        pid = await asyncio.to_thread(_resume_task_locked, task_id, task_dir)

    return {
        "status": "accepted",
        "task_id": task_id,
        "pid": pid,
    }


def _retry_task_locked(task_id: str, task_dir: Path) -> tuple[int, int]:
    """Synchronous body of POST /retry. Runs in a worker thread.

    See :func:`_get_spawn_lock` for why this is split out. Returns
    ``(pid, reset_count)``; raises HTTPException, which ``asyncio.to_thread``
    propagates to the caller unchanged.
    """

    task_yaml = task_dir / "task.yaml"
    plan_path = task_dir / "plan.yaml"
    log_path = task_dir / "log.jsonl"

    with open(task_yaml) as f:
        task_data = yload(f)

    # P4.7 — liveness-check EVERY status, not just active/planning. A task
    # sitting in waiting_user / blocked / halted can still have a live engine
    # (a park is engineless, but a wedged or mid-shutdown one is not), and
    # retrying underneath it is how two engines end up on one task.
    if _is_orchestrator_running(task_id):
        raise HTTPException(409, f"Task {task_id} is currently running. Cannot retry.")

    # P4.7 — retry on a PARKED task is about clearing the gate, not resetting
    # failures. Recorded before the plan is touched because the reset branch
    # below 400s when it finds nothing failed, which is the normal shape of a
    # task parked on a decision.
    _was_parked = bool(task_data.get("awaiting")) or (
        task_data.get("status") == "waiting_user"
    )

    # Reset failed subtasks in plan.yaml
    reset_count = 0
    if plan_path.exists():
        with open(plan_path) as f:
            plan = yload(f)

        for phase in plan.get("phases", []):
            phase_had_failures = False
            for st in phase.get("subtasks", []):
                if st["status"] == "failed":
                    st["status"] = "pending"
                    st["retries"] = 0
                    st["error"] = ""
                    # Clear the review lock — get_ready_subtasks skips any
                    # subtask with review_state == "failed", so resetting
                    # status alone would leave it pending-but-undispatchable
                    # and this endpoint would report N subtasks reset while
                    # the engine made no progress. Same reasoning as
                    # _reset_phase_for_retry; see also the class note in
                    # tests/orchestrator/correctness/test_review_lock_release.py.
                    st["review_state"] = "not_reviewed"
                    reset_count += 1
                    phase_had_failures = True
            if phase_had_failures and phase.get("status") == "done":
                phase["status"] = "pending"

        if reset_count == 0 and not _was_parked:
            raise HTTPException(400, "No failed subtasks to retry")

        from okuro.orchestrator.state import atomic_dump_yaml
        atomic_dump_yaml(plan_path, plan, default_flow_style=False)

        event = {
            "type": "task_retry",
            "subtasks_reset": reset_count,
            "ts": datetime.utcnow().isoformat(),
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(event) + "\n")

    # P4.7 — through the canonical writer, NOT a raw yaml dump.
    #
    # The raw dump set status="active" and left `awaiting` untouched, so a
    # parked task came back as active-with-a-gate. The engine's
    # `_is_human_park` sees `awaiting` and parks again on its first tick:
    # the endpoint returned 202, the user saw nothing change, and the retry
    # was a no-op. save_task_meta enforces the C8 coupling (status !=
    # waiting_user ⇒ awaiting = None) and `_clear_awaiting_resumable` writes
    # the `.resume.touch` marker FIRST, which is what stops the >5min
    # gate-answer force-halt race from eating the respawn.
    #
    # This is also one of P4.10's six raw writers, closed here rather than
    # left for a sweep that would have to re-derive why it mattered.
    from okuro.orchestrator.state import load_task as _load_task_state
    from okuro.orchestrator.state import save_task_meta as _save_task_meta

    _task_obj = _load_task_state(task_id, task_dir.parent)
    if getattr(_task_obj, "awaiting", None) is not None:
        _clear_awaiting_resumable(_task_obj, task_dir.parent, new_status="active")
    else:
        _task_obj.status = "active"
        _save_task_meta(_task_obj, task_dir.parent)

    cmd = [sys.executable, "-m", "okuro.orchestrator.engine", "--resume", task_id]

    pid = spawn_orchestrator(cmd, task_id)
    if not pid:
        raise HTTPException(500, "Failed to spawn task orchestrator")
    return pid, reset_count


@app.post("/api/tasks/{task_id}/retry", status_code=202)
async def retry_task(task_id: str):
    """Retry a failed/blocked task by resetting failed subtasks to pending."""
    _validate_task_id(task_id)

    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    lock = _get_spawn_lock(task_id)
    async with lock:
        pid, reset_count = await asyncio.to_thread(
            _retry_task_locked, task_id, task_dir
        )

    logger.info(f"Task {task_id} retried: {reset_count} subtasks reset")
    return {
        "status": "accepted",
        "task_id": task_id,
        "pid": pid,
        "subtasks_reset": reset_count,
    }


# Mirror of okuro.daemon._handlers._RECONCILE_MAX_ATTEMPTS — the daemon
# engine-reconciler respawns a dead-engine orphan up to this many times per
# progress signature before giving up.
_DAEMON_RECONCILE_MAX_ATTEMPTS = 3
# If the daemon never writes a backoff file (disabled/crashed), the GET-path
# reconciler must still terminalize an orphan rather than leave it spinning
# `active` invisibly — but only after a grace window long enough for ≥1
# daemon cycle (it runs every 2 min).
_DAEMON_RECONCILE_GRACE_S = 300

# How long a human's gate resolution shields the task from the GET-path
# reconciler. Covers the window between "awaiting cleared" and "the fresh
# engine has written its own life signals" — cold start is ~15-30 s, so 180 s
# is wide margin while still bounded: a spawn that genuinely fails is
# terminalized 3 minutes later, not left `active` invisibly forever.
_HUMAN_RESUME_GRACE_S = 180
# Written by _mark_human_resume, read ONLY by _daemon_respawn_exhausted.
# Deliberately NOT .heartbeat / .launcher.sh: those mtimes feed
# _is_orchestrator_running, so touching them would make the API believe a
# dead engine is alive — a far worse lie than the bug being fixed.
_HUMAN_RESUME_MARKER = ".resume.touch"


def _mark_human_resume(task_dir: Path) -> None:
    """Record that a human just resolved a gate and a respawn is imminent.

    THE RACE THIS CLOSES (found live 2026-07-30, task-20260730-233206).
    A `blocked_review` park exits the engine deliberately — park is
    engineless. From that moment `.heartbeat`, `.orchestrator.pid` and
    `.launcher.sh` stop being refreshed, so after _DAEMON_RECONCILE_GRACE_S
    the GET-path reconciler considers the task a dead orphan. It does not act,
    because `awaiting` is set — that flag is the task's ONLY protection while
    parked.

    Resolving the gate removes exactly that protection, before the new engine
    exists. Any UI poll landing in the gap force-halts the task; the fresh
    engine then loads `halted`, `get_ready_subtasks` returns [], and the user
    is handed a second gate saying no steps can run. Measured: parked 23:01:26,
    answered 23:14:18 (772 s later), two `task_halted_reconciled` in the same
    second, second gate at 23:14:18.

    The perverse property: the LONGER a human takes to answer, the more
    certain it is that answering halts the task. Under 5 minutes it works;
    over 5 minutes it does not.

    Widening _DAEMON_RECONCILE_GRACE_S is NOT the fix — a human can sit on a
    gate for hours, so any fixed window loses. The invariant is that a
    liveness signal must exist BEFORE the awaiting is cleared, which is why
    this is always called through _clear_awaiting_resumable.

    Best-effort: a task that cannot be marked is no worse off than before.
    """
    try:
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / _HUMAN_RESUME_MARKER).write_text(str(time.time()))
    except OSError as exc:
        logger.warning("human-resume marker not written for %s: %r", task_dir, exc)


def _clear_awaiting_resumable(task, tasks_dir: Path, *, new_status: str = "active") -> None:
    """clear_awaiting, with the liveness mark written FIRST.

    The order is the fix, so it is enforced here rather than trusted to six
    call sites (approve_subtask, approve_subtasks_batch, resolve_gate,
    override_blocked_review, override_blocked_review_task,
    resolve_subtask_timeout_cap). Every path that hands a parked task back to
    the engine goes through this function — a new gate kind added later
    inherits the fix instead of re-discovering the bug.
    """
    from okuro.orchestrator.state import clear_awaiting

    _mark_human_resume(Path(tasks_dir) / task.id)
    clear_awaiting(task, tasks_dir, new_status=new_status)


def _daemon_respawn_exhausted(task_id: str, base_dir: Path, task_data: dict) -> bool:
    """Has the daemon engine-reconciler spent its respawn budget for this orphan?

    Single-controller liveness. The daemon engine-reconciler
    (``okuro.daemon._handlers.reconcile_orphaned_engines``, cron ``*/2m``)
    OWNS respawn of transient orphans (pending/active/planning with a dead
    engine). The GET-path reconciler force-halting such an orphan on a casual
    page load previously RACED that healer and won — flipping the task to
    terminal ``halted``, which the daemon then refuses to resume
    (``halted`` ∉ ``_RECONCILE_RESUMABLE``), wedging the task forever
    (continuation-halt bug, task-20260618-175644).

    So the GET path DEFERS terminalization to the daemon and only halts when
    the daemon has provably finished trying:
      * ``attempts >= MAX`` for the CURRENT progress signature (it respawned
        repeatedly with no new completed subtask and gave up), OR
      * no backoff file AND the engine has been dead past the grace window
        (daemon disabled/down — don't strand the task ``active`` invisibly).

    Best-effort: a read error => not exhausted => keep deferring, EXCEPT the
    no-file + stale/absent-heartbeat fallback so a dead daemon can't strand
    the task.
    """
    import json as _json
    import time as _time

    task_dir = base_dir / task_id

    # A human resolved a gate moments ago and a respawn is in flight. Checked
    # BEFORE the backoff branch, not folded into the mtime set below, because
    # an Override that resets nothing leaves the progress signature unchanged
    # — so a stale exhausted .reconcile.json would still return True and halt
    # the very task the user just unblocked. See _mark_human_resume.
    try:
        resumed_age = _time.time() - (task_dir / _HUMAN_RESUME_MARKER).stat().st_mtime
        if resumed_age <= _HUMAN_RESUME_GRACE_S:
            return False
    except OSError:
        pass

    backoff = task_dir / ".reconcile.json"
    if not backoff.is_file():
        # Daemon hasn't engaged. Use the FRESHEST sign of spawn/life, NOT
        # heartbeat-absence. A cold-starting engine (heavy in-process embed-model
        # init, ~15-30s) has written its launcher + pid but not yet a heartbeat;
        # halting on "no heartbeat" force-kills a healthy engine mid-init
        # (continuation cold-start wedge, found LIVE 2026-06-19). Defer until
        # the freshest of {heartbeat, pid, launcher} mtime is older than the
        # grace window; only then is it really dead with no daemon to heal it.
        newest = 0.0
        for fn in (".heartbeat", ".orchestrator.pid", ".launcher.sh"):
            try:
                newest = max(newest, (task_dir / fn).stat().st_mtime)
            except OSError:
                pass
        if newest == 0.0:
            return True  # no signal of life at all -> don't defer forever
        return (_time.time() - newest) > _DAEMON_RECONCILE_GRACE_S

    try:
        state = _json.loads(backoff.read_text()) or {}
    except (OSError, ValueError):
        return False
    if int(state.get("attempts", 0) or 0) < _DAEMON_RECONCILE_MAX_ATTEMPTS:
        return False
    # Exhaustion counts only against the signature it was recorded for. If the
    # task advanced since, the daemon resets the counter and keeps trying, so
    # keep deferring. Signature contract: f"{status}:{done_count}"
    # (okuro.daemon._handlers._reconcile_progress_sig).
    status = str(task_data.get("status") or "").lower()
    done = 0
    plan = task_dir / "plan.yaml"
    try:
        pdata = yload(plan.read_text()) or {}
        for phase in pdata.get("phases") or []:
            for st in phase.get("subtasks") or []:
                if str(st.get("status") or "").lower() == "done":
                    done += 1
    except Exception:
        return False
    return state.get("sig") == f"{status}:{done}"


def _reconcile_task_state(task_id: str, tasks_dir: Path | None = None) -> bool:
    """If task.yaml says active/planning but no orchestrator is alive, mark halted.

    Heals state divergence after abnormal exits the engine couldn't catch
    (SIGKILL, OOM, parent process gone). Three layers reconciled together:
      1. task.yaml.status:    active|planning → halted
      2. plan.yaml subtasks:  running|waiting_approval → halted
      3. OkuroThinker:        broadcasts idle so the status bar disappears

    Returns True when any layer was healed so callers refresh their cache.
    Idempotent — only writes when in a transient state with no live PID.

    Gate 2 §C1 — mutations route through the canonical writers
    (``set_task_status`` + ``update_subtask``) so the reconciler joins the
    same lock cohort + closed-schema discipline as every other writer.
    Pre-fix this function rewrote ``task.yaml`` and ``plan.yaml`` directly,
    bypassing ``update_subtask`` / ``update_node`` (no mirror to graph,
    no ``subtask_failed`` event) and racing the engine's task.yaml writes
    (D2 root).
    """
    from okuro.orchestrator.state import (
        load_task as _load_task,
        set_task_status as _set_task_status,
        update_subtask as _update_subtask,
    )

    base_dir = Path(tasks_dir) if tasks_dir is not None else TASKS_DIR
    task_yaml = base_dir / task_id / "task.yaml"
    if not task_yaml.exists():
        return False
    try:
        with open(task_yaml) as f:
            data = yload(f) or {}

        # Decouple the two layer checks. task.yaml may already be halted
        # (e.g. healed by a prior pass) while plan.yaml subtasks are still
        # stuck — happens when the previous reconciler only touched the
        # task-level file. Reconcile plan.yaml whenever orchestrator is
        # gone, independent of the task-level status.
        task_transient = data.get("status") in ("active", "planning")
        # A task parked in a clean escalation — waiting_user, or an `awaiting`
        # surface set (e.g. a blocked_review review-cap awaiting your override) —
        # LEGITIMATELY has no live engine: the engine sets the awaiting surface
        # then exits. That is not a crash. Healing it here force-halts the task
        # and relabels its parked subtask "failed — task halted before subtask
        # completed", which clobbers the real CAP/blocked_review reason and reads
        # as a failure. Leave escalated tasks alone — they're correctly parked.
        if data.get("status") == "waiting_user" or data.get("awaiting"):
            return False
        # C4 — canonical liveness probe (fresh heartbeat trumps one-shot PID).
        orch_alive = _is_orchestrator_running(task_id, tasks_dir=base_dir)
        if orch_alive:
            return False
        # Either layer needs healing → orchestrator is gone.
        healed_anything = False

        # Single-controller liveness: a transient orphan (active/planning with
        # a dead engine) belongs to the daemon engine-reconciler, which
        # respawns it every 2 min. Force-halting it + failing its subtasks here
        # on a casual GET races and DEFEATS that healer — the halt is terminal
        # and the daemon refuses to resume `halted` (continuation-halt bug,
        # task-20260618-175644). Defer until the daemon has exhausted its
        # respawn budget (or is provably down past the grace window).
        if task_transient and not _daemon_respawn_exhausted(task_id, base_dir, data):
            return False

        if task_transient:
            # C1 — route through set_task_status so the closed-schema
            # writer takes the shared lock + serializes the full
            # in-memory Task (no silent-key-preservation), and the
            # status_changed event fires through one canonical path.
            try:
                task_obj = _load_task(task_id, base_dir)
                _set_task_status(task_obj, base_dir, force="halted")
                healed_anything = True
            except Exception as exc:
                logger.warning(
                    f"task.yaml reconcile via set_task_status failed for {task_id}: {exc}"
                )

        # Heal plan.yaml subtasks — without this the timeline shows the last
        # subtask still spinning even though the parent task is halted.
        # Subtask schema (validation.VALID_SUBTASK_STATUSES) doesn't allow
        # "halted" so we land on "failed" with an explanatory error. Phase
        # schema only allows pending|active|done — no terminal state fits,
        # so leave phase status alone (the per-subtask render is what users
        # see ticking).
        # C1 — every per-subtask reset goes through update_subtask so the
        # plan.yaml mutation is locked, the graph-node mirror runs, and
        # subtask_failed events emit. Pre-fix this loop wrote plan.yaml
        # directly and emitted no events.
        plan_yaml = base_dir / task_id / "plan.yaml"
        reset_count = 0
        if plan_yaml.exists():
            try:
                with open(plan_yaml) as f:
                    plan_data = yload(f) or {}
                stuck_subtask_ids: list[str] = []
                for phase in plan_data.get("phases") or []:
                    for st in phase.get("subtasks") or []:
                        # Match transient states AND `halted` (a previous
                        # reconciler version wrote that schema-invalid value;
                        # self-heal those entries on the next pass).
                        if st.get("status") in ("running", "waiting_approval", "halted"):
                            sid = st.get("id")
                            if sid:
                                stuck_subtask_ids.append(str(sid))
                for sid in stuck_subtask_ids:
                    try:
                        _update_subtask(
                            task_id, sid,
                            {
                                "status": "failed",
                                "error": "task halted before subtask completed",
                            },
                            base_dir,
                        )
                        reset_count += 1
                    except Exception as exc:
                        logger.warning(
                            f"plan.yaml reconcile of subtask {sid} failed for {task_id}: {exc}"
                        )
                if reset_count:
                    healed_anything = True
            except Exception as e:
                logger.warning(f"plan.yaml reconcile failed for {task_id}: {e}")

        if not healed_anything:
            # Nothing to heal in task.yaml or plan.yaml, but the orchestrator
            # is gone. Check if log.jsonl's latest orchestrator_state event
            # is stale ("running" / "thinking" / etc.) — if so, append an
            # idle event so server-restart hydration + WS snapshot replay
            # serve the correct terminal state. Without this, task-scoped
            # OkuroThinker widgets keep showing the last running label
            # forever for terminal tasks.
            log_path = base_dir / task_id / "log.jsonl"
            try:
                if log_path.exists():
                    latest_state = None
                    with open(log_path, "rb") as f:
                        f.seek(0, 2)
                        size = f.tell()
                        f.seek(max(0, size - 16384))
                        for line in reversed(f.read().decode(errors="ignore").splitlines()):
                            if not line or "orchestrator_state" not in line:
                                continue
                            try:
                                ev = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if ev.get("type") == "orchestrator_state":
                                latest_state = ev.get("state")
                                break
                    if latest_state and latest_state != "idle":
                        ts_now = datetime.utcnow().isoformat()
                        with open(log_path, "a") as f:
                            f.write(json.dumps({
                                "type": "orchestrator_state",
                                "state": "idle",
                                "label": "",
                                "ts": ts_now,
                            }) + "\n")
                        try:
                            broadcast_orchestrator_state("idle", "", task_id)
                        except Exception:
                            pass
                        logger.info(
                            f"Task {task_id}: orchestrator_state idle event appended "
                            f"(was '{latest_state}')"
                        )
                        return True
            except Exception as e:
                logger.debug(f"orchestrator_state stale-check failed for {task_id}: {e}")
            return False

        log_path = base_dir / task_id / "log.jsonl"
        ts_now = datetime.utcnow().isoformat()
        try:
            with open(log_path, "a") as f:
                f.write(json.dumps({
                    "type": "task_halted_reconciled",
                    "ts": ts_now,
                    "reason": "orchestrator process not found at GET time",
                    "subtasks_reset": reset_count,
                }) + "\n")
                # Persist orchestrator_state=idle so on server restart
                # _hydrate_orchestrator_state_snapshot scans log.jsonls and
                # finds idle as the LATEST orchestrator_state event for this
                # task — without this, the snapshot replay keeps serving the
                # stale "running" label to every fresh /ws/activity client.
                f.write(json.dumps({
                    "type": "orchestrator_state",
                    "state": "idle",
                    "label": "",
                    "ts": ts_now,
                }) + "\n")
        except OSError:
            pass

        # Wake OkuroThinker — push an idle event so the status bar
        # transitions out of "running" / "thinking" / etc.
        try:
            broadcast_orchestrator_state("idle", "", task_id)
        except Exception as e:
            logger.debug(f"orchestrator_state idle broadcast failed: {e}")

        logger.info(
            f"Task {task_id} state reconciled: active → halted "
            f"(no orchestrator, {reset_count} subtask(s) reset)"
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to reconcile {task_id} state: {e}")
        return False


_HEARTBEAT_FRESH_S = 30.0
_SPAWN_GRACE_S = 15.0
# Pure-mtime trust window inside the spawn grace. The engine writes
# .engine.lease within ~1s of entry (checked above), but the bash launcher's
# pre-exec window shows no okuro.orchestrator process in a cmdline scan. For
# the first few seconds after launcher.sh is written we trust the mtime alone
# (true boot). Beyond it, a fresh launcher with NO lease/heartbeat/pid AND no
# matching process means the spawn already died (crash/kill before .exited, or
# a manual launcher-bypassing run) — so we must NOT report "alive" and 409 a
# legitimate /resume.
_PROC_BOOT_WINDOW_S = 3.0


def _is_orchestrator_running(task_id: str, tasks_dir: Path | None = None) -> bool:
    """Three-signal liveness check for the engine subprocess.

    Reliable across the whole spawn → run → exit lifecycle. Order matters:

      1. ``.heartbeat`` mtime within _HEARTBEAT_FRESH_S
         Engine writes a fresh timestamp every loop iteration (~1 Hz, see
         engine.orchestrator_loop). Authoritative WHILE running. The most
         reliable signal because it survives PID-file-write races and
         psutil cmdline-masking on macOS launcher.sh wrappers.

      2. ``.orchestrator.pid`` exists AND the PID is alive
         For the brief window between PID-write and first heartbeat, or
         when --dry-run / non-loop paths skip heartbeat altogether.

      3. Spawn grace via ``.launcher.sh`` mtime < _SPAWN_GRACE_S
         spawn_orchestrator writes launcher.sh at t=0 then Popens it.
         The engine takes ~200 ms – 2 s to reach the loop and write its
         first heartbeat / pid file. Without this grace window, callers
         (reconciler, /continue 409-guard, /resume etc.) see "no PID,
         no heartbeat" and conclude the task is dead — racing the
         spawn AND causing duplicate orchestrators on retry-clicks.

      4. psutil cmdline scan (last resort)
         Misses on macOS launcher.sh path; kept for non-launcher
         supervision (systemd-run scope).

    Reads only — never writes.
    """
    base_dir = Path(tasks_dir) if tasks_dir is not None else TASKS_DIR
    task_dir = base_dir / task_id
    if not task_dir.exists():
        return False

    # 0. Engine exit sentinel — set by `state.mark_engine_exited` on every
    # graceful return path (panel wait, capability gap, blocked review,
    # decision gate, done/failed/halted). When present AND newer than the
    # most recent spawn (launcher.sh), the engine is provably dead. Honors
    # this over the spawn-grace heuristic so the deliberate-loop's panel-
    # wait branch doesn't leave a 15-second window where the API thinks
    # the engine is still booting when it has cleanly exited.
    exited = task_dir / ".exited"
    launcher_for_exit = task_dir / ".launcher.sh"
    if exited.exists():
        try:
            exit_age = time.time() - exited.stat().st_mtime
            launch_age = (
                time.time() - launcher_for_exit.stat().st_mtime
                if launcher_for_exit.exists() else float("inf")
            )
            if exit_age < launch_age:
                return False
        except OSError:
            pass

    # 0c. Engine lease — the ONLY liveness signal continuously refreshed
    # across the entire lifecycle, including the pre-loop agentic decompose
    # window. In auto-execute mode the engine blocks on a multi-minute
    # bridge_invoke decompose BEFORE it writes .heartbeat / .orchestrator.pid
    # (those land in the work loop). Once the 15s spawn grace expires the
    # only positive signal left was the flaky psutil cmdline scan, so a
    # GET-triggered reconcile force-halted a live decompose (the
    # "normal mode never continues after decomposition" bug). The heartbeat
    # thread refreshes .engine.lease every 3s (TTL 15s, PID-validated) from
    # engine entry onward in BOTH modes — honoring it here closes the gap
    # for every present and future decompose-before-loop path.
    try:
        from okuro.orchestrator.engine import is_engine_lease_alive
        if is_engine_lease_alive(task_id, base_dir):
            return True
    except Exception:
        pass

    # 1. Fresh heartbeat → unambiguously alive.
    heartbeat = task_dir / ".heartbeat"
    if heartbeat.exists():
        try:
            age = time.time() - heartbeat.stat().st_mtime
            if age < _HEARTBEAT_FRESH_S:
                return True
            # Stale heartbeat = engine died; do NOT short-circuit return False
            # yet — fall through to PID check, which is authoritative if it
            # disagrees (engine could have just been throttled).
        except OSError:
            pass

    # 2. PID file present → process check is authoritative.
    pid_path = task_dir / ".orchestrator.pid"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False  # PID written but process is gone → genuinely dead
        except (ValueError, OSError):
            pass

    # 3. Spawn grace — launcher.sh just touched, engine not yet at the loop.
    launcher = task_dir / ".launcher.sh"
    if launcher.exists():
        try:
            age = time.time() - launcher.stat().st_mtime
            if age < _SPAWN_GRACE_S:
                # Within the true pre-exec boot window, the engine process
                # may not yet appear in a cmdline scan — trust the mtime.
                if age < _PROC_BOOT_WINDOW_S:
                    return True
                # Past the boot window with no lease/heartbeat/pid: only
                # alive if a matching engine process actually exists. A fresh
                # launcher mtime alone is NOT proof of life (the spawn may
                # have died without writing .exited) — avoid the false-positive
                # that 409s a legitimate /resume.
                for joined in _orchestrator_cmdlines_cached():
                    if task_id in joined:
                        return True
                return False
        except OSError:
            pass

    # 4. psutil last-resort cmdline scan (catches systemd-run scope path).
    # CACHED: active_roles calls this per task (~140×) on every dashboard poll;
    # an un-cached full process_iter+cmdline scan per call pegged the event
    # loop at ~88% CPU and starved SSE (flow draw 90s + batched). The scan
    # result changes slowly — reuse it for a few seconds.
    for joined in _orchestrator_cmdlines_cached():
        if task_id in joined:
            return True
    return False


_PROC_SCAN_CACHE: dict = {"ts": 0.0, "cmds": []}
_PROC_SCAN_TTL = 3.0


def _orchestrator_cmdlines_cached() -> list[str]:
    """Cached list of process cmdlines containing 'okuro.orchestrator'.

    One full psutil scan per :data:`_PROC_SCAN_TTL` seconds, shared across all
    ``_is_orchestrator_running`` calls (and the ~140-task active_roles loop)."""
    now = time.time()
    if now - _PROC_SCAN_CACHE["ts"] < _PROC_SCAN_TTL and _PROC_SCAN_CACHE["cmds"] is not None:
        return _PROC_SCAN_CACHE["cmds"]
    cmds: list[str] = []
    try:
        import psutil

        for proc in psutil.process_iter(["cmdline"]):
            try:
                cl = proc.info.get("cmdline") or []
                joined = " ".join(str(c) for c in cl)
                if "okuro.orchestrator" in joined:
                    cmds.append(joined)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        pass
    _PROC_SCAN_CACHE["ts"] = now
    _PROC_SCAN_CACHE["cmds"] = cmds
    return cmds


# C4 — phantom-event filter.
#
# After the engine writes node_done for (task_id, subtask_id), no further
# events from a subagent process for that subtask should surface to
# consumers. Duplicate dispatches from a concurrent-engine race (LF1/LF2),
# kill-request grace windows, dispatcher 600 s watchdog timeouts, and
# claude-cli session keepalives all keep writing to .activity.jsonl long
# after the engine has moved on. EventWatcher consults is_phantom_event
# on every line and drops phantoms before they reach /ws clients.

_TERMINAL_NODE_EVENTS = frozenset({
    "node_done", "node_failed",
    "subtask_done", "subtask_failed", "subtask_failed_permanently",
    "subtask_skipped", "subtask_cascade_skipped",
    "subtask_cascade_skipped_fallback",
})

_TERMINAL_SUBTASK_STATUSES = frozenset({
    "done", "failed", "skipped", "superseded", "cascade_skipped",
})


def is_phantom_event(
    task_id: str,
    subtask_id: str,
    phantom_ts: str,
    *,
    tasks_dir: Path | None = None,
) -> bool:
    """Return True iff (task_id, subtask_id) is already terminal at the
    time phantom_ts was emitted.

    Two signals consulted, in priority order:
      1. log.jsonl carries a terminal event with ts < phantom_ts (the
         append-only log is the most precise origin).
      2. plan.yaml has the subtask in a terminal status — fallback for
         rotated logs and test fixtures that inject state directly via
         make_task (plan.yaml is the SoT for execution nodes per Gate 2 §C3).

    Closes D11 (phantom subagent emissions for up to 40 min after
    node_done) by giving the EventWatcher a single canonical authority on
    "is this line stale". Best-effort: I/O failure returns False
    (fail-open — FE absorbs one extra row rather than silently swallowing
    a real event).
    """
    base_dir = Path(tasks_dir) if tasks_dir is not None else TASKS_DIR
    if not phantom_ts:
        return False

    log_path = base_dir / task_id / "log.jsonl"
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or '"subtask_id"' not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("subtask_id") != subtask_id:
                        continue
                    etype = ev.get("type") or ev.get("event_type")
                    if etype not in _TERMINAL_NODE_EVENTS:
                        continue
                    ev_ts = ev.get("ts") or ""
                    if ev_ts and ev_ts < phantom_ts:
                        return True
        except OSError:
            pass

    plan_path = base_dir / task_id / "plan.yaml"
    if plan_path.exists():
        try:
            with open(plan_path) as f:
                plan_data = yload(f) or {}
            for phase in plan_data.get("phases") or []:
                for st in phase.get("subtasks") or []:
                    if st.get("id") != subtask_id:
                        continue
                    if st.get("status") in _TERMINAL_SUBTASK_STATUSES:
                        return True
        except Exception:
            pass

    return False


def _cancel_task(task_id: str) -> dict:
    """Cancel a running task by killing the orchestrator and its process tree."""
    import yaml as _yaml

    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    if not _is_orchestrator_running(task_id):
        # Graceful path: process is already gone but task.yaml may still
        # claim active. Reconcile to halted so the dashboard reflects truth
        # and the user's stop click "succeeds" in transitioning the UI.
        reconciled = _reconcile_task_state(task_id)
        return {
            "status": "cancelled",
            "task_id": task_id,
            "killed_pids": [],
            "note": "process already stopped; task marked halted" if reconciled
                    else "process already stopped",
        }

    # Find orchestrator PID
    pid = None
    pid_path = task_dir / ".orchestrator.pid"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            try:
                os.kill(pid, 0)
            except OSError:
                pid = None
        except (ValueError, OSError):
            pass

    # Fallback: find via psutil
    if pid is None:
        try:
            import psutil
            for proc in psutil.process_iter(['pid', 'cmdline']):
                try:
                    cmdline = proc.info.get('cmdline') or []
                    if any('okuro.orchestrator' in str(c) for c in cmdline) and task_id in str(cmdline):
                        pid = proc.info['pid']
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except ImportError:
            pass

    if pid is None:
        raise HTTPException(500, f"Cannot find orchestrator process for {task_id}")

    # Kill the process tree: SIGTERM first, SIGKILL after 5s
    killed_pids = []
    try:
        import psutil
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for p in children + [parent]:
            try:
                p.send_signal(signal.SIGTERM)
                killed_pids.append(p.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, alive = psutil.wait_procs(children + [parent], timeout=5)
        for p in alive:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except ImportError:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            killed_pids.append(pid)
            time.sleep(2)
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, AttributeError):
                # AttributeError: os.killpg / os.getpgid are POSIX-only.
                pass
        except (ProcessLookupError, PermissionError, AttributeError) as e:
            raise HTTPException(500, f"Failed to kill orchestrator: {e}")

    if pid_path.exists():
        pid_path.unlink(missing_ok=True)

    task_yaml = task_dir / "task.yaml"
    if task_yaml.exists():
        with open(task_yaml) as f:
            task_data = yload(f)
        task_data["status"] = "halted"
        # P4.10 — the C8 coupling on a raw dict. Cancelling a PARKED task
        # left its `awaiting` in place, so the task read as halted while the
        # UI still offered a decision on a run that had been cancelled.
        from okuro.orchestrator.state import couple_task_dict
        with open(task_yaml, "w") as f:
            _yaml.dump(couple_task_dict(task_data), f, default_flow_style=False)

    log_path = task_dir / "log.jsonl"
    event = {
        "type": "task_cancelled",
        "killed_pids": killed_pids,
        "ts": datetime.utcnow().isoformat(),
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(event) + "\n")

    logger.info(f"Task {task_id} cancelled (killed PIDs: {killed_pids})")
    return {"status": "cancelled", "task_id": task_id, "killed_pids": killed_pids}


@app.post("/api/tasks/{task_id}/cancel")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def cancel_task(task_id: str):
    """Cancel a running task by killing the orchestrator and its children."""
    _validate_task_id(task_id)
    return _cancel_task(task_id)


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, force: bool = Query(False)):
    """Delete a task and all its files. Use ?force=true to cancel a running task first."""
    _validate_task_id(task_id)
    task_dir = (TASKS_DIR / task_id).resolve()
    if not str(task_dir).startswith(str(TASKS_DIR.resolve())):
        raise HTTPException(400, "Path traversal detected")
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    if _is_orchestrator_running(task_id):
        if not force:
            raise HTTPException(409, f"Task {task_id} is currently running. Use ?force=true to cancel and delete.")
        _cancel_task(task_id)
        await asyncio.sleep(1)

    try:
        shutil.rmtree(task_dir)
    except Exception as e:
        raise HTTPException(500, f"Failed to delete task: {e}")

    logger.info(f"Task {task_id} deleted")
    return {"status": "deleted", "task_id": task_id}


@app.delete("/api/tasks/{task_id}/interventions/{intervention_id}")
async def delete_intervention_endpoint(task_id: str, intervention_id: str):
    """Delete a queued, not-yet-started continuation prompt.

    Only trailing continuations (no plan ever materialized — the
    "queued" state) are removable, and only while no orchestrator is
    alive for the task. The state-layer guards enforce the rest:
    the initial prompt and already-materialized continuations are
    rejected with 409.
    """
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    if _is_orchestrator_running(task_id):
        raise HTTPException(
            409,
            f"Task {task_id} has a running orchestrator; stop it before "
            "deleting a queued prompt.",
        )

    from okuro.orchestrator.state import delete_intervention

    lock = _get_spawn_lock(task_id)
    async with lock:
        try:
            removed = await asyncio.to_thread(
                delete_intervention, task_id, intervention_id, TASKS_DIR
            )
        except KeyError:
            raise HTTPException(
                404, f"Intervention {intervention_id} not found on {task_id}"
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    logger.info(f"Intervention {intervention_id} deleted from {task_id}")
    return {
        "status": "deleted",
        "task_id": task_id,
        "intervention_id": removed.id,
    }


@app.patch("/api/tasks/{task_id}")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def rename_task(task_id: str, body: TaskRenameRequest):
    """Rename a task's description."""
    _validate_task_id(task_id)
    import yaml as _yaml

    task_path = TASKS_DIR / task_id / "task.yaml"
    if not task_path.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    with open(task_path) as f:
        task_data = yload(f)

    old_description = task_data["description"]
    task_data["description"] = body.description

    with open(task_path, "w") as f:
        _yaml.dump(task_data, f, default_flow_style=False)

    log_path = TASKS_DIR / task_id / "log.jsonl"
    event = {
        "type": "task_renamed",
        "old_description": old_description[:200],
        "new_description": body.description[:200],
        "ts": datetime.utcnow().isoformat(),
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(event) + "\n")

    logger.info(f"Task {task_id} renamed")
    return {"status": "ok", "task_id": task_id, "description": body.description}


class TaskIntelligenceRequest(BaseModel):
    intelligence: str  # "" or "max"


@app.put("/api/tasks/{task_id}/intelligence")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def set_task_intelligence(task_id: str, body: TaskIntelligenceRequest):
    """Toggle intelligence level on a task."""
    _validate_task_id(task_id)
    if body.intelligence not in ("", "max"):
        raise HTTPException(400, "intelligence must be '' or 'max'")

    import yaml as _yaml
    task_path = TASKS_DIR / task_id / "task.yaml"
    if not task_path.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    with open(task_path) as f:
        task_data = yload(f)

    task_data["intelligence"] = body.intelligence

    with open(task_path, "w") as f:
        _yaml.dump(task_data, f, default_flow_style=False)

    log_path = TASKS_DIR / task_id / "log.jsonl"
    event = {
        "type": "intelligence_changed",
        "intelligence": body.intelligence,
        "ts": datetime.utcnow().isoformat(),
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(event) + "\n")

    logger.info(f"Task {task_id} intelligence set to '{body.intelligence}'")
    return {"status": "ok", "task_id": task_id, "intelligence": body.intelligence}


class TaskProjectPathRequest(BaseModel):
    project_path: str  # absolute path or "" to clear


@app.put("/api/tasks/{task_id}/project-path")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def set_task_project_path(task_id: str, body: TaskProjectPathRequest):
    """Set the realized-project path that the preview proposer scans.

    Empty string clears the field. Non-empty values must point at an
    existing directory — invalid paths are rejected up front so the
    preview button never starts a build against a vanished workspace
    and the user gets immediate feedback rather than a cryptic recipe
    error 30 s later.

    Engine-side declarative writes (``engine._adopt_declared_project_path``
    on task finalize) are the primary source of project_path; this
    endpoint is the user-edit path the SPA picker uses when no path
    was declared (legacy tasks) or when the declared path is wrong.
    """
    _validate_task_id(task_id)
    import yaml as _yaml
    task_path = TASKS_DIR / task_id / "task.yaml"
    if not task_path.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    raw = (body.project_path or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise HTTPException(400, "project_path must be an absolute path")
        if not candidate.exists() or not candidate.is_dir():
            raise HTTPException(400, f"project_path is not an existing directory: {candidate}")
        raw = str(candidate.resolve())

    with open(task_path) as f:
        task_data = yload(f) or {}
    if raw:
        task_data["project_path"] = raw
    else:
        task_data.pop("project_path", None)
    # Drop the legacy negative-cache sentinel if present. The auto-
    # inference machinery that wrote it is gone; this just keeps
    # task.yaml clean for migrated tasks.
    task_data.pop("project_path_inferred_none", None)
    with open(task_path, "w") as f:
        _yaml.dump(task_data, f, default_flow_style=False)

    log_path = TASKS_DIR / task_id / "log.jsonl"
    event = {
        "type": "project_path_changed",
        "project_path": raw,
        "ts": datetime.utcnow().isoformat(),
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(event) + "\n")

    logger.info(f"Task {task_id} project_path set to {raw!r}")
    return {"status": "ok", "task_id": task_id, "project_path": raw}


@app.get("/api/tasks/{task_id}/project-path/suggestions")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_task_project_path_suggestions(task_id: str, top_n: int = 3):
    """Return ranked candidate ``project_path`` values for a task.

    Backs the SPA's project-path picker for legacy tasks (or any task
    whose subagents did not declare ``brief['project_path']``). The
    candidates come from mining ``plan.yaml`` + ``.activity.jsonl`` +
    the user's task description; each carries hits + build_markers +
    has_git so the UI can render an informed pick. NEVER auto-applied —
    the user picks via PUT /api/tasks/{id}/project-path.

    Returns ``{"suggestions": [...]}`` (always a list, possibly empty
    for research-only tasks where no realized project exists).
    """
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")
    from okuro.orchestrator.preview import suggest_project_paths
    bounded = max(1, min(int(top_n), 10))
    suggestions = suggest_project_paths(task_dir, top_n=bounded)
    return {"suggestions": suggestions}


class SubtaskModelOverrideRequest(BaseModel):
    model: str  # a model exposed by the active CLI's tier_map, or a tier name


@app.patch("/api/tasks/{task_id}/subtasks/{subtask_id}/model")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def set_subtask_model_override(task_id: str, subtask_id: str, body: SubtaskModelOverrideRequest):
    """Override the model for a pending subtask.

    Provider-agnostic: the model must be one the ACTIVE CLI exposes (a value in
    its config tier_map) or a tier name (fast|standard|strategic) — NOT a
    claude-only allowlist, so gemini/codex models are accepted too.
    """
    _validate_task_id(task_id)
    _model = (body.model or "").strip()
    if not _model:
        raise HTTPException(400, "model is required")
    _valid: set = set()
    try:
        from okuro.orchestrator.config import load_config
        _cfg = load_config()
        _cli = getattr(_cfg, "cli_default", "") or ""
        _tool = (getattr(_cfg, "cli_tools", {}) or {}).get(_cli)
        _valid = {v for v in (getattr(_tool, "tier_map", {}) or {}).values() if v}
    except Exception:
        _valid = set()
    _valid |= {"fast", "standard", "strategic"}
    if _model not in _valid:
        raise HTTPException(
            400, f"model {_model!r} not available for the active CLI — choose one of {sorted(_valid)}"
        )
    body.model = _model

    import yaml as _yaml
    plan_path = TASKS_DIR / task_id / "plan.yaml"
    if not plan_path.exists():
        raise HTTPException(404, f"Task {task_id} has no plan")

    with open(plan_path) as f:
        plan_data = yload(f)

    found = False
    for phase in plan_data.get("phases", []):
        for st in phase.get("subtasks", []):
            if st.get("id") == subtask_id:
                if st.get("status", "pending") != "pending":
                    raise HTTPException(409, f"Subtask {subtask_id} is {st.get('status')} — can only override pending subtasks")
                st["model_override"] = body.model
                found = True
                break
        if found:
            break

    if not found:
        raise HTTPException(404, f"Subtask {subtask_id} not found in task {task_id}")

    with open(plan_path, "w") as f:
        _yaml.dump(plan_data, f, default_flow_style=False)

    log_path = TASKS_DIR / task_id / "log.jsonl"
    event = {
        "type": "model_override",
        "subtask_id": subtask_id,
        "model": body.model,
        "ts": datetime.utcnow().isoformat(),
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(event) + "\n")

    logger.info(f"Task {task_id} subtask {subtask_id} model override set to '{body.model}'")
    return {"status": "ok", "task_id": task_id, "subtask_id": subtask_id, "model": body.model}


@app.get("/api/tasks")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def list_tasks(
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
):
    """List all tasks, optionally filtered by status.

    Reconciles transient-state tasks first so abandoned-active tasks roll
    over to halted instead of haunting the list.
    """
    tasks = list_all_tasks(TASKS_DIR)
    reconciled = False
    for t in tasks:
        if t.status in ("active", "planning") and _reconcile_task_state(t.id):
            reconciled = True
    if reconciled:
        tasks = list_all_tasks(TASKS_DIR)
    if status:
        tasks = [t for t in tasks if t.status == status]
    return {"tasks": [t.model_dump() for t in tasks[:limit]], "total": len(tasks)}


@app.get("/api/tasks/{task_id}")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_task(task_id: str):
    """Get detailed task information."""
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")
    _reconcile_task_state(task_id)
    detail = read_task_detail(task_dir)
    if not detail:
        raise HTTPException(404, f"Could not read task {task_id}")
    return detail.model_dump()


@app.get("/api/tasks/{task_id}/state")
def get_task_state_endpoint(task_id: str):
    """Get real-time task state for UI display.

    Sync `def`: _reconcile_task_state + read_task_state are blocking I/O
    (yaml + file stats + _is_orchestrator_running), polled per-task every
    cycle. Kept off the event loop (threadpool) so it can't starve /mcp/v1.
    """
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")
    _reconcile_task_state(task_id)
    state = read_task_state(task_dir)
    if not state:
        raise HTTPException(404, f"Could not read state for {task_id}")
    return state.model_dump()


@app.get("/api/tasks/{task_id}/snapshot")
def get_task_snapshot(task_id: str):
    """Canonical task snapshot — single SoT for the FE.

    Sync `def`: blocking read path (plan/task yaml + derivation), polled
    per-task every cycle. Runs in the threadpool so the event loop stays
    free for the inline /mcp/v1 mount subagents depend on.

    Replaces the constellation of polling endpoints (taskState, gates,
    awaiting, capabilityGap, positions, discussion, graph) that the FE
    composed with up to 5 s cross-slice skew. See
    docs/audit-2026-05-26/02-frontend-state-audit.md §6.

    Step 1 of the canonical-state migration. Steps 2-5 add color_class +
    label + parallel-phase layout signals + WS patch stream subscription.
    """
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")
    _reconcile_task_state(task_id)
    snap = read_task_snapshot(task_dir)
    if snap is None:
        raise HTTPException(404, f"Could not read snapshot for {task_id}")
    return snap


@app.get("/api/tasks/{task_id}/awaiting")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_task_awaiting(task_id: str):
    """Return the awaiting block (or null) — what user input the engine is
    blocked on. Cheap and stable; safe to poll from the UI to drive the
    foreground call-to-action card on every task page.
    """
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")
    task_yaml = task_dir / "task.yaml"
    if not task_yaml.exists():
        return {"task_id": task_id, "awaiting": None}
    try:
        with open(task_yaml) as f:
            td = yload(f) or {}
    except Exception as exc:
        raise HTTPException(500, f"Failed to read task.yaml: {exc}")
    aw = td.get("awaiting")
    if not isinstance(aw, dict) or not aw.get("kind"):
        return {"task_id": task_id, "status": td.get("status", ""), "awaiting": None}
    return {
        "task_id": task_id,
        "status": td.get("status", ""),
        "awaiting": {
            "kind": str(aw.get("kind", "")),
            "message": str(aw.get("message", "")),
            "endpoint": str(aw.get("endpoint", "")),
            "method": str(aw.get("method", "POST")),
            "payload": dict(aw.get("payload") or {}),
            "since": str(aw.get("since", "")),
        },
    }


class RefreshAwaitingRequest(BaseModel):
    """``reset_since`` restarts the "waiting N min" clock. Default False —
    re-wording a card does not restart the wait, and P2.2's TTL staleness
    reads ``since``. Pass True only for a genuine re-park."""
    reset_since: bool = False


@app.post("/api/tasks/{task_id}/awaiting/refresh")
# Sync `def`: this body has ZERO awaits — see the note on get_task_awaiting.
def refresh_task_awaiting(task_id: str, body: RefreshAwaitingRequest | None = None):
    """Re-author the open gate's card from the inputs it was built with (P2.4).

    Gate copy is frozen at park time and the park is engineless, so a card
    written by older copy sits on screen until the user answers it. Every
    wording fix in this plan therefore reached new gates only — never the
    ones a person was actually looking at. This is the correction path: it
    changes what the card SAYS, never what it means or what answering it
    does. Kind, endpoint, method and options all come back identical; only
    the rendered text is re-derived.

    409 rather than 404 when nothing is open: the task exists, the request
    just does not apply to its current state.
    """
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    from okuro.orchestrator.state import (
        load_task as _load_task,
        refresh_awaiting_presentation,
    )

    task = _load_task(task_id, TASKS_DIR)
    if getattr(task, "awaiting", None) is None:
        raise HTTPException(409, f"Task {task_id} has no open gate to refresh")

    result = refresh_awaiting_presentation(
        task, TASKS_DIR, reset_since=bool(body and body.reset_since),
    )
    if not result.get("rebuilt"):
        # Not an error: an old gate that predates `source` is a legitimate
        # state, and the honest answer is "this one cannot be re-authored"
        # rather than a guessed rebuild or a 500.
        return {"task_id": task_id, "refreshed": False, "reason": result.get("reason", "")}

    reloaded = _load_task(task_id, TASKS_DIR)
    return {
        "task_id": task_id,
        "refreshed": True,
        "changed": bool(result.get("changed")),
        "since": getattr(reloaded.awaiting, "since", ""),
        "presentation": (reloaded.awaiting.payload or {}).get("presentation", {}),
    }


@app.get("/api/tasks/{task_id}/logs")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_task_logs(task_id: str, limit: int = Query(100, ge=1, le=1000)):
    """Get task execution log entries."""
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")
    logs = read_task_logs(task_dir, limit=limit)
    return {"task_id": task_id, "logs": [l.model_dump() for l in logs]}


@app.get("/api/tasks/{task_id}/artifacts")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_task_artifacts(task_id: str):
    """List task artifacts."""
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")
    artifacts = read_task_artifacts(task_dir)
    return {"task_id": task_id, "artifacts": [a.model_dump() for a in artifacts]}


@app.get("/api/tasks/{task_id}/deliveries")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_task_deliveries(task_id: str):
    """List Stream C deliveries (audience-adapted renders) for a task.

    Joins via artifact_id -> task_id since deliveries don't carry the
    task_id directly (they live alongside the artifact, the artifact is
    the task-tagged row). Body-free; call /api/deliveries/{id} for the
    rendered payload.
    """
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")
    try:
        from okuro.sense.artifacts import artifact_list
        from okuro.peer.delivery.store import delivery_list
    except Exception as exc:
        logger.warning("delivery API imports failed: %s", exc)
        return {"task_id": task_id, "deliveries": []}

    rows = artifact_list(task_id=task_id, limit=200)
    artifact_ids = [r.get("id") for r in rows if r.get("id")]
    deliveries: list[dict] = []
    for aid in artifact_ids:
        deliveries.extend(delivery_list(artifact_id=aid, limit=50))
    return {"task_id": task_id, "deliveries": deliveries}


@app.get("/api/deliveries/{delivery_id}")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_delivery(delivery_id: str, include_blob: bool = False):
    """Fetch a single delivery row (with body) for ArtifactsViewer rendering.

    include_blob=true returns the binary payload (PPTX, audio); default
    omits it for cheap downloads.
    """
    try:
        from okuro.peer.delivery.store import delivery_get
        row = delivery_get(delivery_id, include_body=True, include_blob=include_blob)
    except Exception:
        row = None
    if row is None:
        raise HTTPException(404, f"Delivery {delivery_id} not found")
    return row


_BRAIN_ARTIFACT_ID_RE = _re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _content_disposition_inline(filename: str) -> str:
    """RFC 5987 inline disposition that survives non-ASCII filenames.

    HTTP headers are latin-1 only; subagent titles routinely contain
    em-dashes, German umlauts, etc. A naive ``filename="<title>"`` raises
    UnicodeEncodeError inside Starlette and the response 500s — the body
    never reaches the browser, so the preview panel renders as empty
    even though the artifact body is fully populated. RFC 5987's
    ``filename*=UTF-8''<percent-encoded>`` is the standard escape; we
    keep an ASCII ``filename=`` fallback (with non-ASCII stripped) for
    legacy clients.
    """
    import urllib.parse
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "artifact"
    encoded = urllib.parse.quote(filename, safe="")
    return f"inline; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


@app.get("/api/tours")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def list_tours(project: str | None = None, limit: int = 20):
    """Return recent code-tour artifacts with their parsed bodies.

    Tours are stored as ``kind=report`` artifacts whose title starts with
    ``"tour: "`` (artifact schema only allows evidence/plan/report — see
    sense/artifacts.py). The tour-builder role enforces this prefix.
    Body is JSON; we parse it server-side so the client only sees
    structured TourBody objects.
    """
    import json as _json

    try:
        from okuro.sense.artifacts import artifact_list, artifact_get
    except Exception as exc:
        logger.warning("tours API imports failed: %s", exc)
        return {"tours": []}

    # Over-fetch reports, then filter to tour-prefixed titles. Plain reports
    # outnumber tours; the limit is what the caller cares about post-filter.
    rows = artifact_list(kind="report", project=project, limit=limit * 5)
    tours: list[dict] = []
    for row in rows:
        title = row.get("title") or ""
        if not title.lower().startswith("tour:"):
            continue
        full = artifact_get(row["id"], include_body=True)
        if not full or not full.get("body"):
            continue
        try:
            body = _json.loads(full["body"])
        except (ValueError, TypeError):
            continue
        if not isinstance(body, dict) or not isinstance(body.get("steps"), list):
            continue
        tours.append({
            "id": full["id"],
            "title": full.get("title") or body.get("project") or "tour",
            "created_at": full.get("created_at"),
            "project": full.get("project"),
            "body": body,
        })
        if len(tours) >= limit:
            break
    return {"tours": tours}


@app.get("/api/tasks/{task_id}/artifacts/{artifact_name}")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_artifact_content(task_id: str, artifact_name: str):
    """Serve an artifact for inline rendering.

    Two storage backends:
      * Brain — ``artifact_name`` is a UUID; body comes from the
        artifacts table via ``artifact_get``. Stream B reports go here.
      * Disk — ``artifact_name`` is a filename under
        tasks/{id}/artifacts/. Used for binaries / screenshots / .stdout
        fallbacks the dispatcher does not route through artifact_write.

    Images / video / audio / PDF stream as binary with the correct
    Content-Type so the browser can render them natively via <img>/<video>
    /<iframe>. Markdown and other text types stream as text/* with a UTF-8
    charset so the frontend can still fetch as text when it chooses.
    """
    _validate_task_id(task_id)

    # Brain path — artifact_name is a UUID and resolves to a brain row
    # tagged with this task_id. Reject cross-task fetches so a leaked id
    # cannot read another task's artifact through this endpoint.
    if _BRAIN_ARTIFACT_ID_RE.match(artifact_name):
        try:
            from okuro.sense.artifacts import artifact_get
            row = artifact_get(artifact_name, include_body=True)
        except Exception:
            row = None
        if row is None:
            raise HTTPException(404, f"Artifact {artifact_name} not found")
        if row.get("task_id") and row.get("task_id") != task_id:
            raise HTTPException(404, f"Artifact {artifact_name} not found in task {task_id}")
        body = row.get("body") or ""
        media_type = row.get("media_type") or "text/markdown"
        if media_type.startswith("text/") and "charset" not in media_type:
            media_type = f"{media_type}; charset=utf-8"
        title = row.get("title") or artifact_name
        return Response(
            content=body if isinstance(body, str) else str(body),
            media_type=media_type,
            headers={"Content-Disposition": _content_disposition_inline(title)},
        )

    artifact_path = _validate_artifact_name(artifact_name, task_id)
    if not artifact_path.exists():
        raise HTTPException(404, f"Artifact {artifact_name} not found")

    import mimetypes
    guessed, _ = mimetypes.guess_type(artifact_path.name)
    media_type = guessed or "application/octet-stream"

    # Ensure text formats advertise utf-8 so the browser/ReactMarkdown
    # doesn't mis-decode non-ASCII content.
    if media_type.startswith("text/") and "charset" not in media_type:
        media_type = f"{media_type}; charset=utf-8"

    # "inline" lets the browser render rather than forcing a download.
    return FileResponse(
        artifact_path,
        media_type=media_type,
        headers={"Content-Disposition": _content_disposition_inline(artifact_path.name)},
    )


@app.get("/api/tasks/{task_id}/activity")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_task_activity(task_id: str, limit: int = Query(20000, ge=1, le=50000)):
    """Get recent agent activity events (for HUD reconnection).

    Default + cap raised in 2026-04 — long-running orchestrations (60+ subtasks,
    4k+ events) were silently truncated to the last 500 events, which made
    every subtask earlier than the last few render as "no activity" in the
    feed when selected. Cap holds raw response under ~10 MB at typical event
    sizes; pagination is the next step if real tasks ever exceed that.
    """
    _validate_task_id(task_id)
    activity_path = TASKS_DIR / task_id / ".activity.jsonl"
    if not activity_path.exists():
        return {"task_id": task_id, "events": []}
    events = []
    try:
        with open(activity_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception:
        pass
    return {"task_id": task_id, "events": events[-limit:]}


@app.get("/api/tasks/{task_id}/review")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_task_review(task_id: str, limit: int = Query(50, ge=1, le=500)):
    """Structured M3 review history from the authoritative ``task_events`` store.

    The activity feed (``.activity.jsonl``) is a lossy, best-effort streaming
    surface — if the reviewer's emit helpers resolve the wrong tasks_dir the
    rows silently vanish and the user is left with "reviewing…" and no outcome.
    This serves the durable ``verdict`` events (VerdictBody: verdict, det
    counts, load_bearing_critic_findings with file/line/summary, scorer_rubric,
    short_circuited) for EVERY verdict — not just the blocked_review case the
    BlockerCard already renders. Newest verdict last (seq ASC), one per review
    pass. Best-effort: a missing table / import error returns an empty list
    rather than 500-ing the task view.
    """
    _validate_task_id(task_id)
    try:
        from okuro.sense.task_events import list_events
        verdicts = list_events(task_id=task_id, event_type="verdict", limit=limit)
    except Exception:
        verdicts = []
    return {"task_id": task_id, "verdicts": verdicts}


def _review_max_attempts() -> int:
    """The review round budget, from the single place that defines it."""
    try:
        from okuro.orchestrator.api.state_reader import _REVIEW_MAX_ATTEMPTS
        return int(_REVIEW_MAX_ATTEMPTS)
    except Exception:
        return 5


@app.get("/api/tasks/{task_id}/review-rounds")
# Sync `def`: this body has ZERO awaits — see the note on get_task_review.
def get_task_review_rounds(task_id: str, limit: int = Query(500, ge=1, le=2000)):
    """Per-subtask, per-ROUND review history (ROCK-SOLID v5 P3.9).

    The plan expected this to come from the already-emitted log rows
    (review_started / critic_finding / scorer_decision / verdict_published /
    review_complete_event). It cannot: those carry ``phase_id`` only —
    reviewer/pipeline.py's ``_emit_log_event`` adds nothing beyond the fields
    passed, and the durable ``verdict`` events are filed under a SYNTHETIC
    ``reviewer-phase-N`` subtask id. Neither can key a per-subtask timeline.

    ``convergence_telemetry`` can, and already does: one row per (subtask,
    attempt) carrying verdict, findings counts, the loop-control reason, and —
    since this plan's own P0.5 — ``stage_timings`` (queue_wait / deterministic
    / critic / scorer). ``state_reader`` already reads these rows and keeps
    only the LATEST attempt for the tile badge; this returns the history it
    discards.

    Grouped by subtask, rounds ascending, so a 30 ms deterministic round and a
    4-minute LLM round leave the same shaped trace — the plan's requirement
    that a fast round still be visible.

    Best-effort: a missing table or import error returns an empty map rather
    than 500-ing the task view.
    """
    _validate_task_id(task_id)
    try:
        from okuro.sense.task_events import list_events
        rows = list_events(
            task_id=task_id, event_type="convergence_telemetry", limit=limit,
        )
    except Exception:
        rows = []

    by_subtask: dict[str, list[dict]] = {}
    for ev in rows:
        body = ev.get("body") or {}
        sid = str(body.get("subtask_id") or "")
        if not sid:
            continue
        timings = body.get("stage_timings") or {}
        by_subtask.setdefault(sid, []).append({
            "attempt": int(body.get("attempt") or 0),
            "verdict": str(body.get("verdict") or ""),
            "findings_count": int(body.get("findings_count") or 0),
            "open_findings": int(body.get("open_findings") or 0),
            "resolved_findings": int(body.get("resolved_findings") or 0),
            # WHY the round ended when the verdict alone does not say it
            # (converged / budget exhausted). Empty for an ordinary round.
            "reason": str(body.get("reason") or ""),
            "artifact_id": str(body.get("artifact_id") or ""),
            "ts": str(body.get("ts") or ""),
            "stage_timings": {
                k: timings.get(k)
                for k in ("queue_wait_s", "deterministic_s", "critic_s", "scorer_s")
                if timings.get(k) is not None
            },
        })

    # Ascending by round. A duplicate attempt number (a re-publish) keeps both
    # rather than being silently deduped — losing a row here would understate
    # how much reviewing actually happened, which is the opposite of the point.
    for sid in by_subtask:
        by_subtask[sid].sort(key=lambda r: (r["attempt"], r["ts"]))

    return {
        "task_id": task_id,
        # One budget, defined where the tile badge already reads it, so the
        # timeline and the badge can never disagree about M in "round N of M".
        "max_attempts": _review_max_attempts(),
        "subtasks": by_subtask,
    }


@app.post("/api/tasks/{task_id}/suggest")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def generate_suggestion(task_id: str):
    """Generate continuation suggestions via the workforce-reviewer role.

    Uses the same generator as the engine's completion hook — one prompt,
    one schema, one set of bugs. The role owns the prompt + output contract
    (src/okuro/roles/catalog/workforce-reviewer.yaml); tune there, no code
    change needed.
    """
    _validate_task_id(task_id)
    if not _rate_limiter.check("llm_calls", max_calls=5, window_seconds=60):
        raise HTTPException(429, "Rate limited — max 5 LLM suggestion calls per minute")
    task_dir = TASKS_DIR / task_id
    task_yaml = task_dir / "task.yaml"
    if not task_yaml.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    import yaml as _yaml
    try:
        with open(task_yaml) as f:
            task_data = yload(f)
    except Exception as e:
        raise HTTPException(500, f"Failed to read task: {e}")

    if task_data.get("status") != "done":
        raise HTTPException(400, "Task is not done")

    try:
        from okuro.orchestrator.config import load_config
        from okuro.orchestrator.suggestions import (
            generate_continuation_suggestions,
        )
        config = load_config(OKURO_ROOT / "config.yaml")
    except Exception as e:
        raise HTTPException(500, f"Config load failed: {e}")

    artifacts_dir = task_dir / "artifacts"
    phases = task_data.get("phases") or []

    # Surface the long LLM call on the OkuroThinker widget so the user
    # sees immediate feedback after clicking "Suggest next steps".
    broadcast_orchestrator_state(
        state="generating",
        label="Generating 3 next steps…",
        task_id=task_id,
    )
    try:
        structured = generate_continuation_suggestions(
            task_id=task_id,
            task_description=task_data.get("description", "") or "",
            phase_count=len(phases),
            artifacts_dir=artifacts_dir,
            config=config,
        )
    finally:
        broadcast_orchestrator_state(
            state="idle",
            label="",
            task_id=task_id,
        )

    if not structured:
        raise HTTPException(502, "No suggestions generated — workforce-reviewer returned empty (check orchestrator logs)")

    # Persist alongside the existing completion-time shape so the state
    # reader and UI render consistently regardless of origin.
    task_data["continuation_suggestions"] = structured
    # Legacy single-string field kept for backward compat with old clients.
    task_data["continuation_suggestion"] = structured[0]["suggestion_text"]
    tmp = task_yaml.with_suffix(".tmp")
    with open(tmp, "w") as fw:
        _yaml.dump(task_data, fw, default_flow_style=False)
    tmp.rename(task_yaml)

    return {
        "task_id": task_id,
        "suggestion": structured[0]["suggestion_text"],
        "suggestions": structured,
    }


@app.post("/api/tasks/{task_id}/suggestions/{sugg_id}/spawn", status_code=202)
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def spawn_task_from_suggestion(task_id: str, sugg_id: str):
    """Promote a continuation suggestion into a real task in one click.

    Audit finding #24 — suggestions used to be passive text in
    ``task.yaml.continuation_suggestions[]``; the user had to copy-paste the
    text into a new task. This endpoint reads the named suggestion, spawns a
    new orchestrator with the suggestion text as the description, and inherits
    the parent's preferred_cli, required_roles, intelligence and
    auto_approve_risk so the new task starts pre-configured for the same
    operator profile.

    Returns 404 with the list of valid suggestion IDs when ``sugg_id`` is not
    found — caller can re-render the picker without another GET round-trip.
    """
    _validate_task_id(task_id)

    parent_dir = TASKS_DIR / task_id
    parent_yaml = parent_dir / "task.yaml"
    if not parent_yaml.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    try:
        with open(parent_yaml) as f:
            parent_data = yload(f) or {}
    except Exception as e:
        raise HTTPException(500, f"Failed to read parent task: {e}")

    suggestions = parent_data.get("continuation_suggestions") or []
    match = next((s for s in suggestions if s.get("id") == sugg_id), None)
    if not match:
        valid = [s.get("id") for s in suggestions if s.get("id")]
        raise HTTPException(
            404,
            f"Suggestion {sugg_id} not found in {task_id}. Valid IDs: {valid}",
        )

    # Compose the new task description: action verb prefix lifted from the
    # suggestion's category when present so the decomposer gets a clear
    # imperative ("Build X" / "Research Y" / "Validate Z" / "Followup on W").
    sugg_text = (match.get("suggestion_text") or "").strip()
    if not sugg_text:
        raise HTTPException(400, f"Suggestion {sugg_id} has no description")
    category = (match.get("category") or "followup").strip().lower()
    verb_map = {
        "build": "Build",
        "business": "Plan",
        "research": "Research",
        "validate": "Validate",
        "followup": "Follow up on",
    }
    verb = verb_map.get(category, "Follow up on")
    description = f"{verb}: {sugg_text}"

    # Inherit the parent's operator profile so the spawned task hits the same
    # CLI / roles / intelligence / approval ceiling without a second prompt.
    preferred_cli = parent_data.get("preferred_cli") or None
    required_roles = parent_data.get("required_roles") or None
    intelligence = parent_data.get("intelligence") or None
    auto_approve_risk = (parent_data.get("auto_approve_risk") or "none").lower()
    if auto_approve_risk not in ("none", "medium", "high"):
        auto_approve_risk = "none"

    new_task_id = f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    cmd = [sys.executable, "-m", "okuro.orchestrator.engine", "--task-id", new_task_id]
    if preferred_cli:
        cmd.extend(["--preferred-cli", preferred_cli])
    if intelligence:
        cmd.extend(["--intelligence", intelligence])
    if required_roles:
        cmd.extend(["--required-roles", ",".join(required_roles)])
    if auto_approve_risk and auto_approve_risk != "none":
        cmd.extend(["--auto-approve-risk", auto_approve_risk])
    cmd.append(description)

    pid = spawn_orchestrator(cmd, new_task_id)
    if not pid:
        raise HTTPException(500, "Failed to spawn task orchestrator")

    logger.info(
        "Spawned task %s from suggestion %s of parent %s",
        new_task_id, sugg_id, task_id,
    )
    return {
        "status": "accepted",
        "task_id": new_task_id,
        "parent_task_id": task_id,
        "suggestion_id": sugg_id,
        "description": description,
        "pid": pid,
        "inherited": {
            "preferred_cli": preferred_cli,
            "required_roles": required_roles,
            "intelligence": intelligence,
            "auto_approve_risk": auto_approve_risk,
        },
    }


@app.get("/api/capabilities")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def list_capabilities():
    """Return all capabilities in the registry."""
    try:
        from okuro.orchestrator.capabilities.registry import CapabilityRegistry
        registry = CapabilityRegistry(OKURO_ROOT / "capabilities.yaml")
        caps = [c.to_dict() for c in registry.all()]
        return {"capabilities": caps, "count": len(caps)}
    except Exception as e:
        raise HTTPException(500, f"Failed to load capabilities: {e}")


@app.get("/api/capabilities/{cap_id}")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_capability(cap_id: str):
    """Return a single capability by ID."""
    try:
        from okuro.orchestrator.capabilities.registry import CapabilityRegistry
        registry = CapabilityRegistry(OKURO_ROOT / "capabilities.yaml")
        cap = registry.get(cap_id)
        if not cap:
            raise HTTPException(404, f"Capability {cap_id} not found")
        return cap.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to load capability: {e}")


# Approval decision → resulting subtask status. "reject" maps to the
# engine's distinct decline branch (wait_for_approval handles "rejected"
# separately from "skipped"); "skip" is the non-destructive pass.
_APPROVAL_ACTIONS = {
    "approve": "approved",
    "skip": "skipped",
    "reject": "rejected",
}

# Approval decision → log event type. Derived from the status map so the two
# can never drift, and every value is registered in _STATE_CHANGE_EVENTS.
_APPROVAL_EVENT_TYPES = {
    action: f"approval_{status}" for action, status in _APPROVAL_ACTIONS.items()
}


class ApprovalRequest(BaseModel):
    subtask_id: str
    action: str  # "approve" | "skip" | "reject"


@app.post("/api/tasks/{task_id}/approve")
# Sync `def`, deliberately: this body has ZERO awaits and does blocking
# work (yaml read-modify-write, file-locked task.yaml writes, a process
# spawn). Starlette runs `async def` path-ops ON the event loop, so as
# `async def` it stalled the same loop serving /api/tasks/{id}/state, the
# /ws push and the inline /mcp/v1 mount. Sync path-ops go to the
# threadpool instead. Same remedy already applied to the hot READ
# endpoints (active_roles / get_task_state_endpoint / get_task_snapshot);
# the write half was never swept.
def approve_subtask(task_id: str, body: ApprovalRequest):
    """Approve, skip, or reject a subtask that is waiting for approval."""
    _validate_task_id(task_id)
    import yaml as _yaml

    task_dir = TASKS_DIR / task_id
    plan_path = task_dir / "plan.yaml"
    log_path = task_dir / "log.jsonl"

    if not plan_path.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    if body.action not in _APPROVAL_ACTIONS:
        raise HTTPException(
            400, "action must be 'approve', 'skip', or 'reject'"
        )

    with open(plan_path) as f:
        plan = yload(f)

    found = False
    for phase in plan.get("phases", []):
        for st in phase.get("subtasks", []):
            if st["id"] == body.subtask_id:
                if st["status"] != "waiting_approval":
                    raise HTTPException(
                        409,
                        f"Subtask {body.subtask_id} is '{st['status']}', not 'waiting_approval'"
                    )
                new_status = _APPROVAL_ACTIONS[body.action]
                st["status"] = new_status
                found = True
                break
        if found:
            break

    if not found:
        raise HTTPException(404, f"Subtask {body.subtask_id} not found")

    with open(plan_path, "w") as f:
        _yaml.dump(plan, f, default_flow_style=False)

    # Explicit map, not `action + "d"`: that f-string produced the malformed
    # "approval_skipd" / "approval_rejectd" for two of the three actions, and
    # an event type only reaches the FE if it matches _STATE_CHANGE_EVENTS
    # byte-for-byte.
    event = {
        "type": _APPROVAL_EVENT_TYPES[body.action],
        "subtask": body.subtask_id,
        "action": body.action,
        "ts": datetime.utcnow().isoformat(),
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(event) + "\n")

    # Park = engineless: wait_for_approval now persists a ``subtask_approval``
    # awaiting + exits the engine instead of polling. Clear the awaiting and
    # respawn a fresh --resume engine that re-evaluates the resolved subtask.
    # Idempotent — _resume_engine_if_idle no-ops when an engine is alive (the
    # legacy poll path, if any engine is still running).
    pid = None
    try:
        from okuro.orchestrator.state import load_task as _load_task, clear_awaiting
        task_reloaded = _load_task(task_id, TASKS_DIR)
        if getattr(task_reloaded, "awaiting", None) is not None and \
                task_reloaded.awaiting.kind == "subtask_approval":
            _clear_awaiting_resumable(task_reloaded, TASKS_DIR, new_status="active")
        pid = _resume_engine_if_idle(task_id, task_dir)
    except Exception as exc:
        logger.warning("approve respawn skipped: %r", exc)

    logger.info(f"Subtask {body.subtask_id} in {task_id}: {body.action}d")
    return {
        "status": "ok", "subtask_id": body.subtask_id,
        "action": body.action, "engine_pid": pid,
    }


class BatchApprovalRequest(BaseModel):
    """Audit finding #23 — clear all pending approvals in one round-trip.

    Two modes (mutually exclusive — pick one per request):
      - approve_all: True/False — bulk approve or skip every subtask currently
        in waiting_approval state.
      - approvals: {subtask_id: "approve" | "skip"} — fine-grained per-subtask
        decisions. Subtasks not present in the dict are left as-is.
    """
    approve_all: Optional[bool] = None
    approvals: Optional[Dict[str, str]] = None


@app.post("/api/tasks/{task_id}/approve-batch")
# Sync `def`, deliberately: this body has ZERO awaits and does blocking
# work (yaml read-modify-write, file-locked task.yaml writes, a process
# spawn). Starlette runs `async def` path-ops ON the event loop, so as
# `async def` it stalled the same loop serving /api/tasks/{id}/state, the
# /ws push and the inline /mcp/v1 mount. Sync path-ops go to the
# threadpool instead. Same remedy already applied to the hot READ
# endpoints (active_roles / get_task_state_endpoint / get_task_snapshot);
# the write half was never swept.
def approve_subtasks_batch(task_id: str, body: BatchApprovalRequest):
    """Resolve every pending HIGH/MED approval for a task in one call.

    Audit #23 — previously each blocked subtask required its own POST
    /api/tasks/{id}/approve round-trip, so a 5-subtask plan with 3 HIGH-risk
    items took 3 user turns to clear. This endpoint writes all decisions to
    plan.yaml at once; the engine's wait_for_approval loop polls plan.yaml
    every 2s and finds its decision already set.

    Bearer auth is enforced by the global BearerAuthMiddleware on /api/*.
    """
    _validate_task_id(task_id)
    import yaml as _yaml

    task_dir = TASKS_DIR / task_id
    plan_path = task_dir / "plan.yaml"
    log_path = task_dir / "log.jsonl"

    if not plan_path.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    if body.approve_all is None and not body.approvals:
        raise HTTPException(400, "Provide either approve_all (bool) or approvals (dict)")
    if body.approve_all is not None and body.approvals:
        raise HTTPException(400, "approve_all and approvals are mutually exclusive — pick one")

    with open(plan_path) as f:
        plan = yload(f)

    cleared: list[dict[str, str]] = []
    unknown: list[str] = []

    if body.approve_all is not None:
        new_status = "approved" if body.approve_all else "skipped"
        action_label = "approve" if body.approve_all else "skip"
        for phase in plan.get("phases", []):
            for st in phase.get("subtasks", []):
                if st.get("status") == "waiting_approval":
                    st["status"] = new_status
                    cleared.append({"subtask_id": st["id"], "action": action_label})
    else:
        # Per-subtask mode. Validate every action up-front so a typo doesn't
        # leave the plan half-mutated.
        for sub_id, act in body.approvals.items():
            if act not in ("approve", "skip"):
                raise HTTPException(400, f"Action for {sub_id} must be 'approve' or 'skip', got {act!r}")

        # Build a quick lookup so we can report unknown ids back to the caller
        # instead of silently dropping them — matches the dispatcher's general
        # "fail loud, don't ghost-succeed" rule.
        all_ids = {st["id"] for ph in plan.get("phases", []) for st in ph.get("subtasks", [])}
        for sub_id in body.approvals:
            if sub_id not in all_ids:
                unknown.append(sub_id)
        if unknown:
            raise HTTPException(404, f"Unknown subtask_id(s): {unknown}")

        for phase in plan.get("phases", []):
            for st in phase.get("subtasks", []):
                if st["id"] in body.approvals and st.get("status") == "waiting_approval":
                    act = body.approvals[st["id"]]
                    new_status = "approved" if act == "approve" else "skipped"
                    st["status"] = new_status
                    cleared.append({"subtask_id": st["id"], "action": act})

    with open(plan_path, "w") as f:
        _yaml.dump(plan, f, default_flow_style=False)

    event = {
        "type": "approval_batch",
        "cleared": cleared,
        "ts": datetime.utcnow().isoformat(),
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(event) + "\n")

    # Park = engineless: respawn a fresh --resume engine to consume the
    # resolved approvals (the parked engine exited rather than polling).
    pid = None
    try:
        from okuro.orchestrator.state import load_task as _load_task, clear_awaiting
        task_reloaded = _load_task(task_id, TASKS_DIR)
        if getattr(task_reloaded, "awaiting", None) is not None and \
                task_reloaded.awaiting.kind == "subtask_approval":
            _clear_awaiting_resumable(task_reloaded, TASKS_DIR, new_status="active")
        pid = _resume_engine_if_idle(task_id, task_dir)
    except Exception as exc:
        logger.warning("approve-batch respawn skipped: %r", exc)

    logger.info(f"Batch approval on {task_id}: cleared {len(cleared)} subtask(s)")
    return {
        "status": "ok", "task_id": task_id, "cleared": cleared,
        "count": len(cleared), "engine_pid": pid,
    }


# -- M1 Decision Gate Endpoints --


class GateResolveRequest(BaseModel):
    """Body for POST /api/tasks/{id}/gates/{gate_id}/resolve.

    Exactly one of `selected_option_id` or `skipped=True` is required.
    `rationale` is optional and stored in the ADR + injected into the
    Locked Decisions block of every downstream subagent brief.
    """
    selected_option_id: Optional[str] = None
    rationale: Optional[str] = ""
    skipped: bool = False


@app.get("/api/tasks/{task_id}/gates")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def list_gates(task_id: str):
    """List every decision gate on a task with its current status."""
    _validate_task_id(task_id)
    from okuro.orchestrator.state import load_task
    if not (TASKS_DIR / task_id).exists():
        raise HTTPException(404, f"Task {task_id} not found")
    task = load_task(task_id, TASKS_DIR)
    gates = []
    for phase in task.phases:
        gate = getattr(phase, "decision_gate", None)
        if gate is None:
            continue
        gates.append({
            "gate_id": gate.id,
            "phase_id": phase.id,
            "phase_name": phase.name,
            "status": gate.status,
            "prompt": gate.prompt,
            "options": [
                {
                    "id": o.id, "label": o.label, "description": o.description,
                    "pros": o.pros, "cons": o.cons, "risk": o.risk,
                    "recommended": o.recommended,
                }
                for o in gate.options
            ],
            "selected_option_id": gate.selected_option_id,
            "selected_rationale": gate.selected_rationale,
            "resolved_at": gate.resolved_at,
        })
    return {"task_id": task_id, "gates_enabled": task.gates_enabled, "gates": gates}


@app.post("/api/tasks/{task_id}/gates/{gate_id}/resolve")
# Sync `def`, deliberately: this body has ZERO awaits and does blocking
# work (yaml read-modify-write, file-locked task.yaml writes, a process
# spawn). Starlette runs `async def` path-ops ON the event loop, so as
# `async def` it stalled the same loop serving /api/tasks/{id}/state, the
# /ws push and the inline /mcp/v1 mount. Sync path-ops go to the
# threadpool instead. Same remedy already applied to the hot READ
# endpoints (active_roles / get_task_state_endpoint / get_task_snapshot);
# the write half was never swept.
def resolve_gate(task_id: str, gate_id: str, body: GateResolveRequest):
    """Lock a user decision into a pending decision gate.

    The engine's headless wait_for_decision poll picks up the resolution
    on its next 2s tick. ADR (when not skipped) is appended to task.adrs
    and injected into every downstream subagent brief.
    """
    _validate_task_id(task_id)
    from okuro.orchestrator.state import load_task, resolve_decision_gate

    if not (TASKS_DIR / task_id).exists():
        raise HTTPException(404, f"Task {task_id} not found")

    if not body.skipped and not body.selected_option_id:
        raise HTTPException(
            400,
            "Either selected_option_id or skipped=true is required",
        )

    task = load_task(task_id, TASKS_DIR)
    target_phase_id: Optional[int] = None
    for phase in task.phases:
        gate = getattr(phase, "decision_gate", None)
        if gate is not None and gate.id == gate_id:
            target_phase_id = phase.id
            break
    if target_phase_id is None:
        raise HTTPException(404, f"Gate {gate_id} not found on task {task_id}")

    try:
        adr = resolve_decision_gate(
            task_id, target_phase_id,
            body.selected_option_id or "",
            body.rationale or "",
            TASKS_DIR,
            skipped=body.skipped,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))

    try:
        task_reloaded = load_task(task_id, TASKS_DIR)
        if task_reloaded.awaiting is not None and task_reloaded.awaiting.kind == "decision_gate":
            _clear_awaiting_resumable(task_reloaded, TASKS_DIR, new_status="active")
            # C8 — if the engine DIED while parked at the decision gate, the
            # in-loop poll that normally wakes on resolve is gone. Respawn so
            # recovery doesn't depend on the daemon cron (≤2 min) or the daemon
            # being alive. Idempotent: a no-op when an engine is already running
            # (the happy path — its own poll loop picks up the resolution).
            _resume_engine_if_idle(task_id, TASKS_DIR)
    except Exception as exc:
        logger.warning("gate.resolve clear_awaiting skipped: %r", exc)

    return {
        "status": "resolved" if not body.skipped else "skipped",
        "task_id": task_id,
        "gate_id": gate_id,
        "phase_id": target_phase_id,
        "adr": adr,
    }


class BlockedReviewActionRequest(BaseModel):
    """Body for the override-blocked-review endpoints.

    The reviewer parks a phase in blocked_review when M3 FAILs and the
    implicated work is at the round cap. The user needs MORE than a single
    "acknowledge & move on" — they may want to re-run the flagged work so it
    can fix the finding (the reviewer re-judges the fresh artifact):

      - ``action="override"`` (default, backward-compat): mark the capped
        subtask ``skipped`` / the phase ``done`` and move on.
      - ``action="retry"``: reset the implicated subtasks (and their phase)
        to ``pending`` so the engine re-dispatches + re-reviews them.

      - ``action="decide"``: the user ADJUDICATES a NEEDS_USER finding. The
        decision is locked as an ADR (durable + enforced by the deterministic
        term check) and injected into a SCOPED re-dispatch as authoritative
        guidance, so the agent applies the call instead of re-inventing one
        each round. Requires ``decision``; ``subtask_id`` scopes it (defaults
        to the first capped subtask). ``chosen_label``/``rejected_labels``
        (optional) make the ADR drive the deterministic drift check.

    ``subtask_ids`` scopes a retry to specific subtasks (the FE passes the
    blocker payload's ``capped_subtasks``). Empty → every done/failed/skipped
    subtask in the blocked phase is reset.
    """
    action: str = "override"
    subtask_ids: list[str] = []
    # decide-action fields
    decision: str = ""
    rationale: str = ""
    subtask_id: str = ""
    chosen_label: str = ""
    rejected_labels: list[str] = []


# Error markers a review/CAP escalation stamps on a parked subtask. Shared by
# the override + retry paths so both recognise the same "this was blocked by
# review, not by a real failure" subtasks (DP10 — one matcher, not three copies).
_OVERRIDE_ACCEPT_NOTE = (
    "[review overridden by user — reviewer FAIL accepted; "
    "deliverable retained as-is]"
)
_OVERRIDE_FAILURE_ACCEPTED_NOTE = (
    "[failure accepted by user — no deliverable was produced; task continued]"
)


def _subtask_has_deliverable(task_id: str, subtask_id: str) -> bool:
    """True when the subagent shipped at least one Stream B artifact.

    This is the EVIDENCE the override path branches on. It replaces
    _is_review_cap_error, which guessed whether a deliverable existed by
    substring-matching the error text for markers like "review cap". That guess
    was wrong in both directions, and its failure was silent: on
    task-20260724-110216 subtask 4.2 the error read "Subagent self-reported
    outcome='partial'", matched no marker, and the user's Override therefore
    discarded a subtask that HAD shipped both an artifact and a handover. The
    reconciler even logged the contradiction ("status='skipped' on disk but
    Stream B row present") and could do nothing about it.

    Asking the artifact store is the same question, answered instead of
    guessed. Reuses the engine's helper so the ghost-completion check and the
    override agree by construction.
    """
    try:
        from okuro.orchestrator.engine import _has_brain_artifact
        return _has_brain_artifact(task_id, subtask_id)
    except Exception as exc:
        # Conservative: unknown evidence means we do NOT claim a deliverable
        # exists, so the subtask terminalizes as `failed` (honest) rather than
        # being falsely promoted to `done`.
        logger.warning(
            "override deliverable check failed for %s/%s: %r", task_id, subtask_id, exc,
        )
        return False


def _retry_reset_phase(
    phase, only_ids: set, *, task_id: str = "", findings: Optional[list] = None,
) -> list:
    """Reset a blocked_review phase + its flagged subtasks to ``pending`` for a
    SURGICAL re-run.

    Operates on the in-memory ``Phase`` dataclass (NOT a raw plan.yaml dict) so
    the caller persists via ``save_plan`` — which re-derives every execution
    node in ``task.graph`` from the mutated subtask/phase. Raw-yaml writes here
    previously bypassed that derivation, leaving deliberate-mode DAG nodes stale
    so the override silently re-dispatched the resolved subtask.

    Review-ledger (Phase A): the reset is no longer context-destroying. Each
    reset subtask gets the reviewer's ``findings`` stamped onto
    ``review_findings`` so ``build_role_prompt`` leads the next brief with a
    fix-these-exactly worklist (surgical patch, not blind regeneration — the
    root cause of identical findings recurring on every retry). We also
    supersede the subtask's prior brain artifacts + role handovers so the
    re-run is not flagged for contradicting its own stale output — the same
    loop-hygiene the engine's automatic retry already runs.

    Returns the list of subtask ids reset. ``only_ids`` (possibly empty) scopes
    which subtasks are reset; empty means every terminal subtask in the phase.
    """
    phase.status = "pending"
    findings = list(findings or [])
    reset: list = []
    for st in phase.subtasks or []:
        sid = str(st.id)
        if only_ids and sid not in only_ids:
            continue
        if st.status in ("done", "failed", "skipped") or (
            getattr(st, "review_state", "") == "failed"
        ):
            st.status = "pending"
            st.retries = 0
            st.error = ""
            # Clear the review lock — the re-run is entitled to a fresh
            # verdict, and leaving it `failed` would hold the dispatch guard
            # shut against the very retry the user just asked for.
            st.review_state = "not_reviewed"
            # Carry the reviewer findings into the next dispatch (surgical fix).
            # Only OVERWRITE when this block actually carried findings — a
            # NEEDS_USER re-block has no critic_findings in its payload, so an
            # empty list here must PRESERVE the worklist already stamped from
            # the prior cap (else the next retry goes blind again).
            if findings:
                st.review_findings = list(findings)
            reset.append(sid)
            # Loop-hygiene: supersede prior artifacts + handovers so the re-run
            # is not flagged for contradicting its own stale output.
            if task_id:
                try:
                    from okuro.sense.artifacts import artifact_supersede_subtask
                    artifact_supersede_subtask(task_id, sid)
                except Exception as exc:
                    logger.warning(
                        "retry-supersede artifacts failed for %s/%s: %s",
                        task_id, sid, exc,
                    )
                try:
                    from okuro.sense.role_handover import (
                        supersede_role_handovers_for_subtask,
                    )
                    supersede_role_handovers_for_subtask(task_id, sid)
                except Exception as exc:
                    logger.warning(
                        "retry-supersede handovers failed for %s/%s: %s",
                        task_id, sid, exc,
                    )
    return reset


def _override_accept_capped_subtasks(phase, task_id: str = "") -> list:
    """Override: accept a parked subtask as-is, branching on EVIDENCE.

    The UI's Override button promises "accept it as-is and continue". This now
    means that in both branches:

      * a deliverable exists  → ``done`` + review-overridden note. The work ran
        and shipped; only the REVIEW is being overridden.
      * no deliverable exists → stays ``failed`` + failure-accepted note. The
        work genuinely did not succeed, and the plan says so.

    Neither branch writes ``skipped``. Pre-split it had to: ``failed`` was not
    terminal, so leaving it failed wedged is_task_complete forever, and
    ``skipped`` was the only terminal status available. Now ``failed`` is in
    FINISHED_STATUSES, so honesty is free — and ``skipped`` stops meaning three
    incompatible things, which is what made the whole class unrecoverable.

    A no-op for M3-capped subtasks already ``done``. Operates on the ``Phase``
    dataclass; caller persists via ``save_plan`` to re-derive the DAG node.
    Returns the accepted subtask ids.
    """
    accepted: list = []
    for st in phase.subtasks or []:
        unresolved_review = getattr(st, "review_state", "") == "failed"
        if st.status != "failed" and not unresolved_review:
            continue
        sid = str(st.id)
        if st.status == "done" or _subtask_has_deliverable(task_id, sid):
            # The work shipped. Overriding settles the REVIEW, nothing else —
            # status stays/becomes `done`, so dependents run normally and no
            # later phase is stranded by a review the human already resolved.
            st.status = "done"
            st.error = ""
            note = _OVERRIDE_ACCEPT_NOTE
        else:
            # Genuinely produced nothing. Terminal, so nothing wedges, and the
            # run reports `completed_partial` rather than a clean `done`.
            note = _OVERRIDE_FAILURE_ACCEPTED_NOTE
        st.review_state = "overridden"
        st.output_summary = (
            (st.output_summary or "").rstrip() + "\n" + note
        ).strip()
        accepted.append(sid)
    return accepted


def _resume_engine_if_idle(task_id: str, task_dir: Path):
    """Flip task.yaml to active + spawn a fresh --resume engine when none is
    alive. Shared by the override/retry paths. Returns the spawned pid or None.
    """
    import yaml as _yaml
    if _is_orchestrator_running(task_id):
        return None
    # Second guard, for the paths that reach here with `awaiting` already None
    # (nothing to clear, so _clear_awaiting_resumable never ran) but a spawn
    # still in flight. Flipping task.yaml to `active` below is precisely what
    # makes the task eligible for the GET-path force-halt, so mark first.
    _mark_human_resume(task_dir)
    task_yaml = task_dir / "task.yaml"
    try:
        with open(task_yaml) as f:
            tdata = yload(f)
        tdata["status"] = "active"
        # P4.10 — see couple_task_dict. Flipping to active while `awaiting`
        # survives is the resume-is-a-no-op shape: the respawned engine's
        # _is_human_park sees the gate and parks again on its first tick.
        from okuro.orchestrator.state import couple_task_dict
        with open(task_yaml, "w") as f:
            _yaml.dump(couple_task_dict(tdata), f, default_flow_style=False)
    except Exception:
        pass
    cmd = [sys.executable, "-m", "okuro.orchestrator.engine", "--resume", task_id]
    return spawn_orchestrator(cmd, task_id)


@app.post("/api/tasks/{task_id}/phases/{phase_id}/override-blocked-review")
# Sync `def`, deliberately: this body has ZERO awaits and does blocking
# work (yaml read-modify-write, file-locked task.yaml writes, a process
# spawn). Starlette runs `async def` path-ops ON the event loop, so as
# `async def` it stalled the same loop serving /api/tasks/{id}/state, the
# /ws push and the inline /mcp/v1 mount. Sync path-ops go to the
# threadpool instead. Same remedy already applied to the hot READ
# endpoints (active_roles / get_task_state_endpoint / get_task_snapshot);
# the write half was never swept.
def override_blocked_review(
    task_id: str,
    phase_id: int,
    body: Optional[BlockedReviewActionRequest] = None,
):
    """Resolve a phase parked in blocked_review — override (skip) OR retry.

    The engine flips a phase to blocked_review when M3 returns FAIL and
    every implicated subtask is at max_retries. The audit playbook P0-A
    requires an explicit user surface. ``action="override"`` flips the phase
    to done (acknowledge the FAIL, move on); ``action="retry"`` resets the
    flagged subtasks to pending so the work re-runs and is re-reviewed.
    """
    _validate_task_id(task_id)
    from okuro.orchestrator.state import (
        load_task as _load, save_plan as _save_plan, clear_awaiting,
    )

    req = body or BlockedReviewActionRequest()
    action = (req.action or "override").lower()

    task_dir = TASKS_DIR / task_id
    if not task_dir.exists() or not (task_dir / "plan.yaml").exists():
        raise HTTPException(404, f"Task {task_id} not found")

    task = _load(task_id, TASKS_DIR)
    target_phase = next(
        (ph for ph in task.phases if int(getattr(ph, "id", 0)) == int(phase_id)),
        None,
    )
    if target_phase is None:
        raise HTTPException(404, f"Phase {phase_id} not on task {task_id}")
    if target_phase.status != "blocked_review":
        raise HTTPException(
            409,
            f"Phase {phase_id} status is {target_phase.status!r}, "
            "expected 'blocked_review'",
        )

    # Review-ledger: the reviewer's structured findings live on the awaiting
    # payload — carry them into the surgical retry instead of discarding them.
    _aw_findings: list = []
    try:
        if getattr(task, "awaiting", None) is not None:
            _aw_findings = list((task.awaiting.payload or {}).get("critic_findings") or [])
    except Exception:
        _aw_findings = []

    reset_subtasks: list = []
    _persisted_by_helper = False
    if action == "retry":
        reset_subtasks = _retry_reset_phase(
            target_phase, set(req.subtask_ids),
            task_id=task_id, findings=_aw_findings,
        )
        event_type = "phase_retry_blocked_review"
    elif action == "decide":
        from okuro.orchestrator.state import lock_blocked_review_decision
        if not (req.decision or "").strip():
            raise HTTPException(422, "action='decide' requires a non-empty 'decision'")
        _cap = []
        if getattr(task, "awaiting", None) is not None:
            _cap = list((task.awaiting.payload or {}).get("capped_subtasks") or [])
        # P3 (#11) — the decision targets the subtask the FINDINGS concern, not
        # roster order. Pre-fix this silently fell back to _cap[0], so on a
        # multi-subtask phase a free-text decision landed on the first capped
        # subtask (e.g. people-registration 1.1) while the findings were about a
        # different one (1.3) — the user's steering hit the wrong author. Now
        # that critic + term findings carry subtask_id (P3 #9/#10), derive the
        # owner from the carried findings: explicit request wins; else the unique
        # finding-owner; else a single capped subtask; else the most-implicated
        # finding-owner (LOGGED, never a silent roster guess).
        _finding_owner_counts: dict[str, int] = {}
        for _f in _aw_findings:
            _sid = (_f or {}).get("subtask_id")
            if _sid:
                _finding_owner_counts[str(_sid)] = _finding_owner_counts.get(str(_sid), 0) + 1
        _owners = list(_finding_owner_counts)
        _decide_sid = (
            req.subtask_id
            or (req.subtask_ids[0] if req.subtask_ids else "")
            or (_owners[0] if len(_owners) == 1 else "")
            or (_cap[0] if len(_cap) == 1 else "")
        )
        if not _decide_sid and _owners:
            # Genuinely ambiguous (multiple finding-owners, no explicit target):
            # pick the most-implicated owner and LOG it — never silent.
            _decide_sid = max(_finding_owner_counts, key=_finding_owner_counts.get)
            logger.warning(
                "decide: no explicit target on multi-owner phase %s task=%s; "
                "routing to most-implicated subtask %s (owners=%s). FE should "
                "send 'subtask_id' to disambiguate.",
                phase_id, task_id, _decide_sid, _finding_owner_counts,
            )
        elif not _decide_sid and len(_cap) > 1:
            # No finding owners AND multiple capped — last-resort roster guess,
            # logged so a wrong-subtask decision is never silent.
            _decide_sid = _cap[0]
            logger.warning(
                "decide: no explicit target and no finding owners on multi-capped "
                "phase %s task=%s; falling back to first capped subtask %s "
                "(capped=%s). FE should send 'subtask_id'.",
                phase_id, task_id, _decide_sid, _cap,
            )
        if not _decide_sid:
            raise HTTPException(422, "action='decide' could not resolve a target subtask_id")
        # lock_blocked_review_decision persists plan + meta under the task lock
        # (ADR → task.yaml, decision_guidance + scoped reset → plan.yaml).
        lock_blocked_review_decision(
            task_id, int(phase_id), _decide_sid, req.decision, TASKS_DIR,
            rationale=req.rationale, chosen_label=req.chosen_label,
            rejected_labels=req.rejected_labels, findings=_aw_findings,
        )
        # Loop-hygiene: supersede the subtask's stale artifacts + handovers so
        # the decided re-run is not flagged for contradicting its own old output.
        try:
            from okuro.sense.artifacts import artifact_supersede_subtask
            artifact_supersede_subtask(task_id, _decide_sid)
        except Exception as exc:
            logger.warning("decide-supersede artifacts failed for %s/%s: %s", task_id, _decide_sid, exc)
        try:
            from okuro.sense.role_handover import supersede_role_handovers_for_subtask
            supersede_role_handovers_for_subtask(task_id, _decide_sid)
        except Exception as exc:
            logger.warning("decide-supersede handovers failed for %s/%s: %s", task_id, _decide_sid, exc)
        reset_subtasks = [_decide_sid]
        event_type = "phase_decide_blocked_review"
        _persisted_by_helper = True
        # Reload so the awaiting-clear below operates on the helper's fresh state.
        task = _load(task_id, TASKS_DIR)
    else:
        target_phase.status = "done"
        reset_subtasks = _override_accept_capped_subtasks(target_phase, task_id)
        event_type = "phase_override_blocked_review"

    # Persist via save_plan — the canonical write boundary. It validates the
    # status enums AND re-derives every execution-node status in task.graph
    # from the mutated subtasks/phase, so deliberate-mode dispatch does not
    # re-pick the resolved node (the desync that re-ran the subtask after an
    # override). Replaces the prior raw-yaml dump, which skipped derivation.
    # Skipped for `decide` — lock_blocked_review_decision already persisted
    # under the task lock; a second save here would clobber it with stale state.
    if not _persisted_by_helper:
        _save_plan(task, TASKS_DIR)

    log_path = task_dir / "log.jsonl"
    try:
        with open(log_path, "a") as f:
            f.write(json.dumps({
                "type": event_type,
                "phase_id": phase_id,
                "reset_subtasks": reset_subtasks,
                "ts": datetime.utcnow().isoformat(),
            }) + "\n")
    except Exception:
        pass

    if task.awaiting is not None and task.awaiting.kind == "blocked_review":
        try:
            _clear_awaiting_resumable(task, TASKS_DIR, new_status="active")
        except Exception as exc:
            logger.warning("blocked_review resolve clear_awaiting skipped: %r", exc)

    pid = _resume_engine_if_idle(task_id, task_dir)

    return {
        "status": "retrying" if action == "retry" else "advanced",
        "task_id": task_id,
        "phase_id": phase_id,
        "reset_subtasks": reset_subtasks,
        "engine_pid": pid,
    }


@app.post("/api/tasks/{task_id}/override-blocked-review")
# Sync `def`, deliberately: this body has ZERO awaits and does blocking
# work (yaml read-modify-write, file-locked task.yaml writes, a process
# spawn). Starlette runs `async def` path-ops ON the event loop, so as
# `async def` it stalled the same loop serving /api/tasks/{id}/state, the
# /ws push and the inline /mcp/v1 mount. Sync path-ops go to the
# threadpool instead. Same remedy already applied to the hot READ
# endpoints (active_roles / get_task_state_endpoint / get_task_snapshot);
# the write half was never swept.
def override_blocked_review_task(
    task_id: str,
    body: Optional[BlockedReviewActionRequest] = None,
):
    """Task-level blocked_review resolve (auto-locates the block).

    The phase-scoped variant above needs the exact phase id. But escalation
    paths write the awaiting `endpoint` in two shapes — phase-scoped
    (M3 phase-gate) and task-level (session-loop CAP / genuinely-stuck) — so
    a task-level POST must also work or the UI's CTA 405s. This finds every
    blocked_review phase AND every subtask parked by a review/CAP escalation
    and resolves them per ``action``:
      - ``override``: accept the deliverable despite the FAIL (P0-A) —
        phase→done, capped subtask→done with an output_summary audit note
        (the work ran; only the REVIEW is overridden, NOT silently passed).
      - ``retry``: reset the blocked phase(s) + their flagged subtasks to
        pending so the work re-runs and is re-reviewed.
    """
    _validate_task_id(task_id)
    from okuro.orchestrator.state import (
        load_task as _load, save_plan as _save_plan, clear_awaiting,
    )

    req = body or BlockedReviewActionRequest()
    action = (req.action or "override").lower()
    only_ids = set(req.subtask_ids)

    task_dir = TASKS_DIR / task_id
    if not task_dir.exists() or not (task_dir / "plan.yaml").exists():
        raise HTTPException(404, f"Task {task_id} not found")

    task = _load(task_id, TASKS_DIR)

    advanced_phases: list = []
    overridden_subtasks: list = []
    if action == "retry":
        # Re-run flagged work: reset every blocked_review phase + its subtasks
        # (scoped by only_ids when given) to pending. Also revive subtasks a
        # CAP escalation parked as failed so the whole flagged unit re-runs.
        for ph in task.phases:
            if ph.status == "blocked_review":
                overridden_subtasks.extend(_retry_reset_phase(ph, only_ids))
                advanced_phases.append(ph.id)
            else:
                for st in ph.subtasks or []:
                    sid = str(st.id)
                    if only_ids and sid not in only_ids:
                        continue
                    # Retry revives ANY failed subtask in scope. Pre-split
                    # this was gated on _is_review_cap_error, so a plain
                    # content failure could not be retried from the task-level
                    # CTA at all — the same substring guess that mis-routed
                    # override. An explicit user retry needs no marker.
                    if st.status == "failed":
                        st.status = "pending"
                        st.retries = 0
                        st.error = ""
                        # Clear the review lock, same as _retry_reset_phase
                        # does for the blocked_review branch above. Without it
                        # a failed review holds the dispatch guard shut against
                        # the retry the user just asked for.
                        st.review_state = "not_reviewed"
                        overridden_subtasks.append(sid)
        event_type = "phase_retry_blocked_review"
    else:
        for ph in task.phases:
            if ph.status == "blocked_review":
                ph.status = "done"
                advanced_phases.append(ph.id)
            overridden_subtasks.extend(
                _override_accept_capped_subtasks(ph, task_id)
            )
        event_type = "phase_override_blocked_review"

    if not advanced_phases and not overridden_subtasks:
        # No blocked_review phase and no review-capped subtask — but the task
        # may still be parked on a genuine `blocked_review` awaiting that fires
        # on a dependency/orphan deadlock (no_subtasks_ready), NOT a review
        # FAIL. That awaiting names THIS endpoint as its only CTA, so 409-ing
        # here is the dead-end: the user's one button cannot clear the block.
        # Honour it — fall through to clear_awaiting + resume below. The
        # respawned engine's reconcile_on_startup revives any subtask orphaned
        # in 'running', so dispatch can proceed. Only 409 when there is truly
        # nothing to resolve (no such awaiting).
        aw = getattr(task, "awaiting", None)
        if aw is None or getattr(aw, "kind", "") != "blocked_review":
            raise HTTPException(
                409, f"Task {task_id} has no blocked_review phase or review-capped subtask to resolve",
            )
        event_type = "phase_override_blocked_review"

    # Canonical write boundary — validates enums AND re-derives task.graph
    # execution-node status from the mutated subtasks/phases (deliberate-mode
    # desync fix). Replaces the prior raw-yaml dump that skipped derivation.
    _save_plan(task, TASKS_DIR)

    log_path = task_dir / "log.jsonl"
    try:
        with open(log_path, "a") as f:
            f.write(json.dumps({
                "type": event_type,
                "scope": "task",
                "advanced_phases": advanced_phases,
                "overridden_subtasks": overridden_subtasks,
                "ts": datetime.utcnow().isoformat(),
            }) + "\n")
    except Exception:
        pass

    if task.awaiting is not None and task.awaiting.kind == "blocked_review":
        try:
            _clear_awaiting_resumable(task, TASKS_DIR, new_status="active")
        except Exception as exc:
            logger.warning("task-level blocked_review resolve clear_awaiting skipped: %r", exc)

    pid = _resume_engine_if_idle(task_id, task_dir)

    return {
        "status": "retrying" if action == "retry" else "advanced",
        "task_id": task_id,
        "advanced_phases": advanced_phases,
        "overridden_subtasks": overridden_subtasks,
        "engine_pid": pid,
    }


class TimeoutCapResolveRequest(BaseModel):
    """Body for /api/tasks/{id}/subtasks/{sub_id}/timeout-cap-resolve.

    ``action`` picks the resolution path; ``extended_timeout_s`` only
    matters for ``extend_and_retry``. The default extension is resolved
    from the per-role base (+ a large bump) instead of a hard-coded
    constant so the same endpoint serves a 600 s ``qa-engineer`` and a
    1200 s ``linux-audio-engineer`` proportionally (DP10).
    """
    action: str  # extend_and_retry | mark_done | skip | permanent_fail
    extended_timeout_s: Optional[int] = None


def _default_extended_timeout(role: str) -> int:
    """Resolve the default ``extend_and_retry`` wall-clock for a role.

    Uses the dispatcher's per-role base + a large bump (2 * cap) so the
    extension is meaningfully bigger than the legacy retry ladder. Falls
    back to 1800 s when the dispatcher helper is unavailable.
    """
    try:
        from okuro.orchestrator.dispatcher import (
            _PER_ROLE_TIMEOUT_BASE as _BASE,
            _DEFAULT_TIMEOUT_BASE as _DEF,
            _RETRY_TIMEOUT_BUMP_CAP as _CAP,
        )
        base = _BASE.get(role or "", _DEF)
        return int(base + 2 * _CAP)
    except Exception:
        return 1800


@app.post("/api/tasks/{task_id}/subtasks/{subtask_id}/timeout-cap-resolve")
# Sync `def`, deliberately: this body has ZERO awaits and does blocking
# work (yaml read-modify-write, file-locked task.yaml writes, a process
# spawn). Starlette runs `async def` path-ops ON the event loop, so as
# `async def` it stalled the same loop serving /api/tasks/{id}/state, the
# /ws push and the inline /mcp/v1 mount. Sync path-ops go to the
# threadpool instead. Same remedy already applied to the hot READ
# endpoints (active_roles / get_task_state_endpoint / get_task_snapshot);
# the write half was never swept.
def resolve_subtask_timeout_cap(
    task_id: str, subtask_id: str, body: TimeoutCapResolveRequest,
):
    """Resolve a subtask parked in the ``timeout_cap`` awaiting state.

    Four actions, one endpoint:
      * ``extend_and_retry`` — stage a one-shot wall-clock override in
        ``<task_dir>/.timeout-overrides.json`` (dispatcher reads + drops
        on next dispatch), then reset the subtask to ``pending`` with
        ``retries=0`` so the C5 guard releases it.
      * ``mark_done`` — flip to ``done`` immediately; downstream
        dependents proceed on their normal schedule.
      * ``skip`` — flip to ``skipped``. A SATISFIED status, so dependents
        stay dispatchable: an explicit skip means "proceed without this".
      * ``permanent_fail`` — mark ``failed`` and emit
        ``subtask_failed_permanently``. Dependents stay ``pending`` and are
        derived as blocked-upstream; retrying this subtask green revives them.

    All four clear the ``timeout_cap`` awaiting block (status flips back
    to ``active``) and respawn the engine if it has exited.
    """
    _validate_task_id(task_id)
    import yaml as _yaml

    task_dir = TASKS_DIR / task_id
    plan_path = task_dir / "plan.yaml"
    if not task_dir.exists() or not plan_path.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    action = (body.action or "").strip().lower()
    if action not in {"extend_and_retry", "mark_done", "skip", "permanent_fail"}:
        raise HTTPException(
            422,
            f"action={body.action!r} not one of "
            "extend_and_retry / mark_done / skip / permanent_fail",
        )

    # Locate the subtask + its role (needed for the default extension).
    from okuro.orchestrator.state import (
        load_task as _load_task,
        update_subtask as _update_subtask,
        append_log as _append_log,
    )
    task = _load_task(task_id, TASKS_DIR)
    target_subtask = None
    for ph in task.phases:
        for st in ph.subtasks:
            if st.id == subtask_id:
                target_subtask = st
                break
        if target_subtask:
            break
    if target_subtask is None:
        raise HTTPException(404, f"Subtask {subtask_id} not on task {task_id}")

    now_iso = datetime.utcnow().isoformat()

    if action == "extend_and_retry":
        # Resolve the timeout: explicit value > per-role default.
        if body.extended_timeout_s and int(body.extended_timeout_s) > 0:
            new_timeout = int(body.extended_timeout_s)
        else:
            new_timeout = _default_extended_timeout(target_subtask.role or "")
        # Stage the sidecar override. Dispatcher one-shot-consumes on
        # next dispatch via _read_timeout_override / _consume_timeout_override.
        sidecar = task_dir / ".timeout-overrides.json"
        try:
            existing = {}
            if sidecar.exists():
                existing = json.loads(sidecar.read_text(encoding="utf-8") or "{}")
                if not isinstance(existing, dict):
                    existing = {}
            existing[subtask_id] = new_timeout
            sidecar.write_text(json.dumps(existing), encoding="utf-8")
        except Exception as exc:
            raise HTTPException(500, f"Failed to stage timeout override: {exc}")
        # Reset retries so C5's max_retries guard releases the subtask.
        _update_subtask(task_id, subtask_id, {
            "status": "pending",
            "retries": 0,
            "error": "",
            "started_at": "",
            "completed_at": "",
        }, TASKS_DIR)
        _append_log(task_id, {
            "type": "subtask_timeout_cap_resolved",
            "subtask": subtask_id,
            "action": "extend_and_retry",
            "extended_timeout_s": new_timeout,
        }, TASKS_DIR)

    elif action == "mark_done":
        _update_subtask(task_id, subtask_id, {
            "status": "done",
            "completed_at": now_iso,
            "error": "",
        }, TASKS_DIR)
        _append_log(task_id, {
            "type": "subtask_done",
            "subtask": subtask_id,
            "via": "timeout_cap_mark_done",
        }, TASKS_DIR)
        _append_log(task_id, {
            "type": "subtask_timeout_cap_resolved",
            "subtask": subtask_id,
            "action": "mark_done",
        }, TASKS_DIR)

    elif action == "skip":
        _update_subtask(task_id, subtask_id, {
            "status": "skipped",
            "completed_at": now_iso,
            "error": "user skipped via timeout_cap resolve",
        }, TASKS_DIR)
        _append_log(task_id, {
            "type": "subtask_skipped",
            "subtask": subtask_id,
            "via": "timeout_cap_skip",
        }, TASKS_DIR)
        _append_log(task_id, {
            "type": "subtask_timeout_cap_resolved",
            "subtask": subtask_id,
            "action": "skip",
        }, TASKS_DIR)
        # No cascade write. `skipped` is a SATISFIED status, so dependents
        # remain dispatchable exactly as they were before — an explicit user
        # skip means "proceed without this", not "kill the subtree".

    else:  # permanent_fail
        _update_subtask(task_id, subtask_id, {
            "status": "failed",
            "completed_at": now_iso,
            "error": "user marked permanent_fail via timeout_cap resolve",
        }, TASKS_DIR)
        _append_log(task_id, {
            "type": "subtask_failed_permanently",
            "subtask": subtask_id,
            "via": "timeout_cap_permanent_fail",
            "retries": target_subtask.retries,
        }, TASKS_DIR)
        _append_log(task_id, {
            "type": "subtask_timeout_cap_resolved",
            "subtask": subtask_id,
            "action": "permanent_fail",
        }, TASKS_DIR)
        # Dependents stay `pending` and are derived as blocked-upstream. If the
        # user later retries this subtask green, they revive on their own.
        try:
            from okuro.orchestrator.state import blocked_upstream_ids
            _blocked = sorted(blocked_upstream_ids(_load_task(task_id, TASKS_DIR)))
            if _blocked:
                _append_log(task_id, {
                    "type": "subtasks_blocked_upstream",
                    "trigger": subtask_id,
                    "blocked_count": len(_blocked),
                    "blocked_ids": _blocked,
                    "note": "derived, not persisted — retrying the trigger revives these",
                }, TASKS_DIR)
        except Exception as exc:
            logger.warning(
                "timeout_cap permanent_fail blocked-upstream derive failed: %r", exc,
            )

    # Clear awaiting + flip status back to active so the engine resumes.
    try:
        task_reloaded = _load_task(task_id, TASKS_DIR)
        if task_reloaded.awaiting is not None and task_reloaded.awaiting.kind == "timeout_cap":
            _clear_awaiting_resumable(task_reloaded, TASKS_DIR, new_status="active")
    except Exception as exc:
        logger.warning("timeout_cap clear_awaiting skipped: %r", exc)

    pid = None
    if not _is_orchestrator_running(task_id):
        task_yaml = task_dir / "task.yaml"
        try:
            with open(task_yaml) as f:
                tdata = yload(f)
            tdata["status"] = "active"
            # P4.10 — coupled like the others. `_clear_awaiting_resumable`
            # usually ran just above, but it is wrapped in a try/except that
            # only warns, so "usually" is exactly the gap the invariant is
            # for. The guard test found this one after I judged it safe.
            from okuro.orchestrator.state import couple_task_dict
            with open(task_yaml, "w") as f:
                _yaml.dump(couple_task_dict(tdata), f, default_flow_style=False)
        except Exception:
            pass
        cmd = [sys.executable, "-m", "okuro.orchestrator.engine", "--resume", task_id]
        pid = spawn_orchestrator(cmd, task_id)

    return {
        "status": "resolved",
        "task_id": task_id,
        "subtask_id": subtask_id,
        "action": action,
        "engine_pid": pid,
    }


# -- Checkpoint Endpoints --

@app.get("/api/tasks/{task_id}/checkpoints")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_checkpoints(task_id: str):
    """List all checkpoints for a task."""
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    from okuro.orchestrator.checkpoint import list_checkpoints
    checkpoints = list_checkpoints(task_id, TASKS_DIR)
    return {
        "task_id": task_id,
        "checkpoints": [cp.to_dict() for cp in checkpoints],
        "count": len(checkpoints),
    }


@app.post("/api/tasks/{task_id}/restore/{checkpoint_id}")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def restore_task_checkpoint(task_id: str, checkpoint_id: str):
    """Restore task state from a checkpoint."""
    _validate_task_id(task_id)
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        raise HTTPException(404, f"Task {task_id} not found")

    if _is_orchestrator_running(task_id):
        raise HTTPException(409, f"Task {task_id} is currently running. Stop it before restoring.")

    from okuro.orchestrator.checkpoint import restore_checkpoint
    success, message = restore_checkpoint(task_id, checkpoint_id, TASKS_DIR)

    if not success:
        raise HTTPException(400, message)

    log_path = task_dir / "log.jsonl"
    event = {
        "type": "checkpoint_restored",
        "checkpoint_id": checkpoint_id,
        "message": message,
        "ts": datetime.utcnow().isoformat(),
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(event) + "\n")

    logger.info(f"Task {task_id} restored to checkpoint {checkpoint_id}")
    return {"status": "restored", "task_id": task_id, "checkpoint_id": checkpoint_id, "message": message}


# -- Viewer Profile Endpoint --

@app.get("/api/viewer-profile")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def viewer_profile(recipient: str = Query(default="self"), context: str = Query(default="personal")):
    """Resolve the full rendering profile for an output."""
    try:
        from okuro.sense.rendering import resolve_output_profile
        return resolve_output_profile(recipient=recipient, context=context)
    except Exception as e:
        logger.warning(f"viewer-profile failed: {e}")
        return _VIEWER_PROFILE_FALLBACK


_VIEWER_PROFILE_FALLBACK = {
    "structure": {
        "preset": "focuser",
        "visual": {"font_scale": 1.0, "contrast": "standard", "animation": "reduced", "size_contrast": "extreme", "whitespace": "spacious"},
        "layout": {"content_pattern": "drill-down", "hierarchy": "progressive-disclosure", "items_per_level": 7, "one_focus_per_screen": True},
        "information": {"context_treatment": "on-demand", "truncation": "title-only", "show_ids": False, "section_numbering": False},
        "interaction": {"action_language": "soft", "max_options": 3, "consistency": "strict", "edit_before_act": True, "navigation": "drill-in-out"},
    },
    # The shipped design system. Was `architecture-noir`, a v0 profile, with a
    # `paint_path` beside it that nothing ever read.
    "paint": "okuro-ds",
    "recipient": "self",
    "context": "personal",
}


@app.get("/api/tools")
# Sync `def`: this body has ZERO awaits. Starlette runs `async def`
# path-ops ON the event loop, so a synchronous body there stalls the same
# loop serving the /ws push and the inline /mcp/v1 mount subagents call.
# Sync path-ops go to the threadpool instead. Swept 2026-07-27 by AST
# (`tests/orchestrator/api/test_no_blocking_async_handlers.py` re-derives
# the set, so a new handler cannot reintroduce the pattern unnoticed).
def get_tools():
    """Get the tool registry."""
    if not TOOLS_REGISTRY.exists():
        return {"tools": {}, "role_defaults": {}}
    with open(TOOLS_REGISTRY) as f:
        registry = yload(f)
    return registry


@app.get("/api/tools/catalog")
async def get_tools_catalog():
    """Rich tool catalog for the /agents Tools tab.

    Introspects the okuro MCP registry (`okuro.mcp._registry`) to return
    every tool's name, namespace, description, and input schema. Also
    returns a namespace→description map and a tool→[role_id] reverse
    lookup derived from each role's `tools` JSON column.

    Response shape:
    {
      "tools": [{"name", "namespace", "description", "input_schema"}, ...],
      "namespaces": {"sense": "...", "cortex": "...", ...},
      "tool_roles": {"write_memory": ["role_a", "role_b"], ...},
      "count": 78
    }
    """
    try:
        from okuro.mcp._registry import list_tools_impl, _dispatch
        tools = await list_tools_impl()
    except Exception as e:
        logger.warning(f"Tool catalog introspection failed: {e}")
        return {"tools": [], "namespaces": {}, "tool_roles": {}, "count": 0}

    # Namespace descriptions come from the canon yaml if available.
    ns_descriptions: dict[str, str] = {}
    try:
        from okuro.canon import registry as canon_registry
        canon_tools = canon_registry.list_tools()
        for entry in canon_tools:
            for mod_key, mod_info in (entry.get("modules") or {}).items():
                desc = mod_info.get("description", "")
                if desc and mod_key not in ns_descriptions:
                    ns_descriptions[mod_key] = desc
    except Exception as e:
        logger.debug(f"Canon namespace enrichment skipped: {e}")

    # Build tool→roles reverse lookup from okuro.db `roles` table.
    tool_roles: dict[str, list[str]] = {}
    try:
        from okuro.db import get_db
        db = get_db()
        rows = db.fetchall("SELECT role_id, tools FROM roles")
        for row in rows:
            role_id = row["role_id"]
            raw = row["tools"] or "[]"
            try:
                tool_list = json.loads(raw) if isinstance(raw, str) else (raw or [])
            except (json.JSONDecodeError, TypeError):
                tool_list = []
            if not isinstance(tool_list, list):
                continue
            for t in tool_list:
                if not isinstance(t, str):
                    continue
                tool_roles.setdefault(t, []).append(role_id)
    except Exception as e:
        logger.debug(f"Role→tools map skipped: {e}")

    # Assemble catalog list.
    catalog = []
    for t in tools:
        ns, _handler = _dispatch.get(t.name, ("unknown", None))
        catalog.append({
            "name": t.name,
            "namespace": ns,
            "description": t.description or "",
            "input_schema": t.inputSchema or {},
            "roles": sorted(tool_roles.get(t.name, [])),
        })

    catalog.sort(key=lambda e: (e["namespace"], e["name"]))

    return {
        "tools": catalog,
        "namespaces": ns_descriptions,
        "tool_roles": tool_roles,
        "count": len(catalog),
    }


# -- Roles Endpoints --
# Moved to okuro.orchestrator.api.roles (subagent #13). All list/detail/CRUD
# plus knowledge + maintenance live there now — this file only wires the router.


# -- WebSocket Endpoint --

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time task events.

    Joins the state pool: receives task-scoped events for tasks it has
    subscribed to, plus cross-task state_change notifications. Does NOT
    receive global agent_activity — that lives on /ws/activity.
    """
    if not _require_ws_auth(websocket):
        await websocket.close(code=1008, reason="bearer required")
        return
    await ws_manager.connect_state(websocket)

    try:
        await websocket.send_json({
            "type": _WS_ENVELOPE_CONNECTED,
            "timestamp": datetime.utcnow().isoformat(),
        })

        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "subscribe":
                task_id = data.get("task_id")
                channels = data.get("channels", [])
                # Replay-from-cursor: the client sends the highest snapshot
                # seq it has already applied. We skip pushing a snapshot it
                # already has (and any older one), so a reconnect replays
                # only what the client missed — no redundant payload, no
                # backwards push. Absent/0 → replay unconditionally.
                try:
                    client_last_seq = int(data.get("last_seq") or 0)
                except (TypeError, ValueError):
                    client_last_seq = 0

                if task_id:
                    await ws_manager.subscribe(websocket, task_id)

                for ch in channels:
                    if ch.startswith("task:"):
                        await ws_manager.subscribe(websocket, ch.split(":", 1)[1])

                await websocket.send_json({
                    "type": _WS_ENVELOPE_SUBSCRIBED,
                    "task_id": task_id,
                    "channels": channels,
                })

                # Subscribe-time state replay — push the current snapshot +
                # last-known orchestrator_state directly to this socket so a
                # fresh page load / reload during a mid-flight LLM call sees
                # current truth immediately instead of waiting for the next
                # broadcast (which may be minutes away during a long
                # decompose / critic / scorer call). DP10: structural — works
                # for ALL awaiting kinds + task states; the subscribe path is
                # a generic "give me current truth" handshake. Best-effort:
                # send failures here are logged but do not break the socket
                # loop.
                replay_targets = []
                if task_id:
                    replay_targets.append(task_id)
                for ch in channels:
                    if ch.startswith("task:"):
                        ch_task = ch.split(":", 1)[1]
                        if ch_task and ch_task not in replay_targets:
                            replay_targets.append(ch_task)

                for replay_task_id in replay_targets:
                    try:
                        # P3.5 — off the event loop. This is the WS handler (an
                        # async def), and read_task_snapshot is a sync function
                        # that re-reads task.yaml + plan.yaml + log.jsonl on a
                        # cache miss — once per replay target, on every
                        # subscribe. The sibling read at the /snapshot endpoint
                        # is a sync `def` path-op, so Starlette threadpools it
                        # already; this was the one site still on the loop.
                        snap = await asyncio.to_thread(
                            read_task_snapshot, Path(TASKS_DIR) / replay_task_id
                        )
                        # Honor the client cursor: only replay a snapshot
                        # strictly newer than what the client already applied.
                        if snap is not None and int(snap.get("seq") or 0) > client_last_seq:
                            await websocket.send_text(json.dumps({
                                "type": _WS_ENVELOPE_SNAPSHOT,
                                "task_id": replay_task_id,
                                "snapshot": snap,
                                "trigger": "subscribe_replay",
                                "timestamp": datetime.utcnow().isoformat(),
                            }, default=str))
                    except Exception as exc:
                        logger.debug(
                            f"subscribe-replay snapshot failed for {replay_task_id}: {exc}"
                        )

                    # Replay this task's cached last orchestrator_state envelope
                    # so the OkuroThinker activity pill is populated on connect
                    # — without this, a reload mid-LLM-call shows an idle
                    # widget even though the engine is actively thinking. The
                    # FE pill seq-guards it, so a stale frame is harmless.
                    cached_state = ws_manager._last_orchestrator_state_msg.get(replay_task_id)
                    if cached_state is not None:
                        try:
                            await websocket.send_text(json.dumps(cached_state, default=str))
                        except Exception as exc:
                            logger.debug(
                                f"subscribe-replay orchestrator_state failed for {replay_task_id}: {exc}"
                            )

            elif msg_type == "ping":
                await websocket.send_json({
                    "type": _WS_ENVELOPE_PONG,
                    "timestamp": datetime.utcnow().isoformat(),
                })

    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        await ws_manager.disconnect(websocket)


@app.websocket("/ws/activity")
async def websocket_activity(websocket: WebSocket):
    """Global activity stream — no subscribe handshake required.

    Emits everything broadcast_activity() touches: agent_activity (tool_use /
    text / mcp_tool_call from telemetry tail) and any event pushed through
    broadcast_agent_event. Intentionally isolated from the /ws state pool
    so task-detail pages cannot receive cross-task activity here.
    """
    if not _require_ws_auth(websocket):
        await websocket.close(code=1008, reason="bearer required")
        return
    await ws_manager.connect_activity(websocket)
    try:
        await websocket.send_json({
            "type": _WS_ENVELOPE_CONNECTED,
            "channel": "activity",
            "timestamp": datetime.utcnow().isoformat(),
        })
        while True:
            # We accept but ignore pings/other messages; this channel is read-only.
            data = await websocket.receive_json()
            if data.get("type") == "ping":
                await websocket.send_json({
                    "type": _WS_ENVELOPE_PONG,
                    "timestamp": datetime.utcnow().isoformat(),
                })
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket /ws/activity error: {e}")
        await ws_manager.disconnect(websocket)


# -- Inline-session HTTP MCP mount (wave 3a) --
# Mounted under /mcp/v1 so CLI subprocesses spawned by bridge_stream_start
# can reach okuro's tool registry over loopback with a per-session bearer.
# The global BearerAuthMiddleware skips /mcp/* (exempt prefix) so the
# inline mount's own InlineMcpBearerMiddleware is the single auth point.
try:
    from okuro.mcp.inline_http import mount_inline_mcp

    mount_inline_mcp(app)
except Exception:
    logger.exception("inline MCP mount failed — inline web sessions disabled")


# -- Inline-session SSE + control routes (wave 3c) --
# Mounted under /api/sessions so the browser can stream events and
# resolve approvals over the per-session bearer. The global bearer
# middleware exempts /api/sessions/ (see _AUTH_EXEMPT_PREFIXES) so the
# sub-app's own _SessionBearerMiddleware is the single auth point.
try:
    from okuro.web.session_sse import mount_sessions_routes

    mount_sessions_routes(app)
except Exception:
    logger.exception("inline session SSE mount failed — browser approval flow disabled")


# -- Prism kit gallery (static) --
# Serve the prism component/layout kit (src/okuro/prism/kit/) at /prism-kit so
# the standalone specimen HTML (gallery/*.html) renders in-app with its relative
# ../board/*.css + component-manifest.json resolving as siblings. A non-/api path
# is bearer-exempt (see BearerAuthMiddleware), so it loads in the browser. MUST
# be registered before mount_spa so the SPA catch-all doesn't shadow it.
try:
    from fastapi.staticfiles import StaticFiles as _StaticFiles
    import okuro.prism as _prism_pkg

    _kit_dir = Path(_prism_pkg.__file__).resolve().parent / "kit"
    if _kit_dir.is_dir():
        app.mount(
            "/prism-kit",
            _StaticFiles(directory=_kit_dir, html=True),
            name="prism-kit",
        )
except Exception:
    logger.exception("prism kit gallery mount failed — /prism-kit disabled")


# -- SPA Static Mount (MUST be last: catch-all shadows any route added after it) --
mount_spa(app)


# -- Entrypoint --

if __name__ == "__main__":
    # The service runs ``-m okuro.orchestrator.api.serve``; see that module for
    # why. This block only covers a direct ``-m okuro.orchestrator.api.main``,
    # and it has to fix the same defect, or the old command keeps shipping it.
    #
    # runpy registered this file as ``__main__`` ONLY. Binding the canonical
    # name to the SAME module object first means serve.py's import of it is a
    # lookup, not a second execution — so there is one module object either
    # way, and every lazy ``from okuro.orchestrator.api.main import ...`` at
    # runtime resolves to the copy actually being served.
    sys.modules.setdefault("okuro.orchestrator.api.main", sys.modules["__main__"])

    from okuro.orchestrator.api.serve import main as _serve

    _serve()
