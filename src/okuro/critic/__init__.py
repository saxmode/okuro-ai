# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The uninformed-critic pass — hand work to a fresh model that owns none of it.
# index: imports | SCOPE_* | _PROMPT | def _parse | def critique
# AGENT_HEADER_END -->
"""The uninformed critic.

WHY THIS EXISTS. An agent cannot reliably review its own proposal. The
specific, repeated failure it is built against: proposing the INSTANCE fix
and presenting it as the whole answer, when the class fix was available.
That failure has a rule (`agent_rules.yaml`: "ALWAYS name the CLASS a
problem belongs to before proposing a fix") and the rule does not hold —
measured 2026-07-29, one session after it shipped, by the assistant that
had just written the class-sweep for a different bug.

WHY NOT A DETECTOR. A `require_systemic_frame` text gate was designed and
rejected the same day, correctly: the block reason is fed back to the
model, so it LEAKS THE PHRASE LIST, and the cheapest way to pass is to
sprinkle "root cause" and "the pattern" into unchanged reasoning. 100%
pass rate, zero behaviour change, and the honest telemetry is lost. The
general rule that came out of it: **a text gate is satisfiable by
vocabulary; only gate what cannot be reworded around.**

WHAT CANNOT BE REWORDED AROUND. A second model that never saw the
reasoning. The author does not control its judgement, so there is no
phrasing that makes a weak proposal pass. This module is that second
model, with two properties that make it work:

  1. UNINFORMED FRAMING. "A friend sent me this — tell me where he is
     wrong." Measured to work twice on 2026-07-29: it found a rule
     contradiction that self-review had missed, and on a separate plan it
     caught a self-deceiving justification plus an unevidenced mitigation.
  2. SCOPE IS A REQUIRED FIELD, not a phrase. Every critique must answer
     "is this the instance fix or the class fix?" about the subject. The
     author cannot satisfy it by writing differently, because the author
     is not the one answering.

Provider-agnostic by construction: it routes through `okuro.bridge`, so it
runs from Claude Code, Codex, Cursor or Antigravity without change.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# The verdict on scope. The whole point of the module is that this is a
# REQUIRED field of every critique rather than language the author may or
# may not reach for.
SCOPE_INSTANCE = "instance"      # fixes the one case in front of you
SCOPE_CLASS = "class"            # fixes every case sharing the assumption
SCOPE_MIXED = "mixed"            # names the class, fixes the instance, says so
SCOPE_NA = "not-a-fix"           # research, a plan with no fix in it, etc.

_VALID_SCOPES = (SCOPE_INSTANCE, SCOPE_CLASS, SCOPE_MIXED, SCOPE_NA)
_VALID_SEVERITIES = ("blocker", "high", "medium", "nitpick")

_SYSTEM_PROMPT = (
    "You are a skeptical senior engineer reviewing a colleague's work. You "
    "did not write it and you owe it nothing. You are direct and you spend "
    "your words on what is wrong. You return JSON only — no prose, no code "
    "fences."
)

_PROMPT_TEMPLATE = """\
A friend sent me this and asked what I think. Tell me where he is wrong.

<subject kind="{kind}">
{subject}
</subject>
{context_block}
Be a skeptical peer, not a collaborator. If something is fine, say so in one
line and move on — spend your words on what is wrong. Do not be agreeable.

Return JSON: {{"scope": ..., "scope_reason": ..., "the_class": ...,
"findings": [...], "verdict": ...}}

  scope        REQUIRED, one of: instance | class | mixed | not-a-fix
               Answer this about the PROPOSAL, not about the problem:
                 instance  — it fixes the one case in front of him, and other
                             cases sharing the same assumption stay broken
                 class     — it fixes every case sharing the assumption
                 mixed     — it names the class and deliberately scopes down,
                             saying so out loud
                 not-a-fix — research, a question, a plan containing no fix
  scope_reason REQUIRED, one sentence. If scope is "instance", name what he
               would still have to fix afterwards. Be concrete.
  the_class    REQUIRED unless scope is "not-a-fix". Name the CLASS the
               problem belongs to in one line — the shared assumption, not
               this occurrence of it. If you cannot find a class, say
               "genuinely a one-off" and say why.
  findings     array, ordered most severe first. Each:
                 severity   blocker | high | medium | nitpick
                 claim      what is wrong, one sentence
                 evidence   what makes you believe it — a quoted line from
                            the subject, a contradiction between two of its
                            parts, or a concrete failure scenario with inputs.
                            If you are reasoning from plausibility rather than
                            evidence, write "UNVERIFIED:" first and say so.
                 fix        what he should do instead, one line
  verdict      one sentence. Lead with whether it is sound.

Rules:
  - Distinguish "I can show this is wrong" from "this smells wrong". Label
    the second UNVERIFIED. A plausible mechanism presented as a finding is
    the failure this whole review exists to catch.
  - Do not invent problems to look useful. An empty findings array with an
    honest scope verdict is a good answer.
  - Silent failure modes outrank cosmetic ones. Ask: if this is wrong, does
    anything tell him — or does it just quietly do nothing?

JSON only."""


def _parse(text: str) -> dict:
    """Extract a JSON object from LLM output, tolerant of surrounding prose.

    ``response_format="json"`` is honoured by local-http providers only —
    the CLI providers (claude/codex/antigravity) parse text and IGNORE it,
    and the default routing table sends most capabilities to a CLI. So the
    tolerant path is the ONLY path in practice, not a fallback. Same
    pattern as ``notes_extract.extractor._parse`` and
    ``ai_models.prompt_research._parse``.
    """
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", (text or "").strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError("critic returned no parseable JSON")


def _coerce(raw: dict) -> dict:
    """Normalise the critic's output; never trust the vocabularies."""
    scope = str(raw.get("scope") or "").strip().lower()
    if scope not in _VALID_SCOPES:
        # Unrecognised scope is NOT silently dropped — a missing scope is
        # exactly the failure this module exists to surface, so it is
        # reported as unknown rather than defaulted to something reassuring.
        scope = "unknown"

    findings = []
    for f in raw.get("findings") or []:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "medium").strip().lower()
        if sev not in _VALID_SEVERITIES:
            sev = "medium"
        evidence = str(f.get("evidence") or "").strip()
        findings.append({
            "severity": sev,
            "claim": str(f.get("claim") or "").strip(),
            "evidence": evidence,
            # Surfaced as a field so a caller can filter on it rather than
            # grepping the prose for the marker.
            "unverified": evidence.upper().startswith("UNVERIFIED"),
            "fix": str(f.get("fix") or "").strip(),
        })
    order = {s: i for i, s in enumerate(_VALID_SEVERITIES)}
    findings.sort(key=lambda f: order.get(f["severity"], 99))

    return {
        "scope": scope,
        "scope_reason": str(raw.get("scope_reason") or "").strip(),
        "the_class": str(raw.get("the_class") or "").strip(),
        "findings": findings,
        "verdict": str(raw.get("verdict") or "").strip(),
        "blockers": sum(1 for f in findings if f["severity"] == "blocker"),
        "unverified_count": sum(1 for f in findings if f["unverified"]),
    }


def critique(
    subject: str,
    kind: str = "proposal",
    context: str = "",
    capability: str = "quality",
    provider: str | None = None,
) -> dict:
    """Hand `subject` to a fresh model that owns none of it.

    Args:
        subject: the text to review — a plan, a diff, a decision, a
            recommendation. Pass the thing itself, not a summary of it: a
            summary is the author's framing, and the author's framing is
            what is under review.
        kind: what it is (plan / diff / decision / recommendation). Only
            shapes the framing.
        context: optional constraints the reviewer could not infer. Keep it
            factual. Anything persuasive here defeats the purpose.
        capability: bridge routing capability. Defaults to "quality" — this
            is a judgement task and the fast tier measurably misses things.
        provider: force a provider. Leave unset to route normally.

    Returns a dict with `scope`, `scope_reason`, `the_class`, `findings`,
    `verdict`, plus `ok`/`error` on failure.
    """
    subject = (subject or "").strip()
    if not subject:
        return {"ok": False, "error": "nothing to critique — subject is empty"}

    context_block = f"\nWhat he could not have known:\n{context.strip()}\n" if context.strip() else ""
    prompt = _PROMPT_TEMPLATE.format(
        kind=kind or "proposal",
        subject=subject,
        context_block=context_block,
    )

    from okuro.bridge import invoke

    result = invoke(
        prompt=prompt,
        capability=capability,
        provider=provider,
        system_prompt=_SYSTEM_PROMPT,
    )
    if not result.get("success"):
        return {"ok": False, "error": result.get("error") or "bridge call failed",
                "provider": result.get("provider")}

    try:
        parsed = _coerce(_parse(result.get("output") or ""))
    except ValueError as exc:
        return {"ok": False, "error": str(exc),
                "provider": result.get("provider"),
                "raw": (result.get("output") or "")[:2000]}

    parsed["ok"] = True
    parsed["provider"] = result.get("provider")
    parsed["model"] = result.get("model")
    parsed["duration"] = result.get("duration")
    return parsed


__all__ = ["critique", "SCOPE_INSTANCE", "SCOPE_CLASS", "SCOPE_MIXED", "SCOPE_NA"]
