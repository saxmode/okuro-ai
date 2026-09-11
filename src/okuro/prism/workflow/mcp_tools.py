# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: MCP surface for the top-down prism deck workflow — the PRIMITIVES an
#   agent loop calls, not a pipeline it is dragged through. Run-scoped: the agent
#   passes a small run_id, the heavy IR lives in the run's cache, and each tool
#   returns just what the next reasoning step needs.
# index: _run_dir | _manifest | _workflow | get_tools | handle_tool
# AGENT_HEADER_END -->
"""MCP tools for building a prism deck as a continuous agent loop.

The staged runner (``workflow/run.py``) walks the nodes in a fixed order and hands
each one only the previous node's serialized IR. That is a pipeline, and it severs
the reasoning thread at every boundary — the reader's profile stops being present
by the time components are chosen, which is exactly where it matters most.

These tools expose the SAME node functions so one agent can hold the thread instead:

    prism_deck_open      -> start/resume a run, see what is already done
    prism_deck_understand-> comprehend the whole artifact
    prism_deck_recipient -> load the reader and what they need from it
    prism_deck_topics    -> the recipient-relevant topic map
    prism_deck_gather    -> material for ONE topic
    prism_deck_author    -> ONE topic's four levels, written by job

State lives in the run's ``StageCache``, so a tool called twice is a cache hit and
a crashed loop resumes rather than restarts. The agent's context carries the
REASONING; the cache carries the data.

Component selection is deliberately NOT here. It is the agent's job, done by
reasoning over ``prism_library`` with the topic and reader still in context.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from mcp.types import TextContent, Tool
from okuro.db.engine import okuro_home

_ROOT = okuro_home() / "prism-workflow"


def _text(data: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _run_id(artifact_id: str, recipients: list[str], brand: str) -> str:
    key = json.dumps({"a": str(artifact_id), "r": sorted(recipients), "b": brand},
                     sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _run_dir(run_id: str) -> Path:
    return _ROOT / run_id


def _manifest(run_id: str) -> dict[str, Any]:
    p = _run_dir(run_id) / "run.json"
    if not p.exists():
        raise FileNotFoundError(
            f"no prism run {run_id!r} — call prism_deck_open first")
    return json.loads(p.read_text())


def _workflow(run_id: str):
    from okuro.prism.workflow.run import PrismWorkflow

    m = _manifest(run_id)
    return PrismWorkflow(
        m["artifact_id"], m["recipients"], brand=m.get("brand", ""),
        brand_pins=m.get("brand_pins") or {}, cache_dir=_run_dir(run_id),
    )


def _done(run_id: str) -> dict[str, bool]:
    """Which nodes already have a cached result for this run."""
    from okuro.prism.workflow.run import NODE_ORDER

    d = _run_dir(run_id)
    return {n: any(d.glob(f"{n}.*.json")) for n in NODE_ORDER}


def get_tools() -> list[Tool]:
    run_arg = {"run_id": {"type": "string", "description": "From prism_deck_open."}}
    return [
        Tool(
            name="prism_deck_open",
            description=(
                "Start or resume building a prism deck from an okuro artifact for named "
                "recipients. Returns a run_id plus which steps already have results "
                "(re-opening the same artifact+recipients resumes rather than restarts). "
                "Call this FIRST, then drive the build yourself: understand -> recipient "
                "-> topics -> per topic gather+author -> choose components by reasoning "
                "over prism_library -> assemble. You are the loop; these are your tools."
            ),
            inputSchema={
                "type": "object",
                "required": ["artifact_id", "recipients"],
                "properties": {
                    "artifact_id": {"type": "string", "description": "okuro artifact id — the source document."},
                    "recipients": {"type": "array", "items": {"type": "string"},
                                   "description": "Named readers, e.g. [\"Marco\"]."},
                    "brand": {"type": "string", "description": "Brand slug for styling (optional)."},
                    "brand_pins": {"type": "object", "description": "{recipient: brand_slug} — resolves a multi-hat reader."},
                },
            },
        ),
        Tool(
            name="prism_deck_understand",
            description=(
                "Read the WHOLE artifact and return a holistic understanding: what kind of "
                "document it is, its thesis, its section map with a gist per section, the "
                "through-lines, and open questions. Chunked internally, so a book-length "
                "source is read entirely, not truncated. Recipient-agnostic on purpose — "
                "load the reader separately so the same source can serve several audiences."
            ),
            inputSchema={"type": "object", "required": ["run_id"], "properties": dict(run_arg)},
        ),
        Tool(
            name="prism_deck_recipient",
            description=(
                "Resolve the reader from the okuro people graph (role, brand, cognitive "
                "lens through the PII firewall) and work out what THEY need from THIS "
                "document: what they care about, decisions they face, what to lead with, "
                "what to suppress, how they want it delivered. Errors with "
                "needs_disambiguation if a reader wears several brand hats — re-open the "
                "run with brand_pins rather than guessing."
            ),
            inputSchema={"type": "object", "required": ["run_id"], "properties": dict(run_arg)},
        ),
        Tool(
            name="prism_deck_topics",
            description=(
                "Return the topic map: the topics THIS reader needs from THIS document, "
                "each with the question it answers, why it earns their attention, and the "
                "sections it draws on — plus the arc joining them. No cap on topic count, "
                "and topics may share sections (one fact can serve several questions)."
            ),
            inputSchema={"type": "object", "required": ["run_id"], "properties": dict(run_arg)},
        ),
        Tool(
            name="prism_deck_gather",
            description=(
                "Pull the artifact material that serves ONE topic — facts with their data "
                "shape, real per-item (label, detail, meta) content, and a verbatim "
                "evidence span. Omit topic_id to gather every topic. A topic whose "
                "sections did not resolve is gathered from the full body, not skipped."
            ),
            inputSchema={
                "type": "object", "required": ["run_id"],
                "properties": {**run_arg,
                               "topic_id": {"type": "string", "description": "One topic; omit for all."}},
            },
        ),
        Tool(
            name="prism_deck_author",
            description=(
                "Write ONE topic's four depth levels BY JOB — L1 what it is + its key "
                "elements (hero-short), L2 explains those elements, L3 adds every relevant "
                "factor while staying readable, L4 full prose as a navigable book-entry. "
                "All levels always exist; thin material yields short levels, never missing "
                "ones. Returns blocks with shape and real item data and NO component — "
                "choosing components is YOUR job, by reasoning over prism_library with this "
                "topic and reader still in mind. Omit topic_id to author every topic."
            ),
            inputSchema={
                "type": "object", "required": ["run_id"],
                "properties": {**run_arg,
                               "topic_id": {"type": "string", "description": "One topic; omit for all."}},
            },
        ),
        Tool(
            name="prism_deck_assemble",
            description=(
                "SAVE the deck — the ONLY way a prism deck is persisted. Takes your "
                "per-block component choices (reasoned over prism_library), validates "
                "them against the typed solver boundary, runs the ladder gate + "
                "accuracy accounting, composes the 37-component kit, and lands the "
                "deck in the deck2 store at /prism/deck. A wrong component choice or "
                "a missing focal is REFUSED with a defect list — fix the choices and "
                "call again; never fall back to any other tool."
            ),
            inputSchema={
                "type": "object", "required": ["run_id", "title", "components"],
                "properties": {
                    **run_arg,
                    "title": {"type": "string", "description": "The deck title — this reader's entry point, not a filename."},
                    "subtext": {"type": "string", "description": "One-line hero subtext/tagline for this reader."},
                    "components": {
                        "type": "object",
                        "description": ("{topic_id: {L1|L2|L3: [component_id per block, "
                                        "in block order]}} — your reasoned choices."),
                    },
                    "family": {"type": "string", "enum": ["swiss", "japanese", "bauhaus", "editorial"],
                               "description": "Grid family for the whole deck. Default swiss."},
                    "mention_audience_members": {
                        "type": "boolean",
                        "description": (
                            "Default FALSE. Tailoring steers the deck SILENTLY — ordering, "
                            "emphasis, entry depth, component choice — and content never "
                            "names a reader, because naming one and getting their view "
                            "wrong is a reputational hazard. Set true ONLY when the deck's "
                            "owner has judged that naming this audience is safe; the build "
                            "then allows persona chips to carry real names."
                        ),
                    },
                },
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        return await _dispatch(name, arguments)
    except FileNotFoundError as e:
        return _text({"error": str(e)})
    except Exception as e:  # noqa: BLE001 — an MCP tool reports, it does not crash the server
        from okuro.prism.compiler.profile import NeedsDisambiguation

        if isinstance(e, NeedsDisambiguation):
            return _text({
                "error": "needs_disambiguation",
                "detail": str(e),
                "options": e.options,
                "fix": "call prism_deck_open again with brand_pins={recipient: brand_slug}",
            })
        return _text({"error": f"{type(e).__name__}: {e}"})


async def _dispatch(name: str, arguments: dict) -> list[TextContent]:
    if name == "prism_deck_open":
        artifact_id = str(arguments["artifact_id"]).strip()
        recipients = [str(r).strip() for r in (arguments.get("recipients") or []) if str(r).strip()]
        if not recipients:
            return _text({"error": "at least one recipient is required"})
        brand = (arguments.get("brand") or "").strip()
        rid = _run_id(artifact_id, recipients, brand)
        d = _run_dir(rid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "run.json").write_text(json.dumps({
            "run_id": rid, "artifact_id": artifact_id, "recipients": recipients,
            "brand": brand, "brand_pins": arguments.get("brand_pins") or {},
        }, indent=2))
        return _text({
            "run_id": rid, "artifact_id": artifact_id, "recipients": recipients,
            "done": _done(rid),
            "next": "prism_deck_understand — read the whole artifact first.",
        })

    run_id = str(arguments.get("run_id") or "").strip()
    if not run_id:
        return _text({"error": "run_id is required — call prism_deck_open first"})
    wf = _workflow(run_id)

    if name == "prism_deck_understand":
        u = wf.node_understand()
        return _text({"run_id": run_id, "understanding": u.to_dict(),
                      "next": "prism_deck_recipient"})

    if name == "prism_deck_recipient":
        u = wf.node_understand()
        b = wf.node_recipient(u)
        return _text({"run_id": run_id, "recipient": b.to_dict(),
                      "next": "prism_deck_topics"})

    if name == "prism_deck_topics":
        u = wf.node_understand()
        b = wf.node_recipient(u)
        t = wf.node_topicmap(u, b)
        return _text({"run_id": run_id, "topic_map": t.to_dict(),
                      "next": "prism_deck_gather (per topic, or all at once)"})

    if name in ("prism_deck_gather", "prism_deck_author"):
        u = wf.node_understand()
        b = wf.node_recipient(u)
        tmap = wf.node_topicmap(u, b)
        wanted = (arguments.get("topic_id") or "").strip()
        if wanted and wanted not in {t.id for t in tmap.topics}:
            return _text({"error": f"unknown topic_id {wanted!r}",
                          "topic_ids": [t.id for t in tmap.topics]})
        gatherings = wf.node_gather(u, b, tmap)

        if name == "prism_deck_gather":
            sel = {k: v.to_dict() for k, v in gatherings.items()
                   if not wanted or k == wanted}
            return _text({"run_id": run_id, "gatherings": sel,
                          "next": "prism_deck_author"})

        authored = wf.node_author(tmap, gatherings, b)
        sel = [t.to_dict() for t in authored if not wanted or t.topic_id == wanted]
        return _text({
            "run_id": run_id, "authored": sel,
            # The component choice happens between this call and assemble, and
            # it is made by an agent — so the reader envelope has to be IN this
            # return or it does not reach the chooser. prefer_modules /
            # avoid_modules are the output of the whole 14-axis affinity
            # matrix and had zero consumers on this path: computed, threaded,
            # then dropped. Nested inside `audience` is not reaching it.
            "delivery_envelope": b.project_for_prompt(),
            "next": ("Choose components per level by reasoning over prism_library "
                     "— the blocks carry shape + real item data, no component. "
                     "Honour delivery_envelope: prefer_modules first, "
                     "avoid_modules not at all, and do not exceed depth_ceiling. "
                     "Then prism_deck_assemble to save."),
        })

    if name == "prism_deck_assemble":
        from okuro.prism.workflow.assemble import AssembleDefects, assemble_deck

        m = _manifest(run_id)
        u = wf.node_understand()
        b = wf.node_recipient(u)
        tmap = wf.node_topicmap(u, b)
        gatherings = wf.node_gather(u, b, tmap)
        authored = wf.node_author(tmap, gatherings, b)
        try:
            result = assemble_deck(
                authored,
                arguments.get("components") or {},
                title=str(arguments.get("title") or "").strip(),
                subtext=str(arguments.get("subtext") or "").strip(),
                brand=(m.get("brand") or "okuro"),
                # "" lets the reader decide it (workflow/layout.derive_family);
                # an explicit choice still wins.
                family=str(arguments.get("family") or ""),
                artifact_id=m.get("artifact_id") or "",
                # The resolved reader is already in hand one line up — it used to
                # be dropped here, which is how a deck reached the right store
                # with the recipient removed.
                brief=b,
                mention_audience_members=bool(
                    arguments.get("mention_audience_members") or False),
                # Gather ran behind the cache; its quality signals (defaulted
                # shapes, low evidence grounding) only reach the deck if the
                # gatherings come with it.
                gatherings=gatherings,
            )
        except AssembleDefects as ad:
            return _text({"run_id": run_id, "saved": False, "defects": ad.defects,
                          "delivery_envelope": b.project_for_prompt(),
                          "fix": "resolve every defect (re-choose components via "
                                 "prism_library, honouring delivery_envelope) and "
                                 "call prism_deck_assemble again"})
        return _text({"run_id": run_id, "saved": True, **result})

    return _text({"error": f"unknown tool: {name}"})
