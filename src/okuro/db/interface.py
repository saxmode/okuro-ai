# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: OkuroDB — abstract database contract.
# index: imports | class OkuroDB
# AGENT_HEADER_END -->
"""OkuroDB — abstract database contract."""

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any


class OkuroDB(ABC):
    """Abstract database contract for okuro.

    All backends must implement this interface.
    Results are returned as dicts (column_name → value).
    """

    @abstractmethod
    def execute(self, sql: str, params: tuple = ()) -> Any:
        """Execute SQL, return cursor-like object."""

    @abstractmethod
    def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        """Execute SQL, return first row as dict or None."""

    @abstractmethod
    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute SQL, return all rows as dicts."""

    @abstractmethod
    def executemany(self, sql: str, params_list: list[tuple]) -> None:
        """Execute SQL for each param tuple."""

    @abstractmethod
    @contextmanager
    def write(self):
        """Take the writer lock and commit on exit. Use for multi-statement
        DML that must be atomic. Single-statement DML autocommits."""

    @abstractmethod
    @contextmanager
    def transaction(self):
        """Backwards-compatible alias for write()."""

    @abstractmethod
    def migrate(self, migrations_dir: Any = None) -> list[str]:
        """Run pending migrations. Returns list of applied migration names.

        ``migrations_dir`` is optional and defaults to the backend's bundled
        migrations directory. Tests use it to point at a synthetic dir.
        """

    @abstractmethod
    def vec_search(
        self, table: str, embedding: bytes, limit: int = 10
    ) -> list[dict]:
        """Search a vec0 virtual table. Returns rows with 'id' and 'distance'."""

    @abstractmethod
    def close(self) -> None:
        """Close the database connection."""
