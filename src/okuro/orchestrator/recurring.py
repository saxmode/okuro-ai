# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Okuro Orchestrator Recurring Task Engine.
# index:
#   imports
#   class RecurringTaskDef
#   def is_def_filename
#   def _schema_version_error
#   def def_load_error
#   def def_rejection_reason
#   def def_problems
#   def bundled_defs_dir
#   def install_bundled_defs
#   def _parse_command
#   def run_command_def
#   def _log_ignored_once
#   def load_recurring_defs
#   def next_cron_after
#   def compute_next_run
#   def get_overdue_defs
#   def create_recurring_run
#   def update_recurring_after_run
#   def _atomic_write
# AGENT_HEADER_END -->
"""Okuro Orchestrator Recurring Task Engine."""

from __future__ import annotations

import json
import logging
import yaml
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from okuro.fsutil import is_data_entry, rejection_reason
from okuro.orchestrator.yamlfast import yload

logger = logging.getLogger("okuro.orchestrator.recurring")

# Tier a recurring run asks its subagents for. Only two values are reachable:
# the dispatcher's tier logic is binary (dispatcher_streaming.py — "strategic"
# when task.intelligence == "max", else "standard"), so there is no `fast` here
# even though okuro.bridge (and therefore the daemon-task registry) has one.
# Spelled in the bridge's vocabulary — `quality` maps to the orchestrator's
# `strategic` on the way down, and the API normalizes the same way on the way
# out, so the UI shows one word for opus across both panels.
TIER_STANDARD = "standard"
TIER_QUALITY = "quality"
RECURRING_TIERS = frozenset({TIER_STANDARD, TIER_QUALITY})

# What a recurring definition RUNS.
#
# Until this existed there was exactly one answer — spawn the orchestrator
# engine on a prose description and let an LLM decompose it — and every
# scheduled job in okuro paid an agent to do work whether or not the work
# needed one. The weekly source poll is the case that made the gap obvious: it
# is nine HTTP requests and a hash comparison, it has no judgement in it at
# all, and wrapping it in a model would have meant an agent deciding whether
# the poll found a change, which is the exact class of "ask a model to report
# on itself" this whole phase exists to remove.
#
#   llm     — the historical behaviour. Description -> task -> engine.
#   command — argv, run by the scheduler, judged by its exit code. No model.
#
# The default is `llm` so every existing definition keeps its meaning without
# being edited.
KIND_LLM = "llm"
KIND_COMMAND = "command"
RECURRING_KINDS = frozenset({KIND_LLM, KIND_COMMAND})

# THE DEFINITION FORMAT HAS A VERSION NOW, AND A COMMAND DEF REQUIRES 2.
#
# The danger `kind` introduced is not a typo, it is an OLD READER. A loader
# written before `kind` existed reads this file, sees no key it recognises
# beyond `description` and `scheduler`, and cheerfully schedules an LLM task —
# handing the decomposer prose written for a program and spending an agent on
# it. That reader exists on any install that has not deployed this commit,
# which includes the machine halfway through an upgrade.
#
# It cannot be fixed in the new loader, because the old loader is the one
# running. So the protection has to be something the OLD code already refuses,
# and there is exactly one such thing in its logic: `get_overdue_defs` skips
# any definition whose `status` is not `scheduled`. A command def therefore
# ships with the sentinel status below, which the old reader sees as "not
# scheduled, ignore" and this reader normalises to `scheduled` once it has
# confirmed the schema version it needs.
#
# `schema_version` is the forward half of the same rule: a definition from the
# future is skipped with an error rather than half-understood.
SUPPORTED_SCHEMA_VERSION = 2

#: The status a `kind: command` definition ships with. Old readers refuse it
#: because it is not `scheduled`; this reader accepts it and only for a
#: definition that declares schema_version >= 2.
SCHEMA_GATED_STATUS = "requires-schema-2"

#: Replaced with ``sys.executable`` in a command def's argv, so a shipped
#: definition names okuro's own interpreter without hardcoding anyone's venv
#: path into a YAML file that installs on other machines.
PYTHON_PLACEHOLDER = "{python}"

#: What a definition may name as the thing to RUN. At least one must be set,
#: and which one is the `kind`'s business, not the gate's — a command def runs
#: its argv, a workflow def compiles its drawn graph, and an LLM def hands its
#: prose to the decomposer. A file naming none of them is not a definition.
PAYLOAD_KEYS = ("command", "workflow_id", "description", "description_template")

#: Wall-clock cap for one command run. The scheduler runs these INLINE (see
#: ``run_command_def``), so a command with no bound would hold up every other
#: overdue definition behind it. Ten minutes is generous for the poll it was
#: written for, whose own whole-run deadline is 60 seconds.
DEFAULT_COMMAND_TIMEOUT = 600


@dataclass
class OutcomeEntry:
    """Record of a single recurring task run outcome."""
    run_id: str
    outcome: str = "unknown"   # useful | empty | failed
    summary: str = ""
    completed_at: str = ""


@dataclass
class RecurringTaskDef:
    id: str
    title: str
    description: str
    role: str                   # primary role (backward compat)
    scheduler: str              # cron expression
    status: str = "scheduled"
    last_run_at: str = ""
    next_run_at: str = ""
    run_count: int = 0
    # --- Intelligent recurring extensions ---
    description_template: str = ""   # template with {last_run_summary} placeholder
    roles: List[str] = field(default_factory=list)  # multi-role (overrides single `role`)
    adaptive: bool = False           # if True, each run incorporates previous learnings
    outcome_history: List[OutcomeEntry] = field(default_factory=list)
    tier: str = TIER_STANDARD        # standard | quality — see RECURRING_TIERS
    # --- Drawn-workflow binding ---
    # Names a MANUALLY ARRANGED workflow (okuro.orchestrator.workflow_store): its
    # drawn graph compiles to the run's plan and the LLM decomposer is never
    # called. Empty = the historical behaviour (plan from the description).
    workflow_id: str = ""
    # Fills the {placeholders} in that workflow's node prompts. Fixed per
    # definition — every run of the schedule compiles the same graph the same way.
    workflow_params: dict = field(default_factory=dict)
    # --- Command defs (kind: command) ---
    kind: str = KIND_LLM
    command: List[str] = field(default_factory=list)
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT
    path: Optional[Path] = field(default=None, repr=False)
    # Why this definition cannot run as written — see `def_problems`. Empty for
    # every healthy def. A non-empty list means the file LOADS (so the panel
    # shows it and PATCH can repair it) but `get_overdue_defs` will never run
    # it. Not persisted: it is derived from the file on every read, so a repair
    # clears it by fixing the file rather than by clearing a flag.
    problems: List[str] = field(default_factory=list)


def is_def_filename(path: Path) -> bool:
    """A dotfile is NEVER a recurring definition, whatever its contents.

    ``recurring_defs/`` lives inside the package tree and ``~/.okuro/
    orchestrator/recurring/`` is a directory agents read, so cortex drops a
    ``.okuro-index.yaml`` sidecar into both. On 2026-09-17 the installer
    copied one into the live dir and the loader listed it as a definition with
    id ``.okuro-index``, kind ``llm``, status ``scheduled``.

    The rule is general-first on purpose: the NEXT generated sidecar name
    needs no edit here. Specific names are the fallback, not the rule.

    It lives in :mod:`okuro.fsutil` now, shared with canon, flows, the design
    store and the prism deck gallery — this is the third subsystem to have hit
    the same defect, and the second time a call site had written the predicate
    inline while the root cause stayed live elsewhere.
    """
    return is_data_entry(path, suffixes=(".yaml", ".yml"))


def def_rejection_reason(data) -> Optional[str]:
    """Why ``data`` is not a recurring definition — or ``None`` if it is one.

    The predicate is STRUCTURAL, not a name check. What makes a YAML file a
    recurring DEFINITION is that it says WHEN to run (``scheduler``, a cron
    expression) and WHAT to run (a ``command``, a drawn ``workflow_id``, or
    prose for the decomposer). A file that says neither is some other document
    that happens to sit in the directory — a cortex sidecar, an editor backup,
    a half-written draft — and there is nothing a scheduler can do with it but
    guess.

    Guessing is the harm this exists to stop. Before it, every parseable YAML
    became a ``RecurringTaskDef`` with ``status: scheduled`` and a title, so it
    showed up in the Scheduled panel as a real job, and
    ``PATCH /api/recurring/{def_id}`` — which resolves its target by
    ``d.id`` over this loader's output — could both hand it a live cron and
    rewrite the file with ``yaml.dump``, destroying whatever it actually was.

    ``id`` is deliberately NOT required: every reader derives it from the
    filename stem (``def_id = yaml_file.stem``), so an ``id:`` key inside the
    file is decoration that nothing reads.

    NEITHER IS ``scheduler``, AND THAT IS A CORRECTION, NOT AN OVERSIGHT.
    Requiring it here threw away a definition somebody had written and then
    mistyped: the file vanished from ``GET /api/recurring``, which made
    ``PATCH /api/recurring/{id}`` answer 404, which meant the one route that
    could have FIXED the cron could no longer reach the file. A guard that
    removes the repair path is worse than what it guards against. A missing
    cron is a :func:`def_problems` entry now.

    The structural machinery is :func:`okuro.fsutil.rejection_reason`; what
    stays here is the one thing that is this subsystem's own — WHICH keys.
    """
    return rejection_reason(
        data,
        require_any=PAYLOAD_KEYS,
        any_label="nothing to run",
    )


def def_problems(data: dict) -> List[str]:
    """What is WRONG with a definition that is nonetheless a definition.

    The line between this and :func:`def_rejection_reason` is whether a person
    meant to write a recurring job at all:

    * **rejected** — the file names nothing to run. Nobody wrote it as a
      definition; it is a generated sidecar, a backup, another document that
      happens to share the directory. It must appear NOWHERE, because
      appearing in the loader's output is exactly what let ``PATCH`` rewrite a
      cortex index file with ``yaml.dump``.
    * **a problem** — the file names something to run but cannot be scheduled
      as written. Somebody DID write it and got a field wrong. It must appear,
      be repairable, and never run.

    Only the second kind can be fixed by the person looking at the panel, and
    only if they can see it. ``get_overdue_defs`` refuses anything carrying a
    problem, so visibility costs nothing.
    """
    problems: List[str] = []
    if not str((data or {}).get("scheduler") or "").strip():
        problems.append("no scheduler")
    return problems


#: Paths already reported as ignored, with the mtime they were reported at.
#: Keyed per process, which is the right scope: the scheduler is a oneshot, so
#: it reports once and exits, while the API process lives for days and calls
#: the loader on every ``GET /api/recurring``.
_IGNORED_REPORTED: dict = {}

#: When the map above outgrows this, it is cleared rather than pruned. A
#: recurring directory holds a handful of files, so reaching this bound means
#: something is churning filenames and the map has stopped being a dedupe
#: table. Clearing costs one repeated log line; growing without bound costs
#: memory in a process that never restarts.
_IGNORED_REPORTED_MAX = 512


def _log_ignored_once(path: Path, message: str, level: int = logging.WARNING) -> None:
    """Log ``message`` about ``path`` once per content change.

    The loader runs on every scheduler tick AND on every read of the four
    ``/api/recurring*`` handlers. A stray YAML in the directory therefore
    produced a WARNING per minute plus one per page load, for a file nobody
    was going to remove that hour — and a warning that repeats forever is a
    warning people configure away, which costs the NEXT real one its reader.

    Keyed on ``(path, mtime)`` rather than on path alone, so editing the file
    reports it again. That is the one case where repeating is informative:
    somebody just tried to fix it, and either succeeded (it loads now, no
    message) or did not (they want to see why).
    """
    key = str(path)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = -1
    if _IGNORED_REPORTED.get(key) == stamp:
        return
    if len(_IGNORED_REPORTED) >= _IGNORED_REPORTED_MAX:
        _IGNORED_REPORTED.clear()
    _IGNORED_REPORTED[key] = stamp
    logger.log(level, message)


def _schema_version_error(data: dict) -> Optional[str]:
    """The format verdict on ``data`` — a reason to refuse it, or ``None``.

    Asked BEFORE any question about the definition's content. A version gate
    that runs after a content gate is not a gate: the content gate reaches its
    own conclusion first, using a vocabulary that by construction does not
    cover the newer format, and reports the file as malformed rather than as
    too new.
    """
    raw = data.get("schema_version")
    try:
        schema_version = int(raw or 1)
    except (TypeError, ValueError):
        return f"unreadable schema_version {raw!r}"
    if schema_version > SUPPORTED_SCHEMA_VERSION:
        # Refused, not best-effort-parsed. A definition written for a newer
        # format may mean something this code cannot see, and running half of
        # it is the failure this version exists to stop.
        return (
            f"declares schema_version {schema_version}, this okuro understands "
            f"{SUPPORTED_SCHEMA_VERSION}. Upgrade okuro."
        )
    return None


def def_load_error(data) -> Optional[tuple]:
    """Would this loader refuse ``data``, and why — as ``(reason, level)``.

    THE ONE ANSWER TO "WOULD THIS LOAD", asked by both readers that need it.

    The installer needs the same verdict the loader will reach, because
    installing a definition no loader accepts creates a scheduled job that
    never runs and whose only symptom is silence. It used to get that verdict
    by writing the bytes into a temp directory and running
    :func:`load_recurring_defs` over them — simulating its own caller, at the
    cost of a third parse and a filesystem round trip, to avoid keeping a
    second copy of the rules. Sharing the predicate is the same guarantee
    without the simulation.

    The gates are ordered, and the order is load-bearing — each one may only
    speak about what the previous ones have already established:

    1. is it a document at all (a mapping, non-empty);
    2. which FORMAT is it written in, before any question about content;
    3. what does its declared ``kind`` require, before the general question;
    4. does it name anything to run at all.

    An unknown ``kind`` is not in here: it is a fallback, not a refusal, and
    the loader logs it and continues with ``llm``.
    """
    shape = rejection_reason(data)
    if shape:
        return (f"not a recurring definition — {shape}", logging.WARNING)

    schema_error = _schema_version_error(data)
    if schema_error:
        return (schema_error, logging.ERROR)

    schema_version = int(data.get("schema_version") or 1)
    declared = str(data.get("kind") or "").strip().lower()

    if declared == KIND_COMMAND and schema_version < 2:
        # A command def that does not declare v2 is one an old reader would
        # have scheduled as an LLM task, which means it shipped without the
        # protection. Refuse it rather than be the reader that makes the
        # unprotected file work.
        return (
            "kind=command without schema_version: 2 — an older okuro would "
            "have run its description through the decomposer",
            logging.ERROR,
        )

    if declared == KIND_COMMAND and not _parse_command(data.get("command")):
        # Refused, not downgraded. Falling back to the LLM path would hand the
        # decomposer a description written for a program and spend a model on
        # it — a typo must cost one job, not a run of an agent doing something
        # nobody asked for.
        return ("kind=command with no command", logging.ERROR)

    reason = def_rejection_reason(data)
    if reason:
        return (f"not a recurring definition — {reason}", logging.WARNING)
    return None


def bundled_defs_dir() -> Path:
    """The recurring definitions that SHIP with okuro.

    Resolved through ``importlib.resources`` for the same reason
    ``config._resolve_package_prompts_dir`` is: the lookup has to work in a
    wheel as well as in an editable checkout.
    """
    try:
        from importlib.resources import files
        return Path(str(files("okuro.orchestrator").joinpath("recurring_defs")))
    except Exception:  # noqa: BLE001 — unusual install shapes
        return Path(__file__).parent / "recurring_defs"


def install_bundled_defs(recurring_dir: Path) -> List[str]:
    """Copy shipped DEFINITIONS into a live ``recurring/`` dir. Never overwrite.

    **Definitions, not files.** The source directory is inside the package
    tree, so it holds whatever anything else put there — and cortex puts a
    ``.okuro-index.yaml`` sidecar in every directory it indexes. Each candidate
    must pass ``is_def_filename`` and ``def_rejection_reason`` before it is
    copied, and every rejection is logged by name.

    **In that order, and each file read exactly once.** An earlier version
    parsed and gated every bundled file and THEN checked whether the target
    already existed, so the common case — everything installed, nothing to do
    — paid for a full parse of every shipped definition and logged a verdict
    about a file it was not going to touch. It then re-read the same bytes
    into a temp directory to run the real loader against them, which parsed
    them a third time. Cheapest question first: does the target exist. The
    validator then sees the mapping this function already has.

    **Why an install step and not a read from the package at runtime.** A
    recurring definition is not static data: the scheduler writes
    ``last_run_at``, ``run_count``, ``next_run_at`` and ``outcome_history``
    back into the same file. Reading them from the installed package would mean
    either writing into site-packages or keeping the state somewhere else, and
    the second is how a definition and its history end up disagreeing about
    when it last ran.

    **Never overwrite** follows directly. Once the file is in
    ``~/.okuro/orchestrator/recurring/`` it belongs to the person who owns that
    machine — they may have retuned the cron, disabled it, or edited the prose,
    and an upgrade that silently reinstates the shipped version would undo that
    with no message. A definition whose SHIPPED shape changed is a migration
    someone has to decide on, not a file copy.

    Returns the names actually installed, so a caller can say "1 new" rather
    than "done".
    """
    src = bundled_defs_dir()
    if not src.is_dir():
        return []
    recurring_dir.mkdir(parents=True, exist_ok=True)
    installed: List[str] = []
    for yaml_file in sorted(src.glob("*.yaml")):
        # (1) IS IT EVEN A CANDIDATE FILENAME.
        #
        # ``Path.glob`` does not implement the shell's hidden-file rule —
        # ``*.yaml`` matches ``.okuro-index.yaml`` — so this directory's
        # contents are not the same set as the definitions that ship in it.
        # General rule before specific, and free: no read, no parse.
        if not is_def_filename(yaml_file):
            logger.info(
                f"Skipping {yaml_file.name}: a dotfile is never a recurring "
                f"definition (cortex writes .okuro-index.yaml into every "
                f"directory it indexes, including this one)"
            )
            continue

        # (2) IS THERE ANYTHING TO DO. Before reading the file, because the
        # answer is usually no: on every okuro start after the first, every
        # shipped definition is already installed. Nothing below this line
        # runs in that case, and nothing is logged about a file this function
        # is not going to touch.
        target = recurring_dir / yaml_file.name
        if target.exists():
            # NEVER OVERWRITE, NOT EVEN A NEWER BUNDLED VERSION.
            #
            # An upgrade may only replace a user file when it can prove the
            # user never changed it, and that proof needs the hash or version
            # of the bundled copy that was installed. Nothing records it:
            # ``installed`` is a return value, not a manifest, and the file
            # itself is rewritten in place by ``update_recurring_after_run``
            # and ``record_outcome`` on every run, so its own bytes cannot
            # answer "is this still what we shipped". Until that provenance is
            # tracked, refusing is the only answer that cannot silently undo a
            # retuned cron.
            continue

        # (3) READ ONCE, PARSE ONCE.
        try:
            raw = yaml_file.read_text()
        except OSError as e:
            logger.warning(f"Could not read bundled def {yaml_file.name}: {e}")
            continue
        try:
            parsed = yload(raw)
        except yaml.YAMLError as e:
            logger.warning(
                f"Skipping {yaml_file.name}: not parseable YAML ({e})"
            )
            continue

        # (4) WOULD THIS SCHEDULER LOAD IT.
        #
        # An installed definition that no loader accepts is a scheduled job
        # that never runs, and the only symptom is silence — the panel shows
        # the file, `run_count` stays 0, and nothing anywhere says why.
        #
        # This used to write the bytes into a temp directory and run the real
        # loader over them — simulating its own caller to avoid keeping a
        # second copy of the acceptance rules, at the cost of a third parse
        # and a filesystem round trip. The rules now live in ONE function that
        # takes a mapping rather than a directory, so the installer asks the
        # loader's own question directly. Same guarantee, reached by sharing
        # the predicate instead of simulating the caller.
        verdict = def_load_error(parsed)
        if verdict is not None:
            reason, _level = verdict
            logger.error(
                f"NOT installing {yaml_file.name}: this okuro's loader would "
                f"reject it ({reason}), so installing it would create a "
                f"scheduled job that never runs and never says why"
            )
            continue

        try:
            _atomic_write(target, raw)
            installed.append(yaml_file.stem)
        except OSError as e:
            logger.warning(f"Could not install recurring def {yaml_file.name}: {e}")
    return installed


def _parse_command(raw) -> List[str]:
    """Normalise a ``command:`` value to argv, substituting the interpreter.

    A list is taken as argv. A string is split with ``shlex`` — POSIX quoting,
    NOT a shell: nothing here expands a glob, reads an environment variable or
    honours a pipe, because a scheduled job that can be made to run a second
    program by a quoting accident is a worse problem than a job that cannot
    express one.
    """
    import shlex
    import sys as _sys

    if not raw:
        return []
    parts = list(raw) if isinstance(raw, (list, tuple)) else shlex.split(str(raw))
    return [
        _sys.executable if str(p) == PYTHON_PLACEHOLDER else str(p) for p in parts
    ]


def run_command_def(def_: RecurringTaskDef) -> dict:
    """Run a ``kind: command`` definition INLINE and report what happened.

    Inline rather than detached, which is the opposite of the LLM path, and the
    reason is that the two runs are judged differently. An engine run is
    long, has its own task directory, its own log and its own status file, so
    the scheduler hands it off and asks the task later. A command run has an
    exit code and a few lines of output, available in seconds — detaching it
    would mean inventing a second status mechanism to recover the one number
    that already exists.

    The outcome vocabulary is the one ``record_outcome`` already uses, so a
    command def's history reads the same as an LLM def's in the same panel:
    ``useful`` for exit 0, ``failed`` otherwise.
    """
    import subprocess

    if not def_.command:
        return {
            "outcome": "failed",
            "returncode": None,
            "summary": "no command",
            "stdout": "",
            "stderr": "",
        }

    try:
        proc = subprocess.run(
            def_.command,
            capture_output=True,
            text=True,
            timeout=def_.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.error(
            f"Recurring command {def_.id} exceeded {def_.timeout_seconds}s — killed"
        )
        return {
            "outcome": "failed",
            "returncode": None,
            "summary": f"timed out after {def_.timeout_seconds}s",
            "stdout": "",
            "stderr": "",
        }
    except Exception as e:  # noqa: BLE001 — a missing binary is one job, not the run
        logger.error(f"Recurring command {def_.id} could not start: {e}")
        return {
            "outcome": "failed",
            "returncode": None,
            "summary": f"could not start: {e}",
            "stdout": "",
            "stderr": "",
        }

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    # The LAST line, not the first: a program that prints progress and then its
    # verdict puts the verdict last, and the verdict is what the history is for.
    tail = stdout.splitlines()[-1] if stdout else (
        stderr.splitlines()[-1] if stderr else ""
    )
    return {
        "outcome": "useful" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "summary": tail[:500],
        "stdout": stdout,
        "stderr": stderr,
    }


def load_recurring_defs(
    recurring_dir: Path,
    ignored: Optional[List[dict]] = None,
) -> List[RecurringTaskDef]:
    """Read a ``recurring/`` directory and return the DEFINITIONS in it.

    This is the single chokepoint every reader goes through — the scheduler
    tick (``scheduler.main``), the engine's outcome recorder
    (``engine.py``) and all four ``/api/recurring*`` handlers call it and
    none of them globs the directory itself. So the gate below is the whole
    fix: a file it refuses cannot reach a panel, a cron, or a ``yaml.dump``
    that would rewrite it.

    Refused, each with a reason:

    * a dotfile — cortex's ``.okuro-index.yaml`` and anything else generated;
    * a YAML that is not a mapping, is empty, declares a schema this version
      does not understand, or names nothing to run.

    A definition whose ``status`` is a gate this loader UNDERSTANDS —
    ``requires-schema-2`` on a file declaring ``schema_version: 2`` — is not
    malformed and stays. One it does not understand is left exactly as it is,
    which means unscheduled, because ``get_overdue_defs`` only ever runs
    ``status == "scheduled"``.

    ``ignored`` is an optional out-list: pass one and it is filled with
    ``{"file", "reason"}`` for every REFUSED entry a person should act on, so
    ``GET /api/recurring`` can show the stray file instead of leaving the log
    as the only place it exists. An out-parameter rather than a changed return
    type, because six call sites read this function and only one of them wants
    the second half.

    Dotfiles are deliberately NOT in that list. They are generated, they are
    present by design on every tick, and putting them in a panel would train
    the reader to ignore the panel — which is the same mistake as warning
    about them every minute, one surface over.
    """
    if not recurring_dir.exists():
        return []
    defs = []
    for yaml_file in sorted(recurring_dir.glob("*.yaml")):
        # A GENERATED SIDECAR IS NOT A SCHEDULED JOB.
        #
        # `debug`, not `warning`, and not surfaced: this file is present on
        # every tick by design, and a minutely warning about an expected file
        # trains people to stop reading the scheduler log — which is where the
        # NEXT, real problem will be reported.
        if not is_def_filename(yaml_file):
            logger.debug(
                f"Ignoring {yaml_file.name} in {recurring_dir}: a dotfile is "
                f"never a recurring definition"
            )
            continue
        try:
            with open(yaml_file) as f:
                data = yload(f)

            def_id = yaml_file.stem

            # WOULD THIS LOAD — ONE QUESTION, ONE ANSWER, SHARED WITH THE
            # INSTALLER.
            #
            # Every ordered gate lives in `def_load_error`: is it a document
            # at all, which format is it written in, what does its declared
            # kind require, does it name anything to run. The order is
            # load-bearing and the reason it is in one function is that the
            # installer must reach the SAME verdict — it used to get there by
            # writing bytes to a temp dir and running this loader over them.
            #
            # `warning` for a shape or payload refusal, `error` for a format
            # or kind refusal; `def_load_error` chooses, because the level is
            # part of the verdict rather than of the caller.
            verdict = def_load_error(data)
            if verdict is not None:
                reason, level = verdict
                _log_ignored_once(
                    yaml_file,
                    f"Ignoring {yaml_file.name} in {recurring_dir}: {reason}",
                    level,
                )
                if ignored is not None:
                    ignored.append({"file": yaml_file.name, "reason": reason})
                continue

            schema_version = int(data.get("schema_version") or 1)

            tier = str(data.get("tier") or TIER_STANDARD).strip().lower()
            if tier not in RECURRING_TIERS:
                # Fall back rather than raise: one typo shouldn't stop the whole
                # schedule. Falling back DOWN is the safe direction — a typo can
                # never silently escalate a daily task onto the expensive tier.
                logger.warning(
                    f"Recurring def {def_id} has unknown tier {tier!r} — using {TIER_STANDARD}"
                )
                tier = TIER_STANDARD

            # An unknown kind is a FALLBACK, not a refusal, which is why it
            # is here and not in `def_load_error`: one typo must not remove a
            # job from the schedule, and falling back to `llm` is what every
            # definition written before `kind` existed already means.
            kind = str(data.get("kind") or KIND_LLM).strip().lower()
            if kind not in RECURRING_KINDS:
                logger.warning(
                    f"Recurring def {def_id} has unknown kind {kind!r} — using {KIND_LLM}"
                )
                kind = KIND_LLM

            status = str(data.get("status", "scheduled"))
            if schema_version >= 2 and status == SCHEMA_GATED_STATUS:
                status = "scheduled"

            command = _parse_command(data.get("command"))

            outcomes = []
            for o in data.get("outcome_history", []):
                outcomes.append(OutcomeEntry(
                    run_id=o.get("run_id", ""),
                    outcome=o.get("outcome", "unknown"),
                    summary=o.get("summary", ""),
                    completed_at=o.get("completed_at", ""),
                ))
            problems = def_problems(data)
            if problems:
                # WARNING, and for the person who wrote the file, not about a
                # stray one: this def is real, it is visible in the panel, and
                # it will not run until somebody fixes it.
                #
                # Deduped like every other per-file report. This branch is the
                # one most likely to sit unfixed for days — it names a job
                # somebody wants — so it is also the one that would produce
                # the most repeats, on every tick and every page load. The
                # panel is where it is meant to be seen; the log says it once.
                _log_ignored_once(
                    yaml_file,
                    f"Recurring def {def_id} cannot be scheduled as written "
                    f"({', '.join(problems)}) — it is loaded and shown so it "
                    f"can be repaired via PATCH /api/recurring/{def_id}, but it "
                    f"will not run",
                )

            defs.append(RecurringTaskDef(
                id=def_id,
                title=data.get("title", def_id),
                description=data.get("description", ""),
                role=data.get("role", ""),
                scheduler=data.get("scheduler", ""),
                status=status,
                last_run_at=data.get("last_run_at") or "",
                next_run_at=data.get("next_run_at") or "",
                run_count=data.get("run_count", 0),
                description_template=data.get("description_template", ""),
                roles=data.get("roles", []),
                adaptive=data.get("adaptive", False),
                outcome_history=outcomes,
                tier=tier,
                workflow_id=(data.get("workflow_id") or "").strip(),
                workflow_params=dict(data.get("workflow_params") or {}),
                kind=kind,
                command=command,
                timeout_seconds=int(
                    data.get("timeout_seconds") or DEFAULT_COMMAND_TIMEOUT
                ),
                path=yaml_file,
                problems=problems,
            ))
        except Exception as e:
            logger.warning(f"Failed to load recurring def {yaml_file}: {e}")
    return defs


def next_cron_after(cron_expr: str, after: datetime) -> datetime:
    from croniter import croniter
    if after.tzinfo is not None:
        after = after.replace(tzinfo=None)
    c = croniter(cron_expr, after)
    return c.get_next(datetime)


def compute_next_run(cron_expr: str, last_run_at: str = "") -> str:
    # All recurring timestamps are stored UTC-naive (last_run_at is written via
    # datetime.utcnow()). Compare in the same zone or the def becomes "overdue"
    # for tz_offset hours every day.
    base = datetime.utcnow()
    if last_run_at:
        try:
            base = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
            if base.tzinfo is not None:
                base = base.replace(tzinfo=None)
        except ValueError:
            base = datetime.utcnow()
    return next_cron_after(cron_expr, base).isoformat()


def get_overdue_defs(defs: List[RecurringTaskDef]) -> List[RecurringTaskDef]:
    now = datetime.utcnow()
    overdue = []
    for def_ in defs:
        if def_.status != "scheduled":
            continue
        if def_.problems:
            # The def is VISIBLE (the panel shows it, PATCH can repair it) and
            # unrunnable. This is the half of the trade that makes visibility
            # free: nothing carrying a problem reaches a run, so loading a
            # broken definition cannot cost anything but a log line.
            #
            # No log here. The loader already said it once per read; repeating
            # it on every tick is how a real warning gets tuned out.
            continue
        if not def_.scheduler:
            # Unreachable via the loader, which files this as a problem above.
            # Kept for a def built by hand in a test or a caller that never
            # went through `load_recurring_defs` — `next_cron_after` would
            # raise on an empty expression.
            logger.warning(f"Recurring def {def_.id} has no scheduler expression — skipping")
            continue
        if not def_.next_run_at:
            overdue.append(def_)
            continue
        try:
            next_run = datetime.fromisoformat(def_.next_run_at.replace("Z", "+00:00"))
            if next_run.tzinfo is not None:
                next_run = next_run.replace(tzinfo=None)
            if next_run <= now:
                overdue.append(def_)
        except ValueError:
            logger.warning(f"Invalid next_run_at for {def_.id}: {def_.next_run_at!r} — triggering")
            overdue.append(def_)
    return overdue


def _resolve_description(def_: RecurringTaskDef) -> str:
    """Build the run description, incorporating adaptive template if set."""
    if def_.adaptive and def_.description_template:
        last_summary = ""
        if def_.outcome_history:
            last = def_.outcome_history[-1]
            last_summary = last.summary or f"({last.outcome})"
        return def_.description_template.replace("{last_run_summary}", last_summary)
    return def_.description


def create_recurring_run(def_: RecurringTaskDef, tasks_dir: Path) -> str:
    task_id = f"task-rec-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{def_.id}"
    task_dir = tasks_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "artifacts").mkdir(exist_ok=True)

    # The description is resolved the same way whether or not a workflow is
    # bound. It is the human-readable description of the run (title bar, logs,
    # continuation prompts) and, when adaptive, still the only channel carrying
    # last run's learnings. What a workflow replaces is the PLAN, not the prose:
    # a drawn node's prompt comes from the node and its {placeholders} come from
    # workflow_params (flow_compiler._subtask), never from this string.
    description = _resolve_description(def_)
    active_roles = def_.roles if def_.roles else [def_.role]
    now_iso = datetime.utcnow().isoformat()

    task_data: dict = {
        "id": task_id,
        "task_type": "recurring",
        "recurring_def_id": def_.id,
        "title": def_.title,
        "description": description,
        "status": "pending",
        "created_at": now_iso,
        "current_phase": 1,
    }
    if def_.roles:
        task_data["required_roles"] = def_.roles
    # Written only when set, so an absent key stays the honest "no workflow"
    # signal all the way down to workflow_run.phases_for_task — the same rule
    # state.create_task follows for its own task.yaml.
    if def_.workflow_id:
        task_data["workflow_id"] = def_.workflow_id
        if def_.workflow_params:
            task_data["workflow_params"] = dict(def_.workflow_params)
    # Tier → the dispatcher's only knob. Before this, create_recurring_run never
    # set intelligence, so dispatcher_streaming's
    #   _tier = "strategic" if task.intelligence == "max" else "standard"
    # pinned EVERY recurring run to standard, permanently and invisibly.
    #
    # Writing intelligence into task.yaml is tier-only here: the engine's
    # intelligence→gates_enabled coupling lives in its --new CLI path, and this
    # task is resumed, so no gate is armed. No LLM decomposition is re-run
    # either — either the plan is pre-written below, or it is compiled from a
    # drawn graph, which is deterministic. An unattended recurring task
    # therefore cannot be left blocking on an approval it has no one to ask.
    if def_.tier == TIER_QUALITY:
        task_data["intelligence"] = "max"
    _atomic_write(task_dir / "task.yaml", yaml.dump(task_data, default_flow_style=False))

    # The plan. Which of the two shapes we write is load-bearing:
    #
    #   no workflow — write the one-phase stub below, as recurring runs always
    #                 have. The engine resumes against it and never plans.
    #   workflow    — write NO plan.yaml at all. load_task leaves task.phases
    #                 empty, and an empty task.phases is the ONE condition under
    #                 which the resume path calls workflow_run.phases_for_task
    #                 and compiles the drawn graph. Writing the stub anyway would
    #                 silently win: the workflow would sit bound in task.yaml,
    #                 unused, and the schedule would quietly run the wrong thing.
    if not def_.workflow_id:
        # Build subtasks — one per role for multi-role, or single subtask for single role
        subtasks = []
        for i, role in enumerate(active_roles, 1):
            subtasks.append({
                "id": f"1.{i}",
                "role": role,
                "description": description,
                "risk": "LOW",
                "complexity": "standard",
                "status": "pending",
                "dependencies": [],
                "artifact_name": f"1.{i}-findings",
                "phase": 1,
                "cli_used": "", "model_used": "", "output_summary": "",
                "started_at": "", "completed_at": "",
                "duration": 0.0, "retries": 0, "error": "",
            })

        plan_data = {
            "phases": [{
                "id": 1,
                "name": "Recurring Run",
                "status": "pending",
                "subtasks": subtasks,
            }],
        }
        _atomic_write(task_dir / "plan.yaml", yaml.dump(plan_data, default_flow_style=False))

    with open(task_dir / "log.jsonl", "w") as f:
        f.write(json.dumps({"timestamp": now_iso, "type": "task_created",
                            "detail": f"Recurring run for {def_.id}"}) + "\n")

    logger.info(f"Created recurring run {task_id} for def {def_.id}")
    return task_id


def update_recurring_after_run(def_: RecurringTaskDef) -> None:
    if def_.path is None:
        logger.warning(f"Cannot update def {def_.id} — no path set")
        return
    try:
        with open(def_.path) as f:
            data = yload(f) or {}
        now_iso = datetime.utcnow().isoformat()
        data["last_run_at"] = now_iso
        data["run_count"] = data.get("run_count", 0) + 1
        data["next_run_at"] = compute_next_run(def_.scheduler, now_iso)
        _atomic_write(def_.path, yaml.dump(data, default_flow_style=False))
        logger.info(f"Updated {def_.id}: next_run_at={data['next_run_at']}")
    except Exception as e:
        logger.error(f"Failed to update recurring def {def_.id}: {e}")


def record_outcome(def_: RecurringTaskDef, run_id: str, outcome: str, summary: str = "") -> None:
    """Record a run outcome in the recurring def's history."""
    if def_.path is None:
        return
    try:
        with open(def_.path) as f:
            data = yload(f) or {}
        history = data.get("outcome_history", [])
        history.append({
            "run_id": run_id,
            "outcome": outcome,
            "summary": summary,
            "completed_at": datetime.utcnow().isoformat(),
        })
        # Keep last 50 entries
        data["outcome_history"] = history[-50:]
        _atomic_write(def_.path, yaml.dump(data, default_flow_style=False))
    except Exception as e:
        logger.error(f"Failed to record outcome for {def_.id}: {e}")


def update_recurring_def(def_: RecurringTaskDef, updates: dict) -> bool:
    """Update a recurring def's YAML with arbitrary fields."""
    if def_.path is None:
        return False
    try:
        with open(def_.path) as f:
            data = yload(f) or {}
        for k, v in updates.items():
            if k in ("id", "path"):
                continue  # immutable
            data[k] = v
        _atomic_write(def_.path, yaml.dump(data, default_flow_style=False))
        return True
    except Exception as e:
        logger.error(f"Failed to update recurring def {def_.id}: {e}")
        return False


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.rename(path)
