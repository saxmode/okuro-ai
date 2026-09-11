# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Turn raw critic findings into ONE answerable question with concrete
#   options. Also filters gates whose findings cannot be phrased at all.
# index: Decision | DecisionBrief | dedupe_findings | build_brief | _PROMPT
# AGENT_HEADER_END -->
"""Make a blocked-review gate answerable by a person.

THE PROBLEM
-----------
``gate_messages`` humanizes the gate FRAME — headline, explanation, action —
and its docstring promises to quarantine "error strings, findings, UUIDs" into
a collapsed details field. It does that job well. But the findings are a
separate payload field rendered directly by the UI, so they bypass it: the
plain-language part tells the user a decision is needed and never says WHAT it
is, while the only place the substance lives is raw reviewer output.

Observed on a real gate (task-20260724-110216 phase 7): seven finding cards,
each reading like

    MUST FIX Replace 'rm850x' with "RAM: single Corsair Vengeance 128GB
    (2x64GB) DDR5-6400 CL42 kit ..." (per event:687476)

Five were the same replacement text pasted against five different tokens —
including a demand to replace a power-supply wattage with a RAM specification.
The two genuinely actionable findings were hidden behind "+2 more item(s)".
No non-author could answer that, and the author could only answer it by
opening the artifact themselves.

THE TEST
--------
A gate is answerable when a competent person who has never seen okuro could
decide it from domain knowledge alone — no ids, no internal vocabulary, no
reading the deliverable first. Everything here serves that test.

THE FILTER
----------
A question the system cannot phrase in the user's language is usually a bug,
not a decision. "Replace a wattage with a RAM spec" has no domain meaning
BECAUSE it is a false positive. So the brief doubles as a quality gate on the
gate: when nothing survives phrasing, ``all_unphrasable`` is set and the caller
can decline to interrupt the user at all.

DEGRADATION
-----------
The LLM pass is best-effort. Any failure returns ``None`` and the UI falls back
to today's raw view. A humanizer must never block a gate — being unable to
phrase the question nicely is not a reason to withhold it entirely.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("okuro.orchestrator.decision_brief")

# Findings past this point are noise for a human decision; the raw list stays
# available behind the details disclosure.
_MAX_DECISIONS = 4
_MAX_EVIDENCE_CHARS = 400


@dataclass
class Decision:
    """One thing the user is actually being asked to choose."""
    question: str                 # plain language, domain vocabulary
    context: str = ""             # quoted deliverable text, if any
    options: list[str] = field(default_factory=list)
    recommended: str = ""         # one of options, or ""
    why: str = ""                 # one line: what is at stake
    finding_ids: list[int] = field(default_factory=list)  # indexes into raw


@dataclass
class DecisionBrief:
    decisions: list[Decision] = field(default_factory=list)
    all_unphrasable: bool = False   # nothing could be phrased → likely bogus
    raw_count: int = 0              # findings before dedupe
    note: str = ""                  # why a filter fired, for the log

    def to_payload(self) -> dict:
        return {
            "decisions": [asdict(d) for d in self.decisions],
            "all_unphrasable": self.all_unphrasable,
            "raw_count": self.raw_count,
            "note": self.note,
        }


# --------------------------------------------------------------------------
# Deterministic pre-pass — cheap, no model, runs even when the bridge is down
# --------------------------------------------------------------------------


def _norm(text: str) -> str:
    """Collapse whitespace + case so near-identical findings compare equal."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def dedupe_findings(findings: list[dict]) -> list[dict]:
    """Collapse findings that state the same thing.

    The observed gate showed seven cards for two real issues: five carried an
    identical ``suggested_fix`` and the remaining two were a duplicated pair.
    Deduping before the model runs cuts cost, and — more importantly — stops
    the UI presenting one issue as five, which reads as five times the problem.

    Keyed on (summary, suggested_fix) rather than on the whole record, because
    the differing fields (artifact_id, evidence, event ref) are exactly the
    ones a human does not decide on. Evidence from collapsed duplicates is
    preserved on the survivor so nothing is lost.
    """
    seen: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        # Findings that share an ``origin`` came from ONE superseded decision.
        # The checker decomposes the old choice into several terms and emits a
        # finding per term, so a single decision arrives as three or four cards
        # with near-identical bodies. To the user that is one thing to decide,
        # so collapse on origin before anything else looks at them.
        origin = _norm(f.get("origin", ""))
        if origin:
            key = ("origin", origin)
        else:
            key = (_norm(f.get("summary", "")), _norm(f.get("suggested_fix", "")))
        if key in seen:
            merged = seen[key]
            ev = (f.get("evidence") or "").strip()
            if ev and ev not in merged.get("_evidence_all", []):
                merged.setdefault("_evidence_all", []).append(ev)
            merged["_duplicate_count"] = merged.get("_duplicate_count", 1) + 1
            continue
        rec = dict(f)
        rec["_duplicate_count"] = 1
        ev = (rec.get("evidence") or "").strip()
        rec["_evidence_all"] = [ev] if ev else []
        seen[key] = rec
        order.append(key)
    return [seen[k] for k in order]


_PROMPT = """\
You are writing the question a person will answer to unblock an automated \
task. They are competent in the task's subject but know NOTHING about the \
system that produced these findings.

TASK: {task_desc}
STEP: {subtask_desc}

QUALITY-CHECK FINDINGS (raw, machine-generated — may contain false positives):
{findings_block}

Turn these into at most {max_decisions} decisions. Rules:

1. One decision per DISTINCT choice. Merge findings that are the same issue.
2. Write in the subject's vocabulary. Never mention findings, tokens, \
artifact ids, event numbers, ledgers, or any system internals.
3. State a CHOICE, never an operation. Not "replace X with Y" but "should the \
old part stay in the comparison table, or be removed?".
4. Give 2-3 concrete options a person can pick. Mark one recommended when \
there is an obvious right answer.
5. `context`: quote the offending text from the evidence verbatim if present, \
so the reader sees what is at issue. Empty string if the evidence has none.
6. `why`: one short line on what is at stake.

CRITICAL — false positives. Some findings are incoherent (e.g. demanding a \
power-supply wattage be replaced with a memory specification). If a finding \
cannot be expressed as a real choice in the subject's terms, DROP it. If NO \
finding can, return an empty decisions list. Never invent a plausible-sounding \
question to cover for a nonsensical finding.

Return ONLY this JSON:
{{"decisions": [{{"question": str, "context": str, "options": [str], \
"recommended": str, "why": str, "finding_ids": [int]}}]}}
"""


def _findings_block(findings: list[dict]) -> str:
    lines = []
    for i, f in enumerate(findings):
        ev = " ".join(f.get("_evidence_all") or [])[:_MAX_EVIDENCE_CHARS]
        dup = f.get("_duplicate_count", 1)
        dup_note = f" (reported {dup}x)" if dup > 1 else ""
        lines.append(
            f"[{i}]{dup_note} severity={f.get('severity', '?')}\n"
            f"  says: {(f.get('summary') or '')[:300]}\n"
            f"  wants: {(f.get('suggested_fix') or '')[:300]}\n"
            f"  seen in: {ev or '(no excerpt)'}"
        )
    return "\n\n".join(lines)


def _parse(raw: str, findings: list[dict]) -> list[Decision]:
    text = (raw or "").strip()
    # Models wrap JSON in prose or fences often enough to be worth handling.
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    out: list[Decision] = []
    for d in (data.get("decisions") or [])[:_MAX_DECISIONS]:
        if not isinstance(d, dict):
            continue
        q = (d.get("question") or "").strip()
        if not q:
            continue
        opts = [str(o).strip() for o in (d.get("options") or []) if str(o).strip()]
        rec = (d.get("recommended") or "").strip()
        if rec and rec not in opts:
            rec = ""  # never point at an option the user cannot pick
        ids = [
            int(i) for i in (d.get("finding_ids") or [])
            if isinstance(i, (int, str)) and str(i).isdigit()
            and int(i) < len(findings)
        ]
        out.append(Decision(
            question=q,
            context=(d.get("context") or "").strip(),
            options=opts,
            recommended=rec,
            why=(d.get("why") or "").strip(),
            finding_ids=ids,
        ))
    return out


def build_brief(
    findings: list[dict],
    *,
    task_desc: str = "",
    subtask_desc: str = "",
    invoke_fn: Optional[Callable] = None,
) -> Optional[DecisionBrief]:
    """Best-effort human-answerable brief. ``None`` means "show the raw view".

    Never raises. Never blocks the gate.
    """
    deduped = dedupe_findings(findings)
    if not deduped:
        return None

    if invoke_fn is None:
        try:
            from okuro.bridge.invoke import invoke as invoke_fn  # type: ignore
        except Exception as exc:
            logger.warning("decision_brief: bridge unavailable (%r)", exc)
            return None

    prompt = _PROMPT.format(
        task_desc=(task_desc or "(not given)")[:600],
        subtask_desc=(subtask_desc or "(not given)")[:600],
        findings_block=_findings_block(deduped),
        max_decisions=_MAX_DECISIONS,
    )
    try:
        result = invoke_fn(
            prompt=prompt,
            capability="fast",
            tool=True,
            system_prompt=(
                "You turn machine diagnostics into questions a non-expert can "
                "answer. Output ONLY the requested JSON object."
            ),
        )
    except Exception as exc:
        logger.warning("decision_brief: invoke failed (%r)", exc)
        return None

    if not isinstance(result, dict) or not result.get("success"):
        logger.warning("decision_brief: no answer from bridge")
        return None

    decisions = _parse(str(result.get("output", "")), deduped)
    brief = DecisionBrief(decisions=decisions, raw_count=len(findings or []))

    if not decisions:
        # The filter. Nothing survived phrasing — on the evidence so far that
        # means the findings are incoherent, not that the user is unqualified.
        brief.all_unphrasable = True
        brief.note = (
            f"{len(deduped)} finding(s) could not be expressed as a decision "
            "in the subject's terms — probable false positives"
        )
        logger.warning("decision_brief: %s", brief.note)
    return brief


def brief_payload(findings: list[dict], task, subtask_ids: list[str] | None = None) -> dict:
    """``{"decision_brief": {...}}`` to merge into an awaiting payload, or ``{}``.

    Convenience for gate sites: resolves the subtask description from the task,
    builds the brief, and swallows every failure. Callers merge the result and
    carry on — a gate must fire whether or not it could be phrased nicely.
    """
    try:
        subtask_desc = ""
        wanted = set(subtask_ids or [])
        if wanted:
            for ph in getattr(task, "phases", []) or []:
                for st in getattr(ph, "subtasks", []) or []:
                    if st.id in wanted:
                        subtask_desc = getattr(st, "description", "") or ""
                        break
                if subtask_desc:
                    break
        brief = build_brief(
            findings,
            task_desc=getattr(task, "description", "") or "",
            subtask_desc=subtask_desc,
        )
        return {"decision_brief": brief.to_payload()} if brief else {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("decision_brief: payload build failed (%r)", exc)
        return {}
