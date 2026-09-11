# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism top-down WORKFLOW RUNNER — wires the recipient-driven nodes
#   over the content-hashed StageCache with resume (--from <node>) and a per-node
#   TRACE for inspectability. Holds no domain logic. Phase B implemented:
#   understand -> recipient -> topicmap. Later phases append to NODE_ORDER.
# index: NODE_ORDER | NodeTrace | PrismWorkflow | run_phase_b
# AGENT_HEADER_END -->
"""The top-down workflow runner.

Same shape as ``compiler.pipeline.PrismCompiler`` on purpose — one cache-aware
wrapper per node, no domain logic in the runner — but a different chain. It runs
in-process and synchronously, so the existing entrypoints (``prism_build_from_artifact``,
``/deck2/compile``) can call it exactly the way they call the compiler today.

**Cache namespace.** ``~/.okuro/prism-workflow/<artifact_id>/``, separate from the
compiler's ``prism-cache``. The two chains have different node names and different
dependency orders; sharing a directory would let one pipeline's ``invalidate_from``
delete the other's work.

**Trace.** Every node records what it did — cached or computed, how long, and a
short summary of its output. This is the inspectability the orchestrator would
have given for free, at a fraction of its cost, and it is what makes a bad deck
diagnosable after the fact rather than only re-runnable.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from okuro.prism.compiler.cache import StageCache
from okuro.prism.workflow import author as _author
from okuro.prism.workflow import gather as _gather
from okuro.prism.workflow import recipient as _recipient
from okuro.prism.workflow import topicmap as _topicmap
from okuro.prism.workflow import understand as _understand
from okuro.prism.workflow.types import (
    AuthoredTopic,
    RecipientBrief,
    TopicGathering,
    TopicMap,
    Understanding,
)
from okuro.db.engine import okuro_home

logger = logging.getLogger(__name__)

# The node dependency chain. `invalidate_from(node)` drops it and everything after.
# Phases D-E append here (select, assemble) — never insert silently.
NODE_ORDER: tuple[str, ...] = ("understand", "recipient", "topicmap", "gather", "author")


@dataclass
class NodeTrace:
    """What one node did, for after-the-fact diagnosis of a bad deck."""

    node: str
    cached: bool
    ms: int
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class PrismWorkflow:
    """Runs the top-down recipient loop for one (artifact, recipients, brand) build."""

    def __init__(
        self,
        artifact_id: str,
        recipients: list[str],
        *,
        brand: str = "",
        brand_pins: Optional[dict[str, str]] = None,
        cache_dir: Optional[str | Path] = None,
        provider: Optional[str] = None,
        invoke_fn=None,
    ):
        self.artifact_id = str(artifact_id)
        self.recipients = list(recipients)
        self.brand = brand
        self.brand_pins = brand_pins or {}
        self.provider = provider
        self.invoke_fn = invoke_fn
        root = cache_dir or (okuro_home() / "prism-workflow" / self.artifact_id)
        self.cache = StageCache(root, order=NODE_ORDER)
        self.trace: list[NodeTrace] = []

    # ── node wrappers (cache-aware + traced) ──────────────────────────────────
    def _node(self, node: str, inputs: Any, compute, summarize) -> Any:
        t0 = time.monotonic()
        hit = self.cache.get(node, inputs)
        if hit is not None:
            self.trace.append(NodeTrace(node=node, cached=True,
                                        ms=int((time.monotonic() - t0) * 1000),
                                        summary=summarize(hit)))
            return hit
        out = compute()
        self.cache.put(node, inputs, out)
        self.trace.append(NodeTrace(node=node, cached=False,
                                    ms=int((time.monotonic() - t0) * 1000),
                                    summary=summarize(out)))
        return out

    def node_understand(self) -> Understanding:
        out = self._node(
            "understand", {"artifact_id": self.artifact_id},
            lambda: _understand.understand(
                self.artifact_id, provider=self.provider, invoke_fn=self.invoke_fn).to_dict(),
            lambda d: {"sections": len(d.get("sections") or []),
                       "words": d.get("word_count", 0),
                       "chunks": d.get("chunk_count", 0),
                       "kind": d.get("document_kind", "")},
        )
        return Understanding.from_dict(out)

    def node_recipient(self, understanding: Understanding) -> RecipientBrief:
        inputs = {"recipients": sorted(self.recipients), "brand_pins": self.brand_pins,
                  "brand": self.brand, "understanding": understanding.to_dict()}
        out = self._node(
            "recipient", inputs,
            lambda: _recipient.recipient_brief(
                self.recipients, understanding, brand_pins=self.brand_pins,
                provider=self.provider, invoke_fn=self.invoke_fn).to_dict(),
            lambda d: {"recipients": d.get("recipients") or [],
                       "cares_about": len(d.get("cares_about") or []),
                       "depth_ceiling": d.get("depth_ceiling")},
        )
        return RecipientBrief.from_dict(out)

    def node_topicmap(self, understanding: Understanding, brief: RecipientBrief) -> TopicMap:
        inputs = {"understanding": understanding.to_dict(), "brief": brief.to_dict()}
        out = self._node(
            "topicmap", inputs,
            lambda: _topicmap.topic_map(
                understanding, brief, provider=self.provider,
                invoke_fn=self.invoke_fn).to_dict(),
            lambda d: {"topics": len(d.get("topics") or []),
                       "unresolved": sum(len(t.get("unresolved_refs") or [])
                                         for t in d.get("topics") or [])},
        )
        return TopicMap.from_dict(out)

    def node_gather(self, understanding: Understanding, brief: RecipientBrief,
                    tmap: TopicMap) -> dict[str, TopicGathering]:
        inputs = {"understanding": understanding.to_dict(), "brief": brief.to_dict(),
                  "topic_map": tmap.to_dict()}
        out = self._node(
            "gather", inputs,
            lambda: {tid: g.to_dict() for tid, g in _gather.gather_all(
                tmap.topics, understanding, brief,
                provider=self.provider, invoke_fn=self.invoke_fn).items()},
            lambda d: {"topics": len(d),
                       "facts": sum(len(g.get("facts") or []) for g in d.values()),
                       "empty_topics": sum(1 for g in d.values() if not g.get("facts"))},
        )
        return {tid: TopicGathering.from_dict(g) for tid, g in out.items()}

    def node_author(self, tmap: TopicMap, gatherings: dict[str, TopicGathering],
                    brief: RecipientBrief) -> list[AuthoredTopic]:
        inputs = {"topic_map": tmap.to_dict(), "brief": brief.to_dict(),
                  "gatherings": {k: v.to_dict() for k, v in sorted(gatherings.items())}}
        out = self._node(
            "author", inputs,
            lambda: [t.to_dict() for t in _author.author_all(
                tmap.topics, gatherings, brief, artifact_id=self.artifact_id,
                provider=self.provider, invoke_fn=self.invoke_fn)],
            lambda d: {"topics_authored": len(d),
                       "levels": sorted({lv for t in d for lv in (t.get("levels") or {})}),
                       "l4_words": sum((t.get("l4") or {}).get("word_count", 0) for t in d)},
        )
        return [AuthoredTopic.from_dict(t) for t in out]

    # ── phases ────────────────────────────────────────────────────────────────
    def run_phase_b(self, *, from_node: Optional[str] = None) -> dict[str, Any]:
        """Understand -> recipient -> topic-map. Returns the three IRs plus the trace.

        ``from_node`` re-runs that node and everything after it even on identical
        inputs (to re-roll a non-deterministic model call). Raises
        ``NeedsDisambiguation`` from the recipient node — a caller must resolve it
        with ``brand_pins``, because guessing a reader's brand is never correct.
        """
        if from_node:
            self.cache.invalidate_from(from_node)
        understanding = self.node_understand()
        brief = self.node_recipient(understanding)
        tmap = self.node_topicmap(understanding, brief)
        return {
            "artifact_id": self.artifact_id,
            "understanding": understanding.to_dict(),
            "recipient": brief.to_dict(),
            "topic_map": tmap.to_dict(),
            "trace": [t.to_dict() for t in self.trace],
        }

    def run_phase_c(self, *, from_node: Optional[str] = None) -> dict[str, Any]:
        """Phase B, then gather -> author. Returns every IR plus the trace.

        Phase C's output (``authored``) is Phase D's input: each topic carries four
        levels of authored CONTENT with shapes and real item data, and NO component
        choice — selecting components by reasoning over ``prism_library`` is Phase D.
        """
        if from_node:
            self.cache.invalidate_from(from_node)
        understanding = self.node_understand()
        brief = self.node_recipient(understanding)
        tmap = self.node_topicmap(understanding, brief)
        gatherings = self.node_gather(understanding, brief, tmap)
        authored = self.node_author(tmap, gatherings, brief)
        if len(authored) != len(tmap.topics):
            logger.warning("workflow: %d/%d topics survived authoring",
                           len(authored), len(tmap.topics))
        return {
            "artifact_id": self.artifact_id,
            "understanding": understanding.to_dict(),
            "recipient": brief.to_dict(),
            "topic_map": tmap.to_dict(),
            "gatherings": {k: v.to_dict() for k, v in gatherings.items()},
            "authored": [t.to_dict() for t in authored],
            "trace": [t.to_dict() for t in self.trace],
        }

    def write_trace(self, path: Optional[str | Path] = None) -> Path:
        """Persist the run trace next to the cache. Inspectability, on disk."""
        p = Path(path) if path else (self.cache.root / "trace.json")
        p.write_text(json.dumps([t.to_dict() for t in self.trace],
                                ensure_ascii=False, indent=2))
        return p


def run_phase_b(
    artifact_id: str,
    recipients: list[str],
    *,
    brand: str = "",
    brand_pins: Optional[dict[str, str]] = None,
    cache_dir: Optional[str | Path] = None,
    provider: Optional[str] = None,
    invoke_fn=None,
    from_node: Optional[str] = None,
) -> dict[str, Any]:
    """Module-level convenience entry, mirroring ``compiler.pipeline.compile_deck``."""
    wf = PrismWorkflow(artifact_id, recipients, brand=brand, brand_pins=brand_pins,
                       cache_dir=cache_dir, provider=provider, invoke_fn=invoke_fn)
    return wf.run_phase_b(from_node=from_node)


def run_phase_c(
    artifact_id: str,
    recipients: list[str],
    *,
    brand: str = "",
    brand_pins: Optional[dict[str, str]] = None,
    cache_dir: Optional[str | Path] = None,
    provider: Optional[str] = None,
    invoke_fn=None,
    from_node: Optional[str] = None,
) -> dict[str, Any]:
    """Phase B + gather + author, in one call."""
    wf = PrismWorkflow(artifact_id, recipients, brand=brand, brand_pins=brand_pins,
                       cache_dir=cache_dir, provider=provider, invoke_fn=invoke_fn)
    return wf.run_phase_c(from_node=from_node)
