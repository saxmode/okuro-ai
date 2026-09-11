# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism workflow NODE 1 (understand) — read the WHOLE artifact and
#   comprehend it holistically BEFORE any recipient or topic exists. Two passes:
#   a per-chunk digest (sections + gists, so nothing is skimmed) and one synthesis
#   over every digest (kind, thesis, through-lines). Emits `Understanding`.
#   This is the top-down replacement for mine's claim atomization as the FIRST act.
# index: understand | _DIGEST_SYSTEM | _SYNTH_SYSTEM | _digest_chunk | _synthesize
# AGENT_HEADER_END -->
"""Node 1 — understand: artifact id -> holistic ``Understanding``.

The bottom-up compiler's first act was to shred the source into ~324 atomic
claims; everything after it reasoned over confetti and could no longer see the
document. This node inverts that: comprehend first, atomize never.

Two passes, always both:

1. **Digest** (once per chunk) — what each stretch of the document says, as
   sections with gists. Chunked so a long source is READ ENTIRELY rather than
   truncated; section ids are renumbered globally in code afterwards.
2. **Synthesize** (once) — over the merged section list: what kind of document
   this is, its thesis, the through-lines that recur across sections.

Pass 2 runs even for a single-chunk source. It is not redundant: the digest
answers "what does this part say", the synthesis answers "what is this document
and what is it arguing" — a different question, and the one the recipient and
topic-map nodes actually consume. Uniform path, no branch to drift.

Sections are merged in CODE and never re-emitted by pass 2, so the synthesis
model cannot quietly drop half of them to fit a comfortable answer length.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from okuro.prism.compiler import llm
from okuro.prism.compiler.mine import fetch_artifact_body
from okuro.prism.text_chunk import chunk_body
from okuro.prism.workflow.types import Section, Understanding

logger = logging.getLogger(__name__)

_MAX_CHARS = 120_000  # per-call budget; a longer body is CHUNKED, never truncated

# No `maxItems` anywhere below — section count follows the document, not a ceiling.
_DIGEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["sections"],
    "properties": {
        "sections": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["heading", "gist"],
                "properties": {
                    "heading": {"type": "string", "minLength": 1},
                    "gist": {"type": "string", "minLength": 1},
                    "covers": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "entities": {"type": "array", "items": {"type": "string"}},
    },
}

_SYNTH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["document_kind", "what_it_is", "thesis"],
    "properties": {
        "document_kind": {"type": "string", "minLength": 1},
        "what_it_is": {"type": "string", "minLength": 1},
        "thesis": {"type": "string", "minLength": 1},
        "domain": {"type": "string"},
        "through_lines": {"type": "array", "items": {"type": "string"}},
        "key_entities": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
}

_DIGEST_SYSTEM = """You are the UNDERSTAND node of the prism deck workflow, on its \
first pass. You are READING a document to comprehend it — you are not extracting \
data, not summarizing for an audience, and not writing any deck copy.

Break the text you are given into its natural SECTIONS — the units the author \
actually wrote in, not equal-sized slices. For each section give:
- heading: a short, specific name for what this stretch is about (use the source's \
own heading when it has one).
- gist: what this section actually SAYS, in 1-3 sentences. State the substance, \
not the topic. "Costs fell 92% after the migration" — not "discusses costs".
- covers: the concrete things this section deals with (entities, systems, numbers, \
decisions, people). Bare noun phrases.

Emit as many sections as the text genuinely contains. Do not merge unrelated \
material to keep the list short, and do not split one idea to pad it.
Return ONLY {"sections": [{"heading","gist","covers"}], "entities": [...]}."""

_SYNTH_SYSTEM = """You are the UNDERSTAND node of the prism deck workflow, on its \
synthesis pass. You are given the complete section map of one document — every \
section, in order, with what each one says. Step back and comprehend the WHOLE.

Answer, about the document as a single object:
- document_kind: what genre of document this is (reference manual, strategy memo, \
research report, spec, post-mortem, pitch, …).
- what_it_is: 1-2 sentences — what this document IS, holistically.
- thesis: the central argument or claim the whole document is making. If it is \
purely descriptive, say what it is fundamentally describing.
- domain: the subject field.
- through_lines: ideas that RECUR across several sections and hold the document \
together. These are the spine, not a list of topics.
- key_entities: the systems, people, products, or concepts the document is about.
- open_questions: what the document raises but does not settle. Empty if none.

Do NOT restate the sections — they are already captured. Reason about the whole.
Return ONLY the JSON object."""


def _digest_chunk(chunk: str, idx: int, total: int, title: str, artifact_id: str,
                  *, provider: Optional[str], invoke_fn) -> dict[str, Any]:
    part = f" (part {idx + 1} of {total})" if total > 1 else ""
    user = (
        f"SOURCE DOCUMENT (id={artifact_id}, title={title!r}){part}:\n\n{chunk}\n\n"
        "Break this into its natural sections and say what each one actually says. "
        "Return ONLY the JSON object."
    )
    return llm.call_json(
        system_prompt=_DIGEST_SYSTEM, user_prompt=user, schema=_DIGEST_SCHEMA,
        stage="wf.understand.digest", provider=provider, capability="standard",
        # 600s: the same full-payload budget mine/project/outline need — this node
        # scales with source size and a book-length chunk is not a 300s call.
        timeout=600, thinking_tokens=8000, invoke_fn=invoke_fn,
    )


def _synthesize(sections: list[Section], title: str, kind_hint: str,
                *, provider: Optional[str], invoke_fn) -> dict[str, Any]:
    view = [{"id": s.id, "heading": s.heading, "gist": s.gist} for s in sections]
    user = (
        f"DOCUMENT: {title!r}{kind_hint}\n"
        f"SECTION MAP ({len(view)} sections, in document order):\n"
        + llm.dumps(view) +
        "\n\nComprehend the document as a whole. Return ONLY the JSON object."
    )
    return llm.call_json(
        system_prompt=_SYNTH_SYSTEM, user_prompt=user, schema=_SYNTH_SCHEMA,
        stage="wf.understand.synth", provider=provider, capability="standard",
        timeout=600, thinking_tokens=8000, invoke_fn=invoke_fn,
    )


def understand(
    artifact_id: str,
    *,
    provider: Optional[str] = None,
    invoke_fn=None,
) -> Understanding:
    """Read an okuro artifact end to end and return holistic ``Understanding``.

    Raises on a missing/empty artifact — and on ANY chunk whose digest still
    fails after a retry pass. This node's contract is "read ENTIRELY, never
    truncated": a silently partial map is a WRONG map, not a degraded one — on
    the first live book-length run (task-20260727-005417) one lost chunk shipped
    a deck missing 45% of the source while claiming the source itself was
    truncated. Coverage is all-or-error, per the fail-loud doctrine.
    """
    title, body = fetch_artifact_body(artifact_id)
    chunks = chunk_body(body, _MAX_CHARS)

    per_chunk: dict[int, dict[str, Any]] = {}
    failed: dict[int, str] = {}
    for idx, chunk in enumerate(chunks):
        try:
            per_chunk[idx] = _digest_chunk(chunk, idx, len(chunks), title, artifact_id,
                                           provider=provider, invoke_fn=invoke_fn)
        except Exception as exc:  # noqa: BLE001 — retried below before anything raises
            failed[idx] = str(exc)
            logger.warning("understand: chunk %d/%d digest failed (will retry): %s",
                           idx + 1, len(chunks), exc)

    # Second pass: transient failures (transport drops, one-off timeouts) get one
    # clean retry after every other chunk has finished.
    for idx in list(failed):
        try:
            per_chunk[idx] = _digest_chunk(chunks[idx], idx, len(chunks), title,
                                           artifact_id, provider=provider,
                                           invoke_fn=invoke_fn)
            del failed[idx]
        except Exception as exc:  # noqa: BLE001 — now it is a real, reportable loss
            failed[idx] = str(exc)

    if failed:
        detail = "; ".join(f"chunk {i + 1}/{len(chunks)}: {e}" for i, e in sorted(failed.items()))
        raise ValueError(
            f"understand: could not read artifact {artifact_id!r} ENTIRELY — "
            f"{len(failed)} of {len(chunks)} chunk(s) failed after retry ({detail}). "
            "Refusing to emit a partial section map: a deck built from it would "
            "silently misrepresent the source.")

    sections: list[Section] = []
    entities: list[str] = []
    for idx in range(len(chunks)):
        data = per_chunk[idx]
        for s in data.get("sections") or []:
            sections.append(Section(
                id="",  # assigned globally below
                heading=(s.get("heading") or "").strip(),
                gist=(s.get("gist") or "").strip(),
                covers=[str(c).strip() for c in (s.get("covers") or []) if str(c).strip()],
            ))
        entities.extend(str(e).strip() for e in (data.get("entities") or []) if str(e).strip())

    if not sections:
        raise ValueError(
            f"understand: no sections from any of {len(chunks)} chunk(s) for "
            f"artifact {artifact_id!r}")

    # Global renumber: per-chunk numbering restarts at 1, so without this two
    # chunks collide on the same id and every downstream section_ref cross-wires.
    for i, s in enumerate(sections, 1):
        s.id = f"s{i}"

    synth = _synthesize(sections, title, "", provider=provider, invoke_fn=invoke_fn)

    # Union the digest-observed entities with the synthesis's, order-stable.
    seen: set[str] = set()
    merged_entities: list[str] = []
    for e in list(synth.get("key_entities") or []) + entities:
        k = e.lower()
        if k not in seen:
            seen.add(k)
            merged_entities.append(e)

    return Understanding(
        source_artifact_id=str(artifact_id),
        source_title=title,
        document_kind=(synth.get("document_kind") or "").strip(),
        what_it_is=(synth.get("what_it_is") or "").strip(),
        thesis=(synth.get("thesis") or "").strip(),
        domain=(synth.get("domain") or "").strip(),
        sections=sections,
        through_lines=[str(t).strip() for t in (synth.get("through_lines") or []) if str(t).strip()],
        key_entities=merged_entities,
        open_questions=[str(q).strip() for q in (synth.get("open_questions") or []) if str(q).strip()],
        word_count=len(body.split()),
        chunk_count=len(chunks),
    )
