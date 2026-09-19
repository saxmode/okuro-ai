# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The one rule for walking a directory of data files — what counts as an entry and what is somebody else's file.
# index:
#   def is_data_entry
#   def data_entries
#   def missing_required_keys
#   def rejection_reason
# AGENT_HEADER_END -->
"""The one rule for walking a directory of data files.

**Why this module exists, and why it is not a third copy of the rule.**
The same defect has now been found three times, in three subsystems, and each
time it was fixed only where it was found:

1. 2026-07-28, canon — ``canon/registry/`` lives inside the package tree, so
   cortex dropped a ``.okuro-index.yaml`` sidecar into it. All four registry
   walkers globbed ``*.yaml`` and loaded the sidecar as a tool.
   ``canon_list_tools`` returned ``{"id": ".okuro-index", ...}`` and
   ``canon_validate`` reported errors naming a tool that does not exist.
2. 2026-09-17, the role catalog — a pin test asserted a directory was absent;
   cortex recreated it, with its sidecar.
3. 2026-09-17, recurring defs — the installer copied the sidecar out of
   ``recurring_defs/`` into the live ``recurring/`` dir, and the loader
   returned it as a definition with status ``scheduled``.

Instance one had already written the correct predicate inline at ONE of its
call sites while the root cause stayed live in the other four. That is the
shape of this whole class: the rule is easy, remembering to apply it is not.
So the rule has one home, and ``tests/test_data_walkers_route_through_fsutil.py``
re-derives every walker in the package from the AST and fails on a new one
that does not route through here.

**The rule, in two halves.**

*The filename half* (:func:`is_data_entry`) is general before specific: a
dotfile is never a data entry, whatever it contains. That catches
``.okuro-index.yaml`` and every future generated-sidecar convention without
another edit anywhere. Specific exclusions — ``index.yaml`` is a listing, not
an entry — are the fallback, named per caller.

*The content half* (:func:`rejection_reason`) is structural: a directory of
shipped data holds files of one shape, and a file of another shape is somebody
else's document that happens to live there. Which keys make that shape is the
caller's business, so the caller passes them; what is shared is that the
question gets asked at all, and that the answer is a SENTENCE naming the
missing half rather than a bare ``False``. A walker that silently drops a file
is indistinguishable from one that never saw it, and that ambiguity is what
kept all three instances above invisible for as long as they were.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

#: Suffixes okuro's own data directories use. Passed explicitly by every
#: caller rather than defaulted, because "every YAML in here" is the
#: assumption this module exists to stop — including about extensions.
DATA_SUFFIXES = (".yaml", ".yml", ".json")


def is_data_entry(
    path: Path,
    *,
    suffixes: Sequence[str] = DATA_SUFFIXES,
    exclude_names: Sequence[str] = (),
) -> bool:
    """Is this directory entry one of the caller's data files?

    Three checks, general before specific:

    * a dotfile is never a data entry — ``.okuro-index.yaml`` and any future
      sidecar convention, with no edit needed here or at any call site;
    * the suffix must be one the caller owns;
    * ``exclude_names`` holds the caller's specific exceptions, such as
      canon's ``index.yaml``, which is a listing rather than an entry.

    Note that this is NOT redundant with the glob pattern that found the file.
    ``Path.glob`` does not implement the shell's hidden-file rule:
    ``glob("*.yaml")`` matches ``.okuro-index.yaml``, and
    ``Path(".okuro-index.yaml").stem`` is ``".okuro-index"``, which is why
    every instance of this defect produced an entry with a plausible-looking
    id instead of an error.
    """
    if path.name.startswith("."):
        return False
    if suffixes and path.suffix.lower() not in tuple(suffixes):
        return False
    return path.name not in tuple(exclude_names)


def data_entries(
    directory: Path,
    *,
    suffixes: Sequence[str] = DATA_SUFFIXES,
    exclude_names: Sequence[str] = (),
    recursive: bool = False,
    pattern: str = "*",
) -> list[Path]:
    """Every data entry in ``directory``, sorted, filtered by the rule above.

    The walker each caller should use instead of its own ``glob``. Returns a
    sorted list rather than a generator so a caller cannot accidentally
    re-walk a directory it is also writing to — which is what the recurring
    installer does.

    ``recursive`` switches ``glob`` for ``rglob`` (canon's registry is nested).
    ``pattern`` is for a caller that needs more than the suffix filter; the
    suffix filter still applies on top of it.
    """
    if not directory.is_dir():
        return []
    walk = directory.rglob if recursive else directory.glob
    return sorted(
        p
        for p in walk(pattern)
        if p.is_file()
        and is_data_entry(p, suffixes=suffixes, exclude_names=exclude_names)
    )


def missing_required_keys(data: Any, required: Iterable[str]) -> list[str]:
    """Which of ``required`` are absent or blank in ``data``.

    Blank counts as absent: a key present with ``""``, ``None`` or ``[]``
    tells a reader nothing a missing key would not, and treating the two
    differently is how a half-written file gets further than an empty one.
    """
    if not isinstance(data, dict):
        return list(required)
    out: list[str] = []
    for key in required:
        value = data.get(key)
        if value is None or (hasattr(value, "__len__") and len(value) == 0):
            out.append(key)
        elif isinstance(value, str) and not value.strip():
            out.append(key)
    return out


def rejection_reason(
    data: Any,
    *,
    require_all: Iterable[str] = (),
    require_any: Iterable[str] = (),
    any_label: str = "",
    extra: Optional[Callable[[Any], Optional[str]]] = None,
) -> Optional[str]:
    """Why ``data`` is not one of the caller's entries — or ``None`` if it is.

    A sentence, not a boolean, because the caller logs it: the reader of a
    scheduler log needs to know WHICH half was missing to know whether the
    file is a stray or a typo in their own definition.

    * ``require_all`` — every key must be present and non-blank.
    * ``require_any`` — at least one of these must be, for a shape with
      alternative payloads (a recurring def runs a command, a drawn workflow
      or prose, and exactly which is not this function's business).
      ``any_label`` names what they collectively are, for the message.
    * ``extra`` — a caller-specific callback for a rule that is neither, such
      as a version gate. Runs LAST, so a structural verdict never pre-empts a
      more specific one the caller considers more important.
    """
    if not isinstance(data, dict):
        return f"top level is {type(data).__name__}, not a mapping"
    if not data:
        return "the file is empty"

    absent = missing_required_keys(data, require_all)
    if absent:
        joined = ", ".join(f"'{k}'" for k in absent)
        return f"no {joined}"

    require_any = tuple(require_any)
    if require_any and len(missing_required_keys(data, require_any)) == len(
        require_any
    ):
        joined = ", ".join(f"'{k}'" for k in require_any)
        tail = f" — {any_label}" if any_label else ""
        return f"none of {joined}{tail}"

    if extra is not None:
        return extra(data)
    return None
