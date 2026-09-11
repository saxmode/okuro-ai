# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Target registry for okuro-handover — the ONE declarative place that
#   says, per destination tool, what content it ACCEPTS and what data it REQUIRES
#   beyond the content (the intake contract). Adding a 5th tool = one entry here;
#   every source gets the handover for free.
# index:
#   class Field / class Target
#   def _resolve_recipient / _resolve_brand
#   _produce_notes / _produce_flow / _produce_prism
#   TARGETS / def get_target / def eligible / def public_contract
# AGENT_HEADER_END -->
"""okuro·handover — target registry + intake contract.

Each ``Target`` declares:
  - ``accepts``  — which IR kinds it can receive
  - ``requires`` — a list of ``Field``s. Each field has a ``mode``:
        ask      → the intake UI must prompt for it (nothing sensible to infer)
        auto     → resolved silently by a named resolver (e.g. brand ← project)
        optional → offered, never required
  - ``produce``  — turns (IR, resolved-inputs) into the destination doc

The frontend reads ``public_contract`` to render ONLY the ``ask`` fields, so the
dialog for a flow→prism handover shows the recipient picker and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from okuro.handover.ir import ContentIR

_DEFAULT_BRAND = "okuro"


@dataclass
class Field:
    key: str
    label: str
    type: str                 # text | recipient | brand | select
    mode: str = "ask"         # ask | auto | optional
    help: str = ""
    options: tuple[str, ...] = ()  # for type=select (may be filled dynamically)

    def public(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "type": self.type,
                "mode": self.mode, "help": self.help, "options": list(self.options)}


@dataclass
class Target:
    id: str
    label: str
    icon: str
    accepts: tuple[str, ...]
    requires: tuple[Field, ...]
    produce: Callable[[ContentIR, dict], dict]

    def ask_fields(self) -> list[Field]:
        return [f for f in self.requires if f.mode in ("ask", "optional")]

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "icon": self.icon,
            "accepts": list(self.accepts),
            "fields": [f.public() for f in self.requires],
        }


# ── resolvers ─────────────────────────────────────────────────────────────────


def _resolve_brand(ir: ContentIR, inputs: dict) -> str:
    """Explicit brand wins; else the project's bound brand; else the default."""
    b = (inputs.get("brand") or "").strip()
    if b:
        return b
    if ir.project:
        try:
            from okuro.stack.registry import active_brand_for

            got = active_brand_for(ir.project)
            if isinstance(got, dict) and got.get("id"):
                return got["id"]
        except Exception:
            pass
    return _DEFAULT_BRAND


def _resolve_recipient(inputs: dict) -> tuple[Optional[str], Optional[str]]:
    """A prism/slides recipient is EITHER a person OR a target group.

    Returns ``(person_id, audience_hint)``:
      - person → (person_id, None): native lens path
      - group  → (representative_person_id | None, group_name): the group's first
        profiled member seeds the lens; the group name frames the whole doc.
    """
    rec = inputs.get("recipient") or {}
    if not isinstance(rec, dict):
        return None, None
    kind = (rec.get("kind") or "").strip()
    rid = (rec.get("id") or "").strip()
    if not rid:
        return None, None
    if kind == "person":
        return rid, None
    if kind == "group":
        try:
            from okuro.peer.target_groups import resolve_audience

            aud = resolve_audience(rid)
            if aud.get("error"):
                return None, None
            members = aud.get("members") or []
            rep = members[0]["person_id"] if members else None
            return rep, aud.get("name") or rid
        except Exception:
            return None, rid
    return None, None


# ── producers (IR + resolved inputs → destination doc) ────────────────────────


def _produce_notes(ir: ContentIR, inputs: dict) -> dict[str, Any]:
    from okuro.notes import upsert_note

    # Translate when the source is structurally NOT a note (a flow / facet) — a
    # fast model re-expresses it as prose. Text is already note-shaped → mechanical.
    if ir.kind in ("subgraph", "facet") and isinstance(ir.structured, dict):
        from okuro.handover.transform import flow_to_note_markdown

        graph = ir.to_graph()
        body = flow_to_note_markdown(ir.title, graph)
        foot = ir._provenance_line()
        body = f"{body}\n\n{foot}".strip() if foot else body
    else:
        body = ir.to_markdown()

    note = upsert_note(
        title=(inputs.get("title") or ir.title or "Untitled"),
        body=body,
        project=ir.project,
        origin="handover",
    )
    return {"id": note.get("id"), "url": "/notes", "target": "notes",
            "label": note.get("title")}


def _produce_flow(ir: ContentIR, inputs: dict) -> dict[str, Any]:
    from okuro.flow_designer import storage

    # A subgraph is already a flow → pass its structure through untouched. Text /
    # facet get TRANSLATED into a real flowchart by a fast model.
    if ir.kind == "subgraph":
        graph = ir.to_graph()
    else:
        from okuro.handover.transform import text_to_flow_graph

        graph = text_to_flow_graph(ir.title, ir.body_md)

    saved = storage.save_flow(
        name=(inputs.get("name") or ir.title or "Handover flow"),
        description=(ir.source or {}).get("label", "") if ir.source else "",
        graph=graph,
        origin="handover",
    )
    return {"id": saved.id, "url": f"/flow?id={saved.id}", "target": "flow",
            "label": saved.name}


def _produce_prism(ir: ContentIR, inputs: dict) -> dict[str, Any]:
    from okuro.prism.generate import generate_doc

    person_id, audience = _resolve_recipient(inputs)
    brand = _resolve_brand(ir, inputs)
    doc = generate_doc(
        topic=ir.as_topic(audience_hint=audience),
        person_id=person_id,
        brand_id=brand,
    )
    return {"id": doc.get("id"), "url": f"/prism?id={doc.get('id')}",
            "target": "prism", "label": doc.get("title")}


def _produce_slides(ir: ContentIR, inputs: dict) -> dict[str, Any]:
    from okuro.slides.generate import generate_deck

    person_id, audience = _resolve_recipient(inputs)
    brand = _resolve_brand(ir, inputs)
    deck = generate_deck(
        topic=ir.as_topic(audience_hint=audience),
        person_id=person_id,
        brand_id=brand,
    )
    return {"id": deck.get("id"), "url": f"/slides?id={deck.get('id')}",
            "target": "slides", "label": deck.get("title")}


def _produce_delivery(ir: ContentIR, inputs: dict) -> dict[str, Any]:
    """Delivery renders + sends an EXISTING artifact through a channel, so first
    persist the selection as an artifact, then run the pipeline for the
    recipient + channel."""
    from okuro.peer.delivery import send
    from okuro.sense.artifacts import artifact_write

    person_id, _ = _resolve_recipient(inputs)
    channel = (inputs.get("channel") or "markdown").strip()
    brand = _resolve_brand(ir, inputs)

    aid = artifact_write(
        kind="report",
        title=(ir.title or "Handover"),
        body=ir.to_markdown(),
        project=ir.project,
        created_by="handover",
    )
    res = send(aid, person_id=person_id, channel=channel,
               brand_id=brand, title=ir.title, created_by="handover")
    if not res.get("success"):
        raise ValueError(res.get("error") or "delivery failed")
    return {"id": res.get("delivery_id"), "url": "/deliveries",
            "target": "delivery",
            "label": f"{channel}" + (f" → {person_id}" if person_id else ""),
            "channel": res.get("channel"), "media_type": res.get("media_type")}


def _context_to_md(ctx: dict) -> str:
    """Render captured page context as an LLM-readable brief — the file a task's
    subagent reads FIRST. Surfaces the entity/record (the workable data) before
    the page metadata and the raw visible text, so a text-only agent can act on
    structured data instead of screen-scraping the screenshot."""
    import json

    ctx = ctx or {}
    lines: list[str] = ["# Captured view", ""]
    for label, key in (("URL", "url"), ("Route", "route"), ("Page title", "title")):
        val = ctx.get(key)
        if val:
            lines.append(f"- **{label}:** {val}")
    entity = ctx.get("entity")
    if isinstance(entity, dict) and entity:
        lines += ["", "## Entity", "",
                  "```json", json.dumps(entity, indent=2, default=str), "```"]
    data = ctx.get("data")
    if data not in (None, {}, [], ""):
        lines += ["", "## Record", "",
                  "```json", json.dumps(data, indent=2, default=str), "```"]
    struct = ctx.get("structure")
    if isinstance(struct, dict):
        headings = [h for h in (struct.get("headings") or []) if isinstance(h, dict)]
        fields = [f for f in (struct.get("fields") or []) if isinstance(f, dict)]
        if headings:
            lines += ["", "## Outline", ""]
            for h in headings:
                lvl = h.get("level") or 1
                text = (h.get("text") or "").strip()
                if text:
                    lines.append("  " * max(0, int(lvl) - 1) + f"- {text}")
        if fields:
            lines += ["", "## Fields", ""]
            for f in fields:
                label = (f.get("label") or "").strip()
                value = (f.get("value") or "").strip()
                if label and value:
                    lines.append(f"- **{label}:** {value}")
    visible = (ctx.get("visibleText") or "").strip()
    if visible:
        lines += ["", "## Visible text", "", visible]
    return "\n".join(lines).strip() + "\n"


def _produce_orchestrator(ir: ContentIR, inputs: dict) -> dict[str, Any]:
    """Hero snapshot target: open a NEW orchestrator task seeded with the captured
    view. The screenshot + page context are written into the task's ``artifacts/``
    dir so its subagents see what the user saw AND the underlying data. Created in
    plan mode (the engine spells that ``deliberate``) — the user reviews the plan
    before it runs. Also accepts plain text / an asset: hand anything to a task."""
    import json
    from okuro.orchestrator.config import load_config
    from okuro.orchestrator.state import create_task

    ctx: dict = {}
    if isinstance(ir.structured, dict):
        ctx = ir.structured.get("context") or {}
    route = ctx.get("route") or ctx.get("url") or "the current view"
    intent = (inputs.get("intent") or "").strip() or "Review and improve what's shown."

    if ir.kind == "snapshot":
        prompt = (
            f"{intent}\n\n"
            f"Context: the user was viewing {route} in okuro and captured a snapshot "
            f"of the current view. Start with artifacts/context.md — an LLM-readable "
            f"brief holding the page's entity/record data, metadata, and visible text. "
            f"artifacts/context.json is the same context as raw JSON; "
            f"artifacts/screenshot.png is what they saw. Ground your work in the record "
            f"data first, then act on the request above."
        )
    else:
        # text / asset handed to a task — no artifacts, seed with the body.
        body = ir.to_markdown()
        prompt = f"{intent}\n\n{body}".strip() if body else intent

    tasks_dir = load_config().tasks_dir
    task = create_task(prompt, tasks_dir, mode="deliberate")

    if ir.kind == "snapshot":
        art_dir = tasks_dir / task.id / "artifacts"
        art_dir.mkdir(parents=True, exist_ok=True)
        png = ir.snapshot_png_bytes()
        if png:
            (art_dir / "screenshot.png").write_bytes(png)
        (art_dir / "context.md").write_text(_context_to_md(ctx), encoding="utf-8")
        (art_dir / "context.json").write_text(
            json.dumps(ctx, indent=2, default=str), encoding="utf-8")

    return {"id": task.id, "url": f"/agents?task={task.id}",
            "target": "orchestrator", "label": task.id}


def _produce_asset(ir: ContentIR, inputs: dict) -> dict[str, Any]:
    """Register a captured snapshot's PNG as a first-class store asset. The
    screenshot is the first source that emits an *unregistered* image, so it earns
    a real ``register_asset`` (kind=image, source=upload) — not the dead wiring the
    store-unification fork warned about (that lane copies rows already in the
    store). Reference-mode promotion of note drawings stays deferred to that fork."""
    from okuro.assets.store import register_asset

    png = ir.snapshot_png_bytes()
    if not png:
        raise ValueError("snapshot has no screenshot to register")
    ctx = ir.structured.get("context") if isinstance(ir.structured, dict) else {}
    folder = (inputs.get("folder") or "").strip() or "snapshots"
    asset = register_asset(
        kind="image", source="upload", data=png, mime="image/png",
        folder=folder, title=(inputs.get("title") or ir.title or "Snapshot"),
        meta={"context": ctx or {}}, tags=["snapshot"],
    )
    return {"id": asset.get("id"), "url": "/assets", "target": "assets",
            "label": asset.get("title")}


# ── the registry ──────────────────────────────────────────────────────────────

_RECIPIENT = Field(
    key="recipient", label="Tailor for", type="recipient", mode="ask",
    help="Pick a person or a target group — sets the depth and tone.",
)
_BRAND = Field(key="brand", label="Brand", type="brand", mode="auto",
               help="Design system. Defaults to the project's brand.")
_CHANNEL = Field(key="channel", label="Channel", type="select", mode="ask",
                 help="How to render & deliver it (markdown, microsite, podcast…).")


def _channel_options() -> list[str]:
    """Live channel names for the delivery intake picker (best-effort)."""
    try:
        from okuro.peer.delivery.channels import list_channels

        return list(list_channels())
    except Exception:
        return ["markdown"]


_INTENT = Field(
    key="intent", label="What should the task do with this?", type="text", mode="ask",
    help="e.g. 'Improve this profile' or 'Draft a follow-up from this'.",
)


TARGETS: dict[str, Target] = {
    "orchestrator": Target(
        id="orchestrator", label="Task", icon="bot",
        accepts=("snapshot", "text", "asset"),
        requires=(_INTENT,),
        produce=_produce_orchestrator,
    ),
    "notes": Target(
        id="notes", label="Note", icon="notebook-pen",
        accepts=("text", "subgraph", "facet", "asset", "snapshot"),
        requires=(Field(key="title", label="Title", type="text", mode="optional"),),
        produce=_produce_notes,
    ),
    "assets": Target(
        id="assets", label="Asset", icon="image",
        accepts=("snapshot",),
        requires=(
            Field(key="title", label="Title", type="text", mode="optional"),
            Field(key="folder", label="Folder", type="text", mode="optional"),
        ),
        produce=_produce_asset,
    ),
    "flow": Target(
        id="flow", label="Flow", icon="network",
        accepts=("text", "subgraph", "facet", "asset"),
        requires=(Field(key="name", label="Name", type="text", mode="optional"),),
        produce=_produce_flow,
    ),
    "prism": Target(
        id="prism", label="Prism", icon="diamond",
        accepts=("text", "facet", "subgraph"),
        requires=(_RECIPIENT, _BRAND),
        produce=_produce_prism,
    ),
    "slides": Target(
        id="slides", label="Slides", icon="presentation",
        accepts=("text", "facet", "subgraph"),
        requires=(_RECIPIENT, _BRAND),
        produce=_produce_slides,
    ),
    "delivery": Target(
        id="delivery", label="Deliver", icon="send",
        accepts=("text", "subgraph", "facet"),
        requires=(_RECIPIENT, _CHANNEL, _BRAND),
        produce=_produce_delivery,
    ),
}


def get_target(target_id: str) -> Optional[Target]:
    return TARGETS.get((target_id or "").strip())


def eligible(kind: str) -> list[Target]:
    """Targets that can receive an IR of this kind."""
    return [t for t in TARGETS.values() if kind in t.accepts]


def public_contract(kind: str) -> list[dict[str, Any]]:
    """What the intake UI/agent reads to know which targets are reachable and
    exactly which fields each one must ask for. Fills dynamic select options
    (e.g. the delivery channel list) at read time."""
    out = []
    for t in eligible(kind):
        pub = t.public()
        for f in pub["fields"]:
            if f["key"] == "channel" and not f["options"]:
                f["options"] = _channel_options()
        out.append(pub)
    return out
