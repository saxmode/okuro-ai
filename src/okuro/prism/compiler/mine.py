# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism compiler STAGE 1 (mine) — resolve an okuro artifact id to
#   its body and extract typed, grounded DATA CLAIMS: each claim is {id, statement,
#   shape ∈ 11 kit shapes, per-shape structured fields, evidence span}. The memory
#   system (an artifact id) is the ONLY source. Schema-validated with bounded
#   re-ask (conditional per-shape field schema), then evidence-grounded verbatim.
# index: mine | build_mine_schema | _SYSTEM | _ground
# AGENT_HEADER_END -->
"""Stage 1 — mine: artifact id -> typed data claims.

Entry is an okuro artifact id (``prism_build_from_artifact`` path); the body is
fetched via ``okuro.sense.artifacts.artifact_get``. The LLM's whole job here is
STRUCTURING: turn prose into claims whose ``shape`` + ``fields`` are exactly what
the board kit's components consume downstream. Numbers may arrive as strings
("−92%", "18 ms") — the structure is the contract, not the scalar type.

The validation schema is CONDITIONAL: each claim's ``fields`` is validated against
``SHAPE_FIELD_SCHEMAS[shape]`` via draft-2020 if/then, so the bounded re-ask in
``llm.call_json`` corrects field-shape mismatches too, not just top-level shape.
After validation, every claim's evidence quote is grounded verbatim against the
fetched body (a per-claim audit flag, never a silent drop).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from okuro.prism.compiler import llm
from okuro.prism.text_chunk import chunk_body
from okuro.prism.compiler.ir import (
    CLAIM_SCHEMA,
    SHAPE_FIELD_SCHEMAS,
    SHAPES,
    Claim,
    Evidence,
    lenient_fields,
    tolerant,
)

logger = logging.getLogger(__name__)

_MAX_CHARS = 120_000  # per mine-call budget; larger bodies are CHUNKED, not sliced


def build_mine_schema() -> dict[str, Any]:
    """MINE_SCHEMA with per-shape conditional ``fields`` validation (if/then).

    Two layers of leniency so the bounded re-ask converges on real model output:
    (1) ``tolerant`` drops every ``additionalProperties: False`` (extra keys are
    ignored, not re-ask traps); (2) ``lenient_fields`` reduces each per-shape
    ``fields`` schema to "the shape's top-level keys are present, values opaque",
    so nested-collection type/required/bounds mismatches (bare-string items,
    checklist-array cells, over-long option lists) no longer fail validation.
    A genuine shape mislabel (missing the shape's core key) still fails and the
    error names the missing key for the re-ask.
    """
    claim = {k: v for k, v in CLAIM_SCHEMA.items()
             if k not in ("properties", "additionalProperties")}
    claim["properties"] = dict(CLAIM_SCHEMA["properties"])
    claim["allOf"] = [
        {
            "if": {"properties": {"shape": {"const": shape}}},
            "then": {"properties": {"fields": lenient_fields(SHAPE_FIELD_SCHEMAS[shape])}},
        }
        for shape in SHAPES
    ]
    schema = {
        "type": "object",
        "required": ["claims"],
        "properties": {
            "claims": {"type": "array", "minItems": 1, "items": claim},
            "coverage_note": {"type": "string"},
        },
    }
    return tolerant(schema)


_SYSTEM = f"""You are the MINE stage of the prism deck compiler. You convert a \
source document into a set of typed DATA CLAIMS. You do not summarize, you do not \
write prose, you do not style anything — you STRUCTURE facts.

For each meaningful claim in the document, emit one object:
- id: "c1", "c2", … (sequential)
- statement: one plain-language sentence stating the claim (this is the prose the \
deck will slice into shorter forms; keep it self-contained and specific)
- shape: the DATA SHAPE, exactly one of: {", ".join(SHAPES)}
- fields: the structured data for that shape (see the schema — different keys per \
shape). Put the real numbers/entities/steps here, never invented ones.
- evidence.quote: a VERBATIM span (>= 12 chars) copied from the source that supports \
the claim. Copy exactly; do not paraphrase.
- salience: 0..1, how load-bearing this claim is to the document's argument.

Shape guide:
- metric: one measured value (+ optional baseline)   - delta: before -> after
- comparison: options × criteria                     - sequence: ordered steps
- relationship: nodes + edges                        - proportion: parts of a whole
- quote: a verbatim quotation                        - set: peers / a collection
- trend: a value over an ordered series              - verdict: a ruling on a subject
- narrative: an assertion best carried as prose

Prefer a specific structured shape over `narrative` whenever the data supports it.
Return ONLY a JSON object: {{"claims": [...], "coverage_note": "..."}}."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _ground(claims: list[Claim], body: str) -> float:
    """Flag each claim grounded iff its evidence quote appears verbatim in body."""
    body_norm = _norm(body)
    grounded = 0
    for c in claims:
        ev = _norm(c.evidence.quote)
        c.grounded = len(ev) >= 12 and ev in body_norm
        grounded += int(c.grounded)
    return round(grounded / len(claims), 3) if claims else 0.0


def fetch_artifact_body(artifact_id: str) -> tuple[str, str]:
    """Return (title, body) for an okuro artifact id. Raises if missing/empty."""
    from okuro.sense.artifacts import artifact_get

    a = artifact_get(str(artifact_id), include_body=True) or {}
    body = (a.get("body") or "").strip()
    if not body:
        raise ValueError(f"artifact {artifact_id!r} not found or has empty body")
    return (a.get("title") or ""), body


def mine(
    artifact_id: str,
    *,
    provider: Optional[str] = None,
    invoke_fn=None,
) -> dict[str, Any]:
    """Extract typed data claims from an okuro artifact.

    Returns ``{"claims": [claim-dict…], "coverage_note", "grounded_ratio",
    "source_artifact_id", "source_title"}``. Raises on a missing/empty artifact
    or an irrecoverable model failure.
    """
    title, body = fetch_artifact_body(artifact_id)
    chunks = chunk_body(body, _MAX_CHARS)

    claims: list[Claim] = []
    coverage_notes: list[str] = []
    for idx, chunk in enumerate(chunks):
        part = f" (part {idx + 1} of {len(chunks)})" if len(chunks) > 1 else ""
        user = (
            f"SOURCE ARTIFACT (id={artifact_id}, title={title!r}){part}:\n\n{chunk}\n\n"
            "Extract the complete set of typed data claims. Return ONLY the JSON object."
        )
        try:
            data = llm.call_json(
                system_prompt=_SYSTEM, user_prompt=user, schema=build_mine_schema(),
                stage="mine", provider=provider, capability="standard",
                timeout=600, thinking_tokens=8000, invoke_fn=invoke_fn,
            )
        except Exception as exc:  # noqa: BLE001 — one bad chunk must not sink a large mine
            # A single chunk that never returns a valid response (exhausted re-asks)
            # used to abort the WHOLE deck. With chunked ingest that is one Nth of the
            # source — skip it, keep the rest, and let the deck build on what mined.
            logger.warning("mine: chunk %d/%d failed after retries — skipping it: %s",
                           idx + 1, len(chunks), exc)
            continue
        for c in data.get("claims", []):
            ev = c.get("evidence") or {}
            claims.append(Claim(
                id=c["id"], statement=c["statement"], shape=c["shape"],
                fields=c.get("fields") or {},
                evidence=Evidence(quote=(ev.get("quote") or "").strip(),
                                  artifact_id=str(artifact_id)),
                salience=float(c.get("salience", 0.5)),
            ))
        cn = (data.get("coverage_note") or "").strip()
        if cn:
            coverage_notes.append(cn)

    if not claims:
        raise ValueError(
            f"mine: no valid claims from any of {len(chunks)} chunk(s) for "
            f"artifact {artifact_id!r}")

    # Per-chunk mining restarts ids at c1, so without a global renumber two
    # chunks collide on the same id and downstream topic/outline refs cross-wire.
    for i, c in enumerate(claims, 1):
        c.id = f"c{i}"

    grounded_ratio = _ground(claims, body)
    if claims and grounded_ratio < 0.6:
        logger.warning("mine: low grounding — %.0f%% of %d claims trace verbatim",
                       grounded_ratio * 100, len(claims))

    return {
        "claims": [c.to_dict() for c in claims],
        "coverage_note": " · ".join(coverage_notes),
        "grounded_ratio": grounded_ratio,
        "truncated": False,  # chunking now covers the whole body — no silent drop
        "source_artifact_id": str(artifact_id),
        "source_title": title,
    }
