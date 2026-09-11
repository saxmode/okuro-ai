# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Backend selection from ~/.okuro/config.yaml.
# index: imports | def okuro_home | def config_path | def default_db_path | def create_db | def _load_config
# AGENT_HEADER_END -->
"""Backend selection from ~/.okuro/config.yaml.

Path resolution happens at CALL time, honouring ``$OKURO_HOME``. Both
properties matter and both were bugs until 2026-07-18:

**Call time, not import time.** These were module constants evaluated as
``Path.home() / ".okuro" / …`` when the module was first imported. Any
process whose HOME changed after that import kept the stale path
forever. In the test suite — where 94 files monkeypatch HOME — whichever
test happened to import this module first froze the DB path for the
whole session. If that test had an isolated HOME, every later test hit
``sqlite3.OperationalError: no such table: roles``, and ``reset_db()``
could not help because ``create_db()`` re-read the frozen constant.

**$OKURO_HOME is honoured.** Eleven other modules already resolved the
okuro home through ``$OKURO_HOME`` with a ``$HOME/.okuro`` fallback;
this one did not, so it was the single layer that ignored the override.
That inconsistency broke agent sessions spawned with a private HOME:
``bridge/streaming/antigravity.py`` gives each agy session its own HOME
(agy's only MCP-scoping seam) and pins ``OKURO_HOME`` back at the real
directory. Every non-DB okuro tool worked; every DB-backed one —
``bootstrap`` included — died with "unable to open database file",
because this module looked at HOME regardless.

Production behaviour is unchanged when ``OKURO_HOME`` is unset, which is
the normal case: the fallback is the same ``Path.home() / ".okuro"`` the
constants used to hold.

``CONFIG_PATH`` / ``DEFAULT_DB_PATH`` survive as module attributes purely
as test seams — 83 test files monkeypatch the latter. They default to
``None`` rather than to a resolved path, so an unset seam always falls
through to fresh resolution.
"""

import os
from pathlib import Path

from .interface import OkuroDB


def okuro_home() -> Path:
    """Resolve the okuro home directory, honouring ``$OKURO_HOME``.

    Matches the resolution used across the rest of the codebase (embed,
    streaming registry, tts previews …) so every layer agrees on where
    okuro lives.
    """
    home_env = os.environ.get("OKURO_HOME")
    if home_env:
        return Path(home_env)
    return Path.home() / ".okuro"


def config_path() -> Path:
    return okuro_home() / "config.yaml"


def default_db_path() -> Path:
    return okuro_home() / "okuro.db"


# Test seams, NOT cached values. 83 test files monkeypatch
# DEFAULT_DB_PATH to point at an isolated database; no source code reads
# either name. They default to None so that "unset" is distinguishable
# from "deliberately overridden" — an earlier attempt initialised them to
# config_path()/default_db_path() at import and thereby reintroduced the
# very staleness this module exists to avoid.
CONFIG_PATH = None
DEFAULT_DB_PATH = None


def create_db(config: dict | None = None) -> OkuroDB:
    """Create a database backend from config.

    Config is read from ~/.okuro/config.yaml under the 'database' key:
        database:
          backend: sqlite          # or 'postgres' (future)
          path: ~/.okuro/okuro.db  # sqlite only
    """
    if config is None:
        config = _load_config()

    db_config = config.get("database", {})
    backend = db_config.get("backend", "sqlite")

    if backend == "sqlite":
        from .sqlite import SQLiteDB

        # A monkeypatched DEFAULT_DB_PATH wins (the pattern used by the
        # isolating test fixtures); otherwise resolve fresh so a HOME or
        # OKURO_HOME change since import is respected.
        override = DEFAULT_DB_PATH
        fallback = str(override) if override is not None else str(default_db_path())
        path = db_config.get("path", fallback)
        return SQLiteDB(path)

    raise ValueError(f"Unknown database backend: {backend}")


def _load_config() -> dict:
    """Load config from ~/.okuro/config.yaml, or return empty dict."""
    path = Path(CONFIG_PATH) if CONFIG_PATH is not None else config_path()
    if not path.exists():
        return {}
    try:
        import yaml

        return yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}
