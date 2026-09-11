# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Shared CLI output helpers using rich.
# index:
#   imports
#   def ok
#   def warn
#   def fail
#   def info
#   def heading
#   def kv_table
#   def data_table
# AGENT_HEADER_END -->
"""Shared CLI output helpers using rich."""

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

console = Console()
err_console = Console(stderr=True)


def ok(msg: str):
    console.print(f"  [green]\u2713[/green] {msg}")


def warn(msg: str):
    console.print(f"  [yellow]\u25b3[/yellow] {msg}")


def fail(msg: str):
    console.print(f"  [red]\u2717[/red] {msg}")


def info(msg: str):
    console.print(f"  [dim]\u2022[/dim] {msg}")


def heading(msg: str):
    console.print(f"\n[bold]{msg}[/bold]")


def kv_table(rows: list[tuple[str, str]], title: str | None = None) -> Table:
    """Build a two-column key-value table."""
    table = Table(show_header=False, box=None, padding=(0, 2), title=title)
    table.add_column("key", style="bold", no_wrap=True)
    table.add_column("value")
    for k, v in rows:
        table.add_row(k, v)
    return table


def data_table(columns: list[str], rows: list[list[str]], title: str | None = None) -> Table:
    """Build a multi-column data table."""
    table = Table(title=title, show_lines=False)
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*row)
    return table
