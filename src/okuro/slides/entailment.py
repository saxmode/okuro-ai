# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The "can't lie" gate — verify every factual claim in a generated deck
#          is entailed by its grounding source; grade grounded/inferred/fabricated.
# index:
#   def extract_claims
#   def check_claims
#   def annotate_deck
#   def fabricated_refs
# AGENT_HEADER_END -->
"""Native slides entailment gate.

The deck-authoring LLM is grounded on facts gathered from okuro's brain
(``gather_context``), but nothing verified that what it WROTE is actually
supported by those facts — so a generated deck could assert an invented figure,
name, or date. This module closes that gap for ``okuro.slides`` WITHOUT any
dependency on ``okuro.prism`` (the concept is prism's adaptation-entailment
critic; the implementation here is fresh and authoring-oriented).

Model — every factual claim gets one of three statuses:
  - ``grounded``   : the source text supports it (or it asserts no verifiable fact)
  - ``inferred``   : reasonable given the source, but not explicitly stated
  - ``fabricated`` : asserts a fact / figure / name / date the source does NOT support

Policy is the caller's (see ``generate.py``): the shipping default is HYBRID —
hard-block ``fabricated`` (redo, then drop), soft-flag ``inferred``, pass
``grounded``. This module only classifies + annotates; it never mutates the
source of truth and never blocks by itself.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger("okuro.slides.entailment")

_STATUSES = ("grounded", "inferred", "fabricated")

_SYSTEM = (
    "You are okuro·slides' ENTAILMENT auditor. You are given SOURCE (trusted "
    "ground-truth facts gathered from a knowledge base) and a numbered list of "
    "CLAIMS taken verbatim from a generated slide deck. For EACH claim, decide "
    "whether the SOURCE supports it. A claim that asserts no verifiable fact "
    "(a title, a label, a call-to-action, a generic statement, a rhetorical "
    "phrase) is 'grounded' by default — you are hunting for INVENTED specifics.\n\n"
    "Statuses:\n"
    "  grounded   — SOURCE supports it, OR it asserts no checkable fact.\n"
    "  inferred   — plausible and consistent with SOURCE, but SOURCE does not "
    "state it explicitly.\n"
    "  fabricated — asserts a fact, figure, number, %, date, name, or quote that "
    "SOURCE does NOT support, or strengthens a hedged claim into a certain one.\n\n"
    "Be strict on invented NUMBERS, PERCENTAGES, DATES, and proper NAMES. "
    "Re-wording, summarising, and omission are never defects.\n\n"
    'Output ONLY a JSON object: {"verdicts":[{"i":int,"status":"grounded"|'
    '"inferred"|"fabricated","why":str} ...]}. One entry per claim index; "why" '
    "is a short reason (<=12 words), required for inferred/fabricated."
)


def _fmt_num(v: Any) -> str:
    """Compact numeric string — 42.0 → "42", 3.5 → "3.5" — so a chart/table figure
    reads as the author meant it and the judge sees a clean, checkable number."""
    f = float(v)
    return str(int(f)) if f.is_integer() else str(f)


def _element_claim_text(el: dict[str, Any]) -> str:
    """The checkable text for ONE element, across EVERY content kind.

    Returns a single string combining the element's fact-bearing fields (empty
    when it carries nothing verifiable). Kept element-granular on purpose: the
    ref stays a real element id, so ``annotate_deck`` / ``drop_refs`` /
    ``fabricated_refs`` (all keyed on that id) keep working unchanged for text,
    frame children, and every Phase C/D primitive. The most dangerous fabricated
    claims — invented NUMBERS in a kpi, chart, or table — now live in the audited
    string instead of escaping the text-only gate.
    """
    kind = el.get("kind")
    if kind == "text":
        return (el.get("text") or "").strip()
    if kind == "list":
        items = [str(i).strip() for i in (el.get("items") or []) if str(i).strip()]
        return " • ".join(items)
    if kind == "kpi":
        parts = [str(el.get(k) or "").strip() for k in ("value", "label", "delta")]
        return " ".join(p for p in parts if p)
    if kind == "quote":
        parts = [str(el.get("text") or "").strip(), str(el.get("attribution") or "").strip()]
        return " — ".join(p for p in parts if p)
    if kind == "table":
        cells: list[str] = []
        for row in el.get("rows") or []:
            if isinstance(row, (list, tuple)):
                cells.extend(str(c).strip() for c in row if str(c).strip())
        return " | ".join(cells)
    if kind == "chart":
        segs: list[str] = []
        for s in el.get("series") or []:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "").strip()
            vals = ", ".join(
                _fmt_num(v)
                for v in (s.get("values") or [])
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            )
            seg = f"{name}: {vals}" if name and vals else (name or vals)
            if seg:
                segs.append(seg)
        cats = [str(c).strip() for c in (el.get("categories") or []) if str(c).strip()]
        if cats:
            segs.append("categories " + ", ".join(cats))
        return " | ".join(segs)
    return ""  # frame / box / image / video / flow / divider — nothing to verify


def extract_claims(deck: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the deck IR and collect every fact-bearing element as a claim.

    Returns a list of ``{"ref": "<slide_id>/<el_id>", "text": str}`` for every
    content-bearing element (text AND the Phase C/D primitives list/kpi/quote/
    table/chart), recursing into frame children, whose combined claim text is
    non-trivial. ``ref`` is stable across the deck so a verdict maps back to its
    element — so chart/table/kpi NUMBERS become checkable claims, not just text.
    """
    claims: list[dict[str, Any]] = []

    def walk(el: dict[str, Any], slide_id: str) -> None:
        if not isinstance(el, dict):
            return
        txt = _element_claim_text(el)
        if len(txt) >= 3:
            claims.append({"ref": f"{slide_id}/{el.get('id')}", "text": txt})
        for child in el.get("children") or []:
            walk(child, slide_id)

    for s in deck.get("slides") or []:
        sid = str(s.get("id") or "")
        for el in s.get("elements") or []:
            walk(el, sid)
    return claims


def _invoke_judge(system: str, user: str, provider: Optional[str], timeout: int = 120) -> dict[str, Any]:
    """One thinking-off judge call, tolerant JSON parse. Raises on invoke error."""
    from okuro.bridge.invoke import invoke

    prev = os.environ.get("MAX_THINKING_TOKENS")
    os.environ["MAX_THINKING_TOKENS"] = "0"
    try:
        res = invoke(prompt=user, system_prompt=system, provider=provider, capability="standard", timeout=timeout)
    finally:
        if prev is None:
            os.environ.pop("MAX_THINKING_TOKENS", None)
        else:
            os.environ["MAX_THINKING_TOKENS"] = prev
    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError((res or {}).get("error") if isinstance(res, dict) else "invoke failed")
    text = (res.get("output") or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("entailment judge returned no JSON object")
    return json.loads(text[start : end + 1])


def check_claims(
    claims: list[dict[str, Any]], source_text: str, *, provider: Optional[str] = None
) -> dict[str, dict[str, str]]:
    """Classify each claim against ``source_text``.

    Returns ``{ref: {"status": ..., "why": ...}}``. With no source or no claims,
    returns everything as ``unverified`` (we cannot prove entailment without a
    source — absence of proof is not proof of fabrication).
    """
    source_text = (source_text or "").strip()
    if not claims:
        return {}
    if not source_text:
        return {c["ref"]: {"status": "unverified", "why": "no grounding source"} for c in claims}

    numbered = "\n".join(f"{i}. {c['text']}" for i, c in enumerate(claims))
    user = (
        f"SOURCE (trusted ground truth):\n{source_text[:9000]}\n\n"
        f"CLAIMS (from the generated deck):\n{numbered}\n\n"
        "Return ONLY the JSON object with one verdict per claim index."
    )
    data = _invoke_judge(_SYSTEM, user, provider)
    verdicts = data.get("verdicts") if isinstance(data, dict) else None
    out: dict[str, dict[str, str]] = {}
    for v in verdicts or []:
        if not isinstance(v, dict):
            continue
        try:
            idx = int(v.get("i"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(claims)):
            continue
        status = v.get("status") if v.get("status") in _STATUSES else "grounded"
        out[claims[idx]["ref"]] = {"status": status, "why": (v.get("why") or "").strip()[:160]}
    # Any claim the judge silently skipped is treated as grounded (no fact flagged).
    for c in claims:
        out.setdefault(c["ref"], {"status": "grounded", "why": ""})
    return out


def annotate_deck(deck: dict[str, Any], verdicts: dict[str, dict[str, str]]) -> None:
    """Write ``provenance`` onto each element in place, keyed by its ref."""

    def walk(el: dict[str, Any], slide_id: str) -> None:
        if not isinstance(el, dict):
            return
        ref = f"{slide_id}/{el.get('id')}"
        v = verdicts.get(ref)
        if v is not None:
            el["provenance"] = {"status": v["status"], "why": v.get("why", "")}
        for child in el.get("children") or []:
            walk(child, slide_id)

    for s in deck.get("slides") or []:
        sid = str(s.get("id") or "")
        for el in s.get("elements") or []:
            walk(el, sid)


def fabricated_refs(verdicts: dict[str, dict[str, str]]) -> list[str]:
    """Refs whose claim was judged ``fabricated`` (the hard-block set)."""
    return [ref for ref, v in verdicts.items() if v.get("status") == "fabricated"]


def counts(verdicts: dict[str, dict[str, str]]) -> dict[str, int]:
    """Tally verdicts by status for gate summaries / UI."""
    out: dict[str, int] = {}
    for v in verdicts.values():
        out[v.get("status", "grounded")] = out.get(v.get("status", "grounded"), 0) + 1
    return out


def drop_refs(deck: dict[str, Any], refs: list[str]) -> int:
    """Remove the elements named in ``refs`` (and any slide left empty) in place.

    The hybrid policy's last resort: fabrication that survives a redo is deleted
    so it never ships. Returns the number of elements removed. Never empties the
    deck — if every slide would be dropped, the deck is left untouched (caller
    should flag this rare 'unremovable' case)."""
    ref_set = set(refs)
    if not ref_set:
        return 0

    def prune(elements: list[dict[str, Any]], slide_id: str) -> tuple[list[dict[str, Any]], int]:
        kept: list[dict[str, Any]] = []
        removed = 0
        for el in elements:
            if not isinstance(el, dict):
                continue
            if f"{slide_id}/{el.get('id')}" in ref_set:
                removed += 1
                continue
            if el.get("children"):
                el["children"], child_removed = prune(el["children"], slide_id)
                removed += child_removed
            kept.append(el)
        return kept, removed

    # Compute on copies first so the "never empty the deck" guard can bail out
    # without having already mutated any slide in place.
    total = 0
    computed: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for s in deck.get("slides") or []:
        sid = str(s.get("id") or "")
        pruned, removed = prune(copy.deepcopy(s.get("elements") or []), sid)
        total += removed
        computed.append((s, pruned))

    surviving = [(s, pruned) for (s, pruned) in computed if pruned]
    if not surviving:
        return 0  # would delete every element → leave the deck untouched
    for s, pruned in surviving:
        s["elements"] = pruned
    deck["slides"] = [s for (s, _) in surviving]
    return total
