# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro features — read the release maturity switch from a terminal.
# index: imports | def features | def _withholds
# AGENT_HEADER_END -->
"""okuro features — which preview surfaces are on for this install.

The switch is deliberately invisible: a withheld tool is absent from the MCP
list, a withheld command is absent from ``okuro --help``, a withheld route
answers with an explanation instead of a page, and a withheld daemon job is
simply a timer that never fires. That is right for the user who should not see
the feature and wrong for the one being supported over a chat window, who
needs "which are on" answerable in one command without reading YAML. This is
that command.

The job case is why the ``withholds`` column exists. The other three surfaces
announce themselves by being asked for and not answering; a job that is not
running produces no symptom at all until someone notices a table has stopped
filling up, which can take weeks.
"""

from __future__ import annotations

import click

from .output import console, data_table, heading, info


@click.command(name="features")
def features():
    """List every declared feature, whether it is on, and where that came from."""
    from okuro.features import FEATURES, _config_features, feature_enabled

    heading("Feature switches")
    if not FEATURES:
        # The common answer, and it has to be a SENTENCE rather than an empty
        # table: someone runs this because a page is missing, and "no rows"
        # does not tell them the switch is not the reason.
        info("(no features declared — everything is on)")
        return

    # The SOURCE column is the reason this reads the config block directly
    # rather than only calling feature_enabled(): "off" and "explicitly turned
    # off" look identical from the outside, and the difference is exactly what
    # someone debugging a missing page needs to know.
    configured = _config_features()

    rows = []
    for name in sorted(FEATURES):
        spec = FEATURES[name]
        raw = configured.get(name)
        source = "config" if isinstance(raw, bool) else "default"
        rows.append([
            name,
            "on" if feature_enabled(name) else "off",
            source,
            _withholds(spec) if not feature_enabled(name) else "—",
            spec.summary,
        ])

    console.print(
        data_table(["feature", "state", "source", "withholds", "summary"], rows)
    )
    console.print()
    info("Turn one on by adding it to ~/.okuro/config.yaml:")
    console.print("    features:\n      <name>: true")
    info("A feature that is not listed above is not gated — it is simply on.")


def _withholds(spec) -> str:
    """One cell naming the surfaces this feature is currently holding back.

    Counts, not names: the list can run to a dozen tool names and this column
    exists to answer "is the switch why that is missing", which a count
    answers. ``mcp_health`` names the withheld tools, ``/api/schedules`` names
    the withheld jobs, and ``okuro --help`` is the list of surviving commands.

    A job is spelled out differently from the other three on purpose. A
    withheld tool or page is something a person went looking for and did not
    find; a withheld job is work that is NOT happening on a schedule, and that
    is the half nobody thinks to check.
    """
    parts = []
    for count, noun in (
        (len(spec.daemon_tasks), "job"),
        (len(spec.tools), "tool"),
        (len(spec.commands), "command"),
        (len(spec.routes), "page"),
    ):
        if count:
            parts.append(f"{count} {noun}{'s' if count != 1 else ''}")
    return " · ".join(parts) or "nothing"
