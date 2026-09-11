# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: F2 autopilot — profile-driven gate auto-answer. When the orchestrator
#   would park on a human decision, resolve_gate picks the most-probable allowed
#   option from the user's profile; autopilot_gate wraps it with a deterministic
#   option guard + confidence floor + durable `autopilot_decision` logging, and
#   tells the caller whether to continue in-process (chosen option) or park (None).
# index: imports | dataclass AutopilotDecision | CONFIDENCE_FLOOR |
#   _profile_context | _build_prompt | _parse_decision | resolve_gate |
#   autopilot_gate
# AGENT_HEADER_END -->
"""F2 autopilot — answer orchestrator gates from the user's profile.

Two layers:

* ``resolve_gate`` — pure resolution. Given a gate (kind + message + allowed
  ``options`` + payload) and a ``profile`` context string, ask the bridge LLM to
  pick ONE of ``options`` with a short rationale and a confidence. It ALWAYS
  returns an :class:`AutopilotDecision`; the deterministic guard forces
  ``confidence=0.0`` (abstain) when the model returns anything outside
  ``options`` — so a mis-answer can never leak past the guard.

* ``autopilot_gate`` — the engine-facing hook. Off ⇒ ``None`` (caller parks as
  today). On ⇒ resolve; if the chosen option is allowed AND confidence ≥
  :data:`CONFIDENCE_FLOOR`, log an ``autopilot_decision`` event and return the
  chosen option (caller applies it in-process + continues). Otherwise ``None``
  (park fallback — nothing is silently mis-answered).

The resolution the engine applies for each option reuses the SAME core the REST
endpoint uses (e.g. ``resolve_decision_gate`` in state.py) — autopilot only
*chooses*; the caller *applies*. No HTTP call to self.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from okuro.clock import utc_now_naive

logger = logging.getLogger(__name__)

# Below this the engine parks instead of auto-answering. A gate is a real
# decision surface; a coin-flip-confident answer is worse than asking.
CONFIDENCE_FLOOR: float = 0.6

# ---------------------------------------------------------------------------
# ROCK-SOLID v5 P2.2 — recurring tasks never park on a human.
#
# A recurring task that parks is a task that silently stops recurring: nobody
# asked for it, nobody is watching it, and the next scheduled run finds the
# previous one still sitting on a gate. The LLM resolver above cannot make
# "never park" true on its own — it abstains on low confidence, and it
# abstains entirely when the bridge is down, which is exactly when a run is
# most likely to hit a gate.
#
# So recurring tasks get a DETERMINISTIC floor under the resolver: on abstain,
# take the option the gate itself names as its safe default. That field
# (``presentation.recommended``) exists for every kind as of P2.1 — this is
# what it was for.
#
# Escalation is the plan's "auto-retry once → skip + log + digest": the first
# time a given gate auto-resolves it takes the recommended action (which for
# most kinds IS retry); if the SAME gate opens again, repeating that action
# would just re-enter the loop, so it skips instead and the digest carries it.
# ---------------------------------------------------------------------------

RECURRING_TASK_TYPE = "recurring"

# Options that mean "give up on this unit and let the run continue", best
# first. Used only for the second occurrence of a gate.
_SKIP_SHAPED_OPTIONS = ("skip", "mark_done", "override", "permanent_fail")


def never_parks(task) -> bool:
    """True when this task must not stop on a human decision (P2.2).

    ``task_type`` is the signal — confirmed present on real recurring tasks
    (e.g. task-rec-20260731-070000-role-refresh). Deliberately a property of
    the TASK, not of the gate: every gate kind a recurring task can hit has
    the same problem, and enumerating them was how the previous attempt at
    this became "5+ sites, each with its own logic".
    """
    return str(getattr(task, "task_type", "") or "") == RECURRING_TASK_TYPE


def _recommended_option(payload: dict | None, options: list[str]) -> str:
    """The gate's own stated safe default, if it is actually on offer.

    Read from the payload rather than taken as a parameter so all seven
    engine call sites gain the behaviour without one of them being edited —
    and so the eighth cannot forget to pass it.
    """
    presentation = (payload or {}).get("presentation") or {}
    rec = str(presentation.get("recommended") or "").strip()
    return rec if rec in options else ""


def _gate_fingerprint(kind: str, ctx: dict) -> str:
    """Identity of a gate for "have we already auto-resolved this one?".

    Kind plus the unit it opened on. NOT the message: copy is re-authorable
    (P2.4), so keying on it would make a re-worded gate look brand new and
    the escalation would never fire.
    """
    return f"{kind}:{ctx.get('phase_id', '')}:{ctx.get('subtask_id', '')}"


def _already_auto_resolved(task_id: str, tasks_dir, fingerprint: str) -> bool:
    """Has this exact gate been auto-resolved before on this task?

    Read from log.jsonl rather than a sidecar file: the record has to survive
    the engine exiting anyway, the log already does that durably, and a new
    state file would need its own lifecycle, cleanup and reconcile story for
    one boolean. Any read failure answers False — a duplicated retry is a
    wasted round, a wrongly-escalated skip drops real work.
    """
    try:
        log_file = tasks_dir / task_id / "log.jsonl"
        if not log_file.exists():
            return False
        for line in log_file.read_text().splitlines():
            if "gate_auto_resolved" not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") == "gate_auto_resolved" and row.get("gate") == fingerprint:
                return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("autopilot: could not read auto-resolve history: %r", exc)
    return False


def _deterministic_fallback(
    task, tasks_dir, *, kind: str, options: list[str], payload: dict, ctx: dict,
) -> tuple[str, str] | None:
    """(option, reason) for a recurring task the resolver would have parked.

    Returns None only when the gate offers nothing to choose — there is no
    honest auto-answer to a gate with no options, and inventing one would be
    worse than the park this exists to avoid.
    """
    if not options:
        return None

    fingerprint = _gate_fingerprint(kind, ctx)
    recommended = _recommended_option(payload, options)

    if _already_auto_resolved(task.id, tasks_dir, fingerprint):
        for candidate in _SKIP_SHAPED_OPTIONS:
            if candidate in options:
                return candidate, (
                    "this gate already auto-resolved once on this recurring task; "
                    "repeating the same action would re-enter the same loop"
                )
        # Nothing skip-shaped on offer. Fall through to the recommendation
        # rather than parking — a recurring task stopping is the outcome this
        # whole policy exists to prevent.

    if recommended:
        return recommended, "recurring task — took the gate's recommended action"
    return options[0], (
        "recurring task — the gate named no recommended action, so the first "
        "offered option was taken"
    )


@dataclass
class AutopilotDecision:
    """One resolver verdict for a gate.

    ``option`` is guaranteed to be a member of the allowed options the resolver
    was given, OR the empty string (abstain). ``confidence`` is clamped to
    ``[0.0, 1.0]``; ``0.0`` means abstain / park.
    """

    option: str
    rationale: str
    confidence: float
    options: list[str] = field(default_factory=list)

    @property
    def confident(self) -> bool:
        return bool(self.option) and self.confidence >= CONFIDENCE_FLOOR


def _profile_context(profile: str | None) -> str:
    """Build a compact persona/preferences block for the resolver prompt.

    ``profile`` empty ⇒ the primary user. We pull the decision-relevant sections
    of okuro's user profile when available; degrade gracefully to a generic
    "the primary user" line so the resolver still runs headless in tests / when
    the store is unreachable. NOTE: a NAMED ``profile`` is not backed by a
    per-persona store yet — the behavioral context is the primary user's, with
    the requested name carried into the framing.
    """
    who = profile or "the primary user"
    try:
        from okuro.yu.profile import get_profile  # type: ignore

        parts = []
        for sect in ("decision_style", "communication", "cognitive_style"):
            block = get_profile(format="markdown", section=sect)
            if block and not block.startswith(("Unknown section", "No profile")):
                parts.append(block.strip())
        if parts:
            return (
                f"You are answering on behalf of {who}. Their profile:\n\n"
                + "\n\n".join(parts)
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("autopilot profile summary unavailable: %r", exc)
    return (
        f"You are answering on behalf of {who}. Decide as they would: prefer "
        "the option that keeps the work moving with the least risk of a wrong, "
        "hard-to-reverse outcome, and that matches a systems-thinking, "
        "quality-over-speed, evidence-driven operator."
    )


def _build_prompt(
    kind: str, message: str, options: list[str], payload: dict, profile: str | None
) -> str:
    """Constrained single-shot prompt: pick exactly one allowed option."""
    opts = ", ".join(json.dumps(o) for o in options)
    payload_txt = ""
    try:
        payload_txt = json.dumps(payload or {}, default=str)[:1500]
    except Exception:
        payload_txt = "{}"
    return (
        f"{_profile_context(profile)}\n\n"
        "The autonomous orchestrator hit a gate that normally waits for a human "
        "decision. Answer it as that person would.\n\n"
        f"GATE KIND: {kind}\n"
        f"GATE MESSAGE:\n{message}\n\n"
        f"CONTEXT (JSON): {payload_txt}\n\n"
        f"ALLOWED OPTIONS (choose EXACTLY ONE, verbatim): [{opts}]\n\n"
        "Respond with ONLY a JSON object, no prose, no code fence:\n"
        '{"option": "<one of the allowed options, verbatim>", '
        '"rationale": "<one short sentence>", '
        '"confidence": <number 0.0-1.0>}\n'
        "If you cannot confidently decide, set confidence below 0.5."
    )


def _parse_decision(raw: str, options: list[str]) -> AutopilotDecision:
    """Parse the resolver's JSON reply and apply the deterministic guard.

    The guard is the safety spine: whatever the model says, the returned
    ``option`` is either a verbatim member of ``options`` or ``""`` (abstain).
    """
    option = ""
    rationale = ""
    confidence = 0.0
    text = (raw or "").strip()
    # Tolerate code fences / surrounding prose — grab the first {...} block.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            option = str(data.get("option", "") or "").strip()
            rationale = str(data.get("rationale", "") or "").strip()
            confidence = float(data.get("confidence", 0.0) or 0.0)
        except (ValueError, TypeError) as exc:
            logger.warning("autopilot: could not parse resolver reply: %r", exc)
    # Deterministic guard — option MUST be one of the allowed options.
    if option not in options:
        # Case-insensitive rescue for trivial casing drift, else abstain.
        lowered = {o.lower(): o for o in options}
        option = lowered.get(option.lower(), "")
        if not option:
            confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    return AutopilotDecision(
        option=option, rationale=rationale, confidence=confidence, options=list(options)
    )


def resolve_gate(
    kind: str,
    message: str,
    options: list[str],
    payload: dict | None = None,
    profile: str | None = None,
    *,
    invoke_fn=None,
) -> AutopilotDecision:
    """Pick one allowed option for a gate. Never raises; abstains on any error.

    ``invoke_fn`` overrides the bridge invoker (tests inject a stub). Default is
    ``okuro.bridge.invoke.invoke`` at ``capability='fast'`` — a gate answer is a
    small, cheap classification, not a deliberation.
    """
    options = [str(o) for o in (options or [])]
    if not options:
        return AutopilotDecision(option="", rationale="no options offered", confidence=0.0)
    prompt = _build_prompt(kind, message, options, payload or {}, profile)
    if invoke_fn is None:
        try:
            from okuro.bridge.invoke import invoke as invoke_fn  # type: ignore
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("autopilot: bridge invoke unavailable: %r", exc)
            return AutopilotDecision(option="", rationale="bridge unavailable", confidence=0.0)
    try:
        result = invoke_fn(
            prompt=prompt,
            capability="fast",
            tool=True,
            system_prompt=(
                "You are a decision resolver. Output ONLY the requested JSON object."
            ),
        )
    except Exception as exc:
        logger.warning("autopilot: resolver invoke failed: %r", exc)
        return AutopilotDecision(option="", rationale=f"invoke error: {exc}", confidence=0.0)
    if not isinstance(result, dict) or not result.get("success"):
        return AutopilotDecision(
            option="", rationale="resolver returned no answer", confidence=0.0
        )
    return _parse_decision(str(result.get("output", "")), options)


def autopilot_gate(
    config,
    task,
    tasks_dir,
    *,
    kind: str,
    options: list[str],
    payload: dict | None = None,
    ctx: dict | None = None,
    invoke_fn=None,
) -> str | None:
    """Engine hook at a gate site. Returns the chosen option, or ``None`` to park.

    ``None`` is returned when: autopilot is off, no options were offered, the
    resolver abstained, or its confidence is below :data:`CONFIDENCE_FLOOR`. On a
    confident answer, an ``autopilot_decision`` event is appended (the durable
    signal F3 rates) and the chosen option string is returned so the caller can
    apply the SAME resolution its REST endpoint would — in-process, no park.
    """
    orch = getattr(config, "orchestrator", None)
    # Task-level override wins; None = inherit the global config default.
    task_flag = getattr(task, "autopilot", None)
    enabled = task_flag if task_flag is not None else getattr(orch, "autopilot", False)
    options = [str(o) for o in (options or [])]
    ctx = dict(ctx or {})
    # P2.2 — a recurring task must not stop on a human, so the resolver runs
    # for it even with autopilot off, and an abstain falls through to the
    # deterministic floor below instead of parking. An explicit
    # autopilot=False is overridden ON PURPOSE and said out loud: the flag
    # asks for gates to be answered by a person, and for a recurring task
    # there is no person in the loop to answer them.
    recurring = never_parks(task)
    if recurring and task_flag is False:
        logger.info(
            "autopilot: task %s sets autopilot=False but is recurring — a park "
            "would stop the schedule, so gates are auto-resolved anyway", task.id,
        )
    if not enabled and not recurring:
        return None
    if not options:
        return None
    profile = getattr(orch, "autopilot_profile", "") or None
    message = str((payload or {}).get("message") or ctx.get("message") or "")
    decision = resolve_gate(
        kind, message, options, payload or {}, profile, invoke_fn=invoke_fn
    )
    if not decision.confident:
        if recurring:
            # THE GUARANTEE. The resolver abstains on low confidence and on
            # any bridge failure — and a bridge failure is precisely when a
            # run hits gates. Without this branch "recurring tasks never
            # park" would hold only while the LLM was available and sure,
            # which is not a guarantee at all.
            fallback = _deterministic_fallback(
                task, tasks_dir, kind=kind, options=options,
                payload=payload or {}, ctx=ctx,
            )
            if fallback is not None:
                chosen, reason = fallback
                _log_auto_resolved(
                    task, tasks_dir, kind=kind, chosen=chosen, reason=reason,
                    options=options, ctx=ctx,
                    abstained_at=decision.confidence,
                )
                return chosen
            logger.warning(
                "autopilot: recurring task %s hit a %s gate with no options — "
                "parking, because there is nothing to choose", task.id, kind,
            )
        logger.info(
            "autopilot: parking %s gate — option=%r confidence=%.2f (floor %.2f)",
            kind, decision.option, decision.confidence, CONFIDENCE_FLOOR,
        )
        return None
    # Durable learning signal — F3 joins on this to rate the decision later.
    try:
        from okuro.orchestrator.state import emit_event

        event = {
            "kind": kind,
            "chosen_option": decision.option,
            "rationale": decision.rationale,
            "confidence": decision.confidence,
            "options": list(options),
            "ts": datetime.utcnow().isoformat(),
        }
        if ctx.get("phase_id") is not None:
            event["phase_id"] = ctx.get("phase_id")
        if ctx.get("subtask_id"):
            event["subtask_id"] = ctx.get("subtask_id")
        # emit_event, not append_log. `autopilot_decision` was added to
        # _STATE_CHANGE_EVENTS in P1.4 to give it a feed row ("autopilot chose
        # X for you") — but it was still written with append_log, which is the
        # raw file writer and skips the C9 snapshot invalidation. The
        # allowlist entry was therefore doing nothing: an open page never
        # refetched, so the row the user was supposed to see did not appear
        # until something else happened to invalidate.
        emit_event(task.id, "autopilot_decision", event, tasks_dir)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("autopilot: failed to log autopilot_decision: %r", exc)
    return decision.option


def _log_auto_resolved(
    task, tasks_dir, *, kind: str, chosen: str, reason: str,
    options: list[str], ctx: dict, abstained_at: float,
) -> None:
    """Record a deterministic (non-LLM) auto-resolution.

    A distinct event type from ``autopilot_decision`` on purpose: this is not
    a judgement the resolver made, it is a policy the run applied because
    nobody was there to ask. The two read differently to a person and they
    must not be rated as one thing by F3 either.

    Also the escalation ledger — ``_already_auto_resolved`` reads these rows
    back to decide whether a repeat of the same gate should skip instead.
    """
    try:
        from okuro.orchestrator.state import emit_event

        event = {
            "kind": kind,
            "gate": _gate_fingerprint(kind, ctx),
            "chosen_option": chosen,
            "reason": reason,
            "options": list(options),
            "resolver_confidence": abstained_at,
            "ts": utc_now_naive().isoformat(),
        }
        if ctx.get("phase_id") is not None:
            event["phase_id"] = ctx.get("phase_id")
        if ctx.get("subtask_id"):
            event["subtask_id"] = ctx.get("subtask_id")
        emit_event(task.id, "gate_auto_resolved", event, tasks_dir)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("autopilot: failed to log gate_auto_resolved: %r", exc)
