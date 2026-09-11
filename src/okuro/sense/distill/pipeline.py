### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The daemon entry point — tier-0 + tier-1 over new sessions, inside a budget, skipping sessions still being written.
# index: imports | defaults | config | select_sessions | run_distill_pipeline
# AGENT_HEADER_END -->
"""The scheduled half of the distill pipeline.

Runs tier-0 and tier-1 over sessions that are finished, not yet mapped, and
affordable. It does NOT judge and it does NOT delete: tier-2 is dormant until
a calibration set exists (:mod:`.judge`) and deletion is the owner's Phase-4
switch (:mod:`.gate`).

Two things this has to get right, and both are the same lesson from a
different angle.

**A session is not finished when its last event is old.** Claude Code resumes
transcripts, and ``trace-ingest`` writes the resumed events with their
ORIGINAL timestamps — that is the defect migration 135 was written for. So
``agent_events.timestamp`` cannot answer "is this session still being written
to". ``agent_sessions.indexed_at`` can: the ingester refreshes it on every
upsert (``trace/claude_code.py:262``), so it is wall-clock evidence that the
transcript changed, not an inference from content. That is the freshness
check.

**A mapped session can grow after it was mapped.** The freshness check alone
would then leave a facet row describing a prefix of a session forever, and
nothing in the row would say so. So selection ALSO re-queues any session whose
current ``MAX(ord)`` has passed the ``scanned_through_ord`` its facet row
recorded — position, not a boolean, for exactly the reason migration 135
gives.

Note the two timestamp formats in play, because mixing them silently returns
the wrong set: ``agent_events.timestamp`` and ``agent_sessions.last_ts`` are
ISO-8601 with ``T`` and ``Z`` and need
``strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)``, while ``indexed_at`` is
``datetime('now')``'s space-separated form and needs ``datetime('now', ?)``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# A session touched within this window may still be receiving events.
FRESH_HOURS = 24

# Config: distill.daily_token_budget. Modest on purpose — the daemon's job is
# to keep up with ~8.2k events/day of new material, not to chew the 10k
# backlog. The backlog is the pilot's job, run by hand.
_CONFIG_SECTION = "distill"
_CONFIG_KEY = "daily_token_budget"
DEFAULT_TOKEN_BUDGET = 200_000

# Characters per token. Used ONLY to convert the budget knob (which is
# expressed in tokens, because that is how everyone reasons about cost) into
# the character count tier-1 can actually measure — neither bridge path
# returns usage. Declared as the estimate it is; nothing here reports a
# measured token count.
CHARS_PER_TOKEN = 4

# Bound on one scheduled pass, independent of the budget. A budget can be
# raised in config; this is the guard against a pass that walks the whole
# corpus because somebody set the budget high.
MAX_SESSIONS_PER_RUN = 400

# How many times an extraction may FAIL before the session is parked.
#
# The re-queue clause below exists so a session tier-1 skipped for budget comes
# back tomorrow. It cannot, on its own, tell that session apart from one whose
# extraction fails identically every time — so without a ceiling a
# deterministic failure is retried forever. Measured on the live backlog
# 2026-08-14: 57-67 sessions per 1000-session chunk refused `unparseable_facet`,
# the same ones each chunk, one model call each, every chunk.
#
# 3 because the failures worth retrying are transient (a timeout, a provider
# blip) and do not survive three passes, while the ones that are not transient
# — a transcript the model reliably will not classify — are settled by the
# second attempt. Raising this re-animates everything below the new ceiling
# with no migration, which is why it is a counter and not a flag.
MAX_FACET_ATTEMPTS = 3


@dataclass
class PipelineRun:
    selected: int = 0
    tier0: dict = field(default_factory=dict)
    tier1: dict = field(default_factory=dict)
    clustered: dict = field(default_factory=dict)
    token_budget: int = 0


def token_budget() -> int:
    """``distill.daily_token_budget`` from config, or the modest default."""
    from okuro.db.engine import _load_config

    raw = (_load_config().get(_CONFIG_SECTION) or {}).get(_CONFIG_KEY)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TOKEN_BUDGET
    return value if value > 0 else DEFAULT_TOKEN_BUDGET


def select_sessions(db, limit: int = MAX_SESSIONS_PER_RUN,
                    fresh_hours: int = FRESH_HOURS) -> list[str]:
    """Sessions that are finished, and either unmapped or grown since mapping.

    Oldest first by ``COALESCE(first_ts, '9999')``: five sessions on the live
    store carry a NULL ``first_ts``, and sorting NULLs to the FRONT would hand
    every run the same five undatable sessions as "the oldest". They are not
    known to be oldest, so they sort last.
    """
    from .facets import EXTRACTION_VERSION

    return [
        r["session_id"]
        for r in db.fetchall(
            """
            SELECT s.session_id
            FROM agent_sessions s
            LEFT JOIN distill_facets f ON f.session_id = s.session_id
            WHERE (s.indexed_at IS NULL
                   OR s.indexed_at < datetime('now', ?))
              AND (
                    f.session_id IS NULL
                 OR f.extraction_version < ?
                 -- Routed to the judge but never facet-extracted, and not
                 -- yet parked. Almost always a session tier-1 skipped when the
                 -- nightly budget ran out; without this clause it would keep
                 -- its tier-0 row forever and the pipeline would silently
                 -- stall after its first night at any budget smaller than the
                 -- backlog.
                 --
                 -- The attempts ceiling is the other half of that: this clause
                 -- alone cannot tell "not reached yet" from "reached, failed,
                 -- and will fail the same way again", so a deterministic
                 -- failure was retried every pass forever. Budget refusals do
                 -- NOT increment the counter — nothing was sent — so being at
                 -- the back of the queue never parks a session.
                 OR (f.stage = 'tier0' AND f.triage = 'judge'
                     AND f.facet_attempts < ?)
                 OR f.scanned_through_ord < (
                        SELECT COALESCE(MAX(ord), -1) FROM agent_events e
                        WHERE e.session_id = s.session_id
                    )
              )
            ORDER BY COALESCE(s.first_ts, '9999'), s.session_id
            LIMIT ?
            """,
            (f"-{int(fresh_hours)} hours", EXTRACTION_VERSION,
             MAX_FACET_ATTEMPTS, int(limit)),
        )
    ]


def run_distill_pipeline(limit: int | None = None,
                         budget_tokens: int | None = None,
                         cluster: bool = True) -> dict:
    """Daemon handler: map new sessions, then re-cluster if anything changed.

    Registered as the ``distill-pipeline`` task. Declared ``mixed`` in the
    registry — tier-0 is free and tier-1 calls a model per session, so an idle
    night costs nothing and a busy one costs the budget.
    """
    from okuro.db import get_db

    from .corpus import cluster_corpus, run_tier1
    from .triage import load_signals, triage_session

    db = get_db()
    budget = budget_tokens if budget_tokens is not None else token_budget()
    run = PipelineRun(token_budget=budget)

    session_ids = select_sessions(db, limit=limit or MAX_SESSIONS_PER_RUN)
    run.selected = len(session_ids)
    if not session_ids:
        return _as_dict(run)

    # Tier-0 for every selected session, keeping the reads so tier-1 does not
    # walk agent_events a second time.
    signals = load_signals(db, session_ids)
    reads = []
    tier0 = {"unusable": 0, "to_judge": 0, "flagged": 0, "failed": 0}
    for sid in session_ids:
        try:
            result, read = triage_session(db, sid, signals=signals.get(sid))
        except Exception as exc:  # noqa: BLE001 — one bad session must not end
            # the pass; it is counted and the next run retries it.
            log.warning("distill tier-0 failed for %s: %s", sid, exc)
            tier0["failed"] += 1
            continue
        if result.triage == "unusable":
            tier0["unusable"] += 1
            continue  # nothing for tier-1 to summarise
        tier0["to_judge"] += 1
        if result.flagged:
            tier0["flagged"] += 1
        reads.append(read)
    run.tier0 = tier0

    # concurrency=None lets run_tier1 read distill.facet_concurrency itself —
    # one place decides, and the daemon does not need to know the number.
    tier1 = run_tier1(reads, db=db, char_budget=budget * CHARS_PER_TOKEN)
    run.tier1 = {
        "considered": tier1.considered,
        "extracted": tier1.extracted,
        "embedded": tier1.embedded,
        "prompt_chars": tier1.prompt_chars,
        "output_chars": tier1.output_chars,
        "estimated_tokens": (tier1.prompt_chars + tier1.output_chars) // CHARS_PER_TOKEN,
        "refused_by_reason": tier1.refused_by_reason,
    }

    # Re-cluster only when the population changed. Cluster ids are relative to
    # the population they were computed over, so re-running on an unchanged
    # corpus would burn CPU to produce the same labels — and skipping it when
    # the corpus DID change would leave new sessions unlabelled next to old
    # ones labelled against a different population.
    if cluster and tier1.embedded:
        result = cluster_corpus(db)
        run.clustered = {
            "k": result.k,
            "iterations": result.iterations,
            "converged": result.converged,
            "sizes": result.sizes,
        }

    log.info(
        "distill pipeline: selected %d, tier-0 %s, tier-1 extracted %d/%d",
        run.selected, tier0, tier1.extracted, tier1.considered,
    )
    return _as_dict(run)


def _as_dict(run: PipelineRun) -> dict:
    return {
        "selected": run.selected,
        "tier0": run.tier0,
        "tier1": run.tier1,
        "clustered": run.clustered,
        "token_budget": run.token_budget,
    }


# ---------------------------------------------------------------------------
# The weekly half — mining and the lesson lifecycle
# ---------------------------------------------------------------------------
# Config: distill.mining_min_tier1_coverage. A fraction of the CORPUS, not of
# the facet table — the two differ by more than an order of magnitude today
# (400 facets against 10 015 sessions) and the facet-relative figure would read
# as near-total coverage while the map covered 4% of what exists.
_KEY_MIN_COVERAGE = "mining_min_tier1_coverage"
DEFAULT_MIN_TIER1_COVERAGE = 0.60


def min_tier1_coverage() -> float:
    """How much of the corpus must be mapped before mining is worth running."""
    from okuro.db.engine import _load_config

    raw = (_load_config().get(_CONFIG_SECTION) or {}).get(_KEY_MIN_COVERAGE)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_TIER1_COVERAGE
    return value if 0.0 < value <= 1.0 else DEFAULT_MIN_TIER1_COVERAGE


def tier1_coverage(db) -> dict:
    """What fraction of the corpus has a tier-1 facet.

    Clusters computed over 4% of the corpus are a map of the sample, not of the
    population, and a lesson mined from them would claim corroboration it does
    not have. So this is the gate, and it is REPORTED on every run rather than
    silently short-circuiting — a task that does nothing and says nothing is
    indistinguishable from a task that is broken.
    """
    sessions = db.fetchone("SELECT COUNT(*) AS n FROM agent_sessions") or {}
    mapped = db.fetchone(
        "SELECT COUNT(*) AS n FROM distill_facets WHERE stage = 'tier1'"
    ) or {}
    total = int(sessions.get("n") or 0)
    tier1 = int(mapped.get("n") or 0)
    return {
        "sessions": total,
        "tier1_facets": tier1,
        "coverage": round(tier1 / total, 4) if total else 0.0,
    }


def run_lesson_pipeline(force_coverage: bool = False) -> dict:
    """Daemon handler: mine clusters, then maintain and emit lessons.

    Two independent switches, and both must be open before a model is called:

    ``distill.mining_enabled`` — the owner's, checked inside
    :func:`~.mining.mine_all`. Ships false.

    ``distill.mining_min_tier1_coverage`` — the corpus one, checked here.
    Dormant below the threshold and counter-reported either way.

    Lesson MAINTENANCE runs regardless of both. It makes no model calls — it
    observes evidence, recomputes counters from rows and moves statuses — and
    an existing active lesson must keep being judged even while mining is off.
    Freezing the lifecycle behind the mining switch would leave a lesson that
    stopped working sitting in front of the owner indefinitely.
    """
    from okuro.db import get_db

    from .lessons import run_lesson_maintenance
    from .mining import mine_all

    db = get_db()
    coverage = tier1_coverage(db)
    threshold = min_tier1_coverage()
    eligible = force_coverage or coverage["coverage"] >= threshold

    mined: dict = {
        "enabled": False,
        "reason": (
            f"tier-1 coverage {coverage['coverage']:.1%} is below "
            f"{threshold:.0%} — the corpus map is not representative enough to "
            f"mine, so no clusters were profiled and no model was called"
        ),
    }
    if eligible:
        mined = mine_all(db)

    maintained = run_lesson_maintenance(db)

    # The push half. A review queue that only answers when asked is one the owner
    # will not remember to open, and an unreviewed lesson never becomes a rule —
    # so the loop would quietly stop at a table nobody reads.
    #
    # Gated on NEW candidates rather than fired every week: re-nudging about a
    # queue that has not changed is how a channel earns being muted. When the
    # count is unchanged the bootstrap section still carries it, which is the
    # quieter of the two channels and the right one for a standing backlog.
    from .lessons import notify_pending_candidates

    new_candidates = int(mined.get("lessons_written") or 0)
    notified = {"action": "skipped", "reason": "no new candidates this run"}
    if new_candidates:
        notified = notify_pending_candidates(db)

    log.info(
        "distill lessons task: coverage %.1f%% (gate %.0f%%), mining %s, "
        "%d lessons maintained, %d new candidates, reminder %s",
        coverage["coverage"] * 100, threshold * 100,
        "ran" if mined.get("enabled") else "dormant",
        maintained["maintained"], new_candidates, notified.get("action"),
    )
    return {
        "coverage": coverage,
        "coverage_threshold": threshold,
        "coverage_gate_open": eligible,
        "mining": mined,
        "lessons": maintained,
        "notified": notified,
    }
