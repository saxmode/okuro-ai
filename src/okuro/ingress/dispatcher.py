# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Dispatcher chain — receives an IngressMessage, runs the
#   security gate for high-stakes intents, parses intent via router,
#   and executes the appropriate okuro tool. Sends user-facing
#   receipts back through the envelope's reply callable.
# index: imports | constants | _execute | _start_challenge | dispatch
# AGENT_HEADER_END -->
"""Glue layer: envelope → router → security → tool call → receipt."""

from __future__ import annotations

import logging
import random
from typing import Optional

from .adapter import IngressMessage
from . import router as ingress_router
from . import security

log = logging.getLogger("okuro.ingress.dispatcher")

DISAMBIGUATE_TTL_SECONDS = 120


# ── Intent executors ─────────────────────────────────────────────────


def _exec_thought(envelope: IngressMessage, payload: dict) -> dict:
    from okuro.sense.thoughts import capture_thought
    tid = capture_thought(
        content=payload.get("body") or envelope.text,
        source=envelope.channel,
        category="ingress",
    )
    return {"kind": "thought", "thought_id": tid}


def _exec_todo(envelope: IngressMessage, payload: dict) -> dict:
    from okuro.sense.todos import todo_add
    rid = todo_add(
        title=payload.get("title") or envelope.text,
        priority=payload.get("priority") or "P2",
    )
    return {"kind": "todo", "todo_id": rid}


def _exec_orchestrator_task(envelope: IngressMessage, payload: dict) -> dict:
    from okuro.sense.mcp_tools import orchestrator_create
    # The user already cleared the challenge-response gate in the
    # ingress layer; pass auto_approve_risk != "none" so the engine
    # flips its binary auto_approve flag and the task actually runs.
    result = orchestrator_create(
        description=payload.get("description") or envelope.text,
        mode=payload.get("mode") or "auto-execute",
        intelligence=payload.get("intelligence") or "balanced",
        auto_approve_risk="low",
    )
    return {"kind": "orchestrator_task", **result}


def _exec_reminder(envelope: IngressMessage, payload: dict) -> dict:
    # Time extraction is step 7 work. For now fall through to a thought
    # tagged as a reminder so nothing is lost.
    from okuro.sense.thoughts import capture_thought
    tid = capture_thought(
        content=payload.get("text") or envelope.text,
        source=envelope.channel,
        category="reminder-unparsed",
    )
    return {
        "kind": "reminder",
        "thought_id": tid,
        "note": "reminder time parsing not yet implemented — saved as thought",
    }


def _exec_question(envelope: IngressMessage, payload: dict) -> dict:
    from okuro.sense.advisor import advise
    text = advise(task_hint=payload.get("task_hint") or envelope.text)
    return {"kind": "question", "advice": text}


_EXECUTORS = {
    "thought": _exec_thought,
    "todo": _exec_todo,
    "orchestrator_task": _exec_orchestrator_task,
    "reminder": _exec_reminder,
    "question": _exec_question,
}


def _format_receipt(result: dict) -> str:
    kind = result.get("kind")
    if kind == "thought":
        return f"✓ Saved as thought."
    if kind == "todo":
        return f"✓ Todo added."
    if kind == "orchestrator_task":
        if "error" in result:
            return f"⚠ Orchestrator error: {result['error']}"
        tid = result.get("task_id", "?")
        summary = (result.get("plan_summary") or "")[:120]
        return f"✓ Orchestrator task dispatched.\nid: {tid}\n{summary}"
    if kind == "reminder":
        return f"✓ Saved (reminder parsing deferred — landed as thought)."
    if kind == "question":
        advice = result.get("advice") or {}
        if isinstance(advice, dict):
            return advice.get("summary") or "(no advice)"
        return str(advice)
    return "✓"


# ── Security helpers ─────────────────────────────────────────────────


async def _start_challenge(envelope: IngressMessage, channel: str, chat_id: int,
                           intent: str, payload: dict) -> None:
    bank = security.load_bank(channel)
    if not bank:
        if envelope.reply:
            await envelope.reply(
                "⚠ High-stakes intent blocked: no security challenges configured. "
                "Add at least one in okuro web → Settings → Integrations → Challenges."
            )
        return
    idx = random.randrange(len(bank))
    security.set_pending(channel, chat_id, intent, payload, challenge_idx=idx)
    challenge = bank[idx]
    if envelope.reply:
        await envelope.reply(
            f"🔒 Challenge: {challenge.question}\n"
            f"(reply within 2 minutes — wrong answer × 3 → lockout)"
        )


async def _consume_pending(envelope: IngressMessage, channel: str, chat_id: int,
                           pending: security.PendingAction) -> bool:
    """Try to interpret the incoming text as an answer to the pending challenge.

    Returns True iff the message was consumed as an answer (right or wrong).
    """
    bank = security.load_bank(channel)
    challenge = (
        bank[pending.challenge_idx]
        if pending.challenge_idx is not None and 0 <= pending.challenge_idx < len(bank)
        else None
    )

    if challenge is None:
        security.clear_pending(channel, chat_id)
        if envelope.reply:
            await envelope.reply("⚠ Challenge bank changed — please re-send your request.")
        return True

    if security._match(challenge, envelope.text):
        security.mark_verified(channel, chat_id)
        intent = pending.intent
        payload = pending.payload
        security.clear_pending(channel, chat_id)
        await _execute_and_reply(envelope, intent, payload)
        return True

    # Wrong answer: bump attempts and possibly lock out.
    locked = security.record_failure(channel, chat_id)
    if locked:
        security.clear_pending(channel, chat_id)
        if envelope.reply:
            await envelope.reply("⛔ Locked out for 5 minutes (too many wrong answers).")
        return True

    if envelope.reply:
        await envelope.reply("✗ Wrong answer. Try again.")
    return True


# ── Core dispatch ────────────────────────────────────────────────────


async def _execute_and_reply(envelope: IngressMessage, intent: str, payload: dict) -> None:
    executor = _EXECUTORS.get(intent, _exec_thought)
    try:
        result = executor(envelope, payload)
    except Exception as exc:
        log.exception("dispatcher executor failed for %s", intent)
        if envelope.reply:
            await envelope.reply(f"⚠ Dispatch failed: {type(exc).__name__}: {exc}")
        return
    if envelope.reply:
        await envelope.reply(_format_receipt(result))


async def dispatch(envelope: IngressMessage) -> None:
    """Top-level on_message handler — replaces default_on_message."""
    channel = envelope.channel
    try:
        chat_id = int(envelope.from_id)
    except (TypeError, ValueError):
        log.warning("non-integer chat_id %r — falling through to thought", envelope.from_id)
        chat_id = 0

    # 1. Pending challenge takes priority — consume the message as an answer.
    pending = security.get_pending(channel, chat_id) if chat_id else None
    if pending is not None:
        await _consume_pending(envelope, channel, chat_id, pending)
        return

    # 2. Lockout block.
    if chat_id and security.is_locked_out(channel, chat_id):
        if envelope.reply:
            await envelope.reply("⛔ Currently locked out. Try again in a few minutes.")
        return

    # 3. Classify.
    decision = ingress_router.classify(envelope.text)

    if decision.ambiguous:
        if envelope.reply:
            await envelope.reply(
                "❓ Ambiguous — saw multiple intents: "
                + ", ".join(decision.candidates)
                + ". Rephrase with the one you want as the leading keyword."
            )
        return

    # 4. High-stakes: gate by verification + challenge.
    if decision.requires_challenge and chat_id:
        if security.is_verified(channel, chat_id) and decision.intent != "orchestrator_task":
            # Inside grace window for non-orchestrator high-stakes → dispatch.
            await _execute_and_reply(envelope, decision.intent, decision.payload)
            return
        # Orchestrator always asks (per policy C) — even when grace is open.
        await _start_challenge(envelope, channel, chat_id, decision.intent, decision.payload)
        return

    # 5. Low-stakes: dispatch immediately.
    await _execute_and_reply(envelope, decision.intent, decision.payload)
