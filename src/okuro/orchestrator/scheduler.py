# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: oneshot recurring task scheduler
# index: imports | def main
# AGENT_HEADER_END -->
"""
Okuro Orchestrator Recurring Task Scheduler

Oneshot script: checks recurring definitions for overdue tasks and spawns
the orchestrator engine for each one in a detached background process.
"""

import logging
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("okuro.orchestrator.scheduler")


def main():
    from okuro.orchestrator.config import load_config
    from okuro.orchestrator.recurring import (
        KIND_COMMAND,
        load_recurring_defs,
        get_overdue_defs,
        create_recurring_run,
        record_outcome,
        run_command_def,
        update_recurring_after_run,
    )

    try:
        config = load_config()
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return

    recurring_dir = config.recurring_dir
    if not recurring_dir or not recurring_dir.exists():
        logger.info("No recurring/ directory — nothing to do")
        return

    defs = load_recurring_defs(recurring_dir)
    if not defs:
        logger.info("No recurring task definitions found")
        return

    overdue = get_overdue_defs(defs)
    if not overdue:
        logger.info(f"No overdue recurring tasks ({len(defs)} definition(s) checked)")
        return

    for def_ in overdue:
        logger.info(f"Triggering recurring task: {def_.id!r} (cron: {def_.scheduler!r})")
        try:
            # A command definition never reaches the engine. It has no
            # description to decompose and no judgement to make, so there is
            # nothing for a model to do — see RECURRING_KINDS. The clock is
            # advanced FIRST, exactly as on the LLM path, so a command that
            # crashes every run still only fires on its schedule instead of
            # being retried on every scheduler tick.
            if def_.kind == KIND_COMMAND:
                update_recurring_after_run(def_)
                result = run_command_def(def_)
                record_outcome(
                    def_,
                    run_id=f"cmd-{def_.id}",
                    outcome=result["outcome"],
                    summary=result["summary"],
                )
                log = logger.info if result["outcome"] == "useful" else logger.error
                log(
                    f"Command def {def_.id!r} finished rc={result['returncode']}: "
                    f"{result['summary']}"
                )
                if result["stderr"]:
                    logger.warning(f"{def_.id!r} stderr: {result['stderr'][:2000]}")
                continue

            task_id = create_recurring_run(def_, config.tasks_dir)
            update_recurring_after_run(def_)

            cmd = [sys.executable, "-m", "okuro.orchestrator.engine", "--resume", task_id, "--yes"]
            log_file = config.tasks_dir / task_id / "orchestrator.log"
            log_fh = open(log_file, "a")
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_fh,
                    stderr=log_fh,
                    start_new_session=True,
                )
                logger.info(f"Spawned orchestrator for {task_id} (PID {proc.pid}, log: {log_file})")
            finally:
                log_fh.close()

        except Exception as e:
            logger.error(f"Failed to trigger recurring task {def_.id!r}: {e}")


if __name__ == "__main__":
    main()
