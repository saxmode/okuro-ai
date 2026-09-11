# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Daemon task registry — built-in tasks + user overrides from config.yaml.
# index: imports | kinds+tiers | class DaemonTask | def load_user_overrides
#   | def get_all_tasks
# AGENT_HEADER_END -->
"""Daemon task registry — built-in tasks + user overrides from config.yaml."""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from okuro.db.engine import okuro_home

log = logging.getLogger(__name__)

CONFIG_PATH = okuro_home() / "daemon" / "config.yaml"

# --- Execution kind ---------------------------------------------------------
# What a task actually DOES when it fires. Before this existed the only signal
# was the `handler` import path plus whatever the description happened to claim
# in prose — and prose drifts: media-recurring advertised "no LLM agent" while
# making two model calls per job. Declare it, then let the drift alarm verify
# the declaration against what the bridge actually observed.
KIND_SCRIPT = "script"              # deterministic Python/SQL/git, no inference
KIND_LLM = "llm"                    # always calls a model
KIND_MIXED = "mixed"                # deterministic; calls a model conditionally
KIND_ORCHESTRATOR = "orchestrator"  # spawns an engine, which picks its own models
KINDS = frozenset({KIND_SCRIPT, KIND_LLM, KIND_MIXED, KIND_ORCHESTRATOR})

# Tier vocabulary as resolved by okuro.bridge (bridge/providers.py). NOT the
# orchestrator's tier_map, which spells the top tier `strategic` — the API layer
# normalizes that to `quality` for display. Empty means "no direct model call"
# (script/orchestrator kinds).
TIER_FAST = "fast"          # claude: haiku
TIER_STANDARD = "standard"  # claude: sonnet
TIER_QUALITY = "quality"    # claude: opus / codex: reasoning_effort=high
TIERS = frozenset({TIER_FAST, TIER_STANDARD, TIER_QUALITY, ""})


@dataclass
class DaemonTask:
    """A recurring task the daemon scheduler can execute."""

    id: str
    description: str
    cron: str  # croniter-compatible expression
    handler: str  # "okuro.module.sub:function"
    enabled: bool = True
    run_on_startup: bool = False
    timeout_seconds: int = 300
    # --- Execution classification (declared; verified by the drift alarm) ---
    kind: str = KIND_SCRIPT
    tier: str = ""      # bridge tier for kind in (llm, mixed); "" otherwise
    embeds: bool = False  # hits the embedding model — a separate axis from tier


BUILTIN_TASKS: list[DaemonTask] = [
    DaemonTask(
        id="reminders",
        description="Evaluate due reminders and fire cascade steps",
        cron="* * * * *",
        handler="okuro.sense.reminders.eval:main",
    ),
    # Alarm when the daemon's loaded code is behind HEAD. A long-lived process
    # freezes its modules at boot, so committed fixes silently miss it until a
    # restart — the failure that hid the memory-metric fix from the daemon for
    # hours (Fable audit G1). run_on_startup so a boot that is already stale
    # says so immediately; silent when in sync.
    DaemonTask(
        id="code-drift-alarm",
        description="Warn when the daemon is running code behind HEAD (restart to deploy). Silent when in sync.",
        cron="*/30 * * * *",
        handler="okuro.daemon._handlers:code_drift_alarm",
        run_on_startup=True,
        timeout_seconds=30,
    ),
    # The verify half of declare-then-verify: code-drift-alarm's sibling, for
    # the registry's own kind/tier claims. Cheap (reads one append-only JSONL),
    # silent when every task matches its declaration. Not run_on_startup — a
    # fresh boot has observed nothing yet, so it would only ever be silent.
    DaemonTask(
        id="kind-drift-alarm",
        description="Warn when a daemon task ran a model its declared kind/tier says it shouldn't. Silent when declarations match reality.",
        cron="17 * * * *",
        handler="okuro.daemon._handlers:kind_drift_alarm",
        timeout_seconds=60,
    ),
    # The full test suite, on a clock. It gated the DEPLOY for one day
    # (2026-08-10) and was moved off that trigger the same day: it charged every
    # merge ~14 minutes before the services would run the code that was merged
    # in order to be used. pre-push now blocks on it, and this covers the gap
    # pre-push leaves — pushes are rare here, so without a clock a regression
    # could sit unseen for days. 04:00 so the CPU burn never lands mid-work.
    # Silent when green or when only the known baseline fails.
    DaemonTask(
        id="full-suite",
        description="Run the full test suite and warn when it fails beyond its known baseline. Silent when green.",
        cron="0 4 * * *",
        handler="okuro.daemon._handlers:full_suite",
        timeout_seconds=3600,
    ),
    # MCP registration is the one agent-facing surface with no reconciler — the
    # daemon's refresh re-runs instructions + hooks but never re-writes MCP
    # configs. This is the push side of registration_status: it warns when a
    # written entry drifts from what canon writes, or when the ~/.mcp.json
    # shadow (the every-session-disconnect cause) is present. Report-only —
    # never rewrites a user config from a background job. run_on_startup so a
    # boot that is already drifted says so immediately; silent when in sync.
    DaemonTask(
        id="model-drift-alarm",
        description="Compare each provider CLI's served models against the configured tier->model tables; signal on drift",
        cron="30 4 * * 1",
        handler="okuro.bridge.model_drift:run_model_drift_alarm",
        timeout_seconds=900,
        # Probing a `validate`-style provider (claude has no list command)
        # costs one tiny inference call per candidate alias. That is a model
        # call, so this declares KIND_LLM even though the task itself does no
        # reasoning — the kind-drift alarm checks this declaration against
        # what the bridge actually observes.
        kind=KIND_LLM,
        tier=TIER_FAST,
    ),
    # Managed-repo status was on-demand only: a repo stayed 'ready' with a green
    # timestamp until someone pressed Sync, so a forge revoking access stayed
    # invisible until the next manual attempt (12 of 13 repos read 'ready' while
    # all were dead). Probes per CREDENTIAL, so the whole registry costs ~2
    # network calls. Marks only on explicit rejection, never on an unreachable
    # network. run_on_startup — a boot that is already locked out should say so.
    DaemonTask(
        id="repo-auth-alarm",
        description="Probe each managed-repo git credential; mark repos whose access a forge now rejects. Silent when every credential is healthy.",
        cron="*/15 * * * *",
        handler="okuro.daemon._handlers:repo_auth_alarm",
        run_on_startup=True,
        timeout_seconds=180,
    ),
    # The auth alarm proves a credential still works; it does NOT prove the INDEX
    # is current. Between 2026-07-13 and 2026-09-03 twelve repos stayed queryable
    # in cortex while locked out of their forge — every answer drawn from them was
    # seven weeks stale and read as authoritative. Syncing on a clock is what makes
    # "the index is current" true rather than hoped-for. 04:00 so a ~20 min ingest
    # never lands in a working hour.
    DaemonTask(
        id="managed-repo-sync",
        description="git pull + re-ingest every healthy managed repo so the cortex index does not silently serve stale code. Skips repos in error or mid-clone.",
        cron="0 4 * * *",
        handler="okuro.daemon._handlers:managed_repo_sync",
        run_on_startup=False,
        timeout_seconds=3600,
    ),
    DaemonTask(
        id="canon-drift-alarm",
        description="Warn when okuro's MCP registration drifted from what canon writes (stale entry or ~/.mcp.json shadow). Silent when in sync. Report-only.",
        cron="*/30 * * * *",
        handler="okuro.daemon._handlers:canon_drift_alarm",
        run_on_startup=True,
        timeout_seconds=30,
    ),
    DaemonTask(
        id="obsidian-sync",
        description="DEFERRED — Obsidian vault → thought DB. Superseded by notes-extract (okuro-notes is the native note surface). Already ingested obsidian thoughts are kept; only the live intake stopped. Re-enable via daemon config if a vault comes back.",
        cron="* * * * *",
        handler="okuro.sense.bridge.obsidian:sync_once",
        enabled=False,
    ),
    DaemonTask(
        id="note-vec-backfill",
        description="Embed note chunks that have no vector yet. Note saves embed off the request path, so a crash or a down okuro-embed can leave chunks unsearchable — this heals them. No-op when nothing is missing.",
        cron="*/10 * * * *",
        handler="okuro.notes.storage:backfill_missing_vectors",
        run_on_startup=True,
        timeout_seconds=300,
    ),
    DaemonTask(
        id="scheduler",
        description="Trigger overdue recurring orchestrator tasks",
        cron="*/5 * * * *",
        handler="okuro.orchestrator.scheduler:main",
        kind=KIND_ORCHESTRATOR,
    ),
    DaemonTask(
        id="engine-reconciler",
        description="Respawn engines for orphaned in-flight tasks (pending/active/planning with a dead engine) — heals tasks frozen by an update/restart that killed their engine process",
        cron="*/2 * * * *",
        handler="okuro.daemon._handlers:reconcile_orphaned_engines",
        run_on_startup=True,
        kind=KIND_ORCHESTRATOR,
    ),
    # Audience vectors go stale WITHOUT anyone touching target_groups: a
    # company + role_class group derives its membership from affiliations, so
    # an affiliation edit silently changes who the audience is. Write-hooks on
    # the group cannot see that, hence a sweep. Cheap at the current scale
    # (single-digit groups); if that reaches the hundreds, replace this with
    # hooks on affiliation_add/_remove.
    DaemonTask(
        id="target-group-embeddings",
        description="Refresh target-group audience vectors (membership drifts via affiliations)",
        cron="*/30 * * * *",
        handler="okuro.peer.target_groups:reembed_all_groups",
        embeds=True,
    ),
    DaemonTask(
        id="maintenance",
        description="Memory decay, thought cleanup, telemetry aggregation",
        cron="0 3 * * 0",
        handler="okuro.sense.improvement:run_maintenance",
        kind=KIND_MIXED,   # fast-judge on <=5 sessions/run; deterministic otherwise
        tier=TIER_FAST,
        embeds=True,
    ),
    DaemonTask(
        id="model-suggestions",
        description="Research box-fit OSS models against the user's goals and file them as proactive suggestions",
        cron="0 4 * * 1",  # Monday 04:00
        handler="okuro.ai_models.suggest:run_suggestions",
        timeout_seconds=600,
        kind=KIND_LLM,
        tier=TIER_STANDARD,
    ),
    DaemonTask(
        id="refresh",
        description="Regenerate agent context files for all providers",
        cron="*/5 * * * *",
        handler="okuro.daemon._handlers:refresh_context",
        run_on_startup=True,
    ),
    DaemonTask(
        id="cortex",
        description="Auto-generate AGENT_HEADERs and rebuild codebase index",
        cron="*/5 * * * *",
        handler="okuro.daemon._handlers:refresh_cortex",
        kind=KIND_MIXED,   # fast-draft only for weak-purpose sidecars
        tier=TIER_FAST,
        embeds=True,
    ),
    # "Deterministic" here has always meant "no orchestrator agent mandate" (see
    # the module docstring) — it never meant "no model call". The old wording
    # said "no LLM agent" and got read as the latter: a due job makes TWO calls,
    # deep-research (codex @ reasoning_effort=high — the priciest call any
    # daemon task makes) + translate. Only DUE jobs fire; an empty store is a
    # no-op tick.
    DaemonTask(
        id="media-recurring",
        description="Generate due recurring media briefs — direct delivery pipeline, no orchestrator agent, off the HTTP worker. Each due job makes 2 model calls: deep-research (web) + translate.",
        cron="*/5 * * * *",
        handler="okuro.peer.delivery.recurring_media:tick",
        timeout_seconds=1800,
        kind=KIND_LLM,
        tier=TIER_QUALITY,  # deep-research leg; the translate leg is standard
    ),
    DaemonTask(
        id="morning-brief",
        description="Mine last-24h activity, compose + render the spoken morning brief so audio is waiting before the user's morning arrival (06:00 UTC ~= 07:00-08:00 Europe/Zurich)",
        cron="0 6 * * *",
        handler="okuro.peer.delivery.morning_brief_daemon:generate_and_deliver",
        timeout_seconds=900,
        kind=KIND_MIXED,   # deterministic sanitiser fallback when the bridge fails
        tier=TIER_STANDARD,
    ),
    # --- A2 daemon-owned hygiene ---
    # Enforcement at the boundary: agents that skip session_report /
    # write_memory-with-care / etc. no longer let rot accumulate. These
    # handlers run regardless of agent discipline.
    DaemonTask(
        id="session-hygiene",
        description="Close orphaned sessions + aggregate provider compliance",
        cron="*/5 * * * *",
        handler="okuro.daemon._handlers:session_hygiene",
    ),
    DaemonTask(
        id="memory-hygiene",
        description="Decay + prune superseded/stale memories; kill doc-dump noise",
        cron="0 * * * *",
        handler="okuro.daemon._handlers:memory_hygiene",
    ),
    DaemonTask(
        id="thoughts-aging",
        description="Dismiss unseen opens > 30d; flag stuck opens > 14d",
        cron="0 2 * * *",
        handler="okuro.daemon._handlers:thoughts_aging",
    ),
    DaemonTask(
        id="wal-truncate",
        description="Checkpoint + truncate WAL so it doesn't grow unbounded",
        cron="*/10 * * * *",
        handler="okuro.daemon._handlers:wal_truncate",
    ),
    DaemonTask(
        id="db-vacuum",
        description="VACUUM okuro.db and truncate WAL",
        cron="0 3 * * 0",
        handler="okuro.daemon._handlers:db_vacuum",
    ),
    DaemonTask(
        id="cortex-prune",
        description="Tombstone cortex_docs rows whose file is gone (soft-delete) + vec-delete their vectors",
        cron="*/15 * * * *",
        handler="okuro.daemon._handlers:cortex_prune_stale",
    ),
    DaemonTask(
        id="cortex-reclaim",
        description="Weekly cortex self-heal: purge orphan vectors + reconcile newly-excluded docs (worktrees, data-JSON)",
        cron="30 3 * * 0",
        handler="okuro.daemon._handlers:cortex_reclaim",
        timeout_seconds=600,
    ),
    DaemonTask(
        id="memory-recall-gate",
        description="Daily recall regression check: run the memory eval against the live store and WARN if recall/MRR/junk-rejection fall below floor",
        cron="15 4 * * *",
        handler="okuro.daemon._handlers:memory_recall_gate",
        timeout_seconds=600,
    ),
    DaemonTask(
        id="trace-ingest",
        description="Ingest agent transcripts (claude-code etc.) into the raw-trace store",
        cron="*/10 * * * *",
        handler="okuro.trace:ingest_all",
        run_on_startup=True,
    ),
    # --- interaction analysis pipeline -------------------------------------
    # Ordered so each stage runs after its input exists: bridge and scan are
    # cheap and deterministic (hourly), analysis is the one model call
    # (weekly), tiering/compaction runs last and only touches sessions the
    # analysis has already read.
    DaemonTask(
        id="interaction-bridge",
        description="Link okuro telemetry session ids to provider-native trace ids (exact methods only)",
        cron="25 * * * *",
        handler="okuro.sense.interaction.bridge:bridge_sessions",
        kind=KIND_SCRIPT,
    ),
    DaemonTask(
        id="interaction-scan",
        description="Run friction/accuracy detectors over new sessions' input turns",
        cron="35 * * * *",
        handler="okuro.sense.interaction.detect:scan_sessions",
        kind=KIND_SCRIPT,
        timeout_seconds=900,
    ),
    DaemonTask(
        id="interaction-embed",
        description="Embed input-side turns for cross-session friction clustering",
        cron="45 */6 * * *",
        handler="okuro.sense.interaction.turns:embed_turns",
        kind=KIND_SCRIPT,
        timeout_seconds=1800,
    ),
    DaemonTask(
        id="interaction-analysis",
        description="Weekly: turn interaction markers into evidenced findings + gotcha memories",
        cron="30 3 * * 0",
        handler="okuro.sense.interaction.analyze:run_analysis",
        timeout_seconds=900,
        kind=KIND_LLM,
        tier=TIER_STANDARD,
    ),
    DaemonTask(
        id="interaction-improve",
        description="Route interaction markers to fixable okuro surfaces; propose changes into the signals queue and verify promoted ones",
        cron="0 5 * * 1",
        handler="okuro.sense.interaction.improve:run_improvements",
        timeout_seconds=1800,
        kind=KIND_LLM,
        tier=TIER_STANDARD,
    ),
    # Sunday 02:15, deliberately: compaction only frees pages onto the
    # freelist, and db-vacuum (Sun 03:00) is the only thing that returns them
    # to the OS. Running Monday 04:15 meant every week's reclaim sat unreturned
    # for six days and 23 hours before the NEXT Sunday's VACUUM saw it. 02:15
    # also keeps the compactor out of the 03:00-03:30 pileup (db-vacuum,
    # interaction-analysis, cortex-reclaim, session-retros).
    DaemonTask(
        id="interaction-lifecycle",
        description="Harvest raw-text extracts, re-tier traces by age, compact analyzed cool/cold sessions (input turns never compacted)",
        cron="15 2 * * 0",
        handler="okuro.sense.interaction.lifecycle:run_lifecycle",
        timeout_seconds=1800,
        kind=KIND_SCRIPT,
    ),
    DaemonTask(
        id="session-retros",
        description="Weekly causal post-mortem: diff low-score vs high-score sessions, persist gotcha memories",
        cron="0 3 * * 0",
        handler="okuro.sense.retros:run_retros",
        timeout_seconds=600,
        kind=KIND_LLM,
        tier=TIER_STANDARD,  # hardcodes provider="claude" — bypasses the router
    ),
    DaemonTask(
        id="memory-utility-decay",
        description="Weekly: decay confidence on memories whose surfacing correlates with lower compliance",
        cron="0 4 * * 0",
        handler="okuro.sense.memory_utility:auto_decay",
    ),
    DaemonTask(
        id="profile-enrich",
        description="Safety-net LLM rationale fill for profile rules (settings edits, raw DB changes)",
        cron="*/15 * * * *",
        handler="okuro.daemon._handlers:profile_enrich",
        kind=KIND_MIXED,   # no call when every rule already has a rationale
        tier=TIER_STANDARD,
    ),
    DaemonTask(
        id="telemetry-rotate",
        description="Rotate usage.jsonl past 10MB; prune archives older than 14 days",
        cron="0 3 * * *",
        handler="okuro.daemon._handlers:telemetry_rotate",
    ),
    DaemonTask(
        id="proactive-scan",
        description="Heuristic proactive suggester — emits signals from stale progress, stuck todos, aging thoughts, sysinfo pressure, session failures",
        cron="0 * * * *",
        handler="okuro.daemon._handlers:proactive_scan",
    ),
    DaemonTask(
        id="notes-extract",
        description="LLM note extractor — reads changed okuro notes, routes typed+tagged items to todos (incl. prose commitments), signals, and thoughts",
        cron="*/15 * * * *",
        handler="okuro.daemon._handlers:notes_extract",
        timeout_seconds=600,
        kind=KIND_MIXED,   # content-hash watermark — zero calls on an idle tick
        tier=TIER_STANDARD,
    ),
    DaemonTask(
        id="proactive-advisor",
        description="LLM continuation advisor — daily 06:30 cross-source scan of active projects for near-complete work; emits continuation signals via claude",
        cron="30 6 * * *",
        handler="okuro.daemon._handlers:proactive_advise",
        timeout_seconds=900,
        kind=KIND_LLM,
        tier=TIER_STANDARD,  # hardcodes provider="claude" — bypasses the router
    ),
    DaemonTask(
        id="inbox-reduce",
        description="Project producer rows into the inbox overlay with salience",
        cron="*/30 * * * *",
        handler="okuro.daemon._handlers:inbox_reduce",
        run_on_startup=True,
    ),
    # Surfacing gate — promotes high-value 'new' rows to 'surfaced'. MUST be
    # listed AFTER inbox-reduce so on startup reduce populates 'new' first,
    # then the heartbeat applies the gate. Silent-when-empty, defer-when-busy.
    DaemonTask(
        id="inbox-heartbeat",
        description="Apply the inbox surfacing gate — promote high-value 'new' rows to 'surfaced' (silent-when-empty, defer-when-busy)",
        cron="*/30 * * * *",
        handler="okuro.daemon._handlers:inbox_heartbeat",
        run_on_startup=True,
    ),
    DaemonTask(
        id="commitment-infer",
        description="LLM-infer per-session implicit follow-ups from recent provider sessions; store as commitments (TTL 7d) surfaced via the inbox",
        cron="*/15 * * * *",
        handler="okuro.daemon._handlers:commitment_infer",
        timeout_seconds=900,
        run_on_startup=False,
        kind=KIND_MIXED,   # bounded by _MAX_SESSIONS; no call without new sessions
        tier=TIER_STANDARD,  # hardcodes provider="claude" — bypasses the router
    ),
    # Daily 05:45, and the slot is picked against WORST-CASE end times rather
    # than start times — a task's cron says when it begins, its timeout says
    # how long it can still be holding the writer lock. Live registry:
    #
    #   02:15 Sun  interaction-lifecycle  the COMPACTOR, which nulls the very
    #                                     bodies this task reads
    #   03:00 Sun  db-vacuum              exclusive lock on the whole database
    #   03:30 Sun  cortex-reclaim, interaction-analysis
    #   04:00 *    full-suite             timeout 3600s -> can run until 05:00
    #   04:15 *    memory-recall-gate     -> until 04:25
    #   05:00 Mon  interaction-improve    timeout 1800s -> until 05:30
    #   06:00 *    morning-brief
    #
    # 04:45 was wrong: full-suite's worst case reaches 05:00, so on a slow
    # night the two overlapped. 05:30 clears full-suite but lands exactly on
    # interaction-improve's Monday worst-case tail. 05:45 is clear of both at
    # the start boundary, which is the one that can be reasoned about.
    #
    # This task's own worst case is nominally 3600s, but that is a ceiling, not
    # an expectation: distill.daily_token_budget (200k) caps tier-1 at roughly
    # 60 sessions per night at ~13.5k characters each, so a real run is minutes.
    # Its tail can reach morning-brief only in a pathological case, and that
    # overlap is the cheap one — morning-brief reads, this writes distill_*
    # tables nothing else touches.
    #
    # Conservative by construction: it skips any session the ingester touched
    # in the last 24h (resume-safety), caps at MAX_SESSIONS_PER_RUN, and stops
    # tier-1 at distill.daily_token_budget. Tier-0 is free; tier-1 is one cheap
    # call per session — hence `mixed`, not `llm`: a night with no finished new
    # sessions makes zero model calls.
    DaemonTask(
        id="distill-pipeline",
        description="Map new trace sessions into the distill corpus — tier-0 triage + tier-1 facets, inside distill.daily_token_budget. No judging, no deletion.",
        cron="45 5 * * *",
        handler="okuro.sense.distill.pipeline:run_distill_pipeline",
        timeout_seconds=3600,
        kind=KIND_MIXED,
        tier=TIER_FAST,
        embeds=True,   # one embedding per extracted facet
    ),
    # Sunday 07:15, weekly, and the slot is picked against WORST-CASE END times
    # the same way distill-pipeline's was. What has to be clear of it:
    #
    #   05:45 *    distill-pipeline    timeout 3600s -> can run until 06:45.
    #                                  This task READS the clusters that one
    #                                  writes, so it must not merely avoid it —
    #                                  it must follow it.
    #   06:00 *    morning-brief
    #   06:30 *    proactive-advisor   timeout 900s  -> until 06:45
    #
    # 07:15 clears both 06:45 boundaries with half an hour in hand. Sunday
    # rather than Monday keeps it off interaction-improve's day (Mon 05:00 ->
    # 05:30), which is the other writer into interaction_improvements; they do
    # not conflict on rows, but sharing a morning would make a slow week's
    # attribution ambiguous for no gain.
    #
    # WEEKLY, not daily, and not folded into distill-pipeline. Mining is the
    # only quality-tier spend in the pipeline and its input is a k-means map
    # that barely moves between two nightly passes — running it daily would pay
    # 7x for the same clusters. Folding it into the daily task would also tie
    # the expensive tier's schedule to the cheap one's, which is the coupling
    # that makes a budget hard to reason about.
    #
    # Declared `mixed`: lesson maintenance is deterministic SQL and always
    # runs, while mining calls a model only when BOTH switches are open —
    # distill.mining_enabled (ships false) and tier-1 coverage over
    # distill.mining_min_tier1_coverage (4% today against a 60% gate). So this
    # task is dormant-but-reporting on both counts right now, and an idle run
    # costs nothing.
    DaemonTask(
        id="distill-lessons",
        description="Mine corroborated defects per cluster into lessons, run the ACE lifecycle, and emit active lessons onto the existing interaction_improvements lifecycle. Dormant until tier-1 coverage and distill.mining_enabled both allow it.",
        cron="15 7 * * 0",
        handler="okuro.sense.distill.pipeline:run_lesson_pipeline",
        timeout_seconds=1800,
        kind=KIND_MIXED,
        tier=TIER_QUALITY,   # one call per cluster WITH a corroborated finding
        embeds=True,         # one embedding per lesson, for dedup
    ),
    DaemonTask(
        id="inbox-hygiene",
        description="Purge terminal-state inbox rows (superseded/expired) + expired commitments so the tables stay bounded",
        cron="0 4 * * *",
        handler="okuro.daemon._handlers:inbox_hygiene",
        run_on_startup=False,
    ),
]


def load_user_overrides() -> dict[str, dict]:
    """Read ``~/.okuro/daemon/config.yaml`` and return per-task overrides.

    Expected format::

        tasks:
          reminders:
            cron: "*/2 * * * *"
          obsidian-sync:
            enabled: false

    Returns a mapping ``{task_id: {field: value, ...}}``.
    """
    if not CONFIG_PATH.is_file():
        return {}
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text())
        if not isinstance(data, dict):
            return {}
        return data.get("tasks", {}) or {}
    except Exception as exc:
        log.warning("Failed to read daemon config %s: %s", CONFIG_PATH, exc)
        return {}


_OVERRIDE_FIELDS = {"cron", "enabled", "run_on_startup", "timeout_seconds"}


def get_all_tasks(overrides: Optional[dict[str, dict]] = None) -> list[DaemonTask]:
    """Return the merged task list (builtins + user overrides).

    Callers can pass *overrides* directly (useful for testing) or leave it
    ``None`` to read from the config file.
    """
    if overrides is None:
        overrides = load_user_overrides()

    tasks: list[DaemonTask] = []
    for task in BUILTIN_TASKS:
        merged = copy.copy(task)
        if task.id in overrides:
            patch = overrides[task.id]
            for key, val in patch.items():
                if key in _OVERRIDE_FIELDS:
                    setattr(merged, key, val)
                else:
                    log.warning("Ignoring unknown override field %r for task %r", key, task.id)
        tasks.append(merged)

    return tasks


def _write_config(data: dict) -> None:
    """Atomically write the daemon config.yaml (temp file + os.replace)."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=True, default_flow_style=False))
    os.replace(tmp, CONFIG_PATH)


def _read_config() -> dict:
    """Return the parsed config.yaml as a dict (empty on absent/malformed)."""
    if not CONFIG_PATH.is_file():
        return {}
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("Failed to read daemon config %s: %s", CONFIG_PATH, exc)
        return {}


def save_task_override(task_id: str, patch: dict) -> dict:
    """Merge *patch* into the persisted override for *task_id*; write config.yaml.

    Only fields in :data:`_OVERRIDE_FIELDS` are persisted; unknown keys are
    dropped with a warning. Returns the resulting per-task override dict.
    Raises ``KeyError`` if *task_id* is not a builtin task.
    """
    if not any(t.id == task_id for t in BUILTIN_TASKS):
        raise KeyError(f"Unknown daemon task {task_id!r}")

    data = _read_config()
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        tasks = {}
    entry = dict(tasks.get(task_id) or {})
    for key, val in patch.items():
        if key in _OVERRIDE_FIELDS:
            entry[key] = val
        else:
            log.warning("Ignoring unknown override field %r for task %r", key, task_id)
    tasks[task_id] = entry
    data["tasks"] = tasks
    _write_config(data)
    return entry


def clear_task_override(task_id: str) -> None:
    """Remove any persisted override for *task_id* (reset to builtin default)."""
    data = _read_config()
    tasks = data.get("tasks")
    if isinstance(tasks, dict) and task_id in tasks:
        del tasks[task_id]
        data["tasks"] = tasks
        _write_config(data)
