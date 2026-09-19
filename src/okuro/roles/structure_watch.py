# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The weekly source watch — nine HTTP requests and a hash comparison,
#   with an agent spawned only when something actually moved.
# index:
#   imports
#   QUARTERLY_FLOOR_DAYS
#   def last_research_at
#   def make_event_gate
#   def run_watch
#   def main
# AGENT_HEADER_END -->
"""Decision 5 in code: poll weekly, research on an EVENT.

**The shape this replaces.** okuro's other scheduled role job, role-refresh,
runs an LLM every single night whether or not there is anything to say. Five
months of that produced 386 knowledge rows that say "no significant change"
and a handful that say anything else — an agent paid nightly to report that
nothing happened, which it then had to write down somewhere, which is how the
knowledge store filled with receipts.

So this one splits the job in two along the line where the judgement actually
is:

* **the poll is not a judgement.** Fetch nine URLs, hash the bodies, compare
  against the registry, raise the alarms whose conditions are arithmetic. There
  is no model in ``source_poll`` and there is none here. It runs every week,
  cheaply, and its output is stored bodies and ``source_health`` action rows.
* **the research is a judgement**, and it only has material when a body
  changed. So the agent is spawned on that event, plus a quarterly floor so a
  registry that has genuinely not moved in three months still gets looked at by
  something that can notice what a diff cannot.

**Why the floor is a floor and not a second schedule.** A source can be stable
and the WORLD still move — a spec nobody edited can be superseded by one nobody
registered. The floor is the admission that change-detection has a blind spot,
and 90 days is how long we are willing to be blind. It is measured from the
last research run, not from a calendar quarter, so a change-triggered run in
August pushes the floor to November rather than firing again in October.

**It does not duplicate the spawn.** Everything after the poll — the
cross-process claim, the in-flight scan, the contract read, the description,
the run marker — is
``okuro.orchestrator.api.roles_structure.run_structure_research``, the same
function the button calls. The only thing this module adds is the predicate.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("okuro.roles.structure_watch")


#: How long the registry may sit unchanged before the researcher runs anyway.
#: Decision 5's quarterly floor.
QUARTERLY_FLOOR_DAYS = 90


def _tasks_dir() -> Path:
    from okuro.orchestrator.api.main import TASKS_DIR  # type: ignore

    return Path(TASKS_DIR)


def last_research_at(tasks_dir: Path | None = None) -> datetime | None:
    """When a structure-research agent was last dispatched, or None.

    Read from the run markers the dispatch writes beside each task, rather than
    from a column, because the marker is already the record of "what did this
    task go and poll" and adding a second home for the same fact is how the two
    disagree later. The marker's ``started_at`` is written at spawn time, which
    is the event the floor measures from.

    Returns None when nothing is on record — no markers, no task directory, an
    unreadable file. Every one of those means the floor is DUE, which is the
    safe direction to fail: the cost of an unnecessary quarterly run is one
    agent, and the cost of a missed one is a blind spot nobody is watching.
    """
    try:
        tasks_dir = tasks_dir or _tasks_dir()
    except Exception:  # noqa: BLE001 — no orchestrator config is "nothing on record"
        return None

    from okuro.orchestrator.api.roles_structure import RUN_MARKER, TASK_PREFIX

    newest: datetime | None = None
    try:
        candidates = sorted(Path(tasks_dir).glob(f"task-{TASK_PREFIX}-*"))
    except Exception:  # noqa: BLE001
        return None

    for task_dir in candidates:
        marker = task_dir / RUN_MARKER
        if not marker.is_file():
            continue
        try:
            started = json.loads(marker.read_text()).get("started_at")
            stamp = datetime.fromisoformat(str(started))
        except (OSError, ValueError, TypeError):
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if newest is None or stamp > newest:
            newest = stamp
    return newest


def make_event_gate(
    *,
    quarterly_floor_days: int = QUARTERLY_FLOOR_DAYS,
    now: datetime | None = None,
    tasks_dir: Path | None = None,
    force: bool = False,
    never: bool = False,
):
    """Build the ``(summary) -> (spawn, reason)`` predicate.

    The reason string is returned, logged and put in the recurring history, so
    a week with no agent is explained rather than silent. "Nothing spawned" and
    "the scheduler did not run" look identical in a log that only records
    spawns, and one of them is a broken clock.
    """
    now = now or datetime.now(timezone.utc)

    def gate(summary: dict) -> tuple[bool, str]:
        if never:
            return False, "dry run — poll only, spawn suppressed"
        if force:
            return True, "forced"

        changed = list(summary.get("changed") or [])
        if changed:
            return True, f"{len(changed)} source(s) changed: {', '.join(changed)}"

        last = last_research_at(tasks_dir)
        if last is None:
            return True, "quarterly floor: no previous research run on record"

        age = (now - last).days
        if age >= quarterly_floor_days:
            return True, (
                f"quarterly floor: last research run {age} days ago "
                f"(floor {quarterly_floor_days}d)"
            )
        return False, (
            f"no source changed; last research run {age} days ago "
            f"(floor {quarterly_floor_days}d)"
        )

    return gate


def run_watch(
    *,
    quarterly_floor_days: int = QUARTERLY_FLOOR_DAYS,
    now: datetime | None = None,
    tasks_dir: Path | None = None,
    force: bool = False,
    never: bool = False,
    http=None,
) -> dict:
    """Poll every enabled source, then spawn the researcher only on an event.

    The poll always happens. It stores the bodies a later quote is checked
    against and files the ``source_health`` actions a dead feed shows up as —
    both worth doing on a week when nothing changed, which is most weeks.
    """
    from okuro.orchestrator.api.roles_structure import run_structure_research

    gate = make_event_gate(
        quarterly_floor_days=quarterly_floor_days,
        now=now,
        tasks_dir=tasks_dir,
        force=force,
        never=never,
    )
    return run_structure_research(gate=gate, http=http)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — this is what the ``structure-watch`` def runs.

    Exit codes are what the scheduler records, so they mean what a scheduler
    needs them to mean rather than what an HTTP client would expect:

      0  the watch ran and did what the gate said. It may or may not have
         spawned an agent; both are correct outcomes and "nothing changed" is
         the common one.
      0  another run was already going (409). The guard doing its job is not
         this job failing.
      1  the watch could not run — the poll blew up, or the researcher role
         carries no acceptance criteria and dispatching would mean handing a
         critic a blank page.
      1  THE GATE SAID SPAWN AND NOTHING SPAWNED. This is the case the first
         version got wrong: it printed "spawned no" and exited 0, so the
         scheduler wrote `outcome: useful` into the definition's history and a
         quarter could pass with the researcher never once running while every
         record said the watch was healthy. A silent no-op that reports success
         is worse than a crash, because a crash gets looked at.
    """
    parser = argparse.ArgumentParser(
        prog="okuro-structure-watch",
        description="Weekly source poll; spawns the structure researcher on change.",
    )
    parser.add_argument(
        "--quarterly-floor-days",
        type=int,
        default=QUARTERLY_FLOOR_DAYS,
        help="Research anyway when the last run is this old (default: %(default)s)",
    )
    parser.add_argument(
        "--force", action="store_true", help="Spawn regardless of the gate"
    )
    parser.add_argument(
        "--no-spawn",
        action="store_true",
        help="Poll and file health actions, never spawn (dry run)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    from fastapi import HTTPException

    try:
        result = run_watch(
            quarterly_floor_days=args.quarterly_floor_days,
            force=args.force,
            never=args.no_spawn,
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            print(f"structure-watch: skipped — {exc.detail}")
            return 0
        print(f"structure-watch: FAILED — {exc.detail}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — the scheduler reads the exit code
        logger.exception("structure-watch failed")
        print(f"structure-watch: FAILED — {exc}", file=sys.stderr)
        return 1

    # The gate said yes and nothing started. `run_structure_research` reports
    # that as status='failed' with an error, and it must not be flattened into
    # the same exit code as a quiet week.
    spawn_failed = (
        not result.get("spawned")
        and str(result.get("status")) == "failed"
    )

    verdict = (
        f"structure-watch: polled {result['polled']}, "
        f"changed {len(result['changed'])}, alarmed {len(result['alarmed'])}, "
        f"actions {len(result.get('health_actions') or [])}, "
        f"spawned {'yes ' + str(result['task_id']) if result['spawned'] else 'no'} "
        f"— {result['gate_reason']}"
    )
    if spawn_failed:
        verdict += (
            f" | SPAWN FAILED: {result.get('error') or 'unknown'} — the gate "
            f"said research and no agent started"
        )
        logger.error(
            "structure-watch gate said spawn and the spawn failed: %s",
            result.get("error"),
        )

    # One line, last, carrying the verdict — the scheduler records the final
    # line of stdout as this run's summary, and pairs it with the exit code as
    # the outcome. Both halves of that record are set here on purpose: the
    # summary says what happened and the code says whether it was alright.
    print(verdict)
    return 1 if spawn_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
