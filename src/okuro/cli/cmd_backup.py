# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro backup — create / list / restore okuro state snapshots.
# index:
#   imports
#   def backup
#   def backup_create
#   def backup_list
#   def backup_restore
#   def backup_export
#   def backup_import
# AGENT_HEADER_END -->
"""okuro backup — snapshot and restore okuro state (DB + keyring)."""

import click

from .output import console, ok, fail, heading


@click.group()
def backup():
    """Back up and restore okuro state (DB + keyring)."""


@backup.command("create")
@click.option("--label", default="manual", help="Short label for this backup.")
@click.option("--no-keyring", is_flag=True, help="Skip the keyring vault.")
def backup_create(label, no_keyring):
    """Create a WAL-consistent snapshot of the current okuro state."""
    from okuro.system import backup as bk

    try:
        m = bk.create_backup(label, include_keyring=not no_keyring)
    except FileNotFoundError as e:
        fail(str(e))
        raise SystemExit(1)
    ok(f"Backup created: {m['path']}")
    console.print(
        f"[dim]schema={m.get('schema_version')} · "
        f"{m['db_bytes'] // 1024} KB · keyring={m.get('keyring')}[/dim]"
    )


@backup.command("list")
def backup_list():
    """List available backups (newest first)."""
    from okuro.system import backup as bk

    items = bk.list_backups()
    if not items:
        console.print("[dim]No backups found[/dim]")
        return
    heading("Backups")
    for m in items:
        counts = m.get("counts") or {}
        tasks = counts.get("tasks")
        extra = f" · tasks={tasks}" if tasks is not None else ""
        console.print(
            f"  {m['id']}  [dim]({m['kind']}, schema={m.get('schema_version')}, "
            f"{m.get('db_bytes', 0) // 1024} KB{extra})[/dim]"
        )


@backup.command("restore")
@click.argument("backup_id")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--no-restart", is_flag=True,
              help="Do NOT stop/start services (you manage them yourself).")
def backup_restore(backup_id, yes, no_restart):
    """Restore okuro state from a backup id (or 'latest').

    Stops the DB-writer services, snapshots the CURRENT state as a safety
    net, swaps in the backup, then restarts.
    """
    from okuro.system import backup as bk

    if not yes:
        click.confirm(
            f"Restore okuro state from '{backup_id}'? Current state is "
            f"snapshotted first, services are cycled.",
            abort=True,
        )
    try:
        res = bk.restore_backup(
            backup_id, manage_services=not no_restart, make_safety=True,
        )
    except FileNotFoundError as e:
        fail(str(e))
        raise SystemExit(1)
    ok(f"Restored from {res['restored_from']}")
    if res.get("safety_backup"):
        console.print(f"[dim]current state saved to {res['safety_backup']}[/dim]")
    if res.get("services_cycled"):
        console.print(f"[dim]services cycled: {', '.join(res['services_cycled'])}[/dim]")


_KEYRING_NOTE = (
    "keyring included — on the TARGET machine set OKURO_KEYRING_PASSWORD to "
    "THIS host's master password before restore, or the secrets won't decrypt"
)


@backup.command("export")
@click.argument("dest")
@click.option("--from", "from_id", default=None,
              help="Export an existing backup id (default: snapshot current state first).")
def backup_export(dest, from_id):
    """Pack a backup into one .tgz for transfer to another machine."""
    from pathlib import Path

    from okuro.system import backup as bk

    try:
        res = bk.export_backup(Path(dest), backup_id=from_id)
    except FileNotFoundError as e:
        fail(str(e))
        raise SystemExit(1)
    ok(f"Exported {res['id']} → {res['path']} ({res['bytes'] // 1024} KB)")
    console.print(f"[dim]copy it over:  scp {res['path']} <user>@<host>:~/[/dim]")
    if res.get("keyring"):
        console.print(f"[dim]{_KEYRING_NOTE}[/dim]")


@backup.command("import")
@click.argument("tgz")
@click.option("--restore", is_flag=True, help="Restore immediately after import.")
@click.option("--yes", is_flag=True, help="Skip the restore confirmation.")
def backup_import(tgz, restore, yes):
    """Unpack an exported .tgz into ~/.okuro/backups/ (optionally restore)."""
    from pathlib import Path

    from okuro.system import backup as bk

    try:
        res = bk.import_backup(Path(tgz))
    except (FileNotFoundError, ValueError) as e:
        fail(str(e))
        raise SystemExit(1)
    ok(f"Imported {res['id']}")
    if res.get("keyring"):
        console.print(f"[dim]{_KEYRING_NOTE}[/dim]")
    if restore:
        if not yes:
            click.confirm(f"Restore from '{res['id']}' now?", abort=True)
        r = bk.restore_backup(res["id"], manage_services=True, make_safety=True)
        ok(f"Restored from {r['restored_from']}")
    else:
        console.print(f"[dim]restore with:  okuro backup restore {res['id']}[/dim]")
