# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: M3 reviewer pipeline orchestrator — runs deterministic → Critic →
#   Scorer, persists verdict event into M2, returns structured result the
#   engine consumes. Mirrors the M2 compressor's in-process bridge_invoke
#   pattern (no spawned subagent — runs in the orchestrator process).
# index: imports | _collect_deliverables_text | run_review
# AGENT_HEADER_END -->
"""Pipeline: deterministic → Critic → Scorer → verdict event.

Engine entrypoint is :func:`run_review`. It is best-effort by default —
a bridge / DB failure logs but does not abort the engine, the verdict
falls back to ``FAIL`` so the producing subtask is re-prompted instead
of silently shipping. Callers pass ``strict=True`` in tests to surface
exceptions.
"""

from __future__ import annotations

import json as _json
import logging
import time as _time
from datetime import datetime as _dt
from pathlib import Path
from typing import Any, Optional

from okuro.orchestrator.reviewer.deterministic import (
    Check, CheckResult, generate_checks, run_checks, summarize,
)
from okuro.orchestrator.write_intent import undeclared_write_intent

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PR A — reviewer event emission. Pre-fix the reviewer pipeline emitted
# nothing per stage; the FE activity feed (activity-feed.tsx:96-160) had
# wiring to render rows tagged ``subtask_id="reviewer:phase{N}:critic"`` /
# ``:scorer`` but no producer wrote them. Engine emitted only ``review_starting``
# / ``review_complete`` markers around the whole pipeline, so users saw
# "Reviewing…" for 10–60s with zero per-stage progress.
#
# Two surfaces fixed here:
#   1. ``.activity.jsonl`` per-stage rows (thinking / result) so the FE
#      reviewer interleave panel lights up.
#   2. ``log.jsonl`` state-change events (``review_started``, ``critic_finding``,
#      ``scorer_decision``, ``verdict_published``, ``review_complete_event``)
#      that the WS allowlist (``_STATE_CHANGE_EVENTS`` in api/main.py)
#      forwards as fresh snapshots.
#
# All emissions are best-effort: a missing tasks-dir, a read-only volume
# or an import failure must never abort the review pipeline. Errors are
# logged and the pipeline continues with default verdict semantics.
# ---------------------------------------------------------------------------

def _resolve_tasks_dir_for_emit(tasks_dir: Optional[Path]) -> Optional[Path]:
    """WP7 — prefer the engine-threaded ``tasks_dir``; only fall back to
    ``load_config()`` when the caller passed nothing (backward-compat for
    tests / legacy callers). A non-default --config run threads its
    ``config.tasks_dir`` so emits land beside the engine's own log.jsonl,
    not under the DEFAULT tasks_dir (the source of the Errno-2 emit leak).
    """
    if tasks_dir is not None:
        return Path(tasks_dir)
    try:
        from okuro.orchestrator.config import load_config
        return load_config().tasks_dir
    except Exception:
        return None


def _emit_activity_row(task: Any, *, subtask_id: str, role: str,
                        row_type: str, tasks_dir: Optional[Path] = None,
                        **fields: Any) -> None:
    """Append one row to <task_dir>/.activity.jsonl (FE feed surface)."""
    base = _resolve_tasks_dir_for_emit(tasks_dir)
    if base is None:
        return
    task_dir = base / (getattr(task, "id", "") or "")
    try:
        path = task_dir / ".activity.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "type": row_type,
            "subtask_id": subtask_id,
            "role": role,
            "ts": _dt.utcnow().isoformat(),
        }
        row.update(fields)
        with open(path, "a") as fh:
            fh.write(_json.dumps(row) + "\n")
    except Exception as exc:
        log.warning("reviewer: activity emit failed: %s", exc)


def _emit_log_event(task: Any, event_type: str,
                    tasks_dir: Optional[Path] = None, **fields: Any) -> None:
    """Append a state-change event to <task_dir>/log.jsonl."""
    base = _resolve_tasks_dir_for_emit(tasks_dir)
    if base is None:
        return
    try:
        from okuro.orchestrator.state import append_log
    except Exception:
        return
    try:
        payload = {"type": event_type}
        payload.update(fields)
        append_log(getattr(task, "id", ""), payload, base)
    except Exception as exc:
        log.warning("reviewer: log event %s emit failed: %s", event_type, exc)


def _cap_with_marker(body: str, cap: int) -> str:
    """Return ``body`` unchanged when within ``cap``; else LOUD-truncate it.

    The prior silent ``body[:cap]`` corrupted the review loop: a Critic handed
    only the first 8 KB of a 30 KB brief reported later sections as "missing"
    and FAILed deliverables that were in fact complete. Because each author
    revision shifted WHICH 8 KB window was visible, the finding set changed
    every round and the subtask never converged → CAP. This was the dominant
    non-convergence root cause (task-20260616-164328 subtask 1.2).

    The visible marker tells the reviewer it is seeing a FRAGMENT so it flags
    "deliverable too large to review" (cosmetic) instead of hallucinating
    absent content as a load-bearing defect. With the cap now sized to the
    reviewer's context window, real deliverables are never truncated at all.
    """
    if len(body) <= cap:
        return body
    omitted = len(body) - cap
    return (
        body[:cap]
        + f"\n\n[[REVIEW-TRUNCATION: {omitted} characters omitted — this "
        f"deliverable exceeds the {cap}-char review window. You are seeing a "
        f"FRAGMENT, not the whole artifact. Do NOT infer that omitted sections "
        f"are missing, undefined, or incomplete; that content may live past "
        f"this marker. If the visible portion is insufficient to judge a "
        f"criterion, flag it 'cosmetic' (deliverable exceeds review window), "
        f"never 'load_bearing'.]]"
    )


def _collect_deliverables_text(
    checks: list[Check],
    *,
    task: Any = None,
    phase: Any = None,
    tasks_dir: Optional[Path] = None,
    cap_per_file: int = 60000,
) -> dict[str, str]:
    """Read the agent's actual deliverables for Critic+Scorer prompts.

    Two sources, in priority order:

    1. **Brain (Stream B)** — `artifact_write` bodies keyed by
       ``(task_id, subtask_id)``. This is the canonical channel the
       subagent ships work through. Keys are rendered as
       ``brain://<artifact_id> · <title>`` so the Critic prompt sees the
       title context inline with the body.
    2. **Filesystem (fallback)** — files referenced by deterministic
       checks (`outputs[]` / `target_paths[]`). Kept as a safety net for
       legacy plans that still declare bare filenames and for the
       deterministic gate's own file-existence semantics, but no longer
       the primary surface the Critic judges against.

    Prior behavior (filesystem-only) hid every artifact the agent wrote
    via ``artifact_write`` because the Critic only saw paths the
    deterministic gate had Checks for — see audit report
    ``docs/audit-2026-05-26/03-m3-review-loop.md`` for the loop this
    caused.
    """
    out: dict[str, str] = {}

    # Source 1 — brain rows for this phase's subtasks.
    try:
        from okuro.sense.artifacts import artifact_list, artifact_get
    except Exception:
        artifact_list = None  # type: ignore
        artifact_get = None  # type: ignore

    if artifact_list and artifact_get and task is not None and phase is not None:
        task_id = getattr(task, "id", "") or ""
        subtasks = list(getattr(phase, "subtasks", []) or [])
        for st in subtasks:
            st_id = getattr(st, "id", "") or ""
            if not task_id or not st_id:
                continue
            try:
                # include_superseded=False excludes prior-loop deliverables
                # (confidence dropped to 0.1 by artifact_supersede_subtask on
                # the previous retry). Critical for retry loops — without it
                # the reviewer would see contradictions between loops as if
                # both were current truth and FAIL forever.
                rows = artifact_list(
                    task_id=task_id, subtask_id=st_id,
                    limit=20, include_superseded=False,
                )
            except Exception as exc:
                log.warning("reviewer: artifact_list(%s,%s) failed: %s", task_id, st_id, exc)
                rows = []
            for row in rows:
                aid = row.get("id") or ""
                if not aid:
                    continue
                try:
                    full = artifact_get(aid, include_body=True) or {}
                except Exception as exc:
                    log.warning("reviewer: artifact_get(%s) failed: %s", aid, exc)
                    continue
                body = full.get("body") or ""
                if not body:
                    continue
                title = full.get("title") or aid
                key = f"brain://{aid} · {title}"
                out[key] = _cap_with_marker(body, cap_per_file)

    # Source 2 — filesystem files referenced by deterministic checks.
    # WP7 — relative deliverable paths must NOT resolve against the engine
    # subprocess CWD (~/.okuro/orchestrator) where nothing lands. Resolve
    # them against the task's artifacts dir under the engine-threaded
    # tasks_dir first, then the task dir, before declaring them missing.
    task_id_for_paths = (getattr(task, "id", "") or "") if task is not None else ""
    search_bases: list[Path] = []
    if tasks_dir is not None and task_id_for_paths:
        search_bases.append(Path(tasks_dir) / task_id_for_paths / "artifacts")
        search_bases.append(Path(tasks_dir) / task_id_for_paths)

    def _resolve_source2(path: str) -> Optional[Path]:
        p = Path(path)
        if p.is_absolute():
            return p if (p.exists() and p.is_file()) else None
        # Try CWD (legacy behavior) then each engine-threaded base.
        for cand in [p, *(base / path for base in search_bases)]:
            if cand.exists() and cand.is_file():
                return cand
        return None

    seen: set[str] = set()
    for c in checks:
        candidates: list[str] = []
        if c.kind == "term_consistency":
            candidates.extend(c.params.get("files") or [])
        elif c.kind in {"file_exists", "schema_assert"}:
            candidates.append(c.params.get("path", ""))
        elif c.kind in {"link_integrity", "secret_name"}:
            candidates.append(c.params.get("file", ""))
        for path in candidates:
            if not path or path in seen:
                continue
            seen.add(path)
            p = _resolve_source2(path)
            if p is None:
                continue
            try:
                data = p.read_bytes()
            except OSError as exc:
                log.warning("reviewer: failed to read %s: %s", p, exc)
                continue
            # Binary deliverables (PDF, xlsx, images) carry NUL bytes, which are
            # valid UTF-8 and survive errors="replace" — they then crash the
            # Critic bridge call ("embedded null byte") on every retry. Inlining
            # raw binary as review text is worthless anyway, so substitute a
            # marker instead of the bytes.
            if b"\x00" in data[:8192]:
                txt = f"[binary file: {len(data)} bytes — not text-reviewed]"
            else:
                txt = data.decode("utf-8", errors="replace")
            out[str(p)] = _cap_with_marker(txt, cap_per_file)
    return out


def _phase_artifact_kinds(task: Any, phase: Any) -> list[str]:
    """Collect the artifact kinds of the latest deliverable per subtask.

    Mirrors ``_collect_deliverables_text`` source-1: one ``artifact_list`` per
    subtask, reading the already-returned ``kind`` column (no body fetch). Used
    to resolve the kind-aware review profile.
    """
    task_id = (getattr(task, "id", "") or "") if task is not None else ""
    kinds: list[str] = []
    if not task_id:
        return kinds
    try:
        from okuro.sense.artifacts import artifact_list
    except Exception:
        return kinds
    for st in getattr(phase, "subtasks", []) or []:
        st_id = getattr(st, "id", "") or ""
        if not st_id:
            continue
        try:
            rows = artifact_list(
                task_id=task_id, subtask_id=st_id,
                limit=20, include_superseded=False,
            )
        except Exception:
            rows = []
        for row in rows:
            k = (row.get("kind") or "").strip()
            if k:
                kinds.append(k)
    return kinds


def _resolve_review_profile(task: Any, phase: Any):
    """Resolve the strictest review profile for ``phase``'s deliverables."""
    from okuro.orchestrator.reviewer.profiles import (
        DEFAULT_PROFILE, resolve_phase_profile,
    )
    try:
        return resolve_phase_profile(_phase_artifact_kinds(task, phase))
    except Exception:
        return DEFAULT_PROFILE


def _has_unresolved_open_question(events: Optional[list[dict]], phase: Any) -> bool:
    """True when a subtask of ``phase`` emitted an open_question event.

    An ``open_question`` is the subagent flagging a decision it cannot make
    without the user (e.g. a policy waiver). For kinds whose profile permits
    it, this routes the verdict to NEEDS_USER — ask the user — instead of
    FAIL → CAP looping.
    """
    phase_subtasks = {
        str(getattr(st, "id", "")) for st in getattr(phase, "subtasks", []) or []
    }
    for e in events or []:
        etype = (e.get("event_type") or e.get("type") or "").lower()
        if etype != "open_question":
            continue
        sid = str(e.get("subtask_id") or "")
        if not phase_subtasks or sid in phase_subtasks:
            return True
    return False


def _fast_track_eligible(task: Any, phase: Any, det_summary: dict) -> bool:
    """P6.7's hard floor, as one function so no caller can meet three of four.

    ALL of these must hold:

      * the task opted in (``task.fast_track``) — never inferred;
      * EVERY subtask in the phase is risk LOW. One MED subtask in a phase
        makes the whole phase LLM-reviewed, because the reviewer's unit is
        the phase and a per-subtask skip would grade the MED work with the
        LOW work's rigour;
      * deterministic ran and found NOTHING — not "nothing load-bearing".
        A cosmetic deterministic failure is a signal the critic is good at
        pulling threads from, and the whole premise of skipping the critic
        is that there was nothing to look at;
      * at least one check actually ran. Zero checks is not a clean bill of
        health, it is an absence of evidence, and fast-tracking on it would
        turn "we could not check" into "PASS".
      * no subtask says it will change files without declaring which. The
        decomposer's risk floor already raises those to MED, so this is the
        second net — for a plan that never went through validation (a
        hand-edited plan.yaml, a compiled flow, a task planned before the
        floor existed). Measured 2026-08-01: a subtask whose own description
        said it would rewrite shebang lines scored LOW, and fast-track then
        published both rounds in ~2 ms with zero findings while it wrote 8
        files outside any workspace.
    """
    if not getattr(task, "fast_track", False):
        return False

    subtasks = list(getattr(phase, "subtasks", None) or [])
    if not subtasks:
        return False
    for st in subtasks:
        if str(getattr(st, "risk", "") or "").strip().upper() != "LOW":
            return False
        if undeclared_write_intent(st):
            return False

    if int(det_summary.get("total_checks", 0) or 0) <= 0:
        return False
    if int(det_summary.get("failed_checks", 0) or 0) > 0:
        return False
    return True


def run_review(
    *,
    task: Any,
    phase: Any,
    adrs: Optional[list[dict]] = None,
    events: Optional[list[dict]] = None,
    provider: Optional[str] = None,
    critic_model: Optional[str] = None,
    scorer_model: Optional[str] = None,
    review_model: Optional[str] = None,
    review_model_conceptual: Optional[str] = None,
    persist_event: bool = True,
    strict: bool = False,
    tasks_dir: Optional[Path] = None,
    prior_findings: Optional[list] = None,
    autofix_terms: bool = True,
    queued_at: Optional[str] = None,
) -> dict:
    """Run the three-stage review for ``phase`` of ``task``.

    ``queued_at`` (ROCK-SOLID v5 P0#5, cheap instrumentation for the P6
    efficiency phase) is an ISO timestamp the caller stamps when the
    reviewable artifact became ready — e.g. ``subtask.completed_at``. When
    given, ``stage_timings.queue_wait_s`` measures the gap between that
    moment and this call actually starting (dispatch-loop scheduling delay,
    not a stage the reviewer itself controls). ``None`` omits the field
    rather than fabricating a number — most unit-test callers won't have it.

    Returns dict::

        {
          "verdict": "PASS" | "CONDITIONAL" | "FAIL",
          "deterministic": {summary fields ...},
          "deterministic_results": [CheckResult dicts ...],
          "critic_findings": [...],
          "scorer": {...},
          "verdict_event_id": str | None,
          "short_circuited": bool,    # True when det stage failed and LLM stages were skipped
          "stage_timings": {          # wall-clock seconds; queue_wait_s only when queued_at given
              "queue_wait_s": float, "deterministic_s": float,
              "critic_s": float, "scorer_s": float,
          },
          "error": "",
        }

    ``persist_event=False`` is the test-friendly path — runs the
    pipeline without writing to the M2 event log.

    ``tasks_dir`` (WP7) is the engine's resolved ``config.tasks_dir``,
    threaded end-to-end so the emit helpers, deterministic file checks,
    and Critic/Scorer activity sinks all resolve against the SAME tasks
    directory the engine dispatched under. ``None`` keeps the
    ``load_config()`` / env fallback for legacy callers and unit tests.
    """
    adrs = adrs or list(getattr(task, "adrs", []) or [])
    events = events if events is not None else _load_events_safe(task, strict=strict)
    # Kind-aware review profile — resolved once, read by the deterministic
    # filter, the critic AC mode, and the NEEDS_USER verdict branch below.
    profile = _resolve_review_profile(task, phase)

    # Reviewer model tier — a haiku-class Critic/Scorer cannot judge a max-tier
    # architecture/strategy brief: it misreads disclosed conditions as defects
    # and hallucinates "missing" sections. The streaming + engine callers thread
    # the configured ``review_model`` (else this silently fell back to haiku at
    # critic.py/scorer.py). Conceptual deliverables (plan/report) escalate to
    # ``review_model_conceptual`` (strategic tier) so review capability matches
    # the gravity of the work. Explicit ``critic_model``/``scorer_model`` args
    # still win (tests / callers that pin a model); None preserves legacy haiku.
    _conceptual = getattr(profile, "critic_ac_mode", "auto") == "conceptual"
    _tier_model = (review_model_conceptual or review_model) if _conceptual else review_model
    if critic_model is None:
        critic_model = _tier_model
    if scorer_model is None:
        scorer_model = _tier_model
    # Tier FLOOR (never haiku) — a gemini/codex CLI profile leaves every
    # tier_map value '' (config.py:341-359), so review_model /
    # review_model_conceptual resolve to '' and the downstream
    # ``model or "haiku"`` in critic.py:552 / scorer.py:304 silently collapses
    # the reviewer to HAIKU — exactly the haiku-judging-a-max-tier-brief failure
    # the conceptual tier was added (pipeline.py:373-380) to prevent. The
    # reviewer always runs on the claude provider (critic.py:546), so coerce an
    # empty/None resolved model to a concrete claude tier floored at the
    # producer's tier: a max-intelligence producer (opus, dispatcher.py:337-338)
    # gets an opus reviewer; everyone else sonnet. An explicit pinned
    # critic_model/scorer_model is truthy and passes through untouched.
    # Provider-aware floor: claude uses its tier aliases (sonnet/opus, which
    # auto-track latest); a NON-claude provider (codex/openai etc.) gets ""
    # so its own CLI picks the account-appropriate default model — never a
    # claude literal (provider-agnostic; lets `preferred_cli=codex` review on
    # the openai model instead of failing with a claude/model mismatch).
    if (provider or "claude") == "claude":
        _floor = "opus" if getattr(task, "intelligence", "") == "max" else "sonnet"
    else:
        _floor = ""
    if not critic_model:
        critic_model = _floor
    if not scorer_model:
        scorer_model = _floor

    # PR A — phase-scoped review_started event so FE re-snapshots immediately
    # rather than after the full deterministic→critic→scorer chain.
    # WP-H — coerce to int. A None/"" phase id produced `reviewer:phase:critic`
    # rows (present in live logs), which activity-feed.tsx cannot parse and
    # drops outright — the reviewer's own output silently vanishing from the
    # feed. The prefix must always carry a number.
    try:
        phase_id_for_emit = int(getattr(phase, "id", 0) or 0)
    except (TypeError, ValueError):
        log.warning(
            "reviewer: unparseable phase id %r — tagging rows as phase 0",
            getattr(phase, "id", None),
        )
        phase_id_for_emit = 0
    reviewer_subtask_prefix = f"reviewer:phase{phase_id_for_emit}"
    # WP7 — normalize the threaded tasks_dir once; every emit/check/sink
    # below uses this single source. None preserves legacy env/default
    # resolution inside each helper.
    _td = Path(tasks_dir) if tasks_dir is not None else None

    _stage_timings: dict = {}
    if queued_at:
        try:
            _queue_wait = (_dt.utcnow() - _dt.fromisoformat(queued_at)).total_seconds()
            _stage_timings["queue_wait_s"] = round(max(_queue_wait, 0.0), 3)
        except (TypeError, ValueError) as exc:
            log.warning("reviewer: unparseable queued_at %r: %r", queued_at, exc)

    if persist_event:
        _emit_log_event(task, "review_started", phase_id=phase_id_for_emit, tasks_dir=_td)

    # --- stage 1: deterministic --------------------------------------------------
    _t_stage0 = _time.monotonic()
    if persist_event:
        _emit_activity_row(
            task, subtask_id=f"{reviewer_subtask_prefix}:deterministic",
            role="reviewer-deterministic", row_type="thinking",
            tasks_dir=_td,
            preview="Running deterministic checks (file_exists, term_consistency, schema).",
        )
    checks = generate_checks(
        task=task, phase=phase, adrs=adrs, events=events, tasks_dir=_td,
        profile=profile,
    )
    det_results = run_checks(checks)
    det_summary = summarize(det_results)

    # PR Pillar-1 — auto-apply deterministic terminology substitutions to brain
    # artifacts (the exact term→replacement is already known) instead of FAILing
    # the deliverable → cold subagent re-dispatch. ~43% of load-bearing FAILs
    # were pure string replacements looped through a full respawn. Re-run the
    # deterministic stage against the corrected bodies so the verdict reflects
    # the fixed text. Best-effort: any failure leaves the legacy path intact.
    if autofix_terms:
        try:
            from okuro.orchestrator.reviewer.deterministic import (
                autofix_term_consistency,
            )
            _n_fixed = autofix_term_consistency(det_results)
        except Exception as exc:
            log.warning("reviewer: terminology autofix failed: %s", exc)
            _n_fixed = 0
        if _n_fixed:
            checks = generate_checks(
                task=task, phase=phase, adrs=adrs, events=events, tasks_dir=_td,
                profile=profile,
            )
            det_results = run_checks(checks)
            det_summary = summarize(det_results)
            log.info(
                "reviewer: terminology autofix corrected %d artifact(s); re-ran "
                "deterministic (failed_checks now %s)",
                _n_fixed, det_summary.get("failed_checks"),
            )

    short_circuit = det_summary["short_circuit"]
    deliverable_text = _collect_deliverables_text(checks, task=task, phase=phase, tasks_dir=_td)

    if persist_event:
        _emit_activity_row(
            task, subtask_id=f"{reviewer_subtask_prefix}:deterministic",
            role="reviewer-deterministic", row_type="result",
            tasks_dir=_td,
            verdict=("FAIL" if short_circuit else "PASS"),
            failed_checks=int(det_summary.get("failed_checks", 0) or 0),
            load_bearing_failures=int(det_summary.get("load_bearing_failures", 0) or 0),
        )

    _t_stage1 = _time.monotonic()
    _stage_timings["deterministic_s"] = round(_t_stage1 - _t_stage0, 3)

    out: dict = {
        "verdict": "FAIL",  # default — overridden by Scorer if everything passes
        "deterministic": det_summary,
        "deterministic_results": [_result_to_dict(r) for r in det_results],
        "critic_findings": [],
        "scorer": {},
        "verdict_event_id": None,
        "short_circuited": short_circuit,
        "stage_timings": _stage_timings,
        "error": "",
    }

    # --- stage 2: critic ---------------------------------------------------------
    if persist_event:
        _emit_activity_row(
            task, subtask_id=f"{reviewer_subtask_prefix}:critic",
            role="reviewer-critic", row_type="thinking",
            tasks_dir=_td,
            preview="Running critic LLM on collected deliverables.",
        )
    # P4 (#8) — GENUINE short-circuit. A deterministic load_bearing failure
    # already FORCES FAIL (verdict reconciliation below); the opus critic+scorer
    # cannot change that verdict. Pre-fix they ran anyway — wasted spend, and
    # their LLM findings then drove rerun_subtasks_from_verdict routing + the
    # convergence trajectory on a verdict that IGNORED them. Skip both bridge
    # calls and surface the DETERMINISTIC load_bearing findings as the verdict
    # basis: they carry subtask_id (routing) + suggested_fix (the blocked_review
    # worklist) + feed carry-across-retry, so every downstream consumer keeps
    # working on the REAL blocking findings. The activity-row + verdict-event
    # emission below runs unchanged on these synthesized findings.
    # ROCK-SOLID v5 P6.7 — fast-track: the INVERSE short-circuit.
    #
    # The branch above skips the LLM stages when deterministic already forces
    # FAIL. This skips them when deterministic passes CLEAN on work the user
    # marked low-risk. Measured on task-20260730-233206: the critic was 4007s
    # of the review pipeline's 4121s, so skipping it is the only change in
    # this phase that moves review time at all.
    #
    # The floor is checked here, not at the caller, because a caller that
    # forgets one clause silently ships unreviewed work — the one failure
    # mode speed must never buy.
    fast_track = (not short_circuit) and _fast_track_eligible(task, phase, det_summary)
    if fast_track:
        out["critic_findings"] = []
        out["critic_raw"] = ""
        out["critic_infra_error"] = False
        out["fast_tracked"] = True
        if persist_event:
            # Visible, not silent. The plan's rule is that speed is never
            # silent: a review that cost nothing must say so in the same feed
            # a full review reports into, or the reader cannot tell a
            # fast-tracked PASS from a reviewed one.
            _emit_activity_row(
                task, subtask_id=f"{reviewer_subtask_prefix}:critic",
                role="reviewer-critic", row_type="result",
                tasks_dir=_td,
                verdict="PASS",
                preview=(
                    "Fast-track: deterministic checks passed clean and every "
                    "part of this step is low risk, so the critic and scorer "
                    "LLM stages were skipped."
                ),
            )
        log.info("reviewer: fast-track PASS for phase %s — critic+scorer skipped",
                 getattr(phase, "id", "?"))
    elif short_circuit:
        out["critic_findings"] = _det_load_bearing_as_findings(det_results)
        out["critic_raw"] = ""
        out["critic_infra_error"] = False
    else:
        from okuro.orchestrator.reviewer.critic import run_critic
        try:
            critic = run_critic(
                task=task,
                phase=phase,
                adrs=adrs,
                events=events,
                deterministic_results=det_results,
                deliverable_text=deliverable_text,
                provider=provider,
                model=critic_model,
                tasks_dir=_td,
                ac_mode=getattr(profile, "critic_ac_mode", "auto"),
                prior_findings=prior_findings,
            )
            out["critic_findings"] = critic.get("findings", [])
            out["critic_raw"] = critic.get("raw", "")
            out["critic_infra_error"] = bool(critic.get("infra_error"))
        except Exception as exc:
            log.exception("critic stage crashed")
            if strict:
                raise
            out["error"] = f"critic stage crashed: {exc}"
            out["stage_timings"]["critic_s"] = round(_time.monotonic() - _t_stage1, 3)
            # default verdict stays FAIL — safer than PASS on a crash
            if persist_event:
                _emit_activity_row(
                    task, subtask_id=f"{reviewer_subtask_prefix}:critic",
                    role="reviewer-critic", row_type="result",
                    tasks_dir=_td,
                    error=str(exc)[:300],
                )
                out["verdict_event_id"] = _persist_verdict(task, phase, out, strict=False)
            return out

    _t_stage2 = _time.monotonic()
    out["stage_timings"]["critic_s"] = round(_t_stage2 - _t_stage1, 3)

    if persist_event:
        # One activity row + one state-change log event per critic finding,
        # so the FE can render finding cards inline (it already wires the
        # event names; the producer side was missing).
        for f in (out.get("critic_findings") or [])[:25]:
            severity = (f or {}).get("severity", "")
            summary = (f or {}).get("summary") or (f or {}).get("evidence", "")
            loc = (f or {}).get("location") or {}
            _emit_activity_row(
                task, subtask_id=f"{reviewer_subtask_prefix}:critic",
                role="reviewer-critic", row_type="critic_finding",
                tasks_dir=_td,
                severity=severity, finding_summary=str(summary)[:300],
                file=loc.get("file") or (f or {}).get("file") or "",
                line=loc.get("line") or (f or {}).get("line") or "",
            )
        _emit_log_event(
            task, "critic_finding",
            tasks_dir=_td,
            phase_id=phase_id_for_emit,
            finding_count=len(out.get("critic_findings") or []),
            load_bearing=len([
                f for f in (out.get("critic_findings") or [])
                if (f or {}).get("severity", "").lower() == "load_bearing"
            ]),
        )

    # --- stage 3: scorer ---------------------------------------------------------
    if persist_event:
        _emit_activity_row(
            task, subtask_id=f"{reviewer_subtask_prefix}:scorer",
            role="reviewer-scorer", row_type="thinking",
            tasks_dir=_td,
            preview="Running scorer LLM on critic findings + deliverables.",
        )
    # Critic-FAIL overrides on load-bearing findings — Scorer cannot soften.
    # Computed BEFORE the scorer stage so the stage can be skipped when its
    # output provably cannot matter (see below).
    load_bearing_critic = [
        f for f in out["critic_findings"]
        if (f or {}).get("severity", "").lower() == "load_bearing"
    ]

    # P4 (#8) — skip the scorer on a deterministic short-circuit: the verdict
    # is already forced to FAIL, so weighing wastes a second opus call.
    #
    # L2 — the same argument, one level up. A load-bearing Critic finding
    # forces FAIL through the reconciliation chain below whatever the Scorer
    # proposes: if it proposes anything but FAIL the override fires, and if it
    # proposes FAIL the verdict is FAIL anyway. So the call is verdict-identical
    # and skipping it is a no-op on behaviour, not a loosening of the gate.
    # Measured over 30 days: 241 of 560 reviews (43%) were in this state.
    # `scorer_skipped_reason` keeps the omission legible in the verdict event
    # rather than looking like a crashed stage.
    # Mirrors ``critic_infra_error``: a FAIL caused by the scorer stage
    # breaking is not a statement about the deliverable, and must never burn a
    # user-subagent retry or be laundered into a pass by the verdict floor
    # below. Set at every branch that produces a FAIL for a non-content reason.
    out["scorer_infra_error"] = False

    if fast_track:
        # P6.7 — stage 3 needs its own branch, and finding that out was the
        # point of reading this far. `out["verdict"]` defaults to FAIL and is
        # OVERRIDDEN BY THE SCORER; skipping only stage 2 would have left
        # every fast-tracked review calling the scorer LLM and then, on the
        # empty finding list it was handed, deciding a verdict on nothing.
        # Half a skip is not half the saving, it is a different bug.
        out["scorer"] = {
            "verdict": "PASS",
            "short_circuited": False,
            "scorer_skipped_reason": (
                "fast-track: deterministic passed clean on all-LOW-risk work"
            ),
        }
        proposed = "PASS"
    elif short_circuit or load_bearing_critic:
        out["scorer"] = {
            "verdict": "FAIL",
            "short_circuited": bool(short_circuit),
            "scorer_skipped_reason": (
                "deterministic load-bearing failure" if short_circuit
                else f"{len(load_bearing_critic)} load-bearing Critic "
                     "finding(s) — verdict already forced to FAIL"
            ),
        }
        proposed = "FAIL"
    else:
        from okuro.orchestrator.reviewer.scorer import run_scorer
        try:
            scorer = run_scorer(
                task=task,
                phase=phase,
                adrs=adrs,
                critic_findings=out["critic_findings"],
                deterministic_results=det_results,
                deliverable_text=deliverable_text,
                provider=provider,
                model=scorer_model,
                tasks_dir=_td,
            )
            out["scorer"] = scorer
            proposed = (scorer.get("verdict") or "FAIL").upper()
        except Exception as exc:
            log.exception("scorer stage crashed")
            if strict:
                raise
            out["error"] = f"scorer stage crashed: {exc}"
            # Infra, not content — same contract the Critic already had.
            out["scorer_infra_error"] = True
            proposed = "FAIL"

    if short_circuit:
        out["verdict"] = "FAIL"
    elif getattr(profile, "allow_needs_user", False) and _has_unresolved_open_question(events, phase):
        # A plan/report flagged an open_question only the user can resolve
        # (e.g. a policy waiver). Route to the user NOW instead of FAIL →
        # 5-round loop → CAP. Reachable only for kinds whose profile permits
        # it; code/evidence still FAIL hard.
        out["verdict"] = "NEEDS_USER"
        out["needs_user_reason"] = (
            "deliverable carries an unresolved open_question requiring a user "
            "decision — routed to the user instead of FAIL/CAP."
        )
    elif load_bearing_critic and proposed != "FAIL":
        out["verdict"] = "FAIL"
        out["override_reason"] = (
            f"Scorer proposed {proposed!r} but {len(load_bearing_critic)} "
            "load-bearing Critic finding(s) present — verdict forced to FAIL "
            "per Critic-owns-recall contract."
        )
    elif proposed in {"PASS", "CONDITIONAL", "FAIL"}:
        out["verdict"] = proposed
    else:
        out["verdict"] = "FAIL"
        out["error"] = f"scorer returned unparseable verdict: {proposed!r}"
        # Unparseable is a broken stage, not a judgement — the Critic already
        # classifies its own unparseable output this way (critic.py).
        out["scorer_infra_error"] = True

    # ── Verdict floor ───────────────────────────────────────────────────────
    # A FAIL with ZERO load-bearing findings is a verdict nobody can act on:
    # there is no worklist for the retry to work from, so the re-run
    # regenerates blind and the next round produces the same nothing. That is
    # the shape that burns the whole retry budget and lands on the user as a
    # cap escalation carrying no findings.
    #
    # The Scorer's own rubric says "Default on uncertainty: FAIL" — deliberate
    # shipping-bias protection — so a content FAIL with no findings is
    # frequently just that default firing, not a defect anyone identified.
    #
    # Raising it to CONDITIONAL PRESERVES the existing invariant rather than
    # bending it: dispatcher_streaming documents that CONDITIONAL is reachable
    # only when there are zero load_bearing findings, and this branch fires
    # only under exactly that condition.
    #
    # Every guard below excludes a FAIL that is NOT "the scorer was harsh":
    #   short_circuit        deterministic load-bearing failure — real, keep FAIL
    #   load_bearing_critic  the Critic found blockers — real, keep FAIL
    #   out["error"]         some stage broke; the FAIL is not about content
    #   critic_infra_error   Critic transport/parse failure
    #   scorer_infra_error   Scorer crash or unparseable verdict
    #   proposed != "FAIL"   only floor a verdict the scorer itself proposed
    # Ordering matters: the infra_error contract (above) has to be in place
    # first, or a crashed stage would be laundered into a pass here.
    if (
        out["verdict"] == "FAIL"
        and not short_circuit
        and not load_bearing_critic
        and not out.get("error")
        and not out.get("critic_infra_error")
        and not out.get("scorer_infra_error")
        and proposed == "FAIL"
    ):
        out["verdict"] = "CONDITIONAL"
        out["floor_reason"] = (
            "Scorer returned FAIL with zero load-bearing findings, no "
            "deterministic short-circuit and no stage error — nothing for a "
            "retry to act on. Floored to CONDITIONAL; any cosmetic findings "
            "are still carried as advisory notes."
        )

    if persist_event:
        _emit_activity_row(
            task, subtask_id=f"{reviewer_subtask_prefix}:scorer",
            role="reviewer-scorer", row_type="result",
            tasks_dir=_td,
            verdict=out["verdict"],
            scorer_verdict=proposed,
            override_reason=out.get("override_reason", "")[:300],
        )
        _emit_log_event(
            task, "scorer_decision",
            tasks_dir=_td,
            phase_id=phase_id_for_emit,
            verdict=out["verdict"],
            scorer_verdict=proposed,
        )
        out["verdict_event_id"] = _persist_verdict(task, phase, out, strict=strict)
        _emit_log_event(
            task, "verdict_published",
            tasks_dir=_td,
            phase_id=phase_id_for_emit,
            verdict=out["verdict"],
            event_id=out.get("verdict_event_id"),
            critic_finding_count=len(out.get("critic_findings") or []),
            short_circuited=bool(out.get("short_circuited")),
        )
        _emit_log_event(
            task, "review_complete_event",
            tasks_dir=_td,
            phase_id=phase_id_for_emit,
            verdict=out["verdict"],
        )
    out["stage_timings"]["scorer_s"] = round(_time.monotonic() - _t_stage2, 3)
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _load_events_safe(task: Any, *, strict: bool) -> list[dict]:
    task_id = getattr(task, "id", None)
    if not task_id:
        return []
    try:
        from okuro.sense.task_events import list_events
        return list_events(task_id=task_id)
    except Exception as exc:
        log.warning("reviewer: list_events failed for task %s: %s", task_id, exc)
        if strict:
            raise
        return []


def _persist_verdict(task: Any, phase: Any, out: dict, *, strict: bool) -> Optional[str]:
    """Append a `verdict` event to the M2 log. None on failure."""
    task_id = getattr(task, "id", None)
    if not task_id:
        return None
    try:
        from okuro.sense.task_events import append_event
    except Exception as exc:
        log.warning("reviewer: task_events import failed: %s", exc)
        if strict:
            raise
        return None

    phase_id = getattr(phase, "id", 0)
    body = {
        "phase_id": int(phase_id) if isinstance(phase_id, (int, str)) else 0,
        "verdict": out["verdict"],
        "deterministic_failed": int(out["deterministic"]["failed_checks"]),
        "deterministic_load_bearing": int(out["deterministic"]["load_bearing_failures"]),
        "critic_finding_count": len(out["critic_findings"]),
        "load_bearing_critic_findings": [
            {
                "file": (f or {}).get("location", {}).get("file") or (f or {}).get("file"),
                "line": (f or {}).get("location", {}).get("line") or (f or {}).get("line"),
                "summary": (f or {}).get("summary") or (f or {}).get("evidence", ""),
            }
            for f in out["critic_findings"]
            if (f or {}).get("severity", "").lower() == "load_bearing"
        ][:25],
        "scorer_rubric": (out.get("scorer") or {}).get("rubric") or {},
        "short_circuited": bool(out["short_circuited"]),
    }
    try:
        return append_event(
            task_id=task_id,
            subtask_id=f"reviewer-phase-{phase_id}",
            from_role="reviewer-pipeline",
            event_type="verdict",
            body=body,
            confidence=0.9,
            created_by="reviewer-pipeline",
            adrs=list(getattr(task, "adrs", []) or []),
        )
    except Exception as exc:
        log.warning("reviewer: append_event(verdict) failed: %s", exc)
        if strict:
            raise
        return None


def has_planned_reviewer(phase: Any) -> bool:
    """True when the phase already includes a planned reviewer subtask.

    Backward-compat helper for the engine: tasks that pre-date M3 ship a
    planned ``role: reviewer`` subtask in plan.yaml. Those finish under
    the old path; new tasks (post-M3 decomposer or ``gates_enabled``)
    skip the planned reviewer and use the engine-level review gate.
    """
    legacy_roles = {"reviewer", "workforce-reviewer", "critic", "scorer"}
    for st in getattr(phase, "subtasks", []) or []:
        role = (getattr(st, "role", "") or "").strip().lower()
        if role in legacy_roles:
            return True
    return False


def _file_overlaps(owned: set, implicated_files: set) -> bool:
    """True if any owned output/target path overlaps an implicated file.

    Exact set intersection first, then substring tolerance for
    repo-relative vs absolute path mismatches (either side may carry the
    other's tail).
    """
    if owned & implicated_files:
        return True
    for f in implicated_files:
        if not f:
            continue
        for own in owned:
            if own and (f.endswith(own) or own.endswith(f)):
                return True
    return False


def _findings_fingerprint(findings: Any) -> frozenset:
    """Stable identity set for one round's findings — ``(file/locus, line,
    subject)`` per finding. Two rounds with the SAME frozenset produced
    byte-identical findings, i.e. the retry made zero forward progress.

    Used by :func:`rerun_subtasks_from_verdict` to escalate-early instead of
    burning the retry budget on a guaranteed re-fail. Robust to the two
    finding shapes (critic dicts and deterministic short-circuit dicts) by
    falling back across the subject-bearing keys.
    """
    out = set()
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        subject = (
            f.get("term") or f.get("summary") or f.get("suggested_fix")
            or f.get("title") or f.get("message") or ""
        )
        out.add((
            str(f.get("file") or f.get("locus") or ""),
            str(f.get("line") or ""),
            str(subject)[:200],
        ))
    return frozenset(out)


def rerun_subtasks_from_verdict(
    *,
    task: Any,
    phase: Any,
    review_result: dict,
    config: Any,
) -> list[str]:
    """Reset subtasks implicated by FAIL findings to ``pending`` with retries+1.

    The orchestrator loop will re-pick them on the next iteration and the
    dispatcher's existing recovery context (G5a) will surface the
    verdict error in the retry prompt. Returns the list of subtask ids
    that were reset. Caller is responsible for skipping the
    phase-done assignment when this list is non-empty.

    Selection logic (in priority order):
      - PRIMARY — owning subtask id. Any finding that carries a
        ``subtask_id`` (e.g. ac_evidence_shape findings, which name the
        owner but carry no ``file``) implicates that subtask directly,
        bypassing the file heuristic. This prevents wrong-subtask retries
        on multi-subtask phases where a file-less finding matched nothing.
      - FALLBACK — file overlap. When a finding has no owner id, its
        ``file`` is matched against each subtask's ``outputs`` /
        ``target_paths`` (with substring tolerance).
      - LAST RESORT — when no finding identified an owner AND no file
        overlapped, the LAST done subtask is reset so the loop makes
        forward progress. This guess is logged at WARNING (never silent).
      - Subtasks already at max_retries are NOT reset — leaving them
        ``done`` makes the returned reset list empty for the capped case,
        so the caller escalates the phase to blocked_review + set_awaiting
        instead of silently advancing.
    """
    from okuro.orchestrator.state import update_subtask

    # If the Critic stage hit a bridge infra error (timeout / connection),
    # the FAIL verdict is not about the deliverable. Don't burn a
    # user-subagent retry — return empty so the engine treats the phase
    # as done. The verdict event is still persisted for telemetry.
    if review_result.get("critic_infra_error") or review_result.get("scorer_infra_error"):
        log.warning(
            "rerun_subtasks_from_verdict: %s stage infra error — "
            "NOT respawning subtask; deliverable retained as-is. "
            "Re-run reviewer offline to obtain a real verdict.",
            "Critic" if review_result.get("critic_infra_error") else "Scorer",
        )
        return []

    findings = review_result.get("critic_findings") or []
    load_bearing = [f for f in findings if (f or {}).get("severity", "").lower() == "load_bearing"]
    det_results = review_result.get("deterministic_results") or []
    # Also include deterministic load-bearing findings (short-circuit case).
    for r in det_results:
        if r.get("passed") or r.get("severity") != "load_bearing":
            continue
        for f in (r.get("findings") or []):
            load_bearing.append({"file": f.get("file"), "line": f.get("line"),
                                 "subtask_id": f.get("subtask_id"),
                                 "summary": f.get("suggested_fix", ""),
                                 "severity": "load_bearing"})

    # ── Fingerprint early-exit (no-progress guard) ──────────────────────────
    # If this round's load-bearing findings are IDENTICAL to the prior round's
    # (carried on each subtask's ``review_findings`` from the last reset),
    # retrying is futile — same deliverable + same checks → same findings.
    # Escalate to the user NOW instead of burning the remaining retry budget on
    # a guaranteed re-fail. Empty on a clean first review (no carried findings),
    # so a genuine first-fail retry is unaffected; the guard fires on the SECOND
    # identical round (escalate-on-repeat, not escalate-at-cap).
    _prior_findings = [
        f for st in (getattr(phase, "subtasks", []) or [])
        for f in (getattr(st, "review_findings", None) or [])
    ]
    _new_fp = _findings_fingerprint(load_bearing)
    if _new_fp and _new_fp == _findings_fingerprint(_prior_findings):
        log.warning(
            "rerun_subtasks_from_verdict: identical findings repeated "
            "(%d load-bearing) — no progress since last round; escalating to "
            "user instead of retrying to cap.", len(_new_fp),
        )
        return []

    # Owning-subtask identity (PRIMARY signal). Some failing findings name
    # the subtask directly but carry NO ``file`` — notably ac_evidence_shape
    # findings, which stamp ``subtask_id`` (deterministic.py) and are
    # ``infra_error`` severity, so they never enter ``load_bearing`` above.
    # Scan EVERY failed deterministic check's findings (not just the
    # load_bearing bucket) plus the critic findings for a ``subtask_id`` so
    # the selection can target the real owner instead of falling through to
    # the last-subtask heuristic on multi-subtask phases.
    implicated_subtask_ids: set = {
        (f or {}).get("subtask_id")
        for f in load_bearing if (f or {}).get("subtask_id")
    }
    for r in det_results:
        if r.get("passed"):
            continue
        for f in (r.get("findings") or []):
            sid = (f or {}).get("subtask_id")
            if sid:
                implicated_subtask_ids.add(sid)

    implicated_files = {(f or {}).get("file") for f in load_bearing if (f or {}).get("file")}
    from okuro.orchestrator.config import DEFAULT_MAX_RETRIES
    max_retries = getattr(getattr(config, "execution", None), "max_retries", DEFAULT_MAX_RETRIES)

    # ── Owner-less load-bearing findings ────────────────────────────────────
    # Findings that name neither a subtask_id nor a file are the NORMAL shape
    # of a Critic FAIL on a prose deliverable (a report, a plan) — critic.py
    # expects document reviews to produce findings without shell/file evidence.
    # With no owner, both selection branches below fall through and EVERY done
    # subtask under the cap gets reset: one unattributable finding re-runs the
    # whole phase, and the agents that re-run have no idea which of them the
    # finding was about.
    #
    # Bounded instead:
    #   exactly one candidate  -> unambiguous owner, reset it (no behaviour
    #                             change; that was already the outcome)
    #   two or more            -> resetting all of them is a guess. Stamp the
    #                             findings and escalate.
    #
    # The stamp is what makes the escalation converge. The fingerprint guard
    # above reads prior findings EXCLUSIVELY from subtask-level
    # ``review_findings`` on the phase, so findings recorded anywhere else are
    # invisible to it and an identical second round would not be recognised as
    # no-progress. Stamping every done subtask puts them where that guard
    # actually looks.
    _unowned = bool(load_bearing) and not implicated_subtask_ids and not implicated_files
    if _unowned:
        _candidates = [
            st for st in (getattr(phase, "subtasks", []) or [])
            if getattr(st, "status", "") == "done"
            and int(getattr(st, "retries", 0) or 0) < max_retries
        ]
        if len(_candidates) > 1:
            for st in _candidates:
                try:
                    update_subtask(
                        task.id, st.id,
                        {"review_findings": list(load_bearing)},
                        config.tasks_dir,
                    )
                except Exception as exc:
                    log.warning(
                        "unowned-findings stamp failed for %s/%s: %s",
                        task.id, st.id, exc,
                    )
            log.warning(
                "rerun_subtasks_from_verdict: %d load-bearing finding(s) name "
                "no subtask and no file, and %d done subtasks are under the "
                "cap — resetting all of them would be a guess. Stamped the "
                "findings on each (so the fingerprint guard sees a repeat) and "
                "escalating instead.",
                len(load_bearing), len(_candidates),
            )
            return []

    reset: list[str] = []
    for st in getattr(phase, "subtasks", []) or []:
        if getattr(st, "status", "") != "done":
            continue
        # Retry invariant: a subtask already at the cap has exhausted its
        # correction budget — do NOT reset it. Leaving it ``done`` makes
        # the returned reset list empty for the capped case, so the caller
        # escalates the phase to blocked_review + set_awaiting instead of
        # silently advancing. A reset to ``retries == max_retries`` is the
        # final correction attempt, which the scheduler still dispatches
        # (it allows ``retries <= max_retries``); the next FAIL escalates.
        if int(getattr(st, "retries", 0) or 0) >= max_retries:
            continue
        # PRIMARY selection: a finding named THIS subtask as the owner.
        # When any finding carries a subtask_id, trust it directly and skip
        # the file-overlap heuristic — file overlap is only a fallback for
        # findings that lack an owner id.
        if st.id in implicated_subtask_ids:
            pass  # selected — fall through to reset
        elif implicated_subtask_ids:
            # Other findings named their owners but none named this subtask.
            # Only file-overlap can still implicate it (a finding may carry
            # a file but no subtask_id). If no file overlaps either, skip.
            owned = {*(getattr(st, "outputs", []) or []),
                     *(getattr(st, "target_paths", []) or [])}
            if not _file_overlaps(owned, implicated_files):
                continue
        elif implicated_files:
            # FALLBACK: no finding identified an owning subtask — match by
            # output/target file overlap (with substring tolerance for
            # repo-relative vs absolute path mismatches).
            owned = {*(getattr(st, "outputs", []) or []),
                     *(getattr(st, "target_paths", []) or [])}
            if not _file_overlaps(owned, implicated_files):
                continue

        err = _verdict_error_message(review_result, st)
        new_retries = int(getattr(st, "retries", 0)) + 1
        # P5 (#6, the DAG twin of P1's #2) — carry the findings THIS subtask
        # owns into its review_findings so the DAG re-dispatch's brief leads
        # with a surgical worklist (build_role_prompt._render_retry_context) and
        # the next phase-gate review VERIFIES them instead of re-hunting. Pre-fix
        # this reset wrote only {status,retries,error}, so the deliberate retry
        # regenerated blind — the same non-convergence the session-loop path had.
        # Owner match: explicit subtask_id (P3 #9/#10), else file-overlap for
        # id-less findings. Only stamp when non-empty (preserve any prior worklist).
        _owned_paths = {*(getattr(st, "outputs", []) or []),
                        *(getattr(st, "target_paths", []) or [])}
        _owned_findings = [
            f for f in load_bearing
            if (f or {}).get("subtask_id") == st.id
            or (
                not (f or {}).get("subtask_id")
                and (f or {}).get("file")
                and _file_overlaps(_owned_paths, {(f or {}).get("file")})
            )
        ]
        _reset_patch = {"status": "pending", "retries": new_retries, "error": err}
        if _owned_findings:
            _reset_patch["review_findings"] = _owned_findings
        try:
            update_subtask(task.id, st.id, _reset_patch, config.tasks_dir)
            reset.append(st.id)
            # Systematic loop-hygiene: mark this subtask's brain rows + role
            # handovers as superseded before the next loop dispatches. Without
            # this the brain accumulates across loops and the reviewer flags
            # current-vs-prior contradictions as load-bearing findings —
            # guaranteeing a permanent FAIL cycle on any retry. Scoped strictly
            # to (task_id, subtask_id); no other task or subtask is touched.
            try:
                from okuro.sense.artifacts import artifact_supersede_subtask
                _n_art = artifact_supersede_subtask(task.id, st.id)
            except Exception as exc:
                log.warning("loop-supersede artifacts failed for %s/%s: %s",
                            task.id, st.id, exc)
                _n_art = 0
            try:
                from okuro.sense.role_handover import supersede_role_handovers_for_subtask
                _n_ho = supersede_role_handovers_for_subtask(task.id, st.id)
            except Exception as exc:
                log.warning("loop-supersede handovers failed for %s/%s: %s",
                            task.id, st.id, exc)
                _n_ho = 0
            # Surface the retry so the UI doesn't look like "the same agent
            # ran twice with no explanation" — show retry N/max + truncated
            # FAIL summary.
            try:
                from okuro.orchestrator.state import append_log
                append_log(task.id, {
                    "type": "subtask_retry",
                    "subtask": st.id,
                    "role": getattr(st, "role", ""),
                    "retries": new_retries,
                    "max_retries": max_retries,
                    "reason": (err or "")[:300],
                    "superseded_artifacts": _n_art,
                    "superseded_handovers": _n_ho,
                }, config.tasks_dir)
            except Exception:
                pass
        except Exception as exc:
            log.warning("rerun_subtasks_from_verdict: failed to reset %s: %s", st.id, exc)

    if not reset and load_bearing:
        # LAST RESORT — no finding identified an owning subtask and no file
        # overlapped any subtask's outputs. Reset the last done subtask so
        # the loop makes forward progress instead of deadlocking. This is a
        # guess: it MUST be logged so a wrong-subtask retry is never silent.
        for st in reversed(list(getattr(phase, "subtasks", []) or [])):
            if getattr(st, "status", "") != "done":
                continue
            # Same retry invariant as the primary loop above: a subtask
            # already at the cap is left ``done`` so the caller escalates.
            if int(getattr(st, "retries", 0) or 0) >= max_retries:
                continue
            log.warning(
                "rerun_subtasks_from_verdict: no subtask_id or file overlap "
                "matched any finding on task=%s phase=%s — falling back to "
                "last-done subtask %s (retry %d). Findings lacked owner "
                "identity; verify the retry targets the right subtask.",
                getattr(task, "id", "?"), getattr(phase, "id", "?"),
                st.id, int(getattr(st, "retries", 0)) + 1,
            )
            err = _verdict_error_message(review_result, st)
            try:
                update_subtask(task.id, st.id, {
                    "status": "pending",
                    "retries": int(getattr(st, "retries", 0)) + 1,
                    "error": err,
                }, config.tasks_dir)
                reset.append(st.id)
                break
            except Exception as exc:
                log.warning("rerun_subtasks_from_verdict: fallback reset of %s failed: %s",
                            st.id, exc)
    return reset


def _verdict_error_message(review_result: dict, subtask: Any) -> str:
    findings = review_result.get("critic_findings") or []
    relevant = [
        f for f in findings
        if (f or {}).get("severity", "").lower() == "load_bearing"
    ][:8]
    parts = [
        "Reviewer-pipeline verdict: FAIL.",
        f"Critic load-bearing findings: {len(relevant)} (showing top 8)",
    ]
    for f in relevant:
        parts.append(
            f"- {(f or {}).get('file', '?')}:{(f or {}).get('line', '?')} — "
            f"{(f or {}).get('summary', '')[:200]} :: "
            f"fix: {(f or {}).get('suggested_fix', '')[:200]}"
        )
    if review_result.get("short_circuited"):
        parts.append("(deterministic stage short-circuited — fix listed issues before re-submitting)")
    return "\n".join(parts)[:4000]


def _result_to_dict(r: CheckResult) -> dict:
    return {
        "kind": r.check.kind,
        "severity": r.check.severity,
        "origin": r.check.origin,
        "passed": r.passed,
        "error": r.error,
        "findings": r.findings,
        "params_summary": _params_summary(r.check),
    }


def _det_load_bearing_as_findings(det_results: list[CheckResult]) -> list[dict]:
    """P4 (#8) — project deterministic load_bearing failures into the critic
    finding shape so a short-circuited review (LLM skipped) still surfaces the
    REAL blocking findings to routing (subtask_id), the blocked_review worklist
    (suggested_fix), and carry-across-retry. One finding per deterministic hit.
    """
    findings: list[dict] = []
    for r in det_results:
        if r.passed or getattr(r.check, "severity", "") != "load_bearing":
            continue
        for f in (r.findings or []):
            findings.append({
                "severity": "load_bearing",
                "subtask_id": (f or {}).get("subtask_id"),
                "file": (f or {}).get("file"),
                "line": (f or {}).get("line"),
                "evidence": ((f or {}).get("evidence") or "")[:600],
                "summary": (
                    (f or {}).get("summary")
                    or (f or {}).get("suggested_fix")
                    or f"deterministic {r.check.kind} check failed"
                )[:400],
                "suggested_fix": ((f or {}).get("suggested_fix") or "")[:600],
                # Carry the check's origin so downstream can tell that several
                # findings came from ONE superseded decision and present them as
                # one thing to decide. Without it the user-facing surface sees
                # unrelated cards and shows a decision three or four times over.
                "origin": getattr(r.check, "origin", "") or "",
                # How many places the term actually appears — the finding is now
                # per-term, not per-line, so this is the count the user needs.
                "occurrence_count": (f or {}).get("occurrence_count") or 1,
            })
    return findings


def _params_summary(c: Check) -> dict:
    """Compact view of params — drop big list of files for log readability."""
    p = dict(c.params)
    files = p.get("files")
    if isinstance(files, list) and len(files) > 3:
        p["files"] = files[:3] + [f"… (+{len(files) - 3} more)"]
    return p
