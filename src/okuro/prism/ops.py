# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Structured server-side edit ops for a prism facet-tree doc (shared by
#   the prism MCP tools + the chat-edit agent). Pure dict manipulation.
# index: def apply_ops | def _apply_one | def _descendants
# AGENT_HEADER_END -->
"""Apply structured edit ops to a PrismDoc facet-tree dict.

The canonical edit surface: agents (chat, roles, orchestrator) emit a list of
small ops instead of rewriting the whole tree — safer, less drift, cheaper.
Ops operate on a deep copy of the doc dict (returns a new dict). Unknown /
invalid ops are skipped (best-effort) and collected in the returned ``errors``.

Op shapes (``op`` field selects):
  set_meta      {title?, brand_id?, entry_facet_id?}
  add_facet     {parent_id?, facet?: {id?, title?, kind?, rungs?}}   -> first
                facet added to an empty doc becomes the root (parent_id=None,
                entry_facet_id set); subsequent adds require an existing
                parent_id
  remove_facet  {facet_id}          -- cascades to all descendants; refuses
                to remove the root facet (parent_id is None)
  move_facet    {facet_id, new_parent_id}   -- reparent; refuses cycles + root
  set_rung      {facet_id, rung, body, media?, callouts?, blocks?}
  set_title     {facet_id, title}   -- rename a facet (title + headline)
  reorder_children {parent_id?, order:[facet_id...]}  -- reorder a parent's
                children (section order); parent_id omitted → the root
  set_block     {facet_id, rung, index, block}  -- replace one block in place
  move_block    {facet_id, rung, from, to}      -- reorder a block within a rung
  remove_block  {facet_id, rung, index}         -- delete one block
  set_layout    {facet_id, rung, layout|null}   -- pin a manual layout (locked
                against the save-time auto-derive); null clears → back to derived

A Facet = {id, title, kind:'topic'|'diagram'|'decision'|'metric', parent_id,
children:[facet_id...], rungs:{L1|L2|L3|L4: {body, media?,
callouts?}}}. Every facet carries all 4 levels — ``add_facet`` seeds empty
bodies for any rung not supplied.
"""

from __future__ import annotations

import uuid
from typing import Any

from okuro.prism.rungs import RUNGS as _RUNGS, normalize_rung, normalize_rungs
_KINDS = ("topic", "diagram", "decision", "metric")


def _fid() -> str:
    return f"f_{uuid.uuid4().hex[:8]}"


def _empty_rungs() -> dict[str, dict]:
    return {r: {"body": ""} for r in _RUNGS}


def apply_ops(doc: dict[str, Any], ops: list[dict]) -> tuple[dict[str, Any], list[str]]:
    """Apply ops to a copy of the doc. Returns (new_doc, errors)."""
    import copy

    d = copy.deepcopy(doc)
    d.setdefault("facets", {})
    d.setdefault("entry_facet_id", None)
    errors: list[str] = []
    for op in ops or []:
        if not isinstance(op, dict):
            continue
        try:
            _apply_one(d, op)
        except Exception as exc:  # noqa: BLE001 — never let one bad op abort the batch
            errors.append(f"{op.get('op')}: {exc}")
    return d, errors


def _descendants(facets: dict, facet_id: str) -> set[str]:
    """All descendant facet ids of ``facet_id`` (not including itself)."""
    out: set[str] = set()
    stack = list((facets.get(facet_id) or {}).get("children") or [])
    while stack:
        fid = stack.pop()
        if fid in out:
            continue
        out.add(fid)
        stack.extend((facets.get(fid) or {}).get("children") or [])
    return out


def _rung(facets: dict, fid: Any, rung: Any) -> dict:
    """The rung-content dict for (facet, rung), validated. Block ops mutate its
    ``blocks`` list in place."""
    if fid not in facets:
        raise ValueError(f"facet '{fid}' not found")
    rung = normalize_rung(rung)  # accept a legacy rung token from an older caller
    if rung not in _RUNGS:
        raise ValueError(f"unknown rung '{rung}'")
    rc = (facets[fid].get("rungs") or {}).get(rung)
    if not isinstance(rc, dict):
        raise ValueError(f"rung '{rung}' not present on facet '{fid}'")
    return rc


def _apply_one(d: dict, op: dict) -> None:
    kind = op.get("op")
    facets: dict = d["facets"]

    if kind == "set_meta":
        for k in ("title", "brand_id"):
            if op.get(k) is not None:
                d[k] = op[k]
        if op.get("entry_facet_id") is not None:
            eid = op["entry_facet_id"]
            if eid not in facets:
                raise ValueError(f"entry_facet_id '{eid}' not in facets")
            d["entry_facet_id"] = eid

    elif kind == "add_facet":
        parent_id = op.get("parent_id")
        raw = op.get("facet") if isinstance(op.get("facet"), dict) else {}
        fid = str(raw.get("id") or _fid())
        if fid in facets:
            raise ValueError(f"facet '{fid}' already exists")
        fkind = raw.get("kind") if raw.get("kind") in _KINDS else "topic"
        rungs = _empty_rungs()
        raw_rungs = normalize_rungs(raw.get("rungs") if isinstance(raw.get("rungs"), dict) else {})
        for r in _RUNGS:
            rr = raw_rungs.get(r)
            if isinstance(rr, dict):
                rungs[r] = {"body": str(rr.get("body") or "")}
                if rr.get("media") is not None:
                    rungs[r]["media"] = rr["media"]
                if rr.get("callouts") is not None:
                    rungs[r]["callouts"] = rr["callouts"]
                if isinstance(rr.get("blocks"), list):
                    rungs[r]["blocks"] = rr["blocks"]

        if not facets:
            # First facet in an empty tree becomes the root.
            parent_id = None
        elif parent_id is None or parent_id not in facets:
            raise ValueError(f"parent_id '{parent_id}' not in facets")

        facets[fid] = {
            "id": fid,
            "title": str(raw.get("title") or fid),
            "kind": fkind,
            "parent_id": parent_id,
            "children": [],
            "rungs": rungs,
        }
        if parent_id is not None:
            facets[parent_id].setdefault("children", []).append(fid)
        else:
            d["entry_facet_id"] = fid

    elif kind == "remove_facet":
        fid = op.get("facet_id")
        if fid not in facets:
            raise ValueError(f"facet '{fid}' not found")
        if facets[fid].get("parent_id") is None:
            raise ValueError("cannot remove the root facet")
        parent_id = facets[fid]["parent_id"]
        for dead in _descendants(facets, fid) | {fid}:
            facets.pop(dead, None)
        if parent_id in facets:
            facets[parent_id]["children"] = [
                c for c in facets[parent_id].get("children") or [] if c != fid
            ]

    elif kind == "move_facet":
        fid, new_parent = op.get("facet_id"), op.get("new_parent_id")
        if fid not in facets:
            raise ValueError(f"facet '{fid}' not found")
        if facets[fid].get("parent_id") is None:
            raise ValueError("cannot move the root facet")
        if new_parent not in facets:
            raise ValueError(f"new_parent_id '{new_parent}' not in facets")
        if new_parent == fid or new_parent in _descendants(facets, fid):
            raise ValueError("move would create a cycle")
        old_parent = facets[fid]["parent_id"]
        if old_parent in facets:
            facets[old_parent]["children"] = [
                c for c in facets[old_parent].get("children") or [] if c != fid
            ]
        facets[fid]["parent_id"] = new_parent
        facets[new_parent].setdefault("children", []).append(fid)

    elif kind == "set_rung":
        fid, rung = op.get("facet_id"), normalize_rung(op.get("rung"))
        if fid not in facets:
            raise ValueError(f"facet '{fid}' not found")
        if rung not in _RUNGS:
            raise ValueError(f"unknown rung '{rung}'")
        existing = (facets[fid].get("rungs") or {}).get(rung) or {}
        entry = {"body": str(op.get("body") or "")}
        if op.get("media") is not None:
            entry["media"] = op["media"]
        if op.get("callouts") is not None:
            entry["callouts"] = op["callouts"]
        # Preserve rich visual blocks across a text-only set_rung (e.g. retailor
        # recasts prose but must not drop a stat/table module), unless the op
        # explicitly sets blocks.
        if op.get("blocks") is not None:
            entry["blocks"] = op["blocks"]
        elif existing.get("blocks"):
            entry["blocks"] = existing["blocks"]
        facets[fid].setdefault("rungs", _empty_rungs())[rung] = entry

    elif kind == "set_title":
        fid = op.get("facet_id")
        if fid not in facets:
            raise ValueError(f"facet '{fid}' not found")
        title = str(op.get("title") or "").strip()
        if not title:
            raise ValueError("title required")
        facets[fid]["title"] = title
        if facets[fid].get("headline"):     # keep the rendered headline in sync
            facets[fid]["headline"] = title

    elif kind == "reorder_children":
        pid = op.get("parent_id")
        if pid is None:
            pid = next((fid for fid, f in facets.items() if f.get("parent_id") is None), None)
        if pid not in facets:
            raise ValueError(f"parent '{pid}' not found")
        order = op.get("order")
        if not isinstance(order, list):
            raise ValueError("reorder_children requires an 'order' list")
        cur = list(facets[pid].get("children") or [])
        if sorted(map(str, order)) != sorted(map(str, cur)):
            raise ValueError("order must be a permutation of the current children")
        facets[pid]["children"] = [str(x) for x in order]

    elif kind == "set_block":
        rc = _rung(facets, op.get("facet_id"), op.get("rung"))
        blocks = rc.get("blocks") or []
        i = op.get("index")
        if not isinstance(i, int) or not (0 <= i < len(blocks)):
            raise ValueError(f"block index {i} out of range (0..{len(blocks) - 1})")
        if not isinstance(op.get("block"), dict) or not op["block"].get("type"):
            raise ValueError("set_block requires a 'block' dict with a type")
        blocks[i] = op["block"]
        rc["blocks"] = blocks

    elif kind == "move_block":
        rc = _rung(facets, op.get("facet_id"), op.get("rung"))
        blocks = rc.get("blocks") or []
        frm, to = op.get("from"), op.get("to")
        if not (isinstance(frm, int) and isinstance(to, int)
                and 0 <= frm < len(blocks) and 0 <= to < len(blocks)):
            raise ValueError(f"from/to out of range (0..{len(blocks) - 1})")
        b = blocks.pop(frm)
        blocks.insert(to, b)
        rc["blocks"] = blocks

    elif kind == "remove_block":
        rc = _rung(facets, op.get("facet_id"), op.get("rung"))
        blocks = rc.get("blocks") or []
        i = op.get("index")
        if not isinstance(i, int) or not (0 <= i < len(blocks)):
            raise ValueError(f"block index {i} out of range (0..{len(blocks) - 1})")
        blocks.pop(i)
        rc["blocks"] = blocks

    elif kind == "set_layout":
        rc = _rung(facets, op.get("facet_id"), op.get("rung"))
        layout = op.get("layout")
        if layout is None:
            rc.pop("layout", None)
            rc.pop("layout_locked", None)     # back to the auto-derived layout
        elif isinstance(layout, dict):
            rc["layout"] = layout
            rc["layout_locked"] = True        # pin against the save-time re-derive
        else:
            raise ValueError("set_layout 'layout' must be a dict or null")

    else:
        raise ValueError(f"unknown op '{kind}'")
