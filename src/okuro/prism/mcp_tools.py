# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism MCP tools — let agents/roles build, read, and edit
#   facet-tree docs that render live at /prism.
# index: imports | OPS_DOC | def get_tools | async def handle_tool
# AGENT_HEADER_END -->
"""okuro·prism MCP tools — ``prism_*``.

The canonical edit surface for autonomous agents/roles (generate, review-edit,
retailor). A save appends to the change-feed, so an open /prism canvas
live-updates. Mirrors okuro-slides' MCP shape exactly (tool names/semantics
layer); ``retailor_facets`` is prism's analog of ``retailor_deck``.
"""

import json

from mcp.types import TextContent, Tool

_ORIGIN = "agent"

#: SEALED 2026-07-27 — the legacy facet engine's write verbs. They are absent
#: from ``get_tools()`` (so they never reach a tool list) AND hard-error in
#: ``handle_tool`` (so a cached surface cannot call them either). Exported as a
#: constant so guards derive the seal from code instead of restating it —
#: adding a verb here automatically reddens any role catalog that teaches it.
SEALED_TOOLS: frozenset[str] = frozenset({
    "prism_generate", "prism_edit", "prism_apply_ops",
    "prism_retailor", "prism_add_visualizer",
})

OPS_DOC = (
    "OPS = list of: "
    '{"op":"set_meta","title?":str,"entry_facet_id?":str} | '
    '{"op":"add_facet","parent_id":str,"facet":{"title":str,"kind?":"topic"|"diagram"|"decision"|"metric",'
    '"rungs?":{"L1|L2|L3|L4":{"body":str}}}} | '
    '{"op":"remove_facet","facet_id":str} | '
    '{"op":"move_facet","facet_id":str,"new_parent_id":str} | '
    '{"op":"set_rung","facet_id":str,"rung":"L1"|"L2"|"L3"|"L4","body":str,"media?":[{"kind":"mermaid","spec":str}],"callouts?":[{"type":"risk"|"decision"|"wow","text":str}],"blocks?":[RichBlock...]} | '
    '{"op":"set_title","facet_id":str,"title":str} | '
    '{"op":"reorder_children","parent_id?":str,"order":[facet_id...]} | '
    '{"op":"set_block","facet_id":str,"rung":str,"index":int,"block":RichBlock} | '
    '{"op":"move_block","facet_id":str,"rung":str,"from":int,"to":int} | '
    '{"op":"remove_block","facet_id":str,"rung":str,"index":int} | '
    '{"op":"set_layout","facet_id":str,"rung":str,"layout":obj|null}. '
    "A Facet carries ALL 4 levels (L1/L2/L3/L4) — the ladder is "
    "the contract, not optional enrichment. add_facet on an EMPTY doc creates "
    "the root (parent_id omitted/ignored); every subsequent add_facet requires "
    "an existing parent_id. BLOCK/ORDER edits: a rung's visual modules live in "
    "its `blocks` list (a RichBlock = {type, ...} per the module catalog) — use "
    "set_block/move_block/remove_block to change one module by INDEX, set_rung "
    "blocks to replace the whole list, reorder_children to change SECTION order, "
    "set_title to rename, set_layout to pin a manual arrangement (null = auto)."
)


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="prism_list",
            description="ARCHIVE (legacy facet docs, read-only): list okuro·prism docs (id, title, brand_id, facet_count, timestamps). New decks live in the deck2 store at /prism/deck — build them with the prism-deck workflow.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="prism_get",
            description="ARCHIVE (legacy facet docs, read-only): get one doc by id, including its full facet tree (all 4 levels).",
            inputSchema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        ),
        Tool(
            name="prism_build_from_artifact",
            description=(
                "Run the TARGET pipeline: TRANSFORM a provided root document (a "
                "research report / analysis / session output) into a faithful, "
                "recipient-fit, depth-laddered, critic-gated deck — the deck is "
                "built ONLY from the source, nothing invented. Use this (not "
                "prism_generate) when you HAVE the content and need it presented. "
                "Async — returns a job_id immediately; the ~15-25min run streams to "
                "the job registry and lands at /prism/deck on completion. NOTE: this "
                "is the COMPILER path; the recipient-driven build is the "
                "prism_deck_* workflow."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "the recipient/group the deck is for, e.g. 'the board deciding whether to fund a rebuild'."},
                    "source_text": {"type": "string", "description": "the root document text (or pass artifact_id / note_id)."},
                    "artifact_id": {"type": "string", "description": "load the root from an okuro artifact."},
                    "note_id": {"type": "string", "description": "load the root from an okuro note."},
                    "title": {"type": "string"},
                    "brand_id": {"type": "string"},
                    # target_group_id / person_id are GONE, not hidden. Both were
                    # declared here and read by NOTHING in the handler — a promise
                    # the tool could not keep. target_group_id's own description
                    # advertised per-seat lens tabs that retell a point "for each
                    # board member", which is precisely the attribution D-P1
                    # forbids, so restoring it would need a decision, not a wiring
                    # fix. Recipient-driven building is the prism_deck_* workflow.
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="sparring_start",
            description=(
                "Open a STATEFUL multi-turn sparring session on a topic/decision — "
                "the prism sparring partner that REMEMBERS. Unlike a one-shot deck, "
                "turns accumulate (challenges, assumptions, decisions, tripwires) "
                "and durable facts persist to the knowledge graph. If prior "
                "sessions sparred this same topic, the session opens with a RECALL "
                "turn surfacing what was assumed / decided / killed — including "
                "assumptions that did NOT hold. Returns the session id + state."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "person_id": {"type": "string", "description": "okuro person this session spars with (frames the adversary panel)."},
                    "brand_id": {"type": "string"},
                },
                "required": ["topic"],
            },
        ),
        Tool(
            name="sparring_move",
            description=(
                "Advance a sparring session by one MOVE. Move types: "
                "'challenge' {content} — raise an objection; "
                "'panel' {audience_hint?} — run the multi-model red-team, landing "
                "each objection as a challenge turn; "
                "'assumption' {content} — surface a load-bearing assumption "
                "(also asserted to the KG for cross-session recall); "
                "'decision' {content} — record a decision (asserted to the KG); "
                "'tripwire' {content, trigger?} — set a revisit condition; "
                "'note' {content} — free annotation; "
                "'verdict' {ref, verdict} — resolve turn #ref "
                "(held|conceded|killed|falsified). Falsifying an assumption "
                "invalidates its KG triple so a later session recalls it as "
                "'did not hold'. Returns the updated session state."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["challenge", "panel", "assumption", "decision", "tripwire", "note", "verdict"],
                    },
                    "content": {"type": "string"},
                    "audience_hint": {"type": "string", "description": "panel move — who the position is pitched to."},
                    "trigger": {"type": "string", "description": "tripwire move — the condition that fires the revisit."},
                    "ref": {"type": "integer", "description": "verdict move — the turn number being resolved."},
                    "verdict": {"type": "string", "enum": ["held", "conceded", "killed", "falsified"]},
                    "source": {"type": "string"},
                },
                "required": ["session_id", "type"],
            },
        ),
        Tool(
            name="sparring_state",
            description=(
                "Get a sparring session's full turn log + the derived live board "
                "(open challenges, standing assumptions, falsified assumptions, "
                "decisions, tripwires). Pass full=true for every turn; default "
                "returns the derived state + summary."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "full": {"type": "boolean", "description": "true → include the complete turn log."},
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="sparring_list",
            description="List sparring sessions (id, topic, person, status, turn_count). Optional topic to filter by topic.",
            inputSchema={
                "type": "object",
                "properties": {"topic": {"type": "string", "description": "filter to sessions on this topic (matched by slug)."}},
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    from okuro.prism import get_doc, list_docs

    try:
        if name == "prism_list":
            docs = list_docs()
            result = {"docs": [d.to_summary() for d in docs], "count": len(docs)}

        elif name == "prism_get":
            doc = get_doc(arguments["id"])
            result = doc.to_detail() if doc else {"error": f"doc '{arguments['id']}' not found"}

        elif name in SEALED_TOOLS:
            # SEALED 2026-07-27: the legacy facet engine is write-closed for agents.
            # Its tools produced the wrong-engine deck on the first drawn-workflow
            # run (task-20260727-005417). One build path only — no fallback.
            result = {"error": (
                f"'{name}' is sealed — the legacy facet engine (/prism) is read-only. "
                "Build decks with the prism-deck workflow: prism_deck_open -> "
                "understand -> recipient -> topics -> gather -> author -> choose "
                "components via prism_library -> prism_deck_assemble. Decks land in "
                "the deck2 store and render at /prism/deck."
            )}

        elif name == "prism_build_from_artifact":
            # PRISM v4 W5: the ARTIFACT-ENTRY path now compiles a deck2 DeckDoc via
            # the authoring engine (resolution ladder + gate + accuracy) and lands it
            # in the deck2 store at /prism/deck — NOT the legacy facet-tree build.
            # Artifact id resolves CROSS-PROJECT (artifact_get is keyed by id alone,
            # via the compiler mine stage). person×brand ambiguity surfaces as a
            # job failure carrying the options — the pipeline never guesses a brand.
            import threading

            from okuro.prism import jobs

            target = (arguments.get("target") or "").strip()
            artifact_id = (arguments.get("artifact_id") or "").strip()
            if not artifact_id:
                raise ValueError("artifact_id required (the deck2 authoring entry compiles "
                                 "from an okuro artifact id)")
            if not target:
                raise ValueError("target required (the audience — a person name or an "
                                 "archetype like 'board' / 'technical')")

            title = (arguments.get("title") or "").strip() or None
            brand_id = (arguments.get("brand_id") or "").strip()
            job_id = jobs.create_job(title or target, brand_id=brand_id or None)

            def _run(_aid=artifact_id, _target=target, _title=title, _brand=brand_id, _jid=job_id) -> None:
                from okuro.prism.compiler.deck_store import save_deck
                from okuro.prism.compiler.pipeline import PrismCompiler
                from okuro.prism.compiler.profile import NeedsDisambiguation
                try:
                    jobs.set_phase(_jid, "compiling")
                    doc = PrismCompiler(_aid, [_target], brand=_brand).compile_authored_deck_doc()
                    doc["source"] = {"artifact_id": _aid, "recipients": [_target],
                                     "brand": _brand or None}
                    if _title:
                        doc["title"] = _title
                    deck_id = save_deck(doc)
                    jobs.complete_job(_jid, doc_id=deck_id, title=doc.get("title") or _target)
                except NeedsDisambiguation as nd:
                    jobs.fail_job(_jid, "person×brand ambiguous — specify a brand. options: "
                                  + str(getattr(nd, "args", nd)))
                except Exception as exc:  # noqa: BLE001 — surface to the poller
                    jobs.fail_job(_jid, str(exc))

            threading.Thread(target=_run, name=f"prism-build-{job_id}", daemon=True).start()
            result = {"success": True, "job_id": job_id, "url": "/prism/deck",
                      "note": "deck2 authoring build running; lands at /prism/deck on completion "
                              "(fetch /api/prism/deck2/<artifact_id>). Poll prism jobs for phase."}

        elif name == "sparring_start":
            from okuro.prism import sparring
            s = sparring.start_session(
                arguments["topic"],
                person_id=(arguments.get("person_id") or None),
                brand_id=(arguments.get("brand_id") or None),
                origin=_ORIGIN,
            )
            result = {"success": True, "session": s.to_state()}

        elif name == "sparring_move":
            from okuro.prism import sparring
            move = {k: v for k, v in arguments.items() if k != "session_id"}
            s = sparring.advance(arguments["session_id"], move, origin=_ORIGIN)
            result = {"success": True, "session": s.to_state()}

        elif name == "sparring_state":
            from okuro.prism import sparring
            s = sparring.get_session(arguments["session_id"])
            if s is None:
                result = {"error": f"session '{arguments['session_id']}' not found"}
            elif arguments.get("full"):
                result = {"success": True, "session": {**s.to_detail(), "state": sparring.session_state(s.turns)}}
            else:
                result = {"success": True, "session": s.to_state()}

        elif name == "sparring_list":
            from okuro.prism import sparring
            topic = (arguments.get("topic") or "").strip()
            key = sparring.slugify(topic) if topic else None
            sessions = sparring.list_sessions(topic_key=key)
            result = {"sessions": [x.to_summary() for x in sessions], "count": len(sessions)}

        else:
            result = {"error": f"unknown tool '{name}'"}

    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)}

    return [TextContent(type="text", text=json.dumps(result, default=str))]
