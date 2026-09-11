# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: /api/prism router — CRUD + change-feed + generate/edit/retailor for
#   okuro·prism facet-tree docs. Thin HTTP surface mirroring /api/slides.
# index: imports | router | list | events | variants | get | generate | edit | apply-ops | retailor | save | delete
# AGENT_HEADER_END -->
"""okuro·prism API — facet-tree doc CRUD + change-feed + generation surface.

Thin HTTP surface over ``okuro.prism`` (storage/generate/retailor/ops). Auth is
the global bearer middleware (same as every other /api router). Mirrors
``okuro.orchestrator.api.slides`` deliberately — same shapes, same endpoint
naming — so the frontend client (``lib/prism-api.ts``) reads like a sibling of
``lib/slides-api.ts``.

``list_variants`` has no equivalent in ``okuro.prism.retailor`` (unlike
slides, where variant lineage lives inside the JSON blob) — prism's
``variant_of`` is a first-class FK column (migration 073), so it is resolved
here directly against ``prism_docs`` rather than added to the prism package.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response

logger = logging.getLogger("okuro.orchestrator.api.prism")

router = APIRouter(prefix="/api/prism", tags=["prism"])

# Placeholder the export template carries; the payload JSON is substituted in.
_EXPORT_PLACEHOLDER = "/*__PRISM_PAYLOAD__*/ null"


def _seal_engine_a(route: str) -> None:
    """Refuse an engine-A DECK BUILD over HTTP while ``prism.strict`` is on.

    The MCP seal (``prism.mcp_tools.SEALED_TOOLS``) closed the agent's tool door
    into the legacy facet engine after it produced the wrong-engine deck
    (task-20260727-005417). These two POST routes are the SAME door over HTTP:
    they are the only ones that build a NEW legacy deck from scratch. The
    remaining engine-A routes (edit / apply-ops / pick-alt / brand / save /
    delete / retailor / alternatives) mutate an EXISTING legacy doc and cannot
    misroute a build, so they stay open for the /prism gallery.

    ``prism.strict: false`` degrades this to a logged warning and lets the build
    through — the documented escape hatch, not a silent fallback.
    """
    from okuro.prism.config import PrismStrictError, warn_or_fail

    try:
        warn_or_fail(
            "engine_a_http_build",
            f"POST {route} builds a deck in the legacy facet engine (/prism), which is "
            "sealed. Build decks with the prism-deck workflow: prism_deck_open -> "
            "understand -> recipient -> topics -> gather -> author -> choose components "
            "via prism_library -> prism_deck_assemble. Decks land in the deck2 store and "
            "render at /prism/deck. Set prism.strict: false in ~/.okuro/config.yaml to "
            "override.",
            route=route,
        )
    except PrismStrictError as exc:
        raise HTTPException(409, str(exc)) from exc


def _export_template_path():
    """Absolute path to the pre-built single-file export template.

    Emitted by `pnpm build:export` into web/export-dist/export.html (kept out of
    web/dist so the main SPA build's emptyOutDir never wipes it)."""
    from okuro.web.app import DIST_DIR

    return DIST_DIR.parent / "export-dist" / "export.html"


def _inline_logos(brand_id: str) -> list:
    """Brand logo assets with their bytes inlined as data: URIs so the exported
    file needs no network. A logo that can't be read is skipped, not fatal."""
    import base64
    from pathlib import Path

    from okuro.brand_assets import list_assets

    out: list = []
    for asset in list_assets(brand_id, "logo"):
        summary = asset.to_summary()
        try:
            raw = Path(asset.path).read_bytes()
            summary["url"] = f"data:{asset.mime};base64," + base64.b64encode(raw).decode("ascii")
        except Exception:  # noqa: BLE001 — a broken logo just drops out of the deck
            continue
        out.append(summary)
    return out


@router.get("/{doc_id}/export")
def export_prism_doc(doc_id: str) -> Response:
    """Render a deck as a single self-contained HTML file (attachment download).

    Bakes the doc + resolved brand theme + logo bytes (data: URIs) into the
    pre-built single-file viewer template, so the result opens offline anywhere
    with full rung navigation. This is the sharing MVP (no server, no login)."""
    import json
    import re

    from okuro.prism import get_doc

    doc = get_doc(doc_id)
    if doc is None:
        raise HTTPException(404, f"doc '{doc_id}' not found")

    detail = doc.to_detail()

    brand = None
    logos: list = []
    corner = "tl"
    brand_id = (doc.brand_id or "").strip()
    if brand_id:
        try:
            from okuro.stack import resolve_brand

            brand = resolve_brand(brand_id)
        except Exception:  # noqa: BLE001 — an unresolvable brand degrades to no theme
            brand = None
        corner = (detail.get("doc", {}).get("logo_corner")
                  or (brand or {}).get("logo_corner")
                  or "tl")
        logos = _inline_logos(brand_id)

    template_path = _export_template_path()
    try:
        template = template_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(
            503,
            "export template missing — run `pnpm build:export` in web/frontend",
        )

    payload = {"doc": detail, "brand": brand, "logos": logos, "corner": corner}
    # Escape "<" so the JSON can never break out of the <script> or be parsed as
    # HTML; json.dumps already \u-escapes non-ASCII (ensure_ascii default).
    payload_js = json.dumps(payload).replace("<", "\\u003c")
    html = template.replace(_EXPORT_PLACEHOLDER, payload_js)

    title = (detail.get("title") or "prism").strip() or "prism"
    safe = re.sub(r"[^A-Za-z0-9._ -]", "", title)[:80].strip() or "prism"
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}.html"'},
    )


@router.get("")
def list_prism_docs() -> dict:
    from okuro.prism import latest_seq, list_docs

    return {"docs": [d.to_summary() for d in list_docs()], "seq": latest_seq()}


@router.get("/events")
def prism_events(since: int = 0) -> dict:
    """Change-feed tail — rows with seq > ``since`` (oldest first) + the latest
    seq, so an open doc can poll for edits made by any process."""
    from okuro.prism import events_since, latest_seq

    return {"events": events_since(since), "seq": latest_seq()}


@router.get("/variants")
def prism_variants(source: str) -> dict:
    """List a doc's audience variants. ``source`` may be the source doc id OR
    any variant id (resolved to its source via the ``variant_of`` FK)."""
    from okuro.prism import get_doc, list_docs

    src_id = (source or "").strip()
    if not src_id:
        raise HTTPException(400, "source required")

    src = get_doc(src_id)
    root_id = src.variant_of if (src and src.variant_of) else src_id
    root = get_doc(root_id)
    variants = [d.to_summary() for d in list_docs() if d.variant_of == root_id]
    return {"source": root.to_summary() if root else None, "variants": variants, "source_id": root_id}


@router.get("/entry-rung")
def prism_entry_rung(person_id: str = "") -> dict:
    """Resolve the DEFAULT entry rung for a person from their cognitive profile
    (density+jargon sliders) — powers the viewer's live "view as persona" depth
    switch: one deck, any reader lands at their depth, no regeneration. Empty or
    unknown person → "brief" (the safe default). Same heuristic the generator
    bakes in via ``resolve_depth_default`` — kept server-side as one source."""
    from okuro.prism.generate import resolve_depth_default

    pid = (person_id or "").strip() or None
    return {"person_id": pid, "rung": resolve_depth_default(pid)}


# ── deck2 (kit-first DeckDoc) surface — the A4 viewer contract ────────────────
# Registered BEFORE the /{doc_id} catch-all so "deck2" is never swallowed as a
# facet-tree doc id. deck2 is a distinct data model (compiler ComposedDeck →
# adapter → DeckDoc) with its own flat JSON store (okuro.prism.compiler.deck_store).


@router.get("/deck2")
def list_deck2() -> dict:
    """Index of compiled deck2 DeckDocs (id + title + brand)."""
    from okuro.prism.compiler.deck_store import list_decks

    return {"decks": list_decks()}


@router.get("/deck2/{deck_id}")
def get_deck2(deck_id: str) -> dict:
    """Fetch a compiled DeckDoc by id (the /prism/deck?id= viewer fetch)."""
    from okuro.prism.compiler.deck_store import get_deck

    doc = get_deck(deck_id)
    if doc is None:
        raise HTTPException(404, f"deck '{deck_id}' not found")
    return doc


@router.post("/deck2/{deck_id}/pick")
def pick_deck2(deck_id: str, payload: dict) -> dict:
    """Persist an A/B pick on a cell. Body: {row: 'hero'|'L0'|'L1'|'L2', col:int,
    pick:int}. Returns the updated DeckDoc."""
    from okuro.prism.compiler.deck_store import apply_pick

    row = (payload.get("row") or "").strip()
    col = int(payload.get("col") or 0)
    pick = int(payload.get("pick") or 0)
    doc = apply_pick(deck_id, row, col, pick)
    if doc is None:
        raise HTTPException(404, f"deck '{deck_id}' not found")
    return doc


@router.post("/deck2/compile")
def compile_deck2(payload: dict) -> dict:
    """Compile a kit-first DeckDoc from an okuro artifact for named recipients and
    store it. Body: {artifact_id, recipients:[name], brand?, brand_pins?}. Async —
    returns a ``job_id``; poll /generate/jobs. On success the DeckDoc lands in the
    deck2 store under the artifact id (fetch via /deck2/{id})."""
    import threading

    from okuro.prism import jobs

    artifact_id = (payload.get("artifact_id") or "").strip()
    if not artifact_id:
        raise HTTPException(400, "artifact_id required")
    recipients = [r for r in (payload.get("recipients") or []) if str(r).strip()]
    if not recipients:
        raise HTTPException(400, "at least one recipient required")
    brand = (payload.get("brand") or "").strip()
    brand_pins = payload.get("brand_pins") if isinstance(payload.get("brand_pins"), dict) else None
    job_id = jobs.create_job(f"deck2:{artifact_id}", brand_id=brand or None)

    def _run() -> None:
        from okuro.prism.compiler.deck_store import save_deck
        from okuro.prism.compiler.pipeline import PrismCompiler

        try:
            jobs.set_phase(job_id, "compiling")
            # v4 W5: compile through the authoring engine (resolution ladder + gate
            # + accuracy), not the legacy plan/compose tail.
            doc = PrismCompiler(artifact_id, recipients, brand=brand,
                                brand_pins=brand_pins).compile_authored_deck_doc()
            # Record the source so the viewer's Retailor control can re-run the
            # compile for a different audience (entry-point reuse; W5 owns the
            # full audience-rewrite). Extra key — ignored by the A4 renderer.
            doc["source"] = {"artifact_id": artifact_id, "recipients": recipients,
                             "brand": brand or None}
            deck_id = save_deck(doc)
            jobs.complete_job(job_id, doc_id=deck_id, title=doc.get("title") or artifact_id)
        except Exception as exc:  # noqa: BLE001 — surface to the poller, never crash the thread
            logger.exception("prism deck2 compile job %s failed", job_id)
            jobs.fail_job(job_id, str(exc))

    threading.Thread(target=_run, name=f"prism-deck2-{job_id}", daemon=True).start()
    return {"job_id": job_id}


@router.post("/deck2/{deck_id}/brand")
def set_deck2_brand(deck_id: str, payload: dict) -> dict:
    """Live restyle: switch a deck's brand (VISUALS only — no regeneration). The
    voice follows the audience; retailor to change wording. Body: {brand}."""
    from okuro.prism.compiler.deck_store import set_brand

    try:
        doc = set_brand(deck_id, str(payload.get("brand") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if doc is None:
        raise HTTPException(404, f"deck '{deck_id}' not found")
    return doc


@router.post("/deck2/{deck_id}/retailor")
def retailor_deck2(deck_id: str, payload: dict) -> dict:
    """Re-tailor a deck to another audience by re-running the compile pipeline for
    a new recipient (entry-point reuse of the EXISTING compile path — the full
    audience-rewrite is W5). Requires the deck to carry `source` (set at compile).
    Body: {recipient, brand?}. Async — returns a job_id."""
    import threading

    from okuro.prism import jobs
    from okuro.prism.compiler.deck_store import get_deck

    deck = get_deck(deck_id)
    if deck is None:
        raise HTTPException(404, f"deck '{deck_id}' not found")
    source = deck.get("source") if isinstance(deck.get("source"), dict) else None
    artifact_id = (source or {}).get("artifact_id")
    if not artifact_id:
        raise HTTPException(400, "this deck has no recorded source to re-tailor from "
                                 "(recompile via /deck2/compile)")
    recipient = (payload.get("recipient") or "").strip()
    if not recipient:
        raise HTTPException(400, "recipient required")
    brand = (payload.get("brand") or (source or {}).get("brand") or "").strip()
    job_id = jobs.create_job(f"deck2-retailor:{artifact_id}", brand_id=brand or None)

    def _run() -> None:
        from okuro.prism.compiler.deck_store import save_deck
        from okuro.prism.compiler.pipeline import PrismCompiler

        try:
            jobs.set_phase(job_id, "compiling")
            doc = PrismCompiler(artifact_id, [recipient],
                                brand=brand or None).compile_authored_deck_doc()
            doc["source"] = {"artifact_id": artifact_id, "recipients": [recipient],
                             "brand": brand or None}
            new_id = save_deck(doc)
            jobs.complete_job(job_id, doc_id=new_id, title=doc.get("title") or artifact_id)
        except Exception as exc:  # noqa: BLE001 — surface to the poller
            logger.exception("prism deck2 retailor job %s failed", job_id)
            jobs.fail_job(job_id, str(exc))

    threading.Thread(target=_run, name=f"prism-deck2-retailor-{job_id}", daemon=True).start()
    return {"job_id": job_id}


@router.delete("/deck2/{deck_id}")
def delete_deck2(deck_id: str) -> dict:
    """Delete a compiled deck."""
    from okuro.prism.compiler.deck_store import delete_deck

    return {"deleted": delete_deck(deck_id)}


@router.get("/deck2/{deck_id}/export")
def export_deck2(deck_id: str) -> Response:
    """Render a deck2 DeckDoc as a single self-contained HTML file (R33).

    Bakes the DeckDoc into the pre-built deck2 export template so the result opens
    offline anywhere with the FULL L1–L4 ladder + 2D nav + zoom + accuracy — the
    same DeckShadowHost the live viewer uses (no drift), brand resolved via the
    kit's data-theme cascade. Verification-grade: the exported file must pass the
    same rendered gates as the live viewer."""
    import json
    import re

    from okuro.prism.compiler.deck_store import get_deck

    doc = get_deck(deck_id)
    if doc is None:
        raise HTTPException(404, f"deck '{deck_id}' not found")

    from okuro.web.app import DIST_DIR

    template_path = DIST_DIR.parent / "export-dist" / "deck" / "export-deck.html"
    try:
        template = template_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(
            503,
            "deck export template missing — run `pnpm build:export-deck` in web/frontend",
        )

    payload = {"deck": doc}
    payload_js = json.dumps(payload).replace("<", "\\u003c")
    html = template.replace("/*__PRISM_DECK_PAYLOAD__*/ null", payload_js)

    title = (doc.get("title") or "prism-deck").strip() or "prism-deck"
    safe = re.sub(r"[^A-Za-z0-9._ -]", "", title)[:80].strip() or "prism-deck"
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}.html"'},
    )


@router.get("/{doc_id}")
def get_prism_doc(doc_id: str) -> dict:
    from okuro.prism import get_doc

    doc = get_doc(doc_id)
    if doc is None:
        raise HTTPException(404, f"doc '{doc_id}' not found")
    return doc.to_detail()


@router.post("/generate")
def generate_prism_doc(payload: dict) -> dict:
    """Kick off a facet-tree generation and return a ``job_id`` immediately.

    Body: {topic, person_id?, brand_id?, tier?, diversity?}. ``tier`` is
    fast|mid|top (→ haiku/sonnet/opus on claude); ``diversity`` is 1-3 (layout
    variety). The two-pass LLM generation (~1-5 min) runs on a background thread
    and pushes phase updates to the job registry; the gallery polls
    ``/generate/jobs`` for the spinner + live phase. On success the new doc lands
    in the normal list/change-feed. Returns {job_id}.

    SEALED under ``prism.strict`` — see :func:`_seal_engine_a`."""
    import threading

    from okuro.prism import jobs

    _seal_engine_a("/api/prism/generate")
    topic = (payload.get("topic") or "").strip()
    if not topic:
        raise HTTPException(400, "topic required")

    person_id = (payload.get("person_id") or "").strip() or None
    brand_id = (payload.get("brand_id") or "okuro").strip()
    tier = (payload.get("tier") or "").strip() or None
    diversity = payload.get("diversity")

    job_id = jobs.create_job(
        topic, brand_id=brand_id, person_id=person_id, tier=tier, diversity=diversity,
    )

    def _run() -> None:
        from okuro.prism.generate import generate_doc

        try:
            detail = generate_doc(
                topic,
                person_id=person_id,
                brand_id=brand_id,
                tier=tier,
                diversity=diversity,
                progress=lambda phase: jobs.set_phase(job_id, phase),
            )
            jobs.complete_job(
                job_id, doc_id=detail.get("id") or "", title=detail.get("title") or topic,
            )
        except Exception as exc:  # noqa: BLE001 — surface to the poller, never crash the thread
            logger.exception("prism generate job %s failed", job_id)
            jobs.fail_job(job_id, str(exc))

    threading.Thread(target=_run, name=f"prism-gen-{job_id}", daemon=True).start()
    return {"job_id": job_id}


@router.get("/generate/jobs")
def prism_generate_jobs() -> dict:
    """Active + recently-finished generation jobs (spinner + live phase source)."""
    from okuro.prism import jobs

    return {"jobs": jobs.list_jobs()}


def _resolve_pipeline_source(payload: dict) -> tuple[str, str]:
    """Resolve the root document for the build pipeline from the request body:
    inline source_text, an okuro artifact_id, or a note_id. Returns (text, kind)."""
    text = (payload.get("source_text") or "").strip()
    if text:
        return text, (payload.get("kind") or "research").strip() or "research"
    aid = (payload.get("artifact_id") or "").strip()
    if aid:
        from okuro.sense.artifacts import artifact_get
        a = artifact_get(aid, include_body=True) or {}
        return (a.get("body") or ""), (a.get("kind") or "research")
    nid = (payload.get("note_id") or "").strip()
    if nid:
        from okuro.notes import get_note
        n = get_note(nid) or {}
        return (n.get("body") or ""), "note"
    return "", "research"


@router.post("/build-from-artifact")
def prism_build_from_artifact(payload: dict) -> dict:
    """Kick off the TARGET pipeline — a provided root document (source_text /
    artifact_id / note_id) transformed for a ``target`` recipient into a faithful,
    depth-laddered, critic-gated deck — and return a ``job_id`` immediately. The
    ~15-25min run streams phases to the same job registry the gallery polls
    (/generate/jobs). Body: {target, source_text?|artifact_id?|note_id?, title?,
    brand_id?, review?, scenario?}.

    SEALED under ``prism.strict`` — see :func:`_seal_engine_a`. This is the door
    the plan's critique found still open beside the MCP seal."""
    import threading

    from okuro.prism import jobs

    _seal_engine_a("/api/prism/build-from-artifact")
    target = (payload.get("target") or "").strip()
    if not target:
        raise HTTPException(400, "target required")
    source, kind = _resolve_pipeline_source(payload)
    if not source.strip():
        raise HTTPException(400, "one of source_text / artifact_id / note_id required")

    title = (payload.get("title") or "").strip() or None
    brand_id = (payload.get("brand_id") or "okuro").strip()
    review = bool(payload.get("review", True))
    scenario = bool(payload.get("scenario", True))
    job_id = jobs.create_job(title or target, brand_id=brand_id)

    def _run() -> None:
        from okuro.prism.assemble import build_deck

        try:
            detail = build_deck(
                source, target, kind=kind, brand_id=brand_id, title=title,
                review=review, scenario=scenario,
                progress=lambda phase: jobs.set_phase(job_id, phase),
            )
            jobs.complete_job(job_id, doc_id=detail.get("id") or "", title=detail.get("title") or (title or target))
        except Exception as exc:  # noqa: BLE001 — surface to the poller, never crash the thread
            logger.exception("prism build-from-artifact job %s failed", job_id)
            jobs.fail_job(job_id, str(exc))

    threading.Thread(target=_run, name=f"prism-build-{job_id}", daemon=True).start()
    return {"job_id": job_id}


@router.post("/edit")
def edit_prism_doc(payload: dict) -> dict:
    """Apply a natural-language edit/review to an existing doc (structured
    ops). Body: {doc_id, instruction}. The open canvas live-updates via the
    change-feed."""
    from okuro.prism.generate import edit_doc

    doc_id = (payload.get("doc_id") or payload.get("id") or "").strip()
    instruction = (payload.get("instruction") or "").strip()
    facet_id = (payload.get("facet_id") or "").strip() or None
    if not doc_id:
        raise HTTPException(400, "doc_id required")
    if not instruction:
        raise HTTPException(400, "instruction required")
    try:
        return edit_doc(doc_id, instruction, facet_id=facet_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"edit failed: {exc}")


@router.post("/{doc_id}/alternatives")
def prism_alternatives(doc_id: str) -> dict:
    """Author a second visual arrangement of every slide (A/B). Additive: each
    rung that gets a genuinely different design gains an ``alts`` entry; the
    primary is untouched. Returns the saved doc detail (+ alternatives_added)."""
    from okuro.prism.generate import generate_alternatives

    try:
        return generate_alternatives(doc_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"alternatives failed: {exc}")


@router.post("/{doc_id}/pick-alt")
def prism_pick_alt(doc_id: str, payload: dict) -> dict:
    """Commit an A/B choice for one facet's rung. Body: {facet_id, rung,
    alt_index?}. Omit alt_index (or null) to keep the primary (A); pass an int to
    adopt that alternative. Either way the rung's alternatives are cleared."""
    from okuro.prism.generate import pick_alternative

    facet_id = (payload.get("facet_id") or "").strip()
    rung = (payload.get("rung") or "").strip()
    if not facet_id or not rung:
        raise HTTPException(400, "facet_id and rung required")
    ai = payload.get("alt_index")
    alt_index = int(ai) if isinstance(ai, (int, float)) else None
    try:
        return pick_alternative(doc_id, facet_id, rung, alt_index=alt_index)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/apply-ops")
def apply_prism_ops(payload: dict) -> dict:
    """Apply structured edit ops directly to a doc, in place. Body:
    {doc_id, ops: [...]}. See ``okuro.prism.ops`` for op shapes."""
    from okuro.prism import get_doc, save_doc
    from okuro.prism.ops import apply_ops

    doc_id = (payload.get("doc_id") or payload.get("id") or "").strip()
    if not doc_id:
        raise HTTPException(400, "doc_id required")
    existing = get_doc(doc_id)
    if existing is None:
        raise HTTPException(404, f"doc '{doc_id}' not found")

    ops = payload.get("ops") if isinstance(payload.get("ops"), list) else []
    new_doc, op_errors = apply_ops(existing.doc, ops)
    saved = save_doc(
        id=existing.id,
        title=new_doc.get("title") or existing.title,
        brand_id=existing.brand_id,
        variant_of=existing.variant_of,
        doc=new_doc,
        origin="web",
        allow_empty=True,
    )
    detail = saved.to_detail()
    detail["op_errors"] = op_errors
    return detail


@router.post("/retailor")
def retailor_prism_doc(payload: dict) -> dict:
    """Re-tailor a doc to a recipient + density/jargon/language/depth_override
    axes. Body: {doc_id, person_id?, depth_override?, density?, jargon?,
    language?, as_variant?}. Text-only edit via scoped ops (facet ids + tree
    shape stable). as_variant (default true) saves a NEW linked variant; false
    edits in place. Returns the saved doc detail."""
    from okuro.prism.retailor import retailor_facets

    doc_id = (payload.get("doc_id") or payload.get("id") or "").strip()
    if not doc_id:
        raise HTTPException(400, "doc_id required")
    try:
        return retailor_facets(
            doc_id,
            person_id=(payload.get("person_id") or "").strip() or None,
            depth_override=(payload.get("depth_override") or "").strip() or None,
            density=(payload.get("density") or "").strip() or None,
            jargon=(payload.get("jargon") or "").strip() or None,
            language=(payload.get("language") or "").strip() or None,
            reselect_modules=bool(payload.get("reselect_modules", False)),
            as_variant=bool(payload.get("as_variant", True)),
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"retailor failed: {exc}")


@router.post("")
def save_prism_doc(payload: dict) -> dict:
    """Create or update a doc. Body: {id?, title, doc, brand_id?, origin?,
    allow_empty?}. ``doc`` is the full facet-tree IR (facets{}, entry_facet_id,
    entry_rung)."""
    from okuro.prism import save_doc

    title = (payload.get("title") or "").strip()
    doc = payload.get("doc") if isinstance(payload.get("doc"), dict) else {}
    if not title:
        title = str(doc.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    try:
        saved = save_doc(
            id=(payload.get("id") or "").strip() or None,
            title=title,
            brand_id=(payload.get("brand_id") or "").strip() or None,
            doc=doc,
            origin=(payload.get("origin") or "").strip(),
            allow_empty=bool(payload.get("allow_empty")),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return saved.to_detail()


@router.post("/{doc_id}/brand")
def set_prism_brand(doc_id: str, payload: dict) -> dict:
    """Restyle an existing deck: switch its brand and/or per-deck logo corner
    without regenerating content. Body: {brand_id?, logo_corner?}. Keys absent
    from the body are left unchanged; ``brand_id: ""`` clears the brand,
    ``logo_corner: ""`` clears the per-deck override (falls back to brand
    default). Emits a change-feed event → open viewers re-theme live."""
    from okuro.prism import update_doc_meta

    kwargs: dict = {}
    if "brand_id" in payload:
        kwargs["brand_id"] = payload.get("brand_id")
    if "logo_corner" in payload:
        kwargs["logo_corner"] = payload.get("logo_corner")
    if not kwargs:
        raise HTTPException(400, "brand_id or logo_corner required")
    try:
        saved = update_doc_meta(doc_id, **kwargs)
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc) else 400, str(exc))
    return saved.to_detail()


@router.delete("/{doc_id}")
def delete_prism_doc(doc_id: str, origin: str = "") -> dict:
    from okuro.prism import delete_doc

    return {"deleted": delete_doc(doc_id, origin=origin)}
