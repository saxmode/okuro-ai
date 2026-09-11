# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro keys — secrets vault (keyring).
# index:
#   imports
#   def keys
#   def keys_init
#   def keys_set
#   def keys_get
#   def keys_list
#   def keys_delete
# AGENT_HEADER_END -->
"""okuro keys — secrets vault (keyring)."""

import secrets

import click

from .output import console, ok, fail, data_table


@click.group()
def keys():
    """Manage secrets in the keyring."""


@keys.command("init")
@click.option("--generate", is_flag=True,
              help="Generate a random master password (unattended). "
                   "Stored in the OS keyring, or a machine-bound key is "
                   "derived headlessly — never written to disk in plaintext.")
@click.option("--force", is_flag=True, help="Re-initialize even if already set up.")
def keys_init(generate, force):
    """Initialize the keyring vault (mirrors the web onboarding wizard)."""
    from okuro.keyring import KeyringStorage

    store = KeyringStorage()
    if store.is_initialized and not force:
        ok("Keyring already initialized. Use --force to re-initialize.")
        return

    if generate:
        password = secrets.token_urlsafe(32)
    else:
        password = click.prompt(
            "Master password", hide_input=True, confirmation_prompt=True
        )

    store.initialize(password)
    ok("Keyring initialized.")


@keys.command("set")
@click.argument("name")
@click.option("--value", prompt=True, hide_input=True, confirmation_prompt=True, help="Secret value.")
def keys_set(name, value):
    """Store a secret."""
    from okuro.keyring import KeyringStorage

    store = KeyringStorage()
    store.add_key(name, value)
    ok(f"Stored: {name}")


@keys.command("get")
@click.argument("name")
def keys_get(name):
    """Retrieve a secret (prints to stdout)."""
    from okuro.keyring import KeyringStorage

    store = KeyringStorage()
    val = store.get_key(name)
    if val is None:
        fail(f"Not found: {name}")
        raise SystemExit(1)
    click.echo(val)


@keys.command("list")
def keys_list():
    """List stored secret names."""
    from okuro.keyring import KeyringStorage

    store = KeyringStorage()
    names = store.list_keys()
    if not names:
        console.print("[dim]No secrets stored[/dim]")
        return
    for name in sorted(names):
        console.print(f"  {name}")


@keys.command("delete")
@click.argument("name")
@click.confirmation_option(prompt="Delete this secret?")
def keys_delete(name):
    """Delete a secret."""
    from okuro.keyring import KeyringStorage

    store = KeyringStorage()
    store.delete_key(name)
    ok(f"Deleted: {name}")
