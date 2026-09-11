# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Generative-workflow registry — persists ComfyUI workflow graphs
#          keyed by (model_key, task), so the UI can answer "is there a workflow
#          for this model?" and the orchestrator can register researched, node-
#          validated workflows it authored. Complements the hand-coded builders
#          in comfy_workflows.py: a builder is just the bootstrap seed; a
#          registered workflow is the durable, per-model, research-grounded one.
#          A stored workflow is a graph + an INPUT MAP (which node.field the
#          prompt/negative/seed/size/ckpt fill), so any authored graph plugs into
#          the runtime without a code change.
# index:
#   _SCHEMA / GenerativeWorkflowRegistry (SQLite, mirrors engine_registry)
#   def required_node_classes            (graph -> node class_types it uses)
#   def missing_nodes                    (required vs a live /object_info)
#   def apply_plan                       (fill a stored graph from an optimizer plan)
# AGENT_HEADER_END -->
"""Generative-workflow registry (okuro-generative-workflows).

The store the ComfyUI generation flow revolves around:

- **Readiness check** — ``exists(model_key, task)`` answers the UI's "there is no
  workflow for SD3.5-large yet" prompt.
- **Authoring target** — the workflow-designer orchestration task registers a
  researched graph here (validated against a live ComfyUI's ``/object_info``).
- **Runtime source** — ``inference.comfy`` resolves a registered workflow, fills
  its declared input slots from the model-conditioned prompt plan, and submits.

Persistence mirrors :class:`~okuro.inference.engine_registry.EngineRegistry`:
the same ``OKURO_INFERENCE_DB`` SQLite file, injectable clock, self-contained
schema. A workflow is stored as ``graph_json`` (a ComfyUI API-format graph) plus
``input_map`` — ``{slot: [node_id, field]}`` — that says where the runtime writes
the prompt/negative/seed/width/height/ckpt. This decouples authored graphs from
hand-coded builders: the orchestrator emits any graph and declares its slots.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional
from okuro.db.engine import okuro_home

DRAFT = "draft"
VALIDATED = "validated"
PUBLISHED = "published"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS generative_workflows (
    workflow_id    TEXT PRIMARY KEY,
    model_key      TEXT NOT NULL,
    task           TEXT NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    graph_json     TEXT NOT NULL,
    input_map      TEXT NOT NULL DEFAULT '{}',
    required_nodes TEXT NOT NULL DEFAULT '[]',
    provenance     TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'draft',
    validated_at   REAL,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_genwf_key ON generative_workflows (model_key, task);
"""


def _default_db_path() -> Path:
    return Path(os.environ.get("OKURO_INFERENCE_DB", str(okuro_home() / "inference.db")))


def required_node_classes(graph: dict) -> list[str]:
    """Distinct ComfyUI node class_types a graph uses (for a node-availability check)."""
    return sorted({n.get("class_type") for n in graph.values() if n.get("class_type")})


def missing_nodes(required: list[str], available: list[str]) -> list[str]:
    """Node classes in ``required`` not present in a live ComfyUI's /object_info keys.

    Drives the orchestration task's "these custom nodes are missing — install
    them?" consent step.
    """
    have = set(available or [])
    return [c for c in required if c not in have]


def apply_plan(
    graph: dict,
    input_map: dict,
    *,
    plan: dict,
    ckpt_name: str,
    seed: int,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> dict:
    """Fill a stored graph's declared input slots from an optimizer ``plan``.

    ``input_map`` is ``{slot: [node_id, field]}``. Recognised slots:
    positive, negative, ckpt, seed, steps, cfg, guidance, sampler, scheduler,
    width, height. The ``sampler`` slot is mapped from the family's friendly
    name to a ComfyUI ``sampler_name`` (and a paired ``scheduler`` if that slot
    is declared); width/height fall back to the plan's resolution bucket. Pure —
    returns a filled deep copy, mutates nothing.
    """
    from okuro.inference.comfy_workflows import _map_sampler, _resolution

    g = copy.deepcopy(graph)
    params = plan.get("params") or {}
    w, h = _resolution(params, width, height)
    sampler_name, scheduler = _map_sampler(params.get("sampler"))
    values = {
        "positive": plan.get("prompt", ""),
        "negative": plan.get("negative", ""),
        "ckpt": ckpt_name,
        "seed": seed,
        "steps": params.get("steps"),
        "cfg": params.get("cfg"),
        "guidance": params.get("guidance"),
        "sampler": sampler_name,
        "scheduler": scheduler,
        "width": w,
        "height": h,
    }
    for slot, target in input_map.items():
        if not (isinstance(target, (list, tuple)) and len(target) == 2):
            continue
        node_id, field = target
        if slot in values and values[slot] is not None and node_id in g:
            g[node_id].setdefault("inputs", {})[field] = values[slot]
    return g


class GenerativeWorkflowRegistry:
    """Durable store of ComfyUI workflows keyed by (model_key, task)."""

    def __init__(
        self,
        db_path: str | os.PathLike | None = None,
        *,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.db_path = Path(db_path) if db_path is not None else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or time.time
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        return con

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in ("graph_json", "input_map", "required_nodes", "provenance"):
            d[k] = json.loads(d[k]) if d.get(k) else ({} if k != "required_nodes" else [])
        d["graph"] = d.pop("graph_json")
        return d

    def register(
        self,
        model_key: str,
        task: str,
        graph: dict,
        *,
        input_map: Optional[dict] = None,
        name: str = "",
        required_nodes: Optional[list[str]] = None,
        provenance: Optional[dict] = None,
        status: str = DRAFT,
        validated_at: Optional[float] = None,
        workflow_id: Optional[str] = None,
    ) -> str:
        """Store (or replace) a workflow. Returns its workflow_id.

        ``required_nodes`` is auto-derived from the graph when omitted. Default
        id is ``<model_key>:<task>`` (one canonical workflow per model+task);
        pass an explicit id to keep variants.
        """
        wid = workflow_id or f"{model_key}:{task}"
        nodes = required_nodes if required_nodes is not None else required_node_classes(graph)
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO generative_workflows "
                "(workflow_id, model_key, task, name, graph_json, input_map, "
                " required_nodes, provenance, status, validated_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (wid, model_key, task, name, json.dumps(graph),
                 json.dumps(input_map or {}), json.dumps(nodes),
                 json.dumps(provenance or {}), status, validated_at, self._clock()),
            )
        return wid

    def get(self, model_key: str, task: str = "text-to-image") -> Optional[dict]:
        """The canonical workflow for a model+task (newest if several), or None."""
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM generative_workflows WHERE model_key=? AND task=? "
                "ORDER BY (status='published') DESC, (status='validated') DESC, created_at DESC "
                "LIMIT 1",
                (model_key, task),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def get_by_id(self, workflow_id: str) -> Optional[dict]:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM generative_workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def exists(self, model_key: str, task: str = "text-to-image") -> bool:
        """The UI readiness check: is a workflow registered for this model+task?"""
        with self._connect() as con:
            return con.execute(
                "SELECT 1 FROM generative_workflows WHERE model_key=? AND task=? LIMIT 1",
                (model_key, task),
            ).fetchone() is not None

    def list(self, *, model_key: Optional[str] = None, task: Optional[str] = None) -> list[dict]:
        clauses, args = [], []
        if model_key:
            clauses.append("model_key=?"); args.append(model_key)
        if task:
            clauses.append("task=?"); args.append(task)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as con:
            return [self._row_to_dict(r) for r in con.execute(
                f"SELECT * FROM generative_workflows{where} ORDER BY created_at DESC", args
            ).fetchall()]

    def mark_validated(self, workflow_id: str, *, status: str = VALIDATED) -> bool:
        with self._connect() as con:
            cur = con.execute(
                "UPDATE generative_workflows SET status=?, validated_at=? WHERE workflow_id=?",
                (status, self._clock(), workflow_id),
            )
            return cur.rowcount > 0

    def delete(self, workflow_id: str) -> bool:
        with self._connect() as con:
            cur = con.execute(
                "DELETE FROM generative_workflows WHERE workflow_id=?", (workflow_id,))
            return cur.rowcount > 0
