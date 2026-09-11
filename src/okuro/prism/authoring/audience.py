# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W5 Phase B — ENGINE 1: AUDIENCE TRANSLATION. Rewrites a topic's
#   authored LadderDoc for a resolved audience: wording, tone, examples and framing
#   are re-authored per recipient across EVERY level (L1 hook -> L3 densest -> the L4
#   reading body) while the FACTS stay immutable — numbers, names, dates, entities
#   and per-claim cardinality are carried through untouched (answer 4: full rewrite,
#   facts immutable). Each rewrite is followed by a BLOCKING per-statement ENTAILMENT
#   gate: every rewritten claim (and the L4 body) must be entailed by its own pre-
#   rewrite source claim; a statement that asserts a fact its source does not support
#   BLOCKS with the precise path, and the rewrite is re-asked once naming the offence.
#   No invented fact can survive — by construction. Structure is untouched, so the
#   ladder/coverage/accuracy gates stay green on the rewritten ladder.
# index:
#   StatementCheck / EntailmentReport / EntailmentBlocked
#   check_entailment / rewrite_ladder
# AGENT_HEADER_END -->
"""Engine 1 — audience translation over the resolution ladder.

The orthogonality invariant (truth ⟂ audience ⟂ brand) is preserved by *verify*,
not by hope: a claim's TEXT may be re-worded freely for the recipient, but the
result is checked to ENTAIL the original claim before it can ship. The source of
truth for each rewritten claim is its OWN pre-rewrite claim (already grounded in
the artifact by the mine/author stages), so the gate is a pure re-verify — it needs
no external corpus and cannot be gamed: any added figure/name/date is caught at the
statement that introduced it, with a path a caller can act on.

Only ``claims`` (their text + per-item content) and the ``l4`` reading body change.
``plans`` / ``units`` / ``loads`` / ``densities`` are keyed on claim IDs, components
and item COUNTS — none of which the rewrite touches — so the rewritten LadderDoc
passes ``check_ladder`` and ``account`` exactly as the original did.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from okuro.prism.authoring.ladder import L4Doc, LadderDoc
from okuro.prism.compiler.ir import AudienceProfile
from okuro.prism.solver.schema import Claim

logger = logging.getLogger(__name__)

_MAX_REWRITE_REASK = 1   # one bounded re-ask on an entailment failure, then BLOCK


# ── per-statement entailment gate ────────────────────────────────────────────


@dataclass(frozen=True)
class StatementCheck:
    """One rewritten statement checked against its source. ``path`` locates it
    (``<topic_id>/claim/<claim_id>`` or ``<topic_id>/l4_body``); ``added`` lists the
    facts the rewrite asserted that the source does not entail (empty on pass)."""

    path: str
    passed: bool
    added: list[dict[str, str]] = field(default_factory=list)
    note: str = ""


@dataclass
class EntailmentReport:
    """The per-statement verdict for one rewritten ladder. ``passed`` iff EVERY
    statement is entailed by its source (the blocking condition)."""

    passed: bool
    checks: list[StatementCheck]

    @property
    def failures(self) -> list[StatementCheck]:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> str:
        n = len(self.checks)
        ok = sum(1 for c in self.checks if c.passed)
        return (f"entailment {'PASS' if self.passed else 'BLOCK'} · {ok}/{n} statements"
                + ("" if self.passed else " · failures: "
                   + "; ".join(f"{c.path}{[a.get('fact') for a in c.added]}"
                               for c in self.failures)))


class EntailmentBlocked(Exception):
    """Raised when a rewritten ladder still asserts an unsupported fact after the
    bounded re-ask. Carries the offending StatementChecks (path + added facts) so
    the caller can surface exactly which statement failed and why."""

    def __init__(self, report: EntailmentReport):
        self.report = report
        super().__init__(report.summary())


def _claim_source_text(c: Claim) -> str:
    """The full fact set of one claim, as the trusted SOURCE text for entailment:
    the claim statement plus every mined per-item triple (label · detail · meta)."""
    parts = [c.text or ""]
    for lab, det, meta in c.content_items():
        row = " · ".join(x for x in (lab, det, meta) if x)
        if row:
            parts.append(row)
    return "\n".join(p for p in parts if p).strip()


def check_entailment(
    original: LadderDoc,
    rewritten: LadderDoc,
    *,
    provider: Optional[str] = None,
    entail_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> EntailmentReport:
    """Verify every rewritten statement is entailed by its pre-rewrite source.

    Per-statement granularity: each claim is checked against its OWN original claim
    (statement + per-item content), and the L4 reading body against the original L4
    body. ``entail_fn`` is injectable for offline tests; defaults to the LLM judge
    ``critic.critique_entailment``.
    """
    if entail_fn is None:
        from okuro.prism.critic import critique_entailment as entail_fn  # type: ignore

    checks: list[StatementCheck] = []
    tid = rewritten.topic_id
    for cid, rc in rewritten.claims.items():
        oc = original.claims.get(cid)
        if oc is None:
            continue
        src = _claim_source_text(oc)
        adapted = _claim_source_text(rc)
        if not adapted or adapted == src:
            # unchanged (or empty) text cannot add a fact — no judge call needed.
            checks.append(StatementCheck(f"{tid}/claim/{cid}", True))
            continue
        res = entail_fn(src, adapted, provider=provider)
        checks.append(StatementCheck(
            f"{tid}/claim/{cid}", bool(res.get("passed")),
            added=list(res.get("added") or []), note=res.get("note", "")))

    # the L4 reading body vs the original master doc.
    o_body = (original.l4.body or "").strip()
    r_body = (rewritten.l4.body or "").strip()
    if r_body and r_body != o_body:
        res = entail_fn(o_body, r_body, provider=provider)
        checks.append(StatementCheck(
            f"{tid}/l4_body", bool(res.get("passed")),
            added=list(res.get("added") or []), note=res.get("note", "")))

    return EntailmentReport(passed=all(c.passed for c in checks), checks=checks)


# ── the rewrite pass ─────────────────────────────────────────────────────────


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["claims"],
    "additionalProperties": False,
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "text"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    # ``shape`` is sent to the model as structural context; a live
                    # model commonly ECHOES it back. Allow it (ignored by
                    # _apply_rewrite, which keeps the source claim's shape) so a
                    # harmless echo never fails the whole rewrite. (W5 Phase D:
                    # live models echo input fields — offline scripted invokes did
                    # not, so this class only shows under a real provider.)
                    "shape": {"type": "string"},
                    "text": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 3, "maxItems": 3,
                        },
                    },
                },
            },
        },
        "l4_body": {"type": "string"},
    },
}

_SYSTEM = """You are ENGINE 1 (audience translation) of the okuro·prism deck \
compiler. You are given a topic's authored content and a description of ONE \
audience. Re-author the WORDING, TONE, EXAMPLES and FRAMING of every claim (and the \
L4 reading body) so it lands for THIS audience — an engineer, a board member and a \
lay reader should each get language pitched to them.

ABSOLUTE RULE — FACTS ARE IMMUTABLE. You may re-word, re-frame, simplify, translate \
and re-order; you may NOT add, remove, alter or strengthen any FACT. Keep every \
number, figure, unit, percentage, name, date, entity and proper noun exactly as \
given. Do NOT invent examples, benefits, ROI, or comparisons the source does not \
state. Do NOT turn a hedged claim ("about", "up to") into a certain one. If a claim \
has per-item content, return the SAME number of items in the SAME order — re-word \
each item's label/detail but keep its underlying fact.

Return ONLY {"claims":[{"id","text","items":[[label,detail,meta],...]}...], \
"l4_body": "..."}. Every claim id you were given must appear once. "items" is \
required only for claims that HAD items (same count); omit it otherwise. Every \
rewritten statement will be machine-checked to ENTAIL its source — an added fact \
is rejected."""


def _rewrite_payload(ladder: LadderDoc) -> dict[str, Any]:
    """Compact, audience-neutral serialization of the claims + L4 body to rewrite."""
    claims = []
    for cid, c in ladder.claims.items():
        entry: dict[str, Any] = {"id": cid, "shape": c.shape, "text": c.text}
        items = c.content_items()
        if items:
            entry["items"] = [list(t) for t in items]
        claims.append(entry)
    return {"claims": claims, "l4_body": ladder.l4.body or ""}


def _apply_rewrite(ladder: LadderDoc, data: dict[str, Any]) -> LadderDoc:
    """Fold an LLM rewrite back into a NEW LadderDoc, facts immutable by construction.

    A claim the model omitted, or whose item count does not match the source, keeps
    its ORIGINAL text/items (never silently blanked or re-shaped) — the entailment
    gate then only ever sees faithful-or-original text. Only ``claims`` + ``l4``
    change; every structural field is carried through untouched."""
    by_id = {c.get("id"): c for c in (data.get("claims") or []) if isinstance(c, dict)}
    new_claims: dict[str, Claim] = {}
    for cid, oc in ladder.claims.items():
        rc = by_id.get(cid)
        if not rc:
            new_claims[cid] = oc
            continue
        text = (rc.get("text") or "").strip() or oc.text
        items = oc.items_content
        r_items = rc.get("items")
        if isinstance(r_items, list) and len(r_items) == len(oc.items_content):
            try:
                items = tuple((str(t[0]), str(t[1]), str(t[2])) for t in r_items)
            except (IndexError, TypeError):
                items = oc.items_content   # malformed -> keep the source items
        new_claims[cid] = dataclasses.replace(
            oc, text=text, chars=len(text), items_content=items)

    body = (data.get("l4_body") or "").strip() or ladder.l4.body
    new_l4 = L4Doc(ladder.l4.topic_id, ladder.l4.title, body, ladder.l4.claim_ids)
    return dataclasses.replace(ladder, claims=new_claims, l4=new_l4)


def _audience_block(audience: AudienceProfile) -> str:
    from okuro.prism.compiler.project import _audience_descriptor
    return _audience_descriptor(audience)


def rewrite_ladder(
    ladder: LadderDoc,
    audience: AudienceProfile,
    *,
    provider: Optional[str] = None,
    invoke_fn: Optional[Callable[..., Any]] = None,
    entail_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> tuple[LadderDoc, EntailmentReport]:
    """Rewrite one topic's ladder for ``audience``, then run the BLOCKING per-
    statement entailment gate. On a failure the rewrite is re-asked ONCE naming the
    exact offending statements; if it still fails, raises ``EntailmentBlocked``.

    Returns ``(rewritten_ladder, entailment_report)`` on success (report.passed).
    ``invoke_fn`` / ``entail_fn`` are injectable so the whole pass runs offline.
    """
    from okuro.prism.compiler import llm

    payload = _rewrite_payload(ladder)
    base_user = (
        f"AUDIENCE:\n{_audience_block(audience)}\n\n"
        f"TOPIC CONTENT to re-author (facts immutable):\n{llm.dumps(payload)}\n\n"
        "Re-author wording/tone/examples for this audience. Return ONLY the JSON."
    )

    feedback = ""
    last_report: Optional[EntailmentReport] = None
    for attempt in range(_MAX_REWRITE_REASK + 1):
        data = llm.call_json(
            system_prompt=_SYSTEM, user_prompt=base_user + feedback, schema=_SCHEMA,
            stage="audience-rewrite", provider=provider, capability="standard",
            timeout=300, thinking_tokens=4000, invoke_fn=invoke_fn,
        )
        candidate = _apply_rewrite(ladder, data)
        report = check_entailment(ladder, candidate, provider=provider, entail_fn=entail_fn)
        last_report = report
        if report.passed:
            if attempt:
                logger.info("audience-rewrite: entailment clean on re-ask attempt %d", attempt + 1)
            return candidate, report
        # blocking failure — re-ask naming the exact unsupported statements.
        offences = "; ".join(
            f"{c.path} added {[a.get('fact') for a in c.added]}" for c in report.failures)
        logger.warning("audience-rewrite: entailment BLOCK (attempt %d): %s",
                       attempt + 1, offences)
        feedback = (
            "\n\n---\nYour previous rewrite INVENTED facts the source does not "
            f"support, at these statements:\n  {offences}\n"
            "Re-author ONLY with facts present in the source. Remove every invented "
            "figure/name/date. Return ONLY the corrected JSON.")

    assert last_report is not None
    raise EntailmentBlocked(last_report)


__all__ = [
    "StatementCheck", "EntailmentReport", "EntailmentBlocked",
    "check_entailment", "rewrite_ladder",
]
