# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: features — the release maturity switch. A feature that ships but is
#   not ready to be presented is declared here and gated at its registration
#   points, so the public package carries it INERT rather than absent.
# index: imports | class Feature | FEATURES | def _config_features |
#   def feature_enabled | def feature_state | def _withheld |
#   def withheld_tools | def withheld_commands | def withheld_routes |
#   def withheld_daemon_tasks
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

WHERE THE GATES SIT. Four registration points read this module, one per
surface, so ONE declaration below hides a feature everywhere:

================  =============================  ==============================
Declared on       Withheld by                    Consumed at
================  =============================  ==============================
``commands``      :func:`withheld_commands`      ``cli/main.py`` — dropped
                                                 from the click group
``tools``         :func:`withheld_tools`         ``mcp/_registry.py`` — left
                                                 out of the tool list
``routes``        :func:`withheld_routes`        ``GET /api/features`` — the
                                                 SPA filters its nav and
                                                 refuses the route
``daemon_tasks``  :func:`withheld_daemon_tasks`  ``daemon/registry.py`` — left
                                                 out of ``get_all_tasks()``,
                                                 so the scheduler never arms
                                                 the timer
================  =============================  ==============================

``daemon_tasks`` is the surface the other three cannot stand in for: a tool,
a command and a route all wait to be ASKED, while a cron job acts on its own.
A feature whose jobs are the feature — one that reads the machine and spends
the install's model budget doing it — is not off until its timers are off,
however thoroughly its UI is hidden. That is why the seam sits in
``get_all_tasks()`` rather than in the scheduler: every enumerator of the
registry goes through it, so there is one place to gate and no second path
that keeps arming a withheld timer.

``routes`` are UI path PREFIXES (``/studio`` withholds ``/studio`` and
``/studio/anything``), matched on segment boundaries so ``/studio-lab``
is untouched. The web surface is the one that cannot hide by absence: the
SPA is one built bundle, so its route still resolves and must explain
itself rather than 404 (see the ``spa_fallback`` note — an absent route in
okuro answers 200 text/html, never 404). That is why the API reports
``summary`` alongside ``enabled``.

WHAT TO PUT ON A DECLARATION. Gate a page's ROUTES, not the machinery other
surfaces share. A page is usually a view over tools and commands that agent
sessions, the orchestrator and the CLI also reach, and withholding those to
hide a screen breaks callers that never open a browser. Before naming a tool
or a command, check its importers with ``cortex_search_code`` — name it only
if the feature is its sole caller.
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

    ``tools``, ``commands`` and ``routes`` name the registration points to
    withhold while the feature is off. They live HERE, next to the default, so
    a feature is declared in exactly one place: adding a surface later is an
    edit to this line, not a hunt through three registries.

    ``routes`` are UI route path PREFIXES, each starting with ``/``. The SPA
    reads them from ``GET /api/features`` and uses them for two jobs at once:
    dropping the nav entries that lead there, and refusing the route itself if
    someone arrives by URL. Prefix, not exact match, so a feature's detail
    routes (``/studio/draft-7``) travel with its index route without being
    listed one by one.

    ``daemon_tasks`` are ``DaemonTask.id`` values from
    :data:`okuro.daemon.registry.BUILTIN_TASKS`, matched EXACTLY — a task id is
    a whole name, not a path, so there is no prefix rule here and
    ``interaction-scan`` never withholds ``interaction-scanner``. Name a job
    only if it is useless or harmful with the feature off: a job whose input
    is the feature's own data qualifies, a job that merely touches it in
    passing does not, and withholding a shared job to hide a feature stops
    work no one asked to stop.
    """

    default: bool
    summary: str
    tools: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    routes: tuple[str, ...] = ()
    daemon_tasks: tuple[str, ...] = ()


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
#
# The register was EMPTY BY DECISION until 2026-09-13, when the transcript
# pipeline became the first thing that had to ship inert rather than absent.
# Everything else on main is presentable and therefore carries no flag.
#
# TO DECLARE ONE, add a single entry. Every field but the first two is optional
# and each names a surface to withhold while the feature is off:
#
#     FEATURES: dict[str, Feature] = {
#         "studio-rework": Feature(
#             default=False,                      # ships OFF; an install opts in
#             summary="The rebuilt Studio editor.",
#             routes=("/studio",),                # UI path prefixes  -> the SPA
#             tools=("studio_draft", ...),        # MCP tool names    -> _registry
#             commands=("studio",),               # click command names -> cli
#             daemon_tasks=("studio-render",),    # DaemonTask ids -> the scheduler
#         ),
#     }
#
# That is the whole job — nothing else changes. `okuro features` lists it, the
# CLI drops the command, the MCP registry withholds the tools, the scheduler
# never arms the timers, and the web shell drops the nav entries and refuses
# the route. Constraints: never a YAML 1.1 boolean word for a name (see above),
# routes start with "/", daemon_tasks are exact task ids that must exist in
# BUILTIN_TASKS, never gate a migration (see the module docstring).
FEATURES: dict[str, Feature] = {
    # The transcript pipeline. okuro copies every agent transcript this machine
    # produces — Claude Code, Codex, Gemini, antigravity — into its own
    # database, then spends the install's model budget mining them for
    # behaviour findings. That is a reasonable thing to want and an
    # unreasonable thing to start doing to someone without asking, which is
    # exactly what a maturity switch is for. It ships OFF; the developer's box
    # opts in.
    #
    # SCOPE, and each tuple was decided by reading the handler rather than the
    # name. The jobs are the ones whose INPUT is the trace store or a table
    # derived from it (agent_events / agent_sessions -> interaction_markers ->
    # interaction_findings; agent_events -> distill_facets -> distill_lessons).
    # Deliberately NOT here: `session-retros`, whose input is okuro's own
    # `sessions` telemetry and which degrades to a metadata-only summary when
    # the trace store is empty (sense/retros.py:76-88); `memory-utility-decay`
    # and `memory-recall-gate`, which touch no trace table at all; and
    # `db-vacuum`, which is whole-database hygiene.
    #
    # TURNING IT OFF LATER DELETES NOTHING. The jobs stop; every row already
    # ingested stays exactly where it is, and turning the feature back on
    # resumes against it. There is no purge path here by design — see the
    # `agent_events` constraint the daemon inherits.
    "transcript-analysis": Feature(
        default=False,
        summary=(
            "Store this machine's agent transcripts in okuro and mine them "
            "for behaviour findings."
        ),
        daemon_tasks=(
            "trace-ingest",
            "interaction-bridge",
            "interaction-scan",
            "interaction-embed",
            "interaction-analysis",
            "interaction-improve",
            "interaction-lifecycle",
            "distill-pipeline",
            "distill-lessons",
        ),
        tools=(
            # okuro.trace.mcp_tools — the module's whole surface; every one of
            # these reads agent_events / agent_sessions and nothing else.
            "session_list",
            "session_trace",
            "trace_search",
            "trace_stats",
            "trace_index",
            # okuro.sense.transcripts — the verbatim per-message store
            # (transcript_messages). A second copy of the same subject matter,
            # written by the same feature.
            "transcript_sweep",
            "transcript_search",
            "transcript_adapters",
            # The human half of the mining loop — reads and writes
            # distill_lessons, which only the distill jobs populate.
            "distill_lessons_review",
            "distill_lesson_approve",
            "distill_lesson_reject",
        ),
        commands=("trace", "distill"),
        routes=("/lessons",),
    ),
}


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


def withheld_routes() -> frozenset[str]:
    """UI route prefixes the SPA must withhold while their feature is off.

    Served by ``GET /api/features`` and consumed by the shell: nav leaves
    under a withheld prefix disappear, and the route itself renders an
    explanation instead of the page.

    Unlike a tool or a command, a route cannot be hidden by ABSENCE — the SPA
    ships as one bundle and its router already knows every path. So this list
    is advisory to the client, and the client fails OPEN if it cannot reach
    this endpoint: a visibility switch that blanks the UI when the API
    hiccups is worse than a preview page someone was not meant to see.
    """
    return _withheld("routes")


def withheld_daemon_tasks() -> frozenset[str]:
    """Daemon task ids the scheduler must not arm while their feature is off.

    Read by :func:`okuro.daemon.registry.get_all_tasks`, which is the single
    enumerator every scheduling path goes through — the scheduler at boot, the
    same scheduler on SIGHUP reload, and the drift alarm. A withheld task is
    therefore never armed, and flipping the config takes effect on the next
    reload without restarting the daemon.

    Withheld, not disabled. ``enabled=False`` is the USER's switch, persisted
    in ``~/.okuro/daemon/config.yaml`` and theirs to flip on the Scheduled
    page; this is the RELEASE's, and the two must not overwrite each other. So
    nothing here writes that file: the task simply does not reach the
    scheduler, and any override the user already saved survives untouched and
    applies again the day the feature is turned on.
    """
    return _withheld("daemon_tasks")


def feature_owning(attr: str, value: str) -> str | None:
    """Name of the currently-OFF feature that withholds *value* on *attr*.

    The inverse of :func:`_withheld`, and it exists for the same reason the
    web payload carries per-feature ``routes``: a surface that has gone quiet
    can only tell the user which config key to flip if something maps the
    missing thing back to its owner. Matching is exact — callers that need
    prefix semantics (routes) do their own longest-prefix walk.

    Returns ``None`` when nothing withholds it, which is also the answer for a
    feature that is currently ON.
    """
    for name, spec in FEATURES.items():
        if value in getattr(spec, attr) and not feature_enabled(name):
            return name
    return None
