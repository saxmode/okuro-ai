# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: real-time terminal dashboard for monitoring task execution
# index: imports | class Dashboard
# AGENT_HEADER_END -->
"""Okuro Orchestrator Terminal Dashboard — Rich-based live task monitoring."""

from pathlib import Path
from typing import Optional
from datetime import datetime
import time
import json

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from okuro.orchestrator.state import Task, Subtask, load_task


class Dashboard:
    def __init__(self, task_dir: Path):
        self.task_dir = Path(task_dir)
        self.console = Console()

    def render_static(self, task: Task) -> None:
        self.console.print(self._build_layout(task))

    def render_live(self) -> None:
        try:
            with Live(self._build_layout_from_files(), console=self.console, refresh_per_second=1) as live:
                while True:
                    time.sleep(1)
                    live.update(self._build_layout_from_files())
        except KeyboardInterrupt:
            self.console.print("\n[yellow]Dashboard stopped[/yellow]")

    def _build_layout_from_files(self) -> Group:
        try:
            task_id = self.task_dir.name
            tasks_dir = self.task_dir.parent
            task = load_task(task_id, tasks_dir)
            return self._build_layout(task)
        except Exception as e:
            return Group(Panel(f"[red]Error loading task: {e}[/red]", title="ERROR", border_style="red"))

    def _build_layout(self, task: Task) -> Group:
        panels = [self._build_header(task)]
        if task.phases:
            panels.append(self._build_plan_table(task))
        active = self._get_active_subtask(task)
        if active:
            panels.append(self._build_active_agent_panel(active))
        panels.append(self._build_log_panel())
        return Group(*panels)

    def _build_header(self, task: Task) -> Panel:
        status_colors = {
            "pending": "dim", "planning": "yellow", "active": "green",
            "done": "blue", "failed": "red", "halted": "yellow",
        }
        status_color = status_colors.get(task.status, "white")
        created_at = datetime.fromisoformat(task.created_at)
        elapsed = self._format_duration((datetime.now() - created_at).total_seconds())

        return Panel(
            Group(
                Text(f"Task: {task.description}", style="bold"),
                Text(),
                Text.assemble(
                    ("Status: ", "dim"), (task.status.upper(), status_color),
                    ("  |  Phase: ", "dim"), (f"{task.current_phase}/{len(task.phases)}", "white"),
                    ("  |  Elapsed: ", "dim"), (elapsed, "white"),
                ),
            ),
            title=Text("OKURO ORCHESTRATOR", style="bold cyan"),
            border_style="cyan",
        )

    def _build_plan_table(self, task: Task) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="dim", width=2)
        table.add_column(style="cyan", width=4)
        table.add_column(width=20)
        table.add_column(width=30)
        table.add_column(style="dim", width=8, justify="right")

        for phase in task.phases:
            phase_style = "bold green" if phase.status == "active" else "bold dim"
            phase_name = f"Phase {phase.id}: {phase.name}"
            if phase.status == "done":
                phase_name += " \u2713"
            table.add_row("", "", Text(phase_name, style=phase_style), "", "")
            table.add_row("")

            for subtask in phase.subtasks:
                icon = self._status_icon(subtask.status)
                dur = self._format_duration(subtask.duration) if subtask.duration > 0 else "-"
                table.add_row(icon, subtask.id,
                              Text(subtask.role, style=self._subtask_style(subtask.status)),
                              Text(subtask.description, style=self._subtask_style(subtask.status)), dur)
            table.add_row("")

        return Panel(table, title="PLAN", border_style="blue")

    def _build_active_agent_panel(self, subtask: Subtask) -> Panel:
        running_time = "-"
        if subtask.started_at:
            elapsed = (datetime.now() - datetime.fromisoformat(subtask.started_at)).total_seconds()
            running_time = self._format_duration(elapsed)

        return Panel(
            Group(
                Text.assemble(
                    ("CLI: ", "dim"), (subtask.cli_used or "?", "white"),
                    ("  Model: ", "dim"), (subtask.model_used or "?", "white"),
                    ("  Role: ", "dim"), (subtask.role, "cyan"),
                ),
                Text(f"Subtask {subtask.id}: {subtask.description}", style="bold"),
                Text(),
                Text.assemble(
                    ("Running: ", "dim"), (running_time, "white"),
                    ("  |  Risk: ", "dim"), (subtask.risk, self._risk_color(subtask.risk)),
                ),
            ),
            title="ACTIVE AGENT", border_style="green",
        )

    def _build_log_panel(self) -> Panel:
        logs = self._load_recent_logs(5)
        if not logs:
            return Panel(Text("No log entries yet", style="dim"), title="LOG", border_style="yellow")

        table = Table.grid(padding=(0, 1))
        table.add_column(style="dim", width=10)
        table.add_column()
        for log in logs:
            table.add_row(self._format_timestamp(log.get("ts", "")), self._format_log_event(log))
        return Panel(table, title="LOG (recent)", border_style="yellow")

    def _status_icon(self, status: str) -> Text:
        icons = {
            "done": Text("\u2713", style="green"), "running": Text("\u27f3", style="cyan"),
            "pending": Text("\u00b7", style="dim"), "failed": Text("\u2717", style="red"),
            "skipped": Text("\u25cb", style="dim"),
        }
        return icons.get(status, Text("\u00b7", style="dim"))

    def _subtask_style(self, status: str) -> str:
        return {"done": "dim", "running": "white", "pending": "dim", "failed": "red", "skipped": "dim"}.get(status, "white")

    def _risk_color(self, risk: str) -> str:
        return {"LOW": "green", "MED": "yellow", "HIGH": "red"}.get(risk, "white")

    def _format_duration(self, seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            m, s = int(seconds // 60), int(seconds % 60)
            return f"{m}m {s}s" if s else f"{m}m"
        else:
            h, m = int(seconds // 3600), int((seconds % 3600) // 60)
            return f"{h}h {m}m" if m else f"{h}h"

    def _format_timestamp(self, iso_ts: str) -> str:
        try:
            return datetime.fromisoformat(iso_ts).strftime("%H:%M:%S")
        except Exception:
            return "??:??:??"

    def _format_log_event(self, log: dict) -> Text:
        et = log.get("type", "unknown")
        if et == "task_created":
            return Text.assemble(Text("\u2726", style="cyan"), " ", ("task_created", "cyan"), f" {log.get('detail', '')}")
        elif et == "plan_created":
            return Text.assemble(Text("\u2726", style="blue"), " ", ("plan_created", "blue"),
                                 f" {log.get('phases', 0)} phases, {log.get('subtasks', 0)} subtasks")
        elif et == "subtask_done":
            dur = self._format_duration(log.get("duration", 0)) if log.get("duration") else "0s"
            return Text.assemble(Text("\u2713", style="green"), " ", ("done", "green"),
                                 f" {log.get('subtask', '')} ({dur})")
        elif et == "subtask_failed":
            return Text.assemble(Text("\u2717", style="red"), " ", ("failed", "red"),
                                 f" {log.get('subtask', '')}: {log.get('error', '')[:40]}")
        return Text(f"{et}", style="dim")

    def _load_recent_logs(self, count: int = 5) -> list[dict]:
        log_file = self.task_dir / "log.jsonl"
        if not log_file.exists():
            return []
        try:
            with open(log_file) as f:
                lines = f.readlines()
            return [json.loads(l.strip()) for l in lines[-count:] if l.strip()]
        except Exception:
            return []

    def _get_active_subtask(self, task: Task) -> Optional[Subtask]:
        for phase in task.phases:
            for subtask in phase.subtasks:
                if subtask.status == "running":
                    return subtask
        return None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Okuro Orchestrator Dashboard")
    parser.add_argument("--task", required=True, help="Task directory path")
    args = parser.parse_args()
    Dashboard(Path(args.task)).render_live()
