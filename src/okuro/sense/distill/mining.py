### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Cluster-level defect mining — aggregate corroborated signals per cluster, synthesise lessons, write candidates.
# index: imports | config | DefectSignal | ClusterProfile | profile_cluster | findings | _PROMPT | synthesise | validate_lesson | store_lessons | mine_cluster | mine_all
# AGENT_HEADER_END -->
"""Tier-3: what a whole CLUSTER of sessions has in common that went wrong.

The three tiers below this one all read a session at a time. That is the wrong
unit for finding a defect in okuro itself, and the reason is a counting
argument rather than a preference: a single session showing ``frustration``
tells you that session went badly. It does not tell you whether the cause is
the user's mood, the task, or a hole in okuro's own instructions — and a
pipeline that emits a lesson from one session emits ten thousand lessons, which
is the same as emitting none.

**One session ASSERTS a candidate. A cluster CORROBORATES it into a finding.**
That is the whole design. Corroboration is counted over DISTINCT sessions in
the cluster (:data:`DEFAULT_MIN_CORROBORATION`, configurable), so a defect has
to recur across independent runs before anything is written down.

Why the expensive model call belongs here and nowhere else
----------------------------------------------------------
Tier-1 spends one cheap call per session because it must — 10 000 sessions need
10 000 summaries. Synthesis is the opposite shape: 14 clusters, one call each,
and only for clusters that actually have a corroborated finding. A clean
cluster costs nothing. That is what makes a quality-tier model affordable here
when it is unthinkable per session, and it is why the deterministic aggregation
below runs FIRST — the model is asked to name a defect the counting already
proved is there, never to go looking for one.

What the model is NOT allowed to invent
---------------------------------------
Three things are validated against the store after synthesis, and a lesson
failing any of them is dropped rather than corrected:

* ``markers`` must be a subset of the markers the cluster actually carries. A
  lesson measured by a marker its evidence never showed cannot be verified, and
  would sit in ``interaction_improvements`` forever scoring ``inconclusive``.
* ``target_surface`` must be one of the seven surfaces
  ``interaction_improvements`` already routes. There is no eighth.
* ``target_ref`` must come from the closed vocabulary in :mod:`.lessons`. Free
  text there is how user project names reached a general mechanism once
  already.

This module writes ``distill_lessons`` and NOTHING ELSE. In particular it never
writes ``session_distillations`` — that table is the retention gate's deletion
licence (migration 136), tier-2 is dormant until a human calibration batch
exists (137), and a mining pass that touched it would be manufacturing delete
permission out of a cluster summary.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_CONFIG_SECTION = "distill"

# OFF by default, and the daemon honours it. Mining is the first thing in the
# pipeline that spends a quality-tier model, so it stays dark until the owner
# turns it on; the pilot script passes force=True to run a measured pass
# without flipping the config for the scheduler.
_KEY_ENABLED = "mining_enabled"
DEFAULT_MINING_ENABLED = False

# Distinct sessions in a cluster that must show a signal before it is a
# finding. Three is the smallest number that is not a coincidence and not a
# pair of related runs.
_KEY_MIN_CORROBORATION = "mining_min_corroboration"
DEFAULT_MIN_CORROBORATION = 3

# Ceiling on lessons synthesised per cluster. A cluster with eleven problems
# has one problem: it is not a cluster.
MAX_LESSONS_PER_CLUSTER = 2

# Sample sizes handed to the model. Bounded because a cluster can hold
# hundreds of sessions and the prompt is the cost.
_MAX_EVIDENCE_SAMPLES = 12
_MAX_FAILURE_SAMPLES = 10

# Cosine similarity above which a synthesised lesson is treated as one okuro
# already knows. Sits well below the near-duplicate band on purpose: two
# lessons phrased differently about the same defect are the same lesson, and a
# playbook that accumulates paraphrases is one nobody reads.
DEDUP_SIMILARITY = 0.88

LESSON_CLASSES = ("instruction-defect", "role-pitfall", "protocol-gap",
                  "harness-bug")

# Outcomes that count as a defect assertion. 'partial' is deliberately absent:
# most real work is partial, and treating it as a defect would make every
# cluster corroborate everything.
_DEFECT_OUTCOMES = ("abandoned",)


def mining_enabled() -> bool:
    """``distill.mining_enabled`` from config. False unless explicitly set."""
    from okuro.db.engine import _load_config

    raw = (_load_config().get(_CONFIG_SECTION) or {}).get(_KEY_ENABLED)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return DEFAULT_MINING_ENABLED


def min_corroboration() -> int:
    """``distill.mining_min_corroboration``, floored at 2.

    Floored rather than trusted: a threshold of 1 turns "corroborated by the
    cluster" back into "asserted by one session", which is the failure mode
    this module exists to avoid. A config typo must not be able to reintroduce
    it.
    """
    from okuro.db.engine import _load_config

    raw = (_load_config().get(_CONFIG_SECTION) or {}).get(_KEY_MIN_CORROBORATION)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_CORROBORATION
    return max(2, value)


@dataclass(frozen=True)
class DefectSignal:
    """One countable defect assertion, in a CLOSED vocabulary.

    ``key`` is ``marker:<id>``, ``flag:<reason>`` or ``outcome:<value>`` — all
    three drawn from enumerations the store already writes, never from model
    output. Corroboration has to be counted over something that means the same
    thing in every session, and a free-text failure phrase does not: two
    sessions describing the same defect in different words would count as one
    each and never corroborate.
    """

    key: str
    sessions: frozenset


@dataclass
class ClusterProfile:
    """Everything deterministic about one cluster, before any model call."""

    cluster_id: int
    size: int = 0
    session_ids: list[str] = field(default_factory=list)
    signals: dict[str, set] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)
    task_types: dict[str, int] = field(default_factory=dict)
    failure_modes: list[str] = field(default_factory=list)
    summaries: list[tuple] = field(default_factory=list)
    all_markers: set = field(default_factory=set)
    flagged: int = 0
    bodies_missing: int = 0

    def findings(self, threshold: int) -> list[DefectSignal]:
        """Signals corroborated by at least ``threshold`` distinct sessions."""
        out = [
            DefectSignal(key=key, sessions=frozenset(sids))
            for key, sids in self.signals.items()
            if len(sids) >= threshold
        ]
        out.sort(key=lambda s: (-len(s.sessions), s.key))
        return out


def _json_list(blob) -> list:
    try:
        parsed = json.loads(blob or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def profile_cluster(db, cluster_id: int) -> ClusterProfile:
    """Aggregate one cluster's tier-0 and tier-1 rows. No model call.

    Reads ``distill_facets`` only. Every signal counted here was written by a
    deterministic tier, which is what makes the corroboration count reproducible
    — re-running this on an unchanged corpus returns the same findings.
    """
    profile = ClusterProfile(cluster_id=int(cluster_id))

    rows = db.fetchall(
        """
        SELECT session_id, markers, flag_reasons, flagged, facet,
               bodies_available
        FROM distill_facets
        WHERE cluster_id = ?
        ORDER BY session_id
        """,
        (int(cluster_id),),
    )

    for row in rows:
        sid = row["session_id"]
        profile.size += 1
        profile.session_ids.append(sid)
        if row["flagged"]:
            profile.flagged += 1
        if not row["bodies_available"]:
            profile.bodies_missing += 1

        for marker in _json_list(row["markers"]):
            profile.all_markers.add(str(marker))
            profile.signals.setdefault(f"marker:{marker}", set()).add(sid)

        for reason in _json_list(row["flag_reasons"]):
            profile.signals.setdefault(f"flag:{reason}", set()).add(sid)

        try:
            facet = json.loads(row["facet"] or "")
        except (TypeError, ValueError):
            facet = None
        if not isinstance(facet, dict):
            continue

        outcome = str(facet.get("outcome") or "unclear")
        profile.outcomes[outcome] = profile.outcomes.get(outcome, 0) + 1
        if outcome in _DEFECT_OUTCOMES:
            profile.signals.setdefault(f"outcome:{outcome}", set()).add(sid)

        task_type = str(facet.get("task_type") or "").strip()
        if task_type:
            profile.task_types[task_type] = profile.task_types.get(task_type, 0) + 1

        failure = facet.get("failure_mode")
        if failure:
            profile.failure_modes.append(str(failure)[:200])
        summary = str(facet.get("summary") or "").strip()
        if summary:
            profile.summaries.append((sid, summary[:300]))

    return profile


_SYSTEM_PROMPT = (
    "You diagnose defects in an agent infrastructure system by reading "
    "aggregated evidence from its own session transcripts. Reply with one JSON "
    "object and nothing else. No preamble, no code fence, no commentary."
)

_PROMPT = """\
You are looking at ONE CLUSTER of agent sessions from okuro, an agent \
infrastructure system. The sessions were grouped by semantic similarity, so \
they are the same KIND of work. Deterministic counting has already established \
which defects recur across this cluster; your job is to say what okuro should \
CHANGE, not to rediscover the defects.

## Cluster {cluster_id}: {size} sessions
Dominant work: {task_types}
Outcomes: {outcomes}
Sessions flagged for review: {flagged}

## Corroborated findings (each seen in at least {threshold} DISTINCT sessions)
{findings}

## Failure phrases the per-session pass recorded
{failures}

## Sample session summaries
{summaries}

## What you may target
`target_surface` MUST be exactly one of:
- `subagent_brief` — the spawn template every subagent brief is built from
- `enforcement_hook` — a gate in okuro's own MCP middleware (provider-agnostic)
- `role` — a role body, or a pitfall attached to a role
- `user_profile` — the behavioural contract
- `principle` — the DP/SYS/ORCH principle set
- `bootstrap` — what the startup packet injects or suppresses
- `memory_hygiene` — memory decay, dedupe, acknowledgement

`target_ref` MUST be exactly one of these okuro-internal locations, or null:
{targets}

`markers` MUST be a subset of the markers this cluster actually carries:
{available_markers}
A marker not in that list makes the lesson unverifiable and it will be dropped.

`lesson_class` MUST be one of:
- `instruction-defect` — okuro told the agent the wrong thing, or nothing
- `role-pitfall` — a specific role leads agents into this
- `protocol-gap` — the protocol has no rule covering this situation
- `harness-bug` — the machinery itself misbehaves; instructions cannot fix it

## Hard constraints
okuro runs the same brain across Claude Code, Codex, Antigravity and Cursor. \
NEVER propose a hand-written provider-native mechanism — a Claude Code hook, a \
CLAUDE.md / AGENTS.md / .cursorrules edit, a settings.json entry, a slash \
command. Those bind okuro to one front-end and will be rejected automatically.

NEVER name a user project, a file path outside okuro, a person, or a customer. \
The mechanism is general; the data is personal. A lesson that only makes sense \
for one project is not a lesson.

## Task
Propose at most {max_lessons} lessons. Each must be specific enough to act on \
without further investigation, and must be supported by the corroborated \
findings above — not by your general knowledge of what agents get wrong.

Reject your own idea if it is advice rather than a change, if it restates a \
finding without saying what to alter, or if the evidence does not support it \
across at least {threshold} sessions.

Returning an empty list is a valid and useful answer.

## Output
STRICT JSON only:
{{"lessons": [{{"lesson_text": "...", "lesson_class": "...", \
"target_surface": "...", "target_ref": "...", "markers": ["..."]}}]}}
"""


def _render_findings(findings, profile: ClusterProfile) -> str:
    if not findings:
        return "(none)"
    return "\n".join(
        f"- `{f.key}` — {len(f.sessions)} of {profile.size} sessions"
        for f in findings
    )


def build_prompt(profile: ClusterProfile, findings, threshold: int) -> str:
    """The exact text sent for one cluster. Pure — the pilot script prints it."""
    from .lessons import LESSON_TARGETS

    task_types = ", ".join(
        f"{t} ({n})"
        for t, n in sorted(profile.task_types.items(),
                           key=lambda kv: -kv[1])[:5]
    ) or "(unknown)"
    outcomes = ", ".join(
        f"{o}: {n}" for o, n in sorted(profile.outcomes.items())
    ) or "(none recorded)"
    failures = "\n".join(
        f"- {f}" for f in profile.failure_modes[:_MAX_FAILURE_SAMPLES]
    ) or "(none recorded)"
    summaries = "\n".join(
        f"- {s}" for _, s in profile.summaries[:_MAX_EVIDENCE_SAMPLES]
    ) or "(none recorded)"
    targets = "\n".join(f"- `{t}`" for t in LESSON_TARGETS) + (
        "\n- `role.pitfall:<role_id>` — for an existing okuro role id"
    )
    markers = ", ".join(f"`{m}`" for m in sorted(profile.all_markers)) or "(none)"

    return _PROMPT.format(
        cluster_id=profile.cluster_id,
        size=profile.size,
        task_types=task_types,
        outcomes=outcomes,
        flagged=profile.flagged,
        threshold=threshold,
        findings=_render_findings(findings, profile),
        failures=failures,
        summaries=summaries,
        targets=targets,
        available_markers=markers,
        max_lessons=MAX_LESSONS_PER_CLUSTER,
    )


def _parse(raw: str) -> list[dict]:
    """Coerce a model reply into a list of lesson dicts. Never raises."""
    if not raw or not raw.strip():
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return []
    blob = text[start : end + 1]
    try:
        parsed = json.loads(blob)
    except (TypeError, ValueError):
        try:
            from json_repair import repair_json

            parsed = json.loads(repair_json(blob))
        except Exception:  # noqa: BLE001 — unparseable is a counted refusal
            return []
    if not isinstance(parsed, dict):
        return []
    lessons = parsed.get("lessons")
    return [x for x in lessons if isinstance(x, dict)] if isinstance(lessons, list) else []


def validate_lesson(candidate: dict, profile: ClusterProfile) -> tuple[dict | None, str]:
    """Coerce one synthesised lesson, or reject it with a reason.

    Grounding, not correction. A lesson naming a marker the cluster never
    showed is dropped rather than repaired: the marker set is what makes the
    lesson measurable later, and silently substituting a different one would
    produce a proposal whose baseline measures something nobody proposed.
    """
    from okuro.sense.interaction.improve import validate_proposal

    from .lessons import validate_target_ref

    text = str(candidate.get("lesson_text") or "").strip()
    if len(text) < 20:
        return None, "lesson_text too short to act on"

    lesson_class = str(candidate.get("lesson_class") or "").strip()
    if lesson_class not in LESSON_CLASSES:
        return None, f"lesson_class {lesson_class!r} not in vocabulary"

    surface = str(candidate.get("target_surface") or "").strip()
    from okuro.sense.interaction.improve import SURFACES

    if surface not in {s.id for s in SURFACES}:
        return None, f"target_surface {surface!r} is not a routable surface"

    raw_ref = candidate.get("target_ref")
    target_ref = str(raw_ref).strip() if raw_ref not in (None, "", "null") else None
    if target_ref is not None and not validate_target_ref(target_ref):
        return None, f"target_ref {target_ref!r} outside the closed vocabulary"

    markers = [
        str(m).strip()
        for m in (candidate.get("markers") or [])
        if str(m).strip()
    ]
    grounded = [m for m in markers if m in profile.all_markers]
    if not grounded:
        # Without a marker the lesson can be proposed but never verified, and
        # an unverifiable proposal is exactly the "accumulates advice" failure
        # interaction_improvements was built to stop.
        return None, "no marker grounded in this cluster's evidence"

    # The same deterministic provider-agnosticism gate the proposal path uses.
    # Reused rather than restated so one definition of "provider-specific"
    # governs both writers into interaction_improvements.
    reason = validate_proposal({
        "title": text[:200],
        "proposal": text,
        "rationale": "",
        "target_ref": target_ref or "",
    })
    if reason:
        return None, reason

    return {
        "lesson_text": text,
        "lesson_class": lesson_class,
        "target_surface": surface,
        "target_ref": target_ref,
        "markers": sorted(set(grounded)),
    }, ""


def _existing_similar(db, text: str) -> int | None:
    """Return the id of an existing lesson that already says this, or None.

    Embedding dedup rather than string matching: the model paraphrases, and two
    phrasings of one defect must not both land in the playbook.

    A failure to embed is NOT treated as "no duplicate" — it returns None and
    the caller writes the lesson, which is the safe direction: a duplicate is
    noise, whereas silently dropping a lesson because the embed service was
    down loses a finding that cost a quality-tier call to produce.
    """
    from .lessons import VEC_TABLE

    try:
        from okuro.embed.client import embed_one, to_bytes
    except Exception:  # noqa: BLE001 — embed unavailable is not a duplicate
        return None
    try:
        blob = to_bytes(embed_one(text))
    except Exception as exc:  # noqa: BLE001
        log.warning("mining: dedup embed failed (%s)", exc)
        return None

    try:
        rows = db.fetchall(
            f"""
            SELECT id, distance FROM {VEC_TABLE}
            WHERE embedding MATCH ? AND k = 3
            """,
            (blob,),
        )
    except Exception as exc:  # noqa: BLE001 — table may not exist yet
        log.debug("mining: dedup query skipped (%s)", exc)
        return None

    for row in rows:
        # cosine distance; the table is declared distance_metric=cosine by
        # embed/repair.py, so 1 - distance is a similarity here and nowhere
        # else. See that module's docstring for what goes wrong under L2.
        if (1.0 - float(row["distance"])) >= DEDUP_SIMILARITY:
            try:
                return int(row["id"])
            except (TypeError, ValueError):
                return None
    return None


@dataclass
class MiningRun:
    clusters_considered: int = 0
    clusters_profiled: int = 0
    clusters_with_findings: int = 0
    model_calls: int = 0
    lessons_written: int = 0
    duplicates: int = 0
    prompt_chars: int = 0
    output_chars: int = 0
    rejected: list = field(default_factory=list)
    refused_by_reason: dict = field(default_factory=dict)

    def refuse(self, reason: str) -> None:
        self.refused_by_reason[reason] = self.refused_by_reason.get(reason, 0) + 1


def mine_cluster(db, cluster_id: int, run: MiningRun, *, threshold: int,
                 capability: str = "quality", dry_run: bool = False) -> list[int]:
    """Profile one cluster, synthesise lessons, store the candidates.

    Returns the ids written. A cluster with no corroborated finding returns
    early and costs NOTHING — no prompt is built and no model is called.
    """
    from .lessons import store_candidate

    profile = profile_cluster(db, cluster_id)
    run.clusters_profiled += 1
    if profile.size == 0:
        run.refuse("empty_cluster")
        return []

    findings = profile.findings(threshold)
    if not findings:
        run.refuse("no_corroborated_finding")
        return []
    run.clusters_with_findings += 1

    prompt = build_prompt(profile, findings, threshold)
    run.prompt_chars += len(prompt)
    if dry_run:
        return []

    from okuro.bridge.invoke import invoke

    reply = invoke(
        prompt=prompt,
        capability=capability,
        system_prompt=_SYSTEM_PROMPT,
        tool=True,
        isolated=True,
    )
    run.model_calls += 1
    output = reply.get("output") or ""
    run.output_chars += len(output)
    if not reply.get("success"):
        run.refuse(f"invoke_failed: {str(reply.get('error'))[:80]}")
        return []

    candidates = _parse(output)
    if not candidates:
        run.refuse("unparseable_or_empty")
        return []

    written: list[int] = []
    for raw in candidates[:MAX_LESSONS_PER_CLUSTER]:
        lesson, reason = validate_lesson(raw, profile)
        if lesson is None:
            run.rejected.append({"cluster": cluster_id, "reason": reason})
            log.info("mining: rejected lesson for cluster %s — %s",
                     cluster_id, reason)
            continue

        duplicate = _existing_similar(db, lesson["lesson_text"])
        if duplicate is not None:
            run.duplicates += 1
            log.info("mining: cluster %s lesson duplicates existing #%s",
                     cluster_id, duplicate)
            continue

        # Evidence is the sessions that asserted the findings this lesson is
        # measured by — not every session in the cluster. A lesson claiming
        # 400 evidence sessions when 6 showed the defect would make its own
        # corroboration count meaningless.
        evidence: set = set()
        for finding in findings:
            if finding.key.startswith("marker:") and \
                    finding.key.split(":", 1)[1] in lesson["markers"]:
                evidence |= set(finding.sessions)
        if not evidence:
            for finding in findings:
                evidence |= set(finding.sessions)

        lesson_id = store_candidate(
            db,
            cluster_id=cluster_id,
            evidence_session_ids=sorted(evidence),
            model=reply.get("model"),
            **lesson,
        )
        written.append(lesson_id)
        run.lessons_written += 1

    return written


def mine_all(db=None, *, cluster_ids=None, threshold: int | None = None,
             capability: str = "quality", dry_run: bool = False,
             force: bool = False) -> dict:
    """Mine every clustered population. The daemon and the pilot both land here.

    ``force`` bypasses ``distill.mining_enabled`` and exists for the pilot
    script, which runs one measured pass under human supervision. The daemon
    never passes it — that is what keeps mining dark until the owner enables it.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()

    run = MiningRun()
    if not force and not mining_enabled():
        return {
            "enabled": False,
            "reason": "distill.mining_enabled is false — no clusters mined, "
                      "no model calls made",
            **_as_dict(run),
        }

    limit = threshold if threshold is not None else min_corroboration()

    if cluster_ids is None:
        cluster_ids = [
            r["cluster_id"]
            for r in db.fetchall(
                """
                SELECT DISTINCT cluster_id FROM distill_facets
                WHERE cluster_id IS NOT NULL
                ORDER BY cluster_id
                """
            )
        ]
    cluster_ids = list(cluster_ids)
    run.clusters_considered = len(cluster_ids)

    for cid in cluster_ids:
        try:
            mine_cluster(db, cid, run, threshold=limit,
                         capability=capability, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 — one bad cluster must not end
            # the pass; the reason is counted and the next run retries it.
            log.warning("mining failed for cluster %s: %s", cid, exc)
            run.refuse(type(exc).__name__)

    log.info(
        "distill mining: %d clusters, %d with findings, %d calls, %d lessons",
        run.clusters_considered, run.clusters_with_findings,
        run.model_calls, run.lessons_written,
    )
    return {"enabled": True, "threshold": limit, "dry_run": dry_run,
            **_as_dict(run)}


def _as_dict(run: MiningRun) -> dict:
    return {
        "clusters_considered": run.clusters_considered,
        "clusters_profiled": run.clusters_profiled,
        "clusters_with_findings": run.clusters_with_findings,
        "model_calls": run.model_calls,
        "lessons_written": run.lessons_written,
        "duplicates": run.duplicates,
        "prompt_chars": run.prompt_chars,
        "output_chars": run.output_chars,
        "rejected": run.rejected,
        "refused_by_reason": run.refused_by_reason,
    }
