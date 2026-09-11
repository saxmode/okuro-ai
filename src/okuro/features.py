# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: features — the release maturity switch. A feature that ships but is
#   not ready to be presented is declared here and gated at its registration
#   points, so the public package carries it INERT rather than absent.
# index: imports | FEATURES | def feature_enabled | def feature_state |
#   def _config_features | def _reset_cache
# AGENT_HEADER_END -->
"""The release maturity switch — features that ship inert.

okuro's public repo is GENERATED from a path allowlist, and that allowlist
ships ``src/`` wholesale. So work-in-progress that lands on ``main`` reaches
the public package by default. This module is how it reaches it switched OFF.

Read from ``~/.okuro/config.yaml``::

    features:
      scale-axis: true      # turn a preview feature on for this install

Every entry in :data:`FEATURES` declares its own default. A feature that is
not ready is declared ``False`` and stays off everywhere until an install
opts in — the developer's box opts in, the release does not.

WHY CONFIG AND NOT SOMETHING ELSE. Three mechanisms were measured before
this one was written:

- **Env var** — rejected for the reason :mod:`okuro.peer.flags` gives: it
  travels by inheritance to every child process and cannot be flipped in a
  running server. Worse for agents specifically: an MCP server is a child of
  the *client*, launched before the agent exists, so an agent cannot set one
  that its own tools will see.
- **A DB row** — breaks at the MCP seam. Tool descriptions are sent once at
  handshake and nothing hot-reloads, so a runtime toggle cannot change the
  tool list without a client reconnect.
- **Excluding the files from the export** — the honest "absent" option, and
  it works only for a subsystem nothing imports. ``cli/main.py`` imports every
  command module in one flat block with no ``try``, so a missing file kills
  every command; API routers are the same shape. Measured: only ``distill``
  and ``redline`` have zero external importers.

Config-time is read fresh per call, survives a restart, is per-install, and
can be flipped without a deploy. Same shape as ``people.strict``,
``prism.strict``, ``distill.mining_enabled`` and ``dev.strict_freshness`` —
four hand-rolled gates this module is the general form of. Those four are
deliberately NOT folded in here yet; each carries its own polarity argument
and absorbing them is a separate change.

WHAT A GATE MUST NOT DO. Never gate a migration. Migrations are contiguous
and one-way: omitting one raises ``MigrationGapError`` and refuses to boot,
for every install. Schema always ships, on or off — the switch gates
BEHAVIOUR. The corollary is worth stating plainly: shipping a feature's
migration is the irreversible act, not enabling the feature.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

log = logging.getLogger(__name__)


class Feature(NamedTuple):
    """A declared feature surface, and everything it is gated at.

    ``default`` is what an install gets when the config says nothing — so a
    feature that is not ready declares ``False`` and the release ships it off.
    ``summary`` is what a user reads when asking why something is missing.

    ``tools`` and ``commands`` name the registration points to withhold while
    the feature is off. They live HERE, next to the default, so a feature is
    declared in exactly one place: adding a surface later is an edit to this
    line, not a hunt through two registries.
    """

    default: bool
    summary: str
    tools: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()


# NAMING CONSTRAINT, and it bites silently. PyYAML follows YAML 1.1, where a
# BARE `off`, `on`, `yes` or `no` key parses as a BOOLEAN — so a feature with
# one of those names can never be addressed from config.yaml and would sit at
# its default forever with no error. Use a descriptive, hyphenated name
# (`scale-axis`, not `on`). Pinned by
# tests/test_features.py::test_a_yaml_boolean_word_can_never_be_a_feature_name.


# The register. A feature must be declared here to be gateable — an unknown
# name is a programming error, not a silently-off feature (see feature_enabled).
#
# Keep this list SHORT. An entry earns its place by being work-in-progress that
# nonetheless has to live on main; a finished feature has no flag.
FEATURES: dict[str, Feature] = {}


def _config_features() -> dict:
    """The ``features:`` block of ``~/.okuro/config.yaml``, or ``{}``.

    Read fresh on every call — see the module docstring. An unreadable,
    absent or malformed config yields ``{}``, which means every feature falls
    back to its declared default. That is the safe direction: a config the
    parser cannot read must never turn a preview surface ON.
    """
    try:
        from okuro.db.engine import okuro_home

        path = okuro_home() / "config.yaml"
        if not path.is_file():
            return {}
        import yaml

        data = yaml.safe_load(path.read_text())
    except Exception:  # noqa: BLE001 — an unreadable config means "defaults"
        return {}
    if not isinstance(data, dict):
        return {}
    block = data.get("features")
    return block if isinstance(block, dict) else {}


def feature_enabled(name: str) -> bool:
    """Is the feature ``name`` on for this install?

    Unknown names raise :class:`KeyError`. A typo must not read as "off" —
    that is how a gate silently disables a shipped surface and nobody
    notices until a user asks where it went.
    """
    if name not in FEATURES:
        raise KeyError(
            f"unknown feature {name!r}. Declare it in okuro.features.FEATURES "
            f"before gating on it. Known: {sorted(FEATURES) or '(none)'}"
        )
    value = _config_features().get(name)
    if isinstance(value, bool):
        return value
    if value is not None:
        log.warning(
            "features.%s in ~/.okuro/config.yaml is %r, not a boolean — "
            "using the declared default (%s).",
            name, value, FEATURES[name].default,
        )
    return FEATURES[name].default


def feature_state() -> dict[str, bool]:
    """Every declared feature and whether it is on. For diagnostics and the UI."""
    return {name: feature_enabled(name) for name in FEATURES}


def _withheld(attr: str) -> frozenset[str]:
    """Names on ``attr`` of every feature that is currently OFF.

    One config read per feature, and the register is short by policy — this
    runs at MCP registry load and at CLI import, not on a hot path.
    """
    out: set[str] = set()
    for name, spec in FEATURES.items():
        if not feature_enabled(name):
            out.update(getattr(spec, attr))
    return frozenset(out)


def withheld_tools() -> frozenset[str]:
    """MCP tool names to leave out of the registry while their feature is off.

    Withheld, not refused: a tool that is absent from the list is a tool the
    agent never learns about. A refusal at call time is for a surface that
    must stay visible and explain itself — that is a different job, and the
    middleware already owns it.
    """
    return _withheld("tools")


def withheld_commands() -> frozenset[str]:
    """CLI command names to drop from the group while their feature is off."""
    return _withheld("commands")
