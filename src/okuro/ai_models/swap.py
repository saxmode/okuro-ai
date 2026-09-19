# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Swap plans — "replace model X in consumer Y with Z" expressed as a
#          set of PROPOSED line edits that nothing in this repo executes,
#          gated per consumer by a manual checklist.
# index:
#   def feature_off / checklist_for   (the gate — config first, tier default)
#   class NewSubject                  (what is swapped IN — a unit, or a release)
#   def rewrite_ref                   (the SAME-SHAPE rule, one arm per match_kind)
#   def line_diff                     (the unified diff of one line)
#   def propose                       (rows + blockers + checklist; writes no config)
#   def tick / reject / apply         (the state machine)
#   def list_plans / show / swap_plan (the one call CLI / API / MCP render)
# AGENT_HEADER_END -->
"""The swap plan: which line to edit, what to put there, and who says when.

Plan v1 §4 is the constraint the whole module is built around — the manager
"does not auto-edit consumer configs. It shows the exact ``file:line`` and the
diff." There is therefore **no executor anywhere in this repo**. ``apply``
records that a person applied it and PRINTS the diff to apply by hand. A
``state`` column that reaches ``applied`` with no writer is the safety
property, not an unfinished feature.

Four properties shape it.

**The unit of review is the LINE.** One swap on this host touches nine lines
across five files in three different reference SHAPES. A person opens lines,
not models, and each line's rewrite can fail on its own — a line naming a
shared parent DIRECTORY cannot be repointed without moving every model
underneath it, and saying so per line is the finding.

**A rewrite keeps the shape it found.** An absolute path stays absolute; a
path written the way a *container* sees it stays a container path; a
bucket-relative suffix keeps its segment count; a tm-inference alias stays an
alias. Rewriting a container path to a host path produces a line that looks
right to a shell and is unopenable by the process that reads it.

**The gate is the human, by ruling.** Ruling 7: a PROTECTED consumer's plan
cannot reach ``applied`` until every checklist item is ticked. Ruling 8: there
is no headroom FLOOR — a shortfall warns and never refuses.

**A prediction is labelled as one.** A release is not downloaded, so the path
it would occupy does not exist. The plan still shows the line it would become,
and ``rewrite_note`` says the destination is predicted rather than measured.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

#: Config key holding per-consumer checklists: ``{consumer: [item, ...]}``.
CHECKLIST_KEY = "ai_models.swap_checklists"

STATES = ("proposed", "testing", "applied", "rejected")
MODES = ("replace", "side-by-side")
KINDS = ("unit", "release")

#: The tiers, strongest first. Mirrors P2 rather than re-deriving it.
_TIER_RANK = {"PROTECTED": 3, "ACTIVE": 2, "ARCHIVE": 1}

FEATURE_OFF_REASON = (
    "no consumer roots configured — a swap plan is a set of edits to a "
    "consumer's config, so there is nothing to plan against. Set "
    "conventions.ai_models.consumer_roots in ~/.okuro/config.yaml."
)

#: The owner placeholder the shipped defaults carry. Substituted at propose
#: time with the profile's name, because a person's name is HOST data: it is
#: what the CONTENT commit guard exists to keep out of this tree, and it is
#: the same rule P3 follows for GPU names.
OWNER = "{owner}"

#: The gate a PROTECTED consumer gets with NO configuration at all. Four
#: items, and it is deliberately not one: falling through to the one-item
#: ACTIVE default would mean that adding a consumer to `protected_consumers`
#: silently weakened its gate, which is the opposite of what that list says.
#:
#: The wording is generic BY RULE. A checklist that named this host's own
#: tools and pipelines would put host data in the repo, which is what the
#: CONTENT commit guard exists to stop, and what P1/P2/P3 already settled the
#: same way for store paths, consumer roots and GPU names.
#: That wording belongs in :data:`CHECKLIST_KEY`, per consumer, where it can
#: say exactly what has to be run without this repo knowing the tool's name.
PROTECTED_DEFAULT: list[str] = [
    "10 reference outputs produced with the new model",
    "side-by-side compared with the current model",
    "this consumer's pipeline run end to end",
    f"approved by {OWNER}",
]

ACTIVE_DEFAULT: list[str] = [f"confirmed by {OWNER}"]

# --- blocker codes ----------------------------------------------------------
# severity 'block' refuses an apply; severity 'warn' never does (ruling 8).
B_NOT_DOWNLOADED = "new-not-downloaded"
B_UNREACHABLE = "new-unreachable"
B_HEADROOM = "headroom"

_GiB = 1024 ** 3


def feature_off() -> dict:
    """A FRESH feature-off result each call — callers render it and mutate it."""
    return {"configured": False, "reason": FEATURE_OFF_REASON, "plans": [],
            "totals": {"plans": 0}}


def _fail(reason: str, **extra: Any) -> dict:
    out = {"ok": False, "reason": reason}
    out.update(extra)
    return out


# --- the checklist ----------------------------------------------------------


def _owner_name() -> str:
    """What to call the person who approves. Profile first, never hardcoded."""
    try:
        from okuro.yu.profile import get_profile_raw

        ident = (get_profile_raw() or {}).get("identity") or {}
        name = ident.get("name") or ident.get("handle")
        if name:
            return str(name)
    except Exception:  # a missing profile must never block a plan
        pass
    return "the owner"


def _declared_checklists() -> dict[str, list[str]]:
    """``conventions.ai_models.swap_checklists``, or ``{}``."""
    try:
        from okuro.yu.conventions import get_convention

        raw = get_convention(CHECKLIST_KEY, {}) or {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        log.warning("%s must be a mapping of {consumer: [items]}", CHECKLIST_KEY)
        return {}
    out: dict[str, list[str]] = {}
    for consumer, items in raw.items():
        if isinstance(items, list) and items:
            out[str(consumer)] = [str(i) for i in items]
        else:
            log.warning("%s.%s is not a non-empty list — ignoring",
                        CHECKLIST_KEY, consumer)
    return out


def checklist_for(consumer: str, tier: str) -> list[str]:
    """The gate for one consumer: the declared list, else the tier default.

    A declared list REPLACES the default rather than extending it — a
    half-overridden safety checklist is worse than either version, because
    nobody can say from the screen which items the host meant.
    """
    owner = _owner_name()
    items = (_declared_checklists().get(consumer)
             or (PROTECTED_DEFAULT if tier == "PROTECTED" else ACTIVE_DEFAULT))
    return [i.replace(OWNER, owner) for i in items]


def _fresh_checklist(consumer: str, tier: str) -> list[dict]:
    return [{"item": i, "done": False, "done_at": None}
            for i in checklist_for(consumer, tier)]


# --- what is being swapped in ----------------------------------------------


@dataclass
class NewSubject:
    """The model a plan swaps IN, whether or not it is on this host."""

    kind: str                              # unit | release
    ref: str                               # canonical id: unit_id or HF id
    given: str                             # what the caller typed
    unit: Optional[dict] = None            # model_units row, when kind == unit
    size_bytes: int = 0
    rel_path: Optional[str] = None
    abs_paths: list[str] = field(default_factory=list)
    primary_file: Optional[str] = None     # absolute path of the biggest weights file
    store: Optional[str] = None
    store_tier: Optional[str] = None

    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / _GiB, 2)

    @property
    def basename(self) -> str:
        """The filename a line would name. Falls back to the last path segment."""
        if self.primary_file:
            return os.path.basename(self.primary_file)
        if self.rel_path:
            return os.path.basename(str(self.rel_path).rstrip("/"))
        return os.path.basename(str(self.ref).rstrip("/"))

    def name_for_parse(self) -> str:
        """The best name to hand the lineage parser."""
        if self.kind == "unit" and self.unit is not None:
            from okuro.ai_models import lineage as L

            cands = L.name_candidates(self.unit)
            return cands[0] if cands else str(self.ref)
        return str(self.ref)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "ref": self.ref, "given": self.given,
                "size_bytes": self.size_bytes, "size_gb": self.size_gb,
                "rel_path": self.rel_path, "store": self.store,
                "store_tier": self.store_tier, "basename": self.basename}


def _release_size_gb(hf_id: str) -> float:
    """What a discovery row already says this release weighs, or 0.0.

    Read from ``model_discoveries`` — the weekly scan already measured it.
    Never guessed: a headroom line carrying an invented number is worse than
    no headroom line, and ``0.0`` is what makes the blocker fall away.
    """
    try:
        from okuro.db import get_db

        row = get_db().fetchone(
            "SELECT min_vram_gb FROM model_discoveries WHERE catalog_id = ? "
            "OR display_name = ?", (hf_id, hf_id))
        if row and row["min_vram_gb"]:
            return float(row["min_vram_gb"])
    except Exception:
        pass
    return 0.0


def resolve_new(new: str, kind: Optional[str] = None) -> NewSubject:
    """A unit when one resolves, a release otherwise.

    Auto-detection is a LOOKUP, not a guess about the string: if the host has
    a unit by that id, path or name, the swap is installed-to-installed;
    otherwise it is a release that has to be pulled first. ``kind`` forces the
    answer for callers who already know.
    """
    from okuro.ai_models import lineage as L

    if kind != "release":
        row, _reason = L.find_unit(str(new))
        if row is not None:
            row = L.enrich_unit(dict(row))
            places = _placements_of(row["unit_id"])
            tier = None
            try:
                from okuro.ai_models.store_scan import resolve_store

                store = resolve_store(str(row.get("store") or ""))
                tier = store.tier if store else None
            except Exception:
                pass
            return NewSubject(
                kind="unit", ref=row["unit_id"], given=str(new), unit=row,
                size_bytes=int(row.get("size_bytes") or 0),
                rel_path=row.get("rel_path"), abs_paths=places,
                primary_file=row.get("primary_file"),
                store=row.get("store"), store_tier=tier)
        if kind == "unit":
            raise LookupError(_reason or f"no unit matches {new!r}")

    return NewSubject(kind="release", ref=str(new), given=str(new),
                      size_bytes=int(_release_size_gb(str(new)) * _GiB))


def _placements_of(unit_id: str) -> list[str]:
    from okuro.db import get_db

    rows = get_db().fetchall(
        "SELECT abs_path FROM model_placements WHERE unit_id = ? "
        "ORDER BY abs_path", (unit_id,))
    return [r["abs_path"] for r in rows]


# --- the rewrite ------------------------------------------------------------


def _to_host(ref: str, mount: Optional[tuple[str, str]]) -> str:
    """A reference written the consumer's way, as this host writes it."""
    if mount and ref.startswith(mount[1].rstrip("/") + "/"):
        return mount[0].rstrip("/") + ref[len(mount[1].rstrip("/")):]
    return ref


def _to_consumer(host_path: str, mount: Optional[tuple[str, str]],
                 was_container: bool) -> str:
    """Back into the shape the line already used."""
    if was_container and mount:
        host = mount[0].rstrip("/")
        if host_path == host or host_path.startswith(host + "/"):
            return mount[1].rstrip("/") + host_path[len(host):]
    return host_path


def _predicted_destination(new: NewSubject, old_unit: dict) -> Optional[str]:
    """Where a P6 pull of an undownloaded release would land it.

    A PREDICTION, and every caller labels it as one. The rule is the only one
    that can be stated without guessing: the hot store, the bucket the model
    it replaces already sits in, the release's own basename.

    It is now ONE function with the pull that will actually do it —
    :func:`okuro.ai_models.acquire.destination_for`, called here with the
    bucket of the unit being replaced. P5 shipped this rule twice by hand and
    flagged the duplication as an open question, because a prediction that
    drifts from the real destination makes every swap diff quietly wrong.
    """
    try:
        from okuro.ai_models.acquire import destination_for

        dest, _why = destination_for(
            new.ref,
            bucket=os.path.dirname(str(old_unit.get("rel_path") or "")).strip("/"))
        return str(dest.path) if dest else None
    except Exception:
        return None


def _new_abs_for(new: NewSubject, old_unit: dict, mount: Optional[tuple[str, str]]
                 ) -> tuple[Optional[str], bool]:
    """``(host path of the new model, is_predicted)``.

    Prefers a placement the consumer's own mount can see, so a swap onto a
    model that exists on both stores rewrites to the copy the consumer can
    actually open rather than to whichever row sorted first.
    """
    if new.kind == "release" or not new.abs_paths:
        return _predicted_destination(new, old_unit), True
    if mount:
        host = mount[0].rstrip("/")
        inside = [p for p in new.abs_paths
                  if p == host or p.startswith(host + "/")]
        if inside:
            return inside[0], False
    return new.abs_paths[0], False


def rewrite_ref(old_ref: str, match_kind: Optional[str], old_unit: dict,
                new: NewSubject, mount: Optional[tuple[str, str]]
                ) -> tuple[Optional[str], Optional[str]]:
    """The new reference, in the SAME SHAPE — ``(new_ref, note)``.

    ``new_ref`` is None exactly when the line cannot be rewritten
    mechanically, and ``note`` then says why. A note beside a non-None
    ``new_ref`` is a caveat about the rewrite, not a refusal.
    """
    raw = old_ref.strip()

    # A line that names a shared parent DIRECTORY is the one honest "no". The
    # directory holds several models; repointing it moves every one of them,
    # which is a different decision from swapping this model and is not
    # something a plan may take on the person's behalf.
    if match_kind in ("dir", "dir-contains"):
        return None, (
            f"this line names a directory ({raw}) that holds more than this "
            f"model — repointing it would move every model underneath it, "
            f"which is a different decision. Edit the line that names the "
            f"model itself.")

    if match_kind == "abs-path":
        was_container = bool(mount and raw.startswith(mount[1].rstrip("/") + "/"))
        host_old = _to_host(raw, mount).rstrip("/")
        new_abs, predicted = _new_abs_for(new, old_unit, mount)
        if not new_abs:
            return None, ("the new model is a release with no download yet and "
                          "this host declares no hot store, so no destination "
                          "path can be named")
        # The line may name a FILE INSIDE the old unit rather than the unit.
        remainder = ""
        for place in sorted(_placements_of(old_unit["unit_id"]), key=len,
                            reverse=True):
            base = place.rstrip("/")
            if host_old == base:
                break
            if host_old.startswith(base + "/"):
                remainder = host_old[len(base) + 1:]
                break
        note = None
        if remainder:
            candidate = os.path.join(new_abs, remainder)
            if not predicted and not os.path.lexists(candidate):
                return None, (
                    f"the line names {remainder} inside the old model and the "
                    f"new model has no such file — this reference needs a "
                    f"human decision, not a path substitution")
            new_abs = candidate
            note = (f"kept the path below the model ({remainder}) — verify it "
                    f"is the right file in the new model")
        if predicted:
            pred = (f"predicted destination: {new.ref} is not downloaded, so "
                    f"this is where a pull into the hot store would land it. "
                    f"It is a prediction, not a measurement.")
            note = f"{note}. {pred}" if note else pred
        return _to_consumer(new_abs, mount, was_container), note

    if match_kind == "path-suffix":
        lead = "/" if raw.startswith("/") else ""
        want = len([s for s in raw.strip("/").split("/") if s])
        source = new.rel_path if new.kind == "unit" else str(new.ref)
        segs = [s for s in str(source or "").strip("/").split("/") if s]
        if not segs:
            return None, f"the new model has no path to take {want} segment(s) from"
        take = segs[-want:] if want <= len(segs) else segs
        note = None
        if want > len(segs):
            note = (f"the reference is {want} segments deep and the new "
                    f"model's path has only {len(segs)} — wrote all of them")
        if new.kind == "release":
            note = ((note + ". ") if note else "") + (
                f"{new.ref} is not downloaded; this suffix is taken from its "
                f"name, not from a path on disk")
        return lead + "/".join(take), note

    if match_kind == "filename":
        name = new.basename
        if not name:
            return None, "the new model has no weights file to name"
        note = None
        if new.kind == "release":
            note = (f"{new.ref} is not downloaded; this is its release name, "
                    f"not a filename measured on disk")
        return name, note

    if match_kind == "alias":
        from okuro.ai_models import consumers as C

        alias = C.registry_alias(new.basename)
        if not alias:
            return None, "no tm-inference alias can be derived from the new name"
        return alias, ("the tm-inference registry generates this alias from the "
                       "filename on its next scan — writing it by hand only "
                       "matches if the file lands under a scanned root")

    if match_kind == "hf-name":
        sep = "--" if "--" in raw else "/"
        target = str(new.ref)
        if new.kind == "unit":
            # An installed unit has no HuggingFace id. The best a rewrite can
            # do is its own name, and saying so is better than inventing an org.
            return None, ("this line names a HuggingFace repo and the new "
                          "model is a local unit with no HuggingFace id — "
                          "write the local path or alias the consumer expects")
        return target.replace("/", sep) if sep == "--" else target, None

    return None, (f"P2 recorded no match kind for this reference, so its shape "
                  f"is unknown and a rewrite would be a guess")


# --- the diff ---------------------------------------------------------------


def read_line(config_path: str, line: int) -> Optional[str]:
    """The text at ``config_path:line``, or None when it cannot be read."""
    try:
        with open(config_path, "r", encoding="utf-8", errors="replace") as fh:
            for n, text in enumerate(fh, 1):
                if n == line:
                    return text.rstrip("\n")
    except OSError:
        return None
    return None


def line_diff(config_path: str, line: int, old_ref: str, new_ref: str
              ) -> tuple[Optional[str], Optional[str]]:
    """``(unified diff of that one line, why_not)``.

    The file is read, never written. A line whose text no longer contains the
    reference P2 recorded is reported as stale rather than rewritten blind —
    editing by line number against a file that has moved on is how a plan
    corrupts a config it was built to protect.
    """
    text = read_line(config_path, line)
    if text is None:
        return None, (f"{config_path}:{line} cannot be read — the file is gone "
                      f"or shorter than the consumer map remembers")
    if old_ref not in text:
        return None, (f"{config_path}:{line} no longer contains {old_ref!r} — "
                      f"the consumer map is stale. Re-run "
                      f"`okuro models consumers --refresh` and propose again.")
    new_text = text.replace(old_ref, new_ref, 1)
    # Both sides name the SAME absolute path. This is an edit to a file in
    # place, not a patch against a checkout, and `a/` + `b/` prefixes on an
    # absolute path produce `a/home/...`, which is a path that exists nowhere.
    diff = difflib.unified_diff(
        [text + "\n"], [new_text + "\n"],
        fromfile=config_path, tofile=config_path, lineterm="\n", n=0)
    out = list(diff)
    # difflib numbers a one-line hunk from 1; the real line number is what a
    # person needs in order to find it.
    out = [f"@@ -{line} +{line} @@\n" if s.startswith("@@") else s for s in out]
    return "".join(out), None


# --- blockers ---------------------------------------------------------------


def _hot_free_gb() -> tuple[Optional[float], Optional[str]]:
    """``(free GB on the first hot store, its name)`` — measured, now.

    Asks P1's store for it rather than calling ``disk_usage`` here: the
    inventory page shows the same number on its store bar, and two
    measurements of one disk are how a warning and a bar start disagreeing.
    """
    try:
        from okuro.ai_models.store_scan import configured_stores

        for store in configured_stores():
            if store.tier != "hot":
                continue
            free_gb, _total = store.free()
            if free_gb is not None:
                return free_gb, store.name
    except Exception as exc:
        log.debug("hot-store free space unavailable: %s", exc)
    return None, None


def compute_blockers(new: NewSubject, mode: str, root: Any,
                     placements: Optional[dict] = None) -> list[dict]:
    """Every reason this plan is not ready, each with a severity.

    ``block`` refuses an apply. ``warn`` never does — ruling 8 removed the
    headroom floor, so a shortfall is something the person is told and then
    decides about.
    """
    from okuro.ai_models import consumers as C

    out: list[dict] = []

    if new.kind == "release":
        out.append({
            "code": B_NOT_DOWNLOADED, "severity": "block", "resolved": False,
            "text": (f"new is a release, not downloaded — {new.ref} has to be "
                     f"pulled before any line can point at it"),
        })
    else:
        places = placements if placements is not None else C._load_placements()
        ok, why = C.reachable_for(new.ref, root, places)
        if not ok:
            out.append({
                "code": B_UNREACHABLE, "severity": "block", "resolved": False,
                "text": (f"new unit not reachable for this consumer's mount: "
                         f"{why}"),
            })

    # Headroom. Needed when the new bytes have to land beside what is already
    # on the hot store: always for side-by-side (both models stay), and for a
    # replace whose new model is not on a hot store yet.
    need = 0.0
    if new.size_gb > 0 and (mode == "side-by-side" or new.store_tier != "hot"):
        need = new.size_gb
    if need > 0:
        free, store_name = _hot_free_gb()
        if free is not None:
            label = store_name or "hot store"
            if free < need:
                text = (f"headroom: {label} free {free:,.1f} GB < new size "
                        f"{need:,.1f} GB")
            elif mode == "side-by-side":
                text = (f"headroom: {label} free {free:,.1f} GB, new size "
                        f"{need:,.1f} GB — side-by-side keeps both models, so "
                        f"the new bytes are added, not exchanged")
            else:
                text = (f"headroom: {label} free {free:,.1f} GB, new size "
                        f"{need:,.1f} GB")
            out.append({"code": B_HEADROOM, "severity": "warn",
                        "resolved": False, "text": text})
    return out


def _blocking(blockers: list[dict]) -> list[dict]:
    return [b for b in blockers
            if b.get("severity") != "warn" and not b.get("resolved")]


# --- propose ----------------------------------------------------------------


def _root_for(consumer: str, config_path: str):
    """The declared root that owns this consumer's config file."""
    from okuro.ai_models import consumers as C

    return C.root_for(consumer, config_path)


def _plan_group() -> str:
    return "sw-" + uuid.uuid4().hex[:8]


def propose(consumer: str, unit_old: str, new: str, *, mode: str = "replace",
            kind: Optional[str] = None, note: Optional[str] = None) -> dict:
    """Build a plan. Writes rows to ``model_swap_plans`` and nothing else.

    No consumer file is opened for writing here or anywhere below it. The
    files ARE read — that is where the diff comes from.
    """
    from okuro.ai_models import consumers as C
    from okuro.ai_models import lineage as L

    if mode not in MODES:
        return _fail(f"mode must be one of {MODES}")
    if not C.configured_roots():
        return {**feature_off(), "ok": False}

    old_row, reason = L.find_unit(str(unit_old))
    if old_row is None:
        return _fail(reason or f"no unit matches {unit_old!r}")
    old_row = L.enrich_unit(dict(old_row))
    old_id = old_row["unit_id"]

    refs = [r for r in C.list_consumers(consumer=consumer, unit=old_id,
                                        limit=10_000)
            if r["unit_id"] == old_id]
    if not refs:
        return _fail(
            f"{consumer} names no reference that resolves to {old_id}. "
            f"`okuro models consumers --unit {old_id}` lists who does; "
            f"`--refresh` re-walks the roots if the map is stale.")

    tier = max((r["tier"] for r in refs), key=lambda t: _TIER_RANK.get(t, 0))
    if tier == "ARCHIVE":
        return _fail(
            f"{consumer} is ARCHIVE — archived consumers are not swappable. "
            f"An archived tool is not maintained, so a plan to change what it "
            f"loads has no one to test it and nothing to protect.",
            consumer=consumer, tier=tier)

    try:
        new_subject = resolve_new(new, kind)
    except LookupError as exc:
        return _fail(str(exc))

    if new_subject.kind == "unit" and new_subject.ref == old_id:
        return _fail(f"{new} resolves to the unit being replaced ({old_id})")

    relation = L.relate(L.parse(new_subject.name_for_parse()),
                        L.unit_parse(old_row), target_unit_id=old_id)

    placements = C._load_placements()
    rows: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for ref in sorted(refs, key=lambda r: (r["config_path"], r["line"])):
        key = (ref["config_path"], ref["line"])
        if key in seen:      # one line can carry two references to one unit
            continue
        seen.add(key)
        root = _root_for(consumer, ref["config_path"])
        mount = root.mount if root is not None else None
        new_ref, why = rewrite_ref(ref["model_ref"], ref["match_kind"],
                                   old_row, new_subject, mount)
        diff, diff_why = (None, None)
        if new_ref is not None:
            diff, diff_why = line_diff(ref["config_path"], ref["line"],
                                       ref["model_ref"], new_ref)
            if diff is None:
                new_ref, why = None, diff_why
        if mode == "side-by-side" and new_ref is not None:
            why = ((why + ". ") if why else "") + (
                "side-by-side: nothing is rewritten — the diff shows what a "
                "replace WOULD change, and both models stay")
        rows.append({
            "config_path": ref["config_path"], "line": ref["line"],
            "old_ref": ref["model_ref"], "new_ref_written": new_ref,
            "match_kind": ref["match_kind"],
            "rewritable": 1 if new_ref is not None else 0,
            "rewrite_note": why, "diff": diff,
        })

    root = next((_root_for(consumer, r["config_path"]) for r in rows), None)
    blockers = compute_blockers(new_subject, mode, root, placements)
    checklist = _fresh_checklist(consumer, tier)
    group = _plan_group()

    _insert(group, consumer=consumer, tier=tier, unit_old=old_id,
            new=new_subject, relation=relation, mode=mode, rows=rows,
            checklist=checklist, blockers=blockers, note=note)
    return {"ok": True, "plan": show(group)["plan"]}


def _insert(group: str, *, consumer: str, tier: str, unit_old: str,
            new: NewSubject, relation: Any, mode: str, rows: list[dict],
            checklist: list[dict], blockers: list[dict],
            note: Optional[str]) -> None:
    from okuro.db import get_db

    db = get_db()
    for row in rows:
        db.execute(
            """
            INSERT OR REPLACE INTO model_swap_plans
              (plan_group, consumer, tier, unit_old, new_kind, new_ref,
               relation, relation_why, mode, config_path, line, old_ref,
               new_ref_written, match_kind, rewritable, rewrite_note, diff,
               checklist, blockers, state, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (group, consumer, tier, unit_old, new.kind, new.ref,
             getattr(relation, "relation", None), getattr(relation, "why", None),
             mode, row["config_path"], row["line"], row["old_ref"],
             row["new_ref_written"], row["match_kind"], row["rewritable"],
             row["rewrite_note"], row["diff"],
             json.dumps(checklist), json.dumps(blockers), "proposed", note))


# --- reading ----------------------------------------------------------------

_COLS = ("id, plan_group, consumer, tier, unit_old, new_kind, new_ref, "
         "relation, relation_why, mode, config_path, line, old_ref, "
         "new_ref_written, match_kind, rewritable, rewrite_note, diff, "
         "checklist, blockers, state, note, created_at, updated_at")


def _shape(rows: list[dict]) -> dict:
    """Group rows into the one plan they belong to."""
    head = rows[0]
    checklist = json.loads(head["checklist"] or "[]")
    blockers = json.loads(head["blockers"] or "[]")
    return {
        "plan_group": head["plan_group"], "consumer": head["consumer"],
        "tier": head["tier"], "unit_old": head["unit_old"],
        "new_kind": head["new_kind"], "new_ref": head["new_ref"],
        "relation": head["relation"], "relation_why": head["relation_why"],
        "mode": head["mode"], "state": head["state"], "note": head["note"],
        "checklist": checklist, "blockers": blockers,
        "created_at": head["created_at"], "updated_at": head["updated_at"],
        "rows": [
            {"id": r["id"], "config_path": r["config_path"], "line": r["line"],
             "old_ref": r["old_ref"], "new_ref_written": r["new_ref_written"],
             "match_kind": r["match_kind"], "rewritable": bool(r["rewritable"]),
             "rewrite_note": r["rewrite_note"], "diff": r["diff"],
             "location": f"{r['config_path']}:{r['line']}"}
            for r in rows],
        "totals": {
            "rows": len(rows),
            "rewritable": sum(1 for r in rows if r["rewritable"]),
            "unrewritable": sum(1 for r in rows if not r["rewritable"]),
            "files": len({r["config_path"] for r in rows}),
            "checklist_done": sum(1 for c in checklist if c.get("done")),
            "checklist_total": len(checklist),
            "blocking": len(_blocking(blockers)),
            "warnings": sum(1 for b in blockers if b.get("severity") == "warn"),
        },
    }


def show(plan_group: str) -> dict:
    """One plan, whole. Accepts a unique prefix of the group id."""
    from okuro.db import get_db

    db = get_db()
    rows = [dict(r) for r in db.fetchall(
        f"SELECT {_COLS} FROM model_swap_plans WHERE plan_group = ? "
        f"ORDER BY config_path, line", (plan_group,))]
    if not rows:
        rows = [dict(r) for r in db.fetchall(
            f"SELECT {_COLS} FROM model_swap_plans WHERE plan_group LIKE ? "
            f"ORDER BY plan_group, config_path, line", (f"{plan_group}%",))]
        groups = {r["plan_group"] for r in rows}
        if len(groups) > 1:
            return _fail(f"{plan_group!r} matches {len(groups)} plans — "
                         f"`okuro models swap list` prints the full ids")
    if not rows:
        return _fail(f"no swap plan {plan_group!r}")
    return {"ok": True, "plan": _shape(rows)}


def list_plans(*, consumer: Optional[str] = None, state: Optional[str] = None,
               unit: Optional[str] = None, limit: int = 50) -> dict:
    """Every plan, newest first, each as a summary with its rows."""
    from okuro.ai_models import consumers as C
    from okuro.db import get_db

    if not C.configured_roots():
        return feature_off()

    clauses, params = [], []
    if consumer:
        clauses.append("consumer = ?")
        params.append(consumer)
    if state:
        clauses.append("state = ?")
        params.append(state)
    if unit:
        clauses.append("(unit_old = ? OR unit_old LIKE ?)")
        params += [unit, f"%{unit}%"]
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    groups = [r["plan_group"] for r in get_db().fetchall(
        f"SELECT plan_group, MAX(created_at) AS c FROM model_swap_plans{where} "
        f"GROUP BY plan_group ORDER BY c DESC, plan_group DESC LIMIT ?",
        tuple(params + [int(limit)]))]
    plans = [show(g)["plan"] for g in groups]
    return {
        "configured": True, "plans": plans,
        "checklists": {"default_active": checklist_for("_active", "ACTIVE"),
                       "config_key": CHECKLIST_KEY},
        "totals": {
            "plans": len(plans),
            "by_state": {s: sum(1 for p in plans if p["state"] == s)
                         for s in STATES},
        },
    }


# --- the state machine ------------------------------------------------------


def _write_group(group: str, **cols: Any) -> None:
    """Every mutation writes by plan_group, never by row id.

    The group-level facts are repeated on every row of a plan (migration 154
    says why), so a write that addressed one row could split a plan in two.
    """
    from okuro.db import get_db

    sets = ", ".join(f"{k} = ?" for k in cols)
    get_db().execute(
        f"UPDATE model_swap_plans SET {sets}, updated_at = datetime('now') "
        f"WHERE plan_group = ?", tuple(list(cols.values()) + [group]))


def tick(plan_group: str, index: int) -> dict:
    """Tick checklist item ``index`` (1-based). First tick opens ``testing``."""
    res = show(plan_group)
    if not res.get("ok"):
        return res
    plan = res["plan"]
    group = plan["plan_group"]
    if plan["state"] in ("applied", "rejected"):
        return _fail(f"plan {group} is {plan['state']} — its checklist is closed",
                     plan=plan)
    items = plan["checklist"]
    if not 1 <= int(index) <= len(items):
        return _fail(f"item {index} is out of range — this plan has "
                     f"{len(items)} checklist item(s)", plan=plan)
    item = items[int(index) - 1]
    if item.get("done"):
        return _fail(f"item {index} is already ticked: {item['item']}", plan=plan)
    from datetime import datetime, timezone

    item["done"] = True
    item["done_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = "testing" if plan["state"] == "proposed" else plan["state"]
    _write_group(group, checklist=json.dumps(items), state=state)
    return show(group)


def reject(plan_group: str, why: str) -> dict:
    """Close a plan without applying it. The reason is the point."""
    res = show(plan_group)
    if not res.get("ok"):
        return res
    plan = res["plan"]
    group = plan["plan_group"]
    if not (why or "").strip():
        return _fail("a rejection needs a reason — a closed plan with no why "
                     "is indistinguishable from a forgotten one", plan=plan)
    if plan["state"] == "applied":
        return _fail(f"plan {group} is already applied — it cannot be rejected "
                     f"afterwards. Propose the reverse swap instead.", plan=plan)
    _write_group(group, state="rejected", note=why)
    return show(group)


def apply(plan_group: str) -> dict:
    """Mark a plan applied — and PRINT the diff. It writes no consumer file.

    Blockers are recomputed here rather than trusted from propose time: a
    release can have been downloaded, a mount can have changed, and the
    question ``apply`` answers is about now.
    """
    from okuro.ai_models import consumers as C

    res = show(plan_group)
    if not res.get("ok"):
        return res
    plan = res["plan"]
    group = plan["plan_group"]

    if plan["state"] == "applied":
        return _fail(f"plan {group} is already applied", plan=plan)
    if plan["state"] == "rejected":
        return _fail(f"plan {group} was rejected — propose a new one", plan=plan)

    undone = [c["item"] for c in plan["checklist"] if not c.get("done")]
    if undone:
        return _fail(
            f"{plan['tier']} gate: {len(undone)} of {len(plan['checklist'])} "
            f"checklist item(s) are not ticked — "
            + "; ".join(undone),
            plan=plan, undone=undone)

    try:
        new_subject = resolve_new(plan["new_ref"],
                                  None if plan["new_kind"] == "unit" else "release")
    except LookupError:
        new_subject = NewSubject(kind="release", ref=plan["new_ref"],
                                 given=plan["new_ref"])
    root = next((_root_for(plan["consumer"], r["config_path"])
                 for r in plan["rows"]), None)
    blockers = compute_blockers(new_subject, plan["mode"], root,
                                C._load_placements())
    _write_group(group, blockers=json.dumps(blockers))
    blocking = _blocking(blockers)
    if blocking:
        plan = show(group)["plan"]
        return _fail(
            f"{len(blocking)} blocker(s) unresolved — "
            + "; ".join(b["text"] for b in blocking),
            plan=plan, blockers=blocking)

    _write_group(group, state="applied")
    out = show(group)
    out["apply_by_hand"] = _apply_text(out["plan"])
    return out


def _apply_text(plan: dict) -> str:
    """The diff a person applies by hand. This IS the deliverable of `apply`."""
    lines = [
        f"# swap plan {plan['plan_group']} — {plan['consumer']} "
        f"({plan['tier']}, mode {plan['mode']})",
        f"# {plan['unit_old']}  ->  {plan['new_ref']}",
        "#",
        "# okuro does not edit consumer configs. Apply these by hand.",
        "",
    ]
    for row in plan["rows"]:
        if row["diff"]:
            lines.append(row["diff"].rstrip("\n"))
        else:
            lines.append(f"# {row['location']} — NOT rewritten: "
                         f"{row['rewrite_note'] or 'no rewrite'}")
        lines.append("")
    return "\n".join(lines)


# --- the one call every surface renders -------------------------------------


def swap_plan(action: str, **kwargs: Any) -> dict:
    """Dispatch for the MCP tool and the API, so they cannot drift apart."""
    if action == "propose":
        return propose(kwargs["consumer"], kwargs["unit_old"], kwargs["new"],
                       mode=kwargs.get("mode") or "replace",
                       kind=kwargs.get("kind"), note=kwargs.get("note"))
    if action == "list":
        return list_plans(consumer=kwargs.get("consumer"),
                          state=kwargs.get("state"), unit=kwargs.get("unit"),
                          limit=int(kwargs.get("limit") or 50))
    if action == "show":
        return show(str(kwargs["plan"]))
    if action == "tick":
        return tick(str(kwargs["plan"]), int(kwargs["item"]))
    if action == "apply":
        return apply(str(kwargs["plan"]))
    if action == "reject":
        return reject(str(kwargs["plan"]), str(kwargs.get("why") or ""))
    return _fail(f"unknown action {action!r} — one of propose|list|show|tick|"
                 f"apply|reject")


__all__ = [
    "CHECKLIST_KEY", "STATES", "MODES", "KINDS", "FEATURE_OFF_REASON",
    "OWNER", "PROTECTED_DEFAULT", "ACTIVE_DEFAULT",
    "B_NOT_DOWNLOADED", "B_UNREACHABLE", "B_HEADROOM",
    "NewSubject", "feature_off", "checklist_for", "resolve_new",
    "rewrite_ref", "read_line", "line_diff", "compute_blockers",
    "propose", "show", "list_plans", "tick", "reject", "apply", "swap_plan",
]
