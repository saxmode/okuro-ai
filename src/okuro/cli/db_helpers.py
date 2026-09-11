# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Shared DB helper — default path for CLI commands.
# index: def default_db_path | def get_db
# AGENT_HEADER_END -->
"""Shared DB helper — default path for CLI commands."""

from pathlib import Path
from okuro.db.engine import okuro_home


def default_db_path() -> Path:
    """Return the default DB path. Pure — does NOT create directories.

    Callers that intend to write (get_db, migrate, seed) use ``ensure_db_dir``
    first; callers that only want to probe for existence should read the
    path without side effects. Earlier versions mkdir'd here and silently
    created ~/.okuro/ during isolated endpoint tests — see H6 regression
    guard in tests/orchestrator/api/test_onboarding_canon_deploy.py.
    """
    return okuro_home() / "okuro.db"


def ensure_db_dir() -> Path:
    """Create the DB parent directory on demand. Returns the DB path."""
    p = default_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_db():
    from okuro.db.sqlite import SQLiteDB
    return SQLiteDB(ensure_db_dir())
