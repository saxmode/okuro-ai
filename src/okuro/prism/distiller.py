# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism Source Distiller — turn a provided root document (an okuro
#   orchestrator artifact: research / report / plan / session) into a structured,
#   cited claim set that is the single ground truth every later pipeline stage
#   cites. Implements the prism-source-distiller role charter as a callable stage.
# index: _DISTILL_SYSTEM | distill
# AGENT_HEADER_END -->
"""Prism Source Distiller.

The first stage of the target Prism pipeline (root document → deck). It does NOT
generate content — it EXTRACTS atomic, cited claims from a source the caller
provides, so nothing downstream is invented: every fact/figure/name a deck shows
must trace back to a claim id emitted here.

Mirrors the ``prism-source-distiller`` role charter (roles/catalog). Kept as code
(not only a role prompt) so the pipeline can call it deterministically and eval
it against a golden source.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("okuro.prism.distiller")

# DATA SHAPE vocabulary — imported from the SINGLE canonical source (prism.shapes),
# the same tuple the arranger's expressiveness/effectiveness maps key on, so a
# distiller-tagged shape and downstream component selection never diverge. Attached
# to each claim at distill time = ground truth, computed once, visible deck-wide.
from okuro.prism.shapes import SHAPES
from okuro.prism.text_chunk import chunk_body

CLAIM_KINDS = ("fact", "figure", "decision", "risk", "option", "definition", "quote")

# Depth GRANULARITY tier of a claim — the reservoir that lets the ladder give a
# deeper rung MORE DATA (not more prose). Ordered shallow→deep; maps to the rung
# at which a claim FIRST surfaces (headline→L1, supporting→L2,
# detail→L3, edge→L4). A short source may yield few detail/edge claims —
# that is honest scarcity, never a licence to pad.
GRANULARITY = ("headline", "supporting", "detail", "edge")

def _norm_shape(raw: Any) -> str:
    """Validate a model-emitted data shape against SHAPES; default to 'text' (the
    safe, component-neutral shape) for anything unknown or missing."""
    return raw if raw in SHAPES else "text"


def _norm(s: str) -> str:
    """Lowercase + collapse whitespace + straighten smart quotes/dashes, so a
    model's verbatim evidence quote can be substring-matched against the source
    across minor punctuation/spacing differences (provenance grounding check)."""
    s = (s or "").lower().replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"').replace("—", "-").replace("–", "-")
    return " ".join(s.split())

_DISTILL_SYSTEM = (
    "You are okuro·prism's SOURCE DISTILLER. Convert the provided root document into "
    "a structured, deduplicated, CITED claim set — the single ground truth a later "
    "pipeline will build a presentation from. Output ONLY a JSON object, nothing else.\n\n"
    "Extract ATOMIC claims — ONE self-contained idea each. For every claim:\n"
    '  {"id":"c1","statement":str,"kind":"fact"|"figure"|"decision"|"risk"|"option"|'
    '"definition"|"quote","granularity":"headline"|"supporting"|"detail"|"edge",'
    '"shape":"single-value"|"part-of-whole"|"ranking"|"time-series"|"comparison"|'
    '"distribution"|"relationship"|"process"|"spatial"|"hierarchy"|"text",'
    '"evidence":str,"confidence":"high"|"med"|"low"}\n'
    "  - statement: one factual assertion, standalone (no 'it'/'this' referring outside).\n"
    "  - kind: figure = carries a number/metric; option = a candidate/alternative; "
    "decision = a chosen direction; risk = a hazard/anti-pattern; definition = what a "
    "term means; quote = a verbatim line worth keeping.\n"
    "  - granularity: the DEPTH TIER this claim belongs at. This is load-bearing — a "
    "deeper presentation layer must add finer DATA, not more words, so tier honestly:\n"
    "      headline = the top-line takeaway/outcome a busy reader needs (few of these);\n"
    "      supporting = the main facts that back the headline;\n"
    "      detail = specific figures, named mechanisms, parameters, concrete specifics;\n"
    "      edge = caveats, failure modes, boundary conditions, non-obvious trade-offs, "
    "exceptions (the things only an expert asks about).\n"
    "    Push every SPECIFIC number, name, or mechanism to detail/edge — do NOT tier "
    "everything headline/supporting, or the expert layer starves and the deck pads.\n"
    "  - shape: the DATA SHAPE this claim's content naturally takes — the signal a "
    "later stage uses to choose the right visual component. Pick ONE: single-value = "
    "one dominant number/metric; part-of-whole = a share of a total; ranking = ordered "
    "magnitudes across items; time-series = a value over time; comparison = A vs B or "
    "option x criteria; distribution = a spread/range/uncertainty; relationship = "
    "dependencies/architecture/how parts connect; process = an ordered sequence of "
    "steps; spatial = a geographic or physical layout; hierarchy = a tree/nesting; "
    "text = an assertion/definition/quote with no inherent data shape. Tag by what "
    "the claim IS — most prose claims are 'text'; reserve the data shapes for claims "
    "that genuinely carry that structure.\n"
    "  - evidence: a VERBATIM quote (about 8-25 words) copied EXACTLY from the SOURCE "
    "that contains this claim — NOT a paraphrase, NOT a section title. It is checked "
    "against the source verbatim, so copy the words as they appear.\n\n"
    "HARD RULES:\n"
    "  1. Every claim MUST trace to the source. If the source doesn't say it, it is NOT a claim.\n"
    "  2. Copy figures, units, names and identifiers VERBATIM — never round or paraphrase a number.\n"
    "  3. NEVER invent, infer beyond the text, or editorialize.\n"
    "  4. Deduplicate near-identical claims — keep the most specific.\n"
    "  5. One idea per claim; split compound sentences.\n\n"
    'Return exactly: {"claims":[ ... ],"coverage_note":str}. coverage_note is 2-3 lines: '
    "what the source covers, and what it does NOT (so downstream knows the gaps)."
)


def distill(
    source_text: str,
    *,
    kind: str = "research",
    provider: Optional[str] = None,
    max_chars: int = 120_000,  # per-call budget; larger sources are chunked, not sliced
) -> dict[str, Any]:
    """Extract a cited claim set from a root document.

    Returns ``{"claims": [{id, statement, kind, granularity, shape, evidence,
    confidence, grounded}, ...], "coverage_note": str, "truncated": bool,
    "source_chars": int}``. ``shape`` is the claim's data shape (SHAPES) — the
    signal the component arranger reads to pick a visual module. Raises on an
    empty source or a failed model call; a parse failure yields an empty claim set
    rather than raising (the caller decides whether that's fatal)."""
    text = (source_text or "").strip()
    if not text:
        raise ValueError("source_text required")

    # Chunk instead of the old body[:max_chars] slice: a large research artifact is
    # mined in FULL (each chunk a separate call), not silently truncated to the
    # first max_chars. Bodies that already fit stay a single call (unchanged path).
    chunks = chunk_body(text, max_chars)

    from okuro.bridge.invoke import invoke
    from okuro.prism.generate import _extract_json_span, _lenient_json_loads

    # Reasoning budget: extraction quality (atomicity, dedup, verbatim grounding,
    # coverage) is the load-bearing input edge — every downstream stage cites these
    # claims, so a bad extraction poisons the whole deck. Give it real thinking
    # (parity with architect/arranger) instead of the pipeline's default 0.
    def _attempt(user: str) -> tuple[list[dict[str, Any]], str]:
        """One invoke + parse → (claims, coverage_note). A parse failure yields an
        empty claim set (never raises) so the caller can retry; a failed model call
        DOES raise (the bridge is down — retrying won't help)."""
        # 600s, not the pipeline's usual 240: distill hands a model a full chunk
        # (up to max_chars) AND an 8k thinking budget, so it is structurally the
        # slowest call. At 240 a ~28k-char plan timed out at stage 1 and killed the
        # whole 15-25min build (nothing before it persists).
        res = invoke(
            prompt=user, system_prompt=_DISTILL_SYSTEM, provider=provider,
            capability="standard", timeout=600,
        )
        if not (isinstance(res, dict) and res.get("success")):
            raise RuntimeError((res or {}).get("error") if isinstance(res, dict) else "invoke failed")
        out: list[dict[str, Any]] = []
        note = ""
        try:
            data = _lenient_json_loads(_extract_json_span(res.get("output") or "", "{", "}"))
            if isinstance(data, dict):
                raw = data.get("claims") if isinstance(data.get("claims"), list) else []
                for i, c in enumerate(raw):
                    if not isinstance(c, dict) or not (c.get("statement") or "").strip():
                        continue
                    kind_v = c.get("kind") if c.get("kind") in CLAIM_KINDS else "fact"
                    gran_v = c.get("granularity") if c.get("granularity") in GRANULARITY else "supporting"
                    out.append({
                        "id": (c.get("id") or f"c{i + 1}").strip(),
                        "statement": c["statement"].strip(),
                        "kind": kind_v,
                        "granularity": gran_v,
                        "shape": _norm_shape(c.get("shape")),
                        "evidence": (c.get("evidence") or "").strip(),
                        "confidence": c.get("confidence") if c.get("confidence") in ("high", "med", "low") else "med",
                    })
                note = (data.get("coverage_note") or "").strip()
        except (ValueError, KeyError) as exc:
            logger.warning("distill: claim parse failed: %s", exc)
        return out, note

    # distill is NON-DETERMINISTIC: the model occasionally returns a short or
    # prose-wrapped response that parses to ZERO claims (live-repro'd 2026-07-21 —
    # one roll gave 197 claims, the next 0). Stage 1 is the load-bearing input edge
    # — an empty extraction aborts the whole 15-25min build before anything
    # persists — so retry ONCE per chunk on an empty parse before giving up.
    claims: list[dict[str, Any]] = []
    coverage_notes: list[str] = []
    _prev = os.environ.get("MAX_THINKING_TOKENS")
    os.environ["MAX_THINKING_TOKENS"] = "8000"
    try:
        for idx, chunk in enumerate(chunks):
            part = f" (part {idx + 1} of {len(chunks)})" if len(chunks) > 1 else ""
            user = (
                f"ROOT DOCUMENT (kind={kind}){part}:\n\n{chunk}\n\n"
                "Extract the complete claim set. Return ONLY the JSON object."
            )
            part_claims, note = _attempt(user)
            if not part_claims:
                logger.warning("distill: chunk %d/%d produced no claims — retrying once",
                               idx + 1, len(chunks))
                part_claims, note = _attempt(user)
            claims.extend(part_claims)
            if note:
                coverage_notes.append(note)
    finally:
        if _prev is None:
            os.environ.pop("MAX_THINKING_TOKENS", None)
        else:
            os.environ["MAX_THINKING_TOKENS"] = _prev

    # Per-chunk parsing restarts ids at c1 — renumber to one global sequence so no
    # two chunks collide on an id a later stage cites.
    for i, c in enumerate(claims, 1):
        c["id"] = f"c{i}"

    # Provenance grounding: verify each claim's evidence quote appears VERBATIM in
    # the full source (we now distil all of it). A quote shorter than 12 normalised
    # chars is too weak to trust as a locator. `grounded` is a per-claim audit flag,
    # not a drop reason — the caller/critic decides; grounded_ratio is the signal.
    text_norm = _norm(text)
    grounded = 0
    for c in claims:
        ev = _norm(c.get("evidence") or "")
        c["grounded"] = len(ev) >= 12 and ev in text_norm
        if c["grounded"]:
            grounded += 1
    grounded_ratio = round(grounded / len(claims), 3) if claims else 0.0
    if claims and grounded_ratio < 0.6:
        logger.warning("distill: low provenance grounding — %.0f%% of %d claims trace verbatim to source",
                       grounded_ratio * 100, len(claims))

    return {
        "claims": claims,
        "coverage_note": "  ·  ".join(coverage_notes),
        "truncated": False,       # chunking now covers the whole body — no drop
        "dropped_chars": 0,
        "grounded_ratio": grounded_ratio,
        "source_chars": len(text),
    }
