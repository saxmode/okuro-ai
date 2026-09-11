# SPDX-License-Identifier: Apache-2.0
# purpose: Single owner of user-facing gate (awaiting_user) text. Turns raw
# engine causes into plain-language headline / explanation / action, and quarantines
# jargon (error strings, findings, UUIDs) into a collapsed technical_details field.
#
# index: imports | plan nouns | CAUSE_PHRASES | dataclass GateMessage | def humanize_gate
#
# Why this exists: gate messages were hand-written at ~12 set_awaiting call sites.
# Tone drifted per site and raw technical strings (critic_infra_error, artifact
# UUIDs, "re-run critic with stricter prompt") were spliced straight into the
# sentence the user reads. This module centralizes the copy so every gate answers
# the same three questions — what happened, should I worry, what do I do — in plain
# language, with engineer diagnostics kept OUT of the main message.

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Plan vocabulary (ROCK-SOLID v5, decision D-A) — the ONE place the
# user-facing nouns for a plan's units are spelled. Frontend counterpart:
# web/frontend/src/lib/nouns.ts. The two must agree.
#
# The gate copy below already said "Step N / part N.M"; the rest of the
# product said "Phase N / subtask N.M", so one screen could show a card
# reading "Step 2 needs your decision" directly under a divider reading
# "Phase 2". The cause is structural, not lexical: the noun was re-authored
# as an f-string at every emit site (12 in engine.py alone), so a sweep fixes
# today's sites and does nothing about tomorrow's. Importing it does.
#
# MACHINE names are deliberately NOT renamed and must never route through
# here: ``phase.id``, plan.yaml's ``phases:`` key, the
# ``/api/tasks/{id}/phases/{id}/…`` routes, event payload keys. Those are
# contracts. This module owns only what a person reads.
# ---------------------------------------------------------------------------

STEP_NOUN = "Step"
STEP_NOUN_LOWER = "step"
PART_NOUN = "part"
PART_NOUN_CAP = "Part"


def step_label(phase_id) -> str:
    """"Step 2" — the canonical way to name a phase to a person.

    A MISSING or PLACEHOLDER phase id degrades to "This step" rather than
    printing itself. Callers that have no phase context pass 0 or None, and
    those values used to reach the user verbatim: P4.9's stranded-blocker
    synthesizer renders "Step 0 can't move forward", and an earlier gate
    produced "Step? can't move forward". There is no step 0 and no step ?.

    Fixed HERE and not at each caller, because this function is the single
    place the noun is spelled — the whole point of the vocabulary module.
    A caller that acquires a real phase id starts naming it automatically.
    """
    try:
        n = int(phase_id)
    except (TypeError, ValueError):
        return f"This {STEP_NOUN.lower()}"
    if n <= 0:
        return f"This {STEP_NOUN.lower()}"
    return f"{STEP_NOUN} {n}"


def part_label(subtask_id) -> str:
    """"part 2.1" — the canonical way to name a subtask to a person."""
    return f"{PART_NOUN} {subtask_id}"


# Raw cause/verdict codes → plain phrases. Used to LABEL technical_details and to
# keep the headline/explanation authored (not string-spliced from raw errors).
# Extend this as new causes appear; unknown codes fall back to a generic phrase.
CAUSE_PHRASES: dict[str, str] = {
    "critic_infra_error": "the quality-checker service was briefly unreachable",
    "infra": "a temporary connection problem with the AI service",
    "reviewer_unavailable": "the quality-checker service was briefly unreachable",
    "NEEDS_USER": "a trade-off only you can decide",
    "SUBAGENT_FAIL": "the step could not finish after repeated attempts",
    "INFRA": "a temporary connection problem with the AI service",
    "timeout": "the step ran longer than its time budget",
}


def plain_cause(code: str) -> str:
    """Map a raw cause/verdict code to a plain phrase (safe fallback)."""
    if not code:
        return "an unexpected condition"
    return CAUSE_PHRASES.get(code.strip(), code.strip())


@dataclass
class GateMessage:
    """Structured, user-facing text for one awaiting_user gate.

    ``headline`` / ``explanation`` / ``action`` are ALWAYS jargon-free — they are
    authored per (kind, cause), never built by splicing a raw error string.
    ``technical_details`` is the only place raw diagnostics live; the UI renders it
    behind a "Show details" disclosure, so a reader who just wants to act never sees
    it. ``options`` drives the action buttons (retry / override / skip / ...).

    ``recommended`` (ROCK-SOLID v5 P2.1) names which entry in ``options`` is the
    safe default — non-empty for every branch below. Pre-fix only decision_gate
    (which builds its own richer options list independent of this dataclass) had
    a real recommendation; every other kind left the reader to guess which of
    2-4 buttons in a row was the sensible one. The FE renders the matching
    button as visually primary, the rest as outline (already done for
    timeout_cap's card in P1.5; this generalises the same pattern to every kind).
    """
    headline: str
    explanation: str
    action: str
    options: list[str] = field(default_factory=list)
    recommended: str = ""
    technical_details: str = ""
    # ROCK-SOLID v5 P2.4 — the (kind, cause, ctx) this card was built from,
    # stamped by ``humanize_gate``. Persisted with the card so a parked gate
    # can be re-authored later; see ``rebuild``. Never rendered.
    source: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Enforce the docstring's guarantee instead of trusting each branch.

        The no-machine-ids rule was per-branch discipline, and discipline lost:
        the ``subtask_approval`` branch spliced a subtask's agent prompt —
        artifact uuid and all — straight into ``explanation``. Auditing the
        seven branches found ``decision_gate`` splicing a raw upstream prompt
        the same way, differing only in whether an id happened to be present.

        Sanitising HERE makes the guarantee structural: a branch cannot leak an
        id, and neither can the eighth branch nobody has written yet.
        ``technical_details`` is deliberately exempt — it is the quarantine.
        """
        self.headline = _strip_ids(self.headline)
        self.explanation = _strip_ids(self.explanation)
        self.action = _strip_ids(self.action)

    def to_message(self) -> str:
        """Backward-compatible flat string for ``Awaiting.message`` — headline +
        explanation + action, jargon-free. Consumers that don't yet read the
        structured presentation still get a clean, readable message."""
        return "\n\n".join(p for p in (self.headline, self.explanation, self.action) if p)

    def to_presentation(self) -> dict:
        """Structured payload the new UI renders: headline (bold) + explanation +
        action + a collapsed technical_details disclosure."""
        return {
            "headline": self.headline,
            "explanation": self.explanation,
            "action": self.action,
            "options": list(self.options),
            "recommended": self.recommended,
            "technical_details": self.technical_details,
            # Inputs, not output. Keeps the card re-authorable after the
            # engine that wrote it has exited (P2.4). The UI ignores it.
            "source": dict(self.source),
        }


def _trim(text: str, cap: int = 600) -> str:
    text = (text or "").strip()
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _strip_ids(text: str) -> str:
    """Drop machine ids from text destined for headline / explanation / action.

    This module's contract is that uuids live in ``technical_details`` and
    nowhere else. Upstream strings that get spliced into user copy — a subtask
    description (an AGENT PROMPT), a decision-gate prompt — routinely name
    artifacts by uuid, so the rule has to be enforced on the way out rather
    than trusted at each call site.

    Removed, not substituted: a uuid can be an artifact, a task, a session or a
    role id, and this function cannot tell which. Naming it wrongly would be a
    worse failure than omitting it — the id is still in technical_details for
    anyone who needs it.
    """
    cleaned = _UUID_RE.sub("", text or "")
    # Tidy the punctuation an inlined id leaves behind: '' or "" wrappers,
    # doubled spaces, and a space before terminal punctuation.
    cleaned = cleaned.replace("''", "").replace('""', "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" +([,.;:!?])", r"\1", cleaned)
    return cleaned.strip()


def _plural(n, word: str, plural: str | None = None) -> str:
    """"3 attempts" / "1 attempt" — never "3 attempt(s)".

    The "(s)" hack appeared in five places across four branches. It is small,
    but it is the tell that a card was written by a program rather than for a
    person, and this module's whole job is that the reader cannot tell.
    """
    try:
        count = int(n)
    except (TypeError, ValueError):
        return f"{n} {plural or word + 's'}"
    return f"{count} {word}" if count == 1 else f"{count} {plural or word + 's'}"


def _summary_line(text: str, cap: int = 140) -> str:
    """THE single gate for upstream text entering user copy.

    Every branch that shows text it did not author routes through here, so the
    treatment is uniform. Before this there were three different truncations —
    _trim(…, 200) for a subtask description, _trim(…, 400) for a decision-gate
    prompt, and none at all for role names — each inventing its own idea of
    what a human should see, and each free to spill an agent instruction into
    the card. Character-truncating machine-facing text is not sanitising it.

    Returns the human-readable summary line of the text.

    A subtask ``description`` is a multi-step agent brief: a summary line,
    then numbered instructions addressed to the agent. Only the summary line
    describes the work. Character-truncating the whole blob (the previous
    behaviour) cut mid-word into step 1 and showed the user an instruction
    written for a machine.

    Deliberately the first LINE, not the first sentence: real briefs open with
    a context clause and put the work in sentence two — subtask 3.1 of the
    prism-deck workflow reads "Every topic is built. Now assemble the deck
    around them.", where sentence one alone says nothing about what runs.
    """
    body = _strip_ids((text or "").strip())
    # Numbered steps start the machine-facing half — cut there.
    body = re.split(r"\n\s*\d+[.)]\s", body, maxsplit=1)[0].strip()
    body = body.split("\n", 1)[0].strip()
    return _trim(body.rstrip(" :"), cap)


def humanize_gate(kind: str, cause: str, ctx: dict | None = None) -> GateMessage:
    """Build a plain-language GateMessage for a gate.

    ``kind``  — the awaiting kind (blocked_review, decision_gate, capability_gap, …).
    ``cause`` — a coarse bucket within the kind (infra, decision, subagent_fail,
                reviewer, …). For gates with a single meaning, pass "".
    ``ctx``   — free dict of hints: phase_id, subtask_id, rounds, attempts,
                raw_reason (the engineer string → technical_details), etc.

    Unknown (kind, cause) pairs fall back to a safe generic gate so a new call site
    never renders an empty or crashing message.

    The returned message REMEMBERS these three arguments (``.source``), and
    ``to_presentation()`` persists them with the card — see ``rebuild`` below
    for why that is load-bearing rather than bookkeeping.
    """
    msg = _build_gate_message(kind, cause, ctx)
    msg.source = {"kind": kind, "cause": cause or "", "ctx": _jsonish(ctx or {})}
    return msg


def _jsonish(value):
    """Reduce a ctx to what survives a YAML round-trip through task.yaml.

    ``source`` is persisted, so anything not representable would either break
    the write or come back as a different type and silently change the copy on
    rebuild. Unknown objects degrade to ``str`` rather than being dropped: a
    stringified role is still better copy than a missing one.
    """
    if isinstance(value, dict):
        return {str(k): _jsonish(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonish(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def rebuild(presentation: dict | None) -> GateMessage | None:
    """Re-author a stored card from the inputs it was built with (P2.4).

    Gate copy is written ONCE, by the engine, at park time — and a human park
    is engineless (the engine exits, see engine.park_engine_exit). Nothing is
    alive to re-render it, so a card written by last week's copy stays on
    screen until the user resolves the gate. Correcting it meant re-parking
    the task, which is not a thing you can ask a user to do.

    Rebuilding needs (kind, cause, ctx), and only ``kind`` was recoverable
    from a stored gate: ``cause`` — the value that picks between "quality
    check couldn't run" and "needs your decision" inside blocked_review —
    lived in a local variable at the call site and was never written down.
    Re-running with cause="" would have quietly downgraded every infra gate
    to the generic reviewer-FAIL branch, which is a WRONG card rather than a
    stale one. Hence ``.source``.

    Returns None when the card predates ``source`` (parked before this
    landed), so callers can fall back deliberately instead of guessing.
    """
    src = (presentation or {}).get("source") or {}
    kind = src.get("kind")
    if not kind:
        return None
    return humanize_gate(str(kind), str(src.get("cause") or ""), dict(src.get("ctx") or {}))


def _build_gate_message(kind: str, cause: str, ctx: dict | None = None) -> GateMessage:
    """The authored copy per (kind, cause). Call ``humanize_gate`` instead —
    it stamps ``source`` on the result, which this deliberately does not do
    (it would have to be repeated in all twelve branches below)."""
    ctx = ctx or {}
    phase = ctx.get("phase_id", "?")
    subtask = ctx.get("subtask_id", "")
    rounds = int(ctx.get("rounds", 0) or 0)
    attempts = int(ctx.get("attempts", 0) or 0)
    raw = _trim(str(ctx.get("raw_reason", "") or ""))
    part = part_label(subtask) if subtask else f"this {STEP_NOUN_LOWER}"

    if kind == "blocked_review":
        if cause == "infra":
            return GateMessage(
                headline=f"{step_label(phase)}: quality check couldn't run",
                explanation=(
                    "Your work is fine — nothing was changed. The quality-checker "
                    f"couldn't complete its check of {part}. Sometimes this is a "
                    "brief blip that Retry clears; but if Retry keeps returning you "
                    "here, the check is failing repeatably on this deliverable and "
                    "retrying will not help."
                ),
                action=(
                    "Press Retry to run the check again. If the same message comes "
                    "straight back, use Override to accept the work as-is and "
                    "continue."
                ),
                options=["retry", "override", "skip"],
                recommended="retry",
                technical_details=raw,
            )
        if cause == "decision":
            return GateMessage(
                headline=f"{step_label(phase)} needs your decision",
                explanation=(
                    f"The quality check flagged the same issue {_plural(rounds, 'time')} and "
                    "the AI couldn't settle it. This usually means a real trade-off "
                    "only you can call — not careless work."
                ),
                action=(
                    "Retry with a note telling the AI what to do, or Override to "
                    "accept it as-is and continue."
                ),
                options=["retry", "override", "skip"],
                # A bare Retry (no new guidance) on a trade-off the AI already
                # couldn't settle after N rounds tends to just repeat the same
                # cycle — the action copy itself frames productive retry as
                # "with a note". Override (accept + move on) is the more
                # defensible single-click default; the user can always Retry
                # with guidance instead if they disagree.
                recommended="override",
                technical_details=raw,
            )
        if cause == "stuck":
            return GateMessage(
                headline=f"{step_label(phase)} can't move forward",
                explanation=(
                    "No steps are ready to run and none are in progress — the plan "
                    "is waiting on something that never arrived."
                ),
                action="Review the remaining steps, then Retry or Skip to continue.",
                options=["retry", "skip"],
                recommended="retry",
                technical_details=raw,
            )
        if cause == "subagent_fail":
            return GateMessage(
                headline=f"{step_label(phase)} couldn't finish",
                explanation=(
                    f"{part.capitalize()} failed after {_plural(attempts, 'attempt')}. This is "
                    "a real failure, not a quality-check flag."
                ),
                action="Press Retry to try again, or Override to accept and continue.",
                options=["retry", "override", "skip"],
                recommended="retry",
                technical_details=raw,
            )
        # default: reviewer FAIL
        return GateMessage(
            headline=f"{step_label(phase)}: quality check didn't pass",
            explanation=(
                f"The check couldn't pass {part} after {_plural(rounds, 'attempt')}. The "
                "result, or something the check flagged, needs a look."
            ),
            action="Retry against the flagged issues, or Override to accept as-is.",
            options=["retry", "override", "skip"],
            recommended="retry",
            technical_details=raw,
        )

    if kind == "capability_gap":
        roles = ctx.get("roles") or []
        # Slugs are machine names. Rendered as words here, the same way the
        # subtask_approval branch renders a role — a card should never make the
        # reader parse kebab-case.
        roles_txt = (
            ", ".join(_summary_line(str(r), 60).replace("-", " ") for r in roles)
            if roles else "a few specialists"
        )
        n = len(roles) if roles else ctx.get("count", 0)
        return GateMessage(
            headline="This task needs new expertise",
            explanation=(
                f"It calls for {_plural(n, 'kind') if n else 'some kinds'} of expert the system doesn't "
                f"have yet: {roles_txt}."
            ),
            action="Approve to create them, or adjust the task and resubmit.",
            options=list(ctx.get("options", ["accept"]) or ["accept"]),
            recommended="accept",
            technical_details=raw or str(ctx.get("summary", "")),
        )

    if kind == "decision_gate":
        # Same upstream-text gate as subtask_approval. Previously this spliced
        # a raw 400-char prompt straight into explanation — the identical shape
        # as the subtask_approval defect, differing only in whether an agent
        # instruction or an id happened to be present in that particular
        # prompt. The question itself IS what the user must answer, so it is
        # kept; it is normalised, not truncated mid-word.
        prompt = _summary_line(str(ctx.get("prompt", "") or ""), 240)
        return GateMessage(
            headline=f"{step_label(phase)} needs a decision from you",
            explanation=(
                f"{prompt}\n\nThis is a choice the run cannot make for you."
                if prompt
                else "The task reached a choice only you can make."
            ),
            action="Pick an option below to continue.",
            options=list(ctx.get("options", []) or []),
            # recommended deliberately left unset here: decision_gate builds
            # its own richer per-option structure (id/label/pros/cons/
            # recommended, straight from the typed DecisionGate options) in
            # engine.py's payload, independent of this flat options list —
            # the real recommendation lives there, not here.
            technical_details=raw,
        )

    if kind == "subtask_approval":
        # Answer the three questions a human actually has: what will run, why
        # am I being asked, and what do I lose by skipping. The previous copy
        # answered none of them — it truncated the subtask's AGENT PROMPT at
        # 200 chars (cutting mid-sentence into "1. Read the original artifact
        # '<uuid>' …") and labelled a HIGH-risk step "higher-impact", which
        # names the severity without naming the work. It also leaked a raw
        # uuid into the explanation, the exact thing this module exists to
        # quarantine into technical_details.
        risk = str(ctx.get("risk", "") or "").lower()
        risk_txt = {
            "high": "This one is flagged high-impact",
            "med": "This one is flagged moderate-impact",
            "medium": "This one is flagged moderate-impact",
        }.get(risk, "This step is held for your go-ahead")
        what = _summary_line(str(ctx.get("description", "") or ""))
        role = str(ctx.get("role", "") or "").strip()
        outputs = [str(o).strip() for o in (ctx.get("outputs") or []) if str(o).strip()]

        who = f"The {role.replace('-', ' ')} will" if role else "This step will"
        produces = f" and produce {outputs[0]}" if len(outputs) == 1 else (
            f" and produce {len(outputs)} files" if outputs else ""
        )
        lost = (
            f"Skip it and the run continues without {outputs[0]}."
            if len(outputs) == 1
            else "Skip it and the run continues without this step's output."
        )
        return GateMessage(
            headline=f"Run this step? — {what}" if what else "Run this step?",
            explanation=f"{who} do this{produces}. {risk_txt}, so it waits for you. {lost}",
            action="Approve to run it, or Skip to continue without it.",
            options=list(ctx.get("options", ["approve", "skip"]) or ["approve", "skip"]),
            recommended="approve",
            technical_details=(
                f"{subtask} · {role or 'no role'} · risk {risk or '?'}"
            ).strip(),
        )

    if kind == "panel_confirmation":
        n = int(ctx.get("count", 0) or 0)
        if n:
            return GateMessage(
                headline="Confirm the expert panel",
                explanation=f"{_plural(n, 'expert role')} {'is' if n == 1 else 'are'} proposed for this task.",
                action="Confirm the panel, or edit it before the work begins.",
                options=list(ctx.get("options", ["confirm", "edit"]) or ["confirm", "edit"]),
                recommended="confirm",
                technical_details=raw,
            )
        return GateMessage(
            headline="No experts matched this task",
            explanation="The system couldn't match any expert roles to what you asked for.",
            action="Add a hint or name the roles you want, then resubmit.",
            options=list(ctx.get("options", ["edit"]) or ["edit"]),
            recommended="edit",
            technical_details=raw,
        )

    if kind == "timeout_cap":
        secs = ctx.get("seconds", ctx.get("last_timeout_s", "?"))
        # ROCK-SOLID v5 P2.1 — the fallback options list here was stale: real
        # timeout_cap emissions (engine.py) always pass their own options and
        # use the actual action ids (extend_and_retry/mark_done/skip/
        # permanent_fail), never this list's old extend/retry/done/skip
        # shorthand. Only a caller that forgot to pass options would ever see
        # this default, but a wrong default is worse than none — fixed to
        # match the real vocabulary.
        _default_options = ["extend_and_retry", "mark_done", "skip", "permanent_fail"]
        return GateMessage(
            headline=f"{step_label(phase)} is taking longer than planned",
            explanation=f"{part.capitalize()} passed its {secs}s time budget without finishing.",
            action="Choose: give it more time, mark it done, skip it, or fail it permanently.",
            options=list(ctx.get("options", _default_options) or _default_options),
            recommended="extend_and_retry",
            technical_details=raw,
        )

    if kind == "discussion_proceed":
        return GateMessage(
            headline="The expert panel is ready",
            explanation="The experts have each shared their position on this task.",
            action="Review the positions and press Proceed to continue.",
            options=list(ctx.get("options", ["proceed"]) or ["proceed"]),
            recommended="proceed",
            technical_details=_trim(str(ctx.get("node_id", "") or "")),
        )

    # Generic fallback for gate kinds not yet migrated — still clean + actionable.
    return GateMessage(
        headline=f"{step_label(phase)} needs your input",
        explanation=(
            f"The task paused on {part} because of {plain_cause(cause)}."
        ),
        action="Review the options below and choose how to continue.",
        options=list(ctx.get("options", []) or []),
        technical_details=raw,
    )
