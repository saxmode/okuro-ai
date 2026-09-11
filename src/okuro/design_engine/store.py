# SPDX-License-Identifier: Apache-2.0
"""Where a builder's brand lives -- and where it must never live.

    "OKURO SHIPS EXACTLY TWO THINGS in the design layer: the BASE DESIGN SYSTEM
    ... and okuro-ds, okuro's OWN brand kit. THAT IS ALL."

A brand a builder authors is that builder's, not okuro's. It goes to the user
store under ``~/.okuro/design-engine/kits`` and never into the package tree,
which is git-tracked and shipped. That is not tidiness: two customers' brand
kits reached a public repository once by exactly this route, through a save
path that defaulted to the package directory.

So the rule here is not a default that can be overridden -- it is a REFUSAL.
:func:`save` writes to the user store, always, and raises rather than write
over a shipped id. A builder who wants to start from okuro-ds forks it under a
new name; the fork is theirs and the shipped kit stays byte-identical.

An id is a FILENAME, so it validates as a slug before it becomes a path. That
check lives in this module rather than at the route, because every caller --
route, CLI, test, a future MCP tool -- goes through `save` and none of them may
skip it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from pydantic import BaseModel

from . import kits as shipped
from .schema import Brand
from okuro.db.engine import okuro_home

_SAME = object()
"""Sentinel: this field does not override its base. Distinct from `None`, which
is a value a brand can legitimately store."""


def _as_json(value):
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value

KIT_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_KIT_ID = 64

ENV_PATH = "OKURO_DESIGN_ENGINE_DIR"
"""Points the store somewhere else. Exists so a test never touches a real
user's brands -- the design_systems suite learned that the expensive way."""


class KitExists(ValueError):
    """A create would overwrite something that already exists."""


class ShippedKit(ValueError):
    """The id belongs to a kit okuro ships. Fork it instead."""


def store_dir() -> Path:
    """The user store. Created on demand, never inside the package."""
    override = os.environ.get(ENV_PATH)
    root = Path(override) if override else okuro_home() / "design-engine"
    return root / "kits"


def _validate_id(kit_id: str) -> str:
    if not kit_id or len(kit_id) > MAX_KIT_ID or not KIT_ID.match(kit_id):
        raise ValueError(
            f"kit id {kit_id!r} is not a slug; an id becomes a filename, so it "
            f"must match {KIT_ID.pattern} and be at most {MAX_KIT_ID} characters"
        )
    return kit_id


def _path(kit_id: str) -> Path:
    return store_dir() / f"{_validate_id(kit_id)}.json"


def shipped_ids() -> tuple[str, ...]:
    return tuple(sorted(shipped.KITS))


def list_kits() -> list[dict]:
    """Every brand this machine can open: the shipped ones, then the user's.

    `origin` is carried because it is the difference between editing okuro's
    repository and editing your own files, and that was previously knowable
    only by reading the source.
    """
    out: list[dict] = [
        {"id": kit_id, "origin": "package", "editable": False}
        for kit_id in shipped_ids()
    ]
    directory = store_dir()
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            out.append({"id": path.stem, "origin": "user", "editable": True})
    return out


RETIRED_SLOTS = ("base", "root_percent")
"""Fields a brand used to author and no longer may.

His ruling of 2026-08-17 made both system constants -- "Brands do not change
their base. Base is always the same." A brand saved before that carries them,
and `Brand` forbids extra keys, so loading one would raise instead of opening.

Dropping rather than translating is the whole migration, and it is lossless by
construction: both fields only ever held the values that are now the constants,
and a brand that had moved one is a brand whose sizes the ruling redefined
anyway. The file is rewritten without them the next time it is saved.
"""


def migrate(payload: dict) -> dict:
    """A stored brand, brought to the current schema. Pure, and idempotent."""
    if not any(slot in payload for slot in RETIRED_SLOTS):
        return payload
    return {k: v for k, v in payload.items() if k not in RETIRED_SLOTS}


BASE_KIT = "okuro-ds"
"""The kit every authored brand is a difference FROM.

His ruling, 2026-09-07: *"only overwrites need to be stored."* It is the
product model already in the charter, stated about files instead of about the
UI -- "okuro-ds is shipping as a default, which is not editable. If I want a
custom system, I need to duplicate. Inside the duplicated version I can adjust."
A duplicate that copies every value is not a duplicate that ADJUSTS some of
them; it is a snapshot, and a snapshot stops tracking the thing it came from.

WHAT IT COST BEFORE THIS EXISTED, measured 2026-09-06: `--shadow-xl` emitted a
value smaller than every rung below it. The defect was fixed in the schema
default and reached NOTHING, because the shipped kit authors its own effects and
every user kit had inherited them as literals at fork time. Three kits on disk,
three frozen copies, and an engine fix that could not reach any of them.
"""


def _model_diff(value, base) -> object:
    """What `value` overrides in `base`, or `_SAME`. Structure-aware, both ways.

    IT WALKS THE MODEL, NOT THE DICT, and that is the whole correctness
    argument. A `Brand` contains two kinds of dict and they cannot be treated
    alike:

      STRUCTURAL -- a nested `_Strict` model. Its key set is fixed by the
      schema, so descending into it is safe: a key can never be missing, and a
      sparse patch can always be merged back without ambiguity.

      FREE-FORM -- a mapping a brand fills in, like `font.faces` or
      `motion.speeds`. Its key set is DATA. Descend into one and a removed key
      becomes unrepresentable: the merge would restore it from the base and
      nobody would ever see the difference.

    So a mapping is a LEAF -- identical, or stored whole. That is why this takes
    the pydantic objects rather than their dumps; `model_fields` is the only
    thing that knows which of the two a given dict is.
    """
    if isinstance(value, BaseModel) and isinstance(base, BaseModel):
        if type(value) is not type(base):
            return value.model_dump(mode="json")
        out: dict = {}
        for name in type(value).model_fields:
            sub = _model_diff(getattr(value, name), getattr(base, name))
            if sub is not _SAME:
                out[name] = sub
        return out or _SAME
    dumped = _as_json(value)
    return _SAME if dumped == _as_json(base) else dumped


def _model_merge(patch: dict, base: BaseModel) -> dict:
    """The base's payload with the overrides applied. The inverse of the diff.

    Mirrors `_model_diff` field by field off the same `model_fields`, so it
    descends exactly where the diff descended and replaces exactly where the
    diff stored a leaf whole.

    A FULL PAYLOAD MERGES TO ITSELF, which is what makes this backward
    compatible with every kit written before the ruling: those files carry every
    key, so each one wins over its base and the merged result is byte-identical
    to what the old loader produced. They simply become sparse the next time
    they are saved -- the same "rewritten on next save" contract `RETIRED_SLOTS`
    already uses.
    """
    out = base.model_dump(mode="json")
    fields = type(base).model_fields
    for name, override in patch.items():
        current = getattr(base, name, None)
        if (
            name in fields
            and isinstance(current, BaseModel)
            and isinstance(override, dict)
        ):
            out[name] = _model_merge(override, current)
        else:
            out[name] = override
    return out


def load(kit_id: str) -> Brand:
    """One brand, shipped or authored.

    The shipped path goes through `kits.get`, so okuro-ds is always the kit
    module's version and never a stale copy someone saved beside it.
    """
    if kit_id in shipped.KITS:
        return shipped.get(kit_id).brand
    path = _path(kit_id)
    if not path.is_file():
        raise FileNotFoundError(
            f"no brand {kit_id!r}; shipped are {list(shipped_ids())} and the "
            f"user store is {store_dir()}"
        )
    payload = migrate(json.loads(path.read_text("utf-8")))
    # THE STORED FILE IS A DIFFERENCE, so the base is what it is a difference
    # FROM. `id` is the one field that must survive the merge as the file's own
    # -- everything else may legitimately be inherited, but a kit that adopted
    # the base's id would shadow the shipped kit.
    merged = _model_merge(payload, shipped.get(BASE_KIT).brand)
    merged["id"] = payload.get("id", kit_id)
    return Brand.model_validate(merged)


def save(brand: Brand, *, create: bool = False) -> Path:
    """Write a brand to the USER store. Never anywhere else.

    `create=True` refuses an existing file. One route cannot mean both: on a
    save an overwrite is the point, on a create it is a brand destroyed by a
    typo with no undo.
    """
    kit_id = _validate_id(brand.id)
    if kit_id in shipped.KITS:
        raise ShippedKit(
            f"{kit_id!r} is a kit okuro ships; saving it here would shadow the "
            "package's own version invisibly. Fork it under a different id -- "
            "the fork is yours and the shipped kit stays untouched."
        )
    directory = store_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{kit_id}.json"
    if create and path.exists():
        raise KitExists(f"a brand {kit_id!r} already exists at {path}")
    # ONLY THE OVERRIDES. Everything this brand shares with okuro-ds stays
    # unwritten, so an engine change to a shared value reaches this kit instead
    # of being shadowed by a copy of the old one.
    overrides = _model_diff(brand, shipped.get(BASE_KIT).brand)
    payload = {} if overrides is _SAME else dict(overrides)
    payload["id"] = brand.id
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def delete(kit_id: str) -> None:
    if kit_id in shipped.KITS:
        raise ShippedKit(f"{kit_id!r} is shipped and cannot be deleted")
    _path(kit_id).unlink(missing_ok=True)
