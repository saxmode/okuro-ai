# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism top-down WORKFLOW IR — the typed values the recipient-driven
#   deck loop passes between nodes: Understanding (holistic comprehension of the
#   whole artifact), RecipientBrief (who reads it + what they need), TopicMap
#   (recipient-relevant topics, NOT a claim partition). Deliberately carries NO
#   caps: every collection is as large as the source x recipient warrant.
# index: Section | Understanding | RecipientBrief | WorkflowTopic | TopicMap
# AGENT_HEADER_END -->
"""Workflow IR for the top-down, recipient-driven deck loop.

Contrast with ``compiler.ir``, which is bottom-up: it models atomized *claims*
and a strict claim->topic partition. This IR models what a designer actually
holds in their head — what the document IS, who it is for, and which topics
serve that reader. Two consequences follow, and both are load-bearing:

* **No partition.** A fact may serve several topics. ``WorkflowTopic.section_refs``
  therefore MAY overlap between topics, and nothing repairs that "conflict" —
  the overlap is correct, not a defect to be normalized away.
* **No caps.** No field here has a maximum length, and no schema that produces
  one sets ``maxItems``. Output size is a function of input x audience. A
  46k-word reference is free to yield many sections and many topics; a memo,
  few. Capping topics at 7 is what crushed a book into a pamphlet.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Section:
    """One coherent stretch of the source document, as understood (not extracted).

    ``id`` is assigned in code (``s1``..``sN``) across ALL chunks, never by the
    model — per-chunk numbering restarts at 1 and silently cross-wires refs.
    """

    id: str
    heading: str
    gist: str
    covers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Section":
        return cls(id=d.get("id", ""), heading=d.get("heading", ""),
                   gist=d.get("gist", ""), covers=list(d.get("covers") or []))


@dataclass
class Understanding:
    """Holistic comprehension of the WHOLE artifact, before any recipient exists.

    This is step 1 of the loop and it is deliberately recipient-agnostic: what the
    document is, what it argues, how it is built. Slanting it toward a reader here
    would leak the recipient into every downstream node and make the same source
    un-reusable for a second audience.
    """

    source_artifact_id: str = ""
    source_title: str = ""
    document_kind: str = ""          # "reference manual" | "strategy memo" | …
    what_it_is: str = ""             # holistic, 1-2 sentences
    thesis: str = ""                 # the central argument of the whole document
    domain: str = ""
    sections: list[Section] = field(default_factory=list)
    through_lines: list[str] = field(default_factory=list)   # ideas recurring across sections
    key_entities: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    word_count: int = 0
    chunk_count: int = 0

    def section_ids(self) -> list[str]:
        return [s.id for s in self.sections]

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["sections"] = [s.to_dict() for s in self.sections]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Understanding":
        d = dict(d)
        d["sections"] = [Section.from_dict(s) for s in d.get("sections") or []]
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class RecipientBrief:
    """Who the deck is for, and what that implies for what gets said.

    ``audience`` is the verbatim ``AudienceProfile`` dict from the existing
    ``compiler.profile`` stage — reused rather than re-derived, so the people
    graph, the cognitive-lens PII firewall, and person x brand disambiguation
    keep working exactly once (DP10). The LLM-authored fields sit alongside it.
    """

    recipients: list[str] = field(default_factory=list)
    audience: dict[str, Any] = field(default_factory=dict)   # AudienceProfile.to_dict()
    cares_about: list[str] = field(default_factory=list)
    decisions_faced: list[str] = field(default_factory=list)
    lead_with: str = ""
    suppress: list[str] = field(default_factory=list)
    delivery: str = ""               # how this reader wants information delivered
    jargon_tolerance: str = "medium"
    depth_ceiling: int = 3
    # The ceiling in the L-rung vocabulary the author/writer prompts speak.
    # Carried explicitly rather than re-derived per node, so the two depth
    # namespaces stay converted in exactly one place (prism.rungs).
    depth_ceiling_rung: str = "L3"
    # May the deck NAME its readers? Default False: a recipient's identity is
    # firewalled everywhere else, so naming them is an opt-in, never a
    # default. People ships the field and the pass-through; prism owns the
    # setter and the no-attribution lint.
    mention_audience_members: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RecipientBrief":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def project_for_prompt(self, *, exclude: "Iterable[str]" = ()) -> dict[str, Any]:
        """Every reader-shape field, or a loud failure. Never a hand-picked subset.

        Each node used to build its own ad-hoc reader dict: author took 5 of
        9 fields, gather took 1, assemble took 0. So depth_ceiling was
        computed and dropped at the node that writes all four depth levels,
        decisions_faced never reached a prompt at all, and
        prefer_modules/avoid_modules — the entire output of the 14-axis
        module-affinity matrix — never reached the component chooser.
        Producers wrote, no consumer read: nine instances of one class.

        This projection is exhaustive by construction. A field added to the
        dataclass enters every prompt automatically; a field a node does not
        want must be NAMED in ``exclude``, and naming a field that does not
        exist raises. There is no silent drop left.

        ``recipients`` and ``audience`` are structural rather than
        reader-shape — ``recipients`` carries names (identity, firewalled
        everywhere else) and ``audience`` is the nested AudienceProfile —
        so they are withheld by default. The audience POLICY it contains is
        flattened up, because that is the part a prompt has to see.
        """
        known = {f.name for f in dataclasses.fields(self)}
        excluded = set(exclude)
        unknown = excluded - known
        if unknown:
            raise ValueError(
                f"RecipientBrief.project_for_prompt: unknown field(s) in exclude: "
                f"{', '.join(sorted(unknown))}. Known: {', '.join(sorted(known))}."
            )

        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            if f.name in _STRUCTURAL_FIELDS or f.name in excluded:
                continue
            out[f.name] = getattr(self, f.name)

        policy = self.audience if isinstance(self.audience, dict) else {}
        for key in ("prefer_modules", "avoid_modules"):
            if key in excluded:
                continue
            value = policy.get(key)
            if value:
                out[key] = value
        return out


# Withheld from prompts by default: `recipients` is identity, `audience` is a
# nested structure whose load-bearing keys are flattened up instead.
_STRUCTURAL_FIELDS = frozenset({"recipients", "audience"})


@dataclass
class WorkflowTopic:
    """One topic of the deck, chosen FOR a recipient — not a cluster of claims.

    ``section_refs`` points into ``Understanding.sections``. It may overlap with
    another topic's refs: one fact legitimately serves more than one topic, and
    forcing a partition is what made topics arbitrary.
    """

    id: str = ""
    title: str = ""
    question_it_answers: str = ""
    why_this_recipient: str = ""
    section_refs: list[str] = field(default_factory=list)
    priority: float = 0.5            # 0..1, ordering weight for this reader
    order: int = 0
    unresolved_refs: list[str] = field(default_factory=list)  # refs no section matched

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkflowTopic":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class TopicMap:
    """The ordered set of recipient-relevant topics plus the arc joining them."""

    arc: str = ""
    topics: list[WorkflowTopic] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"arc": self.arc, "topics": [t.to_dict() for t in self.topics]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TopicMap":
        return cls(arc=d.get("arc", ""),
                   topics=[WorkflowTopic.from_dict(t) for t in d.get("topics") or []])


@dataclass
class GatheredFact:
    """One piece of the artifact's material, pulled because a topic needs it.

    A fact is NOT a claim in the compiler's sense: it is not the atomic unit the
    whole deck is built from, it is material gathered in service of a topic that
    already exists. The same fact may be gathered for several topics — ``for_topics``
    records that rather than forcing an owner.

    ``shape`` is one of the 11 kit shapes, and ``items_content`` holds the REAL
    per-item ``(label, detail, meta)`` triples the kit components bind to. Both
    exist so Phase D can reason about which component carries this well; neither
    picks a component here.
    """

    id: str = ""
    text: str = ""
    shape: str = "narrative"
    items_content: list[list[str]] = field(default_factory=list)  # (label, detail, meta)
    source_refs: list[str] = field(default_factory=list)          # Understanding section ids
    evidence: str = ""            # verbatim span from the artifact, for the gate
    salience: float = 0.5
    for_topics: list[str] = field(default_factory=list)
    #: The model returned no shape, or one outside the 11-value enum, and
    #: "narrative" was substituted. Carried in the IR rather than logged because
    #: gather runs behind a cache — a log line is gone by the time the deck is
    #: assembled, and `narrative` is the shape whose fit-set is 8/10 pure text.
    shape_defaulted: bool = False
    #: The offending value when ``shape_defaulted`` — "" for a missing shape.
    shape_raw: str = ""

    @property
    def items(self) -> int:
        return max(1, len(self.items_content))

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GatheredFact":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class TopicGathering:
    """Everything the artifact offers for ONE topic, before any level is authored."""

    topic_id: str = ""
    facts: list[GatheredFact] = field(default_factory=list)
    coverage_note: str = ""
    #: Share of this topic's facts whose ``evidence`` span traces VERBATIM to the
    #: source body. Measured at gather and carried here because the same span is
    #: attached to the deck as a per-cell "verbatim quote" provenance affordance
    #: — a paraphrase in that slot contradicts a user-visible claim, so the rate
    #: has to reach the deck rather than a logger. Measured 3/32 on the first
    #: real run: the model returns faithful REWRITES, not quotes.
    grounding_rate: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"topic_id": self.topic_id, "coverage_note": self.coverage_note,
                "grounding_rate": self.grounding_rate,
                "facts": [f.to_dict() for f in self.facts]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TopicGathering":
        return cls(topic_id=d.get("topic_id", ""),
                   coverage_note=d.get("coverage_note", ""),
                   grounding_rate=float(d.get("grounding_rate", 1.0)),
                   facts=[GatheredFact.from_dict(f) for f in d.get("facts") or []])


@dataclass
class AuthoredBlock:
    """One authored unit of content on one level. Carries NO component.

    Component selection is Phase D's job, by reasoning over ``prism_library``.
    A block states WHAT is being said and what SHAPE the content has; which of the
    37 kit components renders it is decided later, with the library's guides in
    hand. Baking a component in here would resurrect the frozen fit-table.
    """

    id: str = ""
    job: str = ""                 # what this block does ON THIS LEVEL
    text: str = ""
    shape: str = "narrative"
    items_content: list[list[str]] = field(default_factory=list)
    emphasis: str = "supporting"  # focal | primary | supporting | aside
    tier: int = 1                 # 0 = most important
    fact_ids: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AuthoredBlock":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class AuthoredLevel:
    """One depth level of one topic, authored BY JOB.

    The level exists because its JOB exists, not because enough claims survived to
    fill it. That inversion is the whole point of Phase C: the old ladder built
    depth from claim DENSITY and dropped a level when the material was thin
    (``authoring/ladder.py`` ``select_kept_levels``), which made shallow topics
    silently lose their deep levels.
    """

    level: str = ""               # L1 | L2 | L3
    job: str = ""                 # the job this level performs for the reader
    blocks: list[AuthoredBlock] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "job": self.job,
                "blocks": [b.to_dict() for b in self.blocks]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AuthoredLevel":
        return cls(level=d.get("level", ""), job=d.get("job", ""),
                   blocks=[AuthoredBlock.from_dict(b) for b in d.get("blocks") or []])


@dataclass
class L4Section:
    heading: str = ""
    body: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class L4Doc:
    """L4 — full prose, as a navigable book-entry rather than a slide.

    ``artifact_id`` is what the viewer's "open the full original" overlay opens.
    That overlay is reachable from EVERY level, not only from L4, so the reader
    can always drop from any depth straight to the unabridged source.
    """

    topic_id: str = ""
    title: str = ""
    sections: list[L4Section] = field(default_factory=list)
    artifact_id: str = ""         # the full original, openable as an overlay
    word_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"topic_id": self.topic_id, "title": self.title,
                "artifact_id": self.artifact_id, "word_count": self.word_count,
                "sections": [s.to_dict() for s in self.sections]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "L4Doc":
        return cls(topic_id=d.get("topic_id", ""), title=d.get("title", ""),
                   artifact_id=d.get("artifact_id", ""),
                   word_count=int(d.get("word_count", 0) or 0),
                   sections=[L4Section(heading=s.get("heading", ""), body=s.get("body", ""))
                             for s in d.get("sections") or []])


@dataclass
class AuthoredTopic:
    """One topic authored across all four depth levels. Phase D's input."""

    topic_id: str = ""
    title: str = ""
    levels: dict[str, AuthoredLevel] = field(default_factory=dict)   # "L1"|"L2"|"L3"
    l4: L4Doc = field(default_factory=L4Doc)
    facts: list[GatheredFact] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"topic_id": self.topic_id, "title": self.title,
                "levels": {k: v.to_dict() for k, v in self.levels.items()},
                "l4": self.l4.to_dict(),
                "facts": [f.to_dict() for f in self.facts]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AuthoredTopic":
        return cls(
            topic_id=d.get("topic_id", ""), title=d.get("title", ""),
            levels={k: AuthoredLevel.from_dict(v) for k, v in (d.get("levels") or {}).items()},
            l4=L4Doc.from_dict(d.get("l4") or {}),
            facts=[GatheredFact.from_dict(f) for f in d.get("facts") or []],
        )


__all__ = [
    "Section", "Understanding", "RecipientBrief", "WorkflowTopic", "TopicMap",
    "GatheredFact", "TopicGathering", "AuthoredBlock", "AuthoredLevel",
    "L4Section", "L4Doc", "AuthoredTopic",
]
