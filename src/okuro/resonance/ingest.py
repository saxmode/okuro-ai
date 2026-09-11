# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Resonance INTAKE — land a document in the okuro brain with provenance.
#   Stores the doc as an evidence artifact (what gather_context surfaces) and
#   extracts atomic claims into the KG with source_artifact_id + confidence.
# index:
#   def _extract_json_span
#   def _extract_claims
#   def ingest_document
# AGENT_HEADER_END -->
"""Resonance intake: documents → brain, with a provenance spine.

The first stage of the Resonance compiler. A document must enter the brain in
a form the downstream stages can (a) retrieve and (b) trust:

- **Retrievable** — stored as an ``artifact(kind="evidence")``. That is exactly
  what ``sense.grounding.gather_context`` surfaces (semantic search over
  artifacts), so the PCO builder and every generator can ground on it.
- **Trustable** — atomic claims are extracted into the KG as triples carrying
  ``source_artifact_id`` + ``confidence``. This is the provenance spine the gap
  engine and PCO builder read: every fact traces back to the document it came
  from.

Claim extraction is best-effort — a missing provider or malformed JSON degrades
to "artifact stored, 0 claims" rather than failing the ingest.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger("okuro.resonance.ingest")

_CLAIM_SYSTEM = (
    "You extract atomic, verifiable claims from a document for a knowledge "
    "graph. Return ONLY a JSON array (no prose). Each item: "
    '{"subject": str, "predicate": str, "object": str, "confidence": float}. '
    "confidence 0.0-1.0 = how strongly the DOCUMENT asserts it (hedged -> lower). "
    "Keep subject/object short noun phrases; predicate a short verb phrase. "
    "Extract 3-12 of the most load-bearing claims. Never invent facts not in "
    "the text."
)


def _extract_json_span(text: str, open_ch: str, close_ch: str) -> str:
    """Return the substring from the first ``open_ch`` to its matching close."""
    start = text.find(open_ch)
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def _extract_claims(text: str, provider: Optional[str]) -> list[dict[str, Any]]:
    """LLM → list of {subject, predicate, object, confidence}. Best-effort."""
    from okuro.bridge.invoke import invoke

    res = invoke(prompt=text[:12000], system_prompt=_CLAIM_SYSTEM,
                 capability="standard", provider=provider, timeout=120)
    if not (isinstance(res, dict) and res.get("success")):
        log.info("claim extraction failed: %s", (res or {}).get("error"))
        return []
    raw = _extract_json_span(res.get("output") or "", "[", "]")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        try:
            import json_repair
            data = json_repair.loads(raw)
        except Exception:
            log.info("claim JSON unparseable")
            return []
    out = []
    for c in data if isinstance(data, list) else []:
        if isinstance(c, dict) and c.get("subject") and c.get("object"):
            out.append({
                "subject": str(c["subject"])[:200],
                "predicate": str(c.get("predicate") or "relates to")[:100],
                "object": str(c["object"])[:400],
                "confidence": float(c.get("confidence", 0.7)),
            })
    return out


def ingest_document(
    text: str,
    *,
    title: str,
    source_ref: Optional[str] = None,
    project: Optional[str] = None,
    extract_claims: bool = True,
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Land a document in the brain: evidence artifact + KG provenance spine.

    Args:
        text: the document body (already extracted to plain text).
        title: human title for the evidence artifact.
        source_ref: origin (filename / URL) — recorded in the body header and
            carried as the claim provenance label.
        project: scope for the artifact + KG triples.
        extract_claims: run the LLM claim extractor into the KG.

    Returns ``{artifact_id, source_ref, claims_added, claims}``.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty document")

    from okuro.sense.artifacts import artifact_write

    header = f"SOURCE: {source_ref}\n\n" if source_ref else ""
    aid = artifact_write(
        kind="evidence",
        title=title,
        summary=(text[:200] + ("…" if len(text) > 200 else "")),
        body=header + text,
        project=project,
        created_by="resonance.ingest",
        confidence=0.9,
    )

    claims: list[dict[str, Any]] = []
    if extract_claims:
        from okuro.sense.kg import kg_add
        claims = _extract_claims(text, provider)
        for c in claims:
            try:
                kg_add(subject=c["subject"], predicate=c["predicate"],
                       object=c["object"], project=project,
                       source_artifact_id=aid, confidence=c["confidence"])
            except Exception as exc:  # noqa: BLE001
                log.info("kg_add failed for claim: %s", exc)

    return {"artifact_id": aid, "source_ref": source_ref,
            "claims_added": len(claims), "claims": claims}
