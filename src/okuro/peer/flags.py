# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.flags — the `people.strict` correctness gate.
#   Every behaviour change that alters what a SHIPPED path produces
#   (structure, prompt shape, stored values) sits behind this flag so a
#   regression is one config line away from being rolled back — without a
#   deploy, without a revert, without a restart.
# index: imports | def people_strict_enabled | def warn_lenient |
#   def _reset_warn_cache
# AGENT_HEADER_END -->
"""`people.strict` — the people-module correctness gate.

Read from ``~/.okuro/config.yaml``::

    people:
      strict: false   # opt OUT; absent or true means strict

Deliberately mirrors :func:`okuro.system.code_version.strict_freshness_enabled`
— a file read fresh on every call, never an env var read once at import.
An env var travels by inheritance to every child process and cannot be
flipped in a running server, which is the exact failure this gate exists
to make recoverable.

POLARITY IS INVERTED versus ``dev.strict_freshness``, on purpose.
``strict_freshness`` is opt-IN because refusing tool calls on a working box
is hostile. ``people.strict`` is opt-OUT because the behaviours it gates are
*bug fixes*: the correct behaviour must be what a fresh install gets, and the
broken-but-familiar behaviour must be the thing you have to ask for.

The lenient path is never silent — :func:`warn_lenient` logs at WARNING once
per context per process, so "why is the depth lever still dead" is answerable
from the logs instead of from a bisect.
"""

from __future__ import annotations

import logging
from pathlib import Path
from okuro.db.engine import okuro_home

log = logging.getLogger(__name__)

# Contexts already warned about in this process. Deduped so a per-recipient
# code path does not emit one line per delivery, which would be noise, not a
# warning — but NOT deduped globally, so every distinct lenient path is named.
_WARNED: set[str] = set()


def people_strict_enabled() -> bool:
    """True unless ``~/.okuro/config.yaml`` sets ``people.strict: false``.

    Default ON. An unreadable, absent or malformed config yields ON — the
    safe direction, because the flag guards corrected behaviour and a
    config the parser cannot read must never silently restore a bug.
    """
    path = okuro_home() / "config.yaml"
    if not path.is_file():
        return True
    try:
        import yaml

        data = yaml.safe_load(path.read_text())
    except Exception:  # noqa: BLE001 — an unreadable config means "strict"
        return True
    if not isinstance(data, dict):
        return True
    people = data.get("people")
    if not isinstance(people, dict):
        return True
    return people.get("strict") is not False


def warn_lenient(context: str, detail: str = "") -> None:
    """Announce that a lenient (pre-fix) branch is running.

    ``context`` is a stable identifier for the call site — it is the dedupe
    key, so keep it constant per branch rather than interpolating values
    into it.
    """
    if context in _WARNED:
        return
    _WARNED.add(context)
    suffix = f" — {detail}" if detail else ""
    log.warning(
        "people.strict is OFF: running the legacy (known-incorrect) path at %s%s. "
        "Remove `people: {strict: false}` from ~/.okuro/config.yaml to restore "
        "corrected behaviour.",
        context,
        suffix,
    )


def _reset_warn_cache() -> None:
    """Clear the per-process dedupe set. Tests only."""
    _WARNED.clear()
