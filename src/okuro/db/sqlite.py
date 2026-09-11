# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: SQLite + sqlite-vec backend for okuro.db.
# index:
#   imports
#   def _dict_factory
#   def _parse_migration_number
#   def _scan_migrations
#   class MigrationGapError
#   class MigrationNewerThanCodeError
#   class SQLiteDB
# AGENT_HEADER_END -->
"""SQLite + sqlite-vec backend for okuro.db."""

import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlite_vec

from .interface import OkuroDB

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Numeric prefixes that are intentionally absent from the on-disk migrations.
# Sprint-1 audit C: 013/014/015 were dropped during early development — the
# numbering jumps 012 → 016. Backfilling them now would change downstream
# migration ordering for users who already passed 016, so we accept the gap as
# historical debt and forbid any NEW gap that a future contributor introduces.
# When you decide to fill the gap, remove the entries here so the gap-scan
# enforces strict contiguity.
_ALLOWED_GAPS: frozenset[int] = frozenset({13, 14, 15})

_MIGRATION_RX = re.compile(r"^(\d+)_")


class MigrationGapError(RuntimeError):
    """Raised when migrations/ has a non-contiguous numeric sequence.

    Migrations must form an unbroken sequence from min(prefix) to max(prefix).
    Anything else means a developer either renamed a file or pushed a branch
    that referenced a migration that never landed — silent skips here corrupt
    schema state for downstream users."""


class MigrationNewerThanCodeError(RuntimeError):
    """Raised when the DB has been migrated past the highest on-disk migration.

    Indicates a binary downgrade (pip install of an older okuro version) on
    a DB that was previously migrated by a newer one. Refuse to start rather
    than run old code against a future schema."""


def _dict_factory(cursor, row):
    # `cursor.description` is None for statements that return ZERO-COLUMN rows.
    # `PRAGMA incremental_vacuum` is the one that matters: it yields one such
    # row per page freed, so iterating the cursor IS the reclaim — and this
    # factory used to raise `TypeError: 'NoneType' object is not iterable` on
    # the first row, aborting the drain after a couple of pages. Measured on a
    # scratch store: freelist 1002 -> 1000 per call and a traceback, versus
    # 1002 -> 0 once this returns instead of raising.
    #
    # An empty dict is the honest representation of a row with no columns, and
    # no existing query is affected: every statement that returns actual
    # columns has a description.
    if cursor.description is None:
        return {}
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def _parse_migration_number(name: str) -> int | None:
    """Return the leading numeric prefix of a migration filename, or None.

    ``012_todos.sql`` → 12. Files without a numeric prefix are ignored by
    the gap-scan but will still be applied in lexical order — this matches
    the pre-existing behaviour and avoids breaking any side-loaded test
    fixtures that drop bare ``.sql`` files into a tmp dir.
    """
    m = _MIGRATION_RX.match(name)
    if not m:
        return None
    return int(m.group(1))


def _scan_migrations(directory: Path) -> list[tuple[int | None, Path]]:
    """Return ``[(numeric_prefix or None, path)]`` sorted by filename.

    Lexical sort matches the historical apply order — the numeric prefix is
    purely a contiguity check, not an ordering key.
    """
    return [(_parse_migration_number(p.name), p) for p in sorted(directory.glob("*.sql"))]


class SQLiteDB(OkuroDB):
    """SQLite backend with sqlite-vec for vector search.

    Thread-safe: each thread gets its own connection via threading.local.
    All connections share the same PRAGMAs and sqlite-vec extension.
    """

    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        self._local = threading.local()

    def _make_conn(self) -> sqlite3.Connection:
        """Create and configure a new connection for the current thread."""
        # isolation_level=None: autocommit. We take the writer lock
        # explicitly via self.write() / self.transaction() when atomicity
        # matters. Stops implicit DEFERRED transactions from silently
        # holding the writer lock across slow work (e.g. an HTTP embed
        # call between DELETE and INSERT).
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=30.0)
        conn.row_factory = _dict_factory
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        # THE SENTINELS BEHIND agent_events' TWO SCHEMA GUARDS (migration 136):
        # okuro_distill_gate_armed for BEFORE DELETE, okuro_trace_upsert_armed
        # for BEFORE INSERT onto an existing uuid. Both ship DISARMED; the
        # retention gate and the four trace ingesters arm their own around the
        # one statement each is entitled to.
        #
        # The INSERT guard exists because `INSERT OR REPLACE` resolves a
        # conflict by DELETING the existing row, and with recursive_triggers
        # OFF — the SQLite default — that delete fires no DELETE trigger at
        # all. Measured: it drove an agent_events row straight past the
        # retention guard and left the FTS index in a state where FTS5's own
        # integrity-check passes while a content read raises "database disk
        # image is malformed".
        #
        # Turning recursive_triggers ON was tried first and REVERTED. It does
        # close that path, but it is not safe on this schema: two triggers
        # (artifacts_touch_updated_at, knowledge_saved_queries_touch_updated_at)
        # are AFTER UPDATE triggers that UPDATE their own table, which recurse
        # to the depth limit once the pragma is on — 16 failures and 3 errors
        # across artifacts, memory and orchestrator. The trigger bodies were
        # read to establish that, after an earlier pass checked only trigger
        # headers and wrongly reported the schema recursion-free.
        #
        # A raw sqlite3 shell cannot define either function, so nothing it
        # issues against agent_events even compiles.
        #
        # NOT a security boundary against okuro's own Python — any code can
        # arm a flag. They separate a deliberate delete or upsert from an
        # accidental one, and they close non-Python writers absolutely.
        #
        # Registered ONCE, each reading a thread-local flag, rather than
        # re-registered per call: sqlite3 refuses create_function while an
        # unconsumed cursor is live on the connection, and okuro's row factory
        # leaves statements active routinely.
        from .sentinels import register_all

        register_all(conn)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._make_conn()
            self._local.conn = c
        return c

    def execute(self, sql: str, params: tuple = ()) -> Any:
        return self.conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        return self.conn.execute(sql, params).fetchall()

    def executemany(self, sql: str, params_list: list[tuple]) -> None:
        # Under isolation_level=None each executemany row would autocommit
        # individually. Wrap in one writer transaction for batch atomicity.
        with self.write():
            self.conn.executemany(sql, params_list)

    @contextmanager
    def write(self):
        """Take the writer lock and commit on exit.

        Use this for any multi-statement DML that must be atomic. Single
        DML statements autocommit under isolation_level=None — no wrapper
        needed.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @contextmanager
    def transaction(self):
        """Backwards-compatible alias for write()."""
        with self.write() as conn:
            yield conn

    # ------------------------------------------------------------------
    # Migration safety helpers
    # ------------------------------------------------------------------

    def _ensure_migrations_table(self) -> None:
        """Create or upgrade ``_migrations`` to the 3-column schema.

        Pre-Sprint-1C the table was ``(name, applied_at)``. We add
        ``code_version`` as a NULL-default column so legacy rows survive
        the upgrade — they keep NULL, new rows record ``okuro.__version__``.
        """
        # Create with the new shape if it doesn't exist.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations ("
            "name TEXT PRIMARY KEY, "
            "applied_at TEXT DEFAULT (datetime('now')), "
            "code_version TEXT"
            ")"
        )
        # Upgrade legacy 2-column tables: add code_version if missing.
        cols = {
            row["name"]
            for row in self.fetchall("PRAGMA table_info(_migrations)")
        }
        if "code_version" not in cols:
            self.conn.execute("ALTER TABLE _migrations ADD COLUMN code_version TEXT")

    def _check_gap(
        self,
        scanned: list[tuple[int | None, Path]],
        migrations_dir: Path,
    ) -> None:
        """Raise MigrationGapError if numbered migrations are non-contiguous.

        Files without a numeric prefix are skipped. The contiguity check is
        ``set(prefixes) == set(range(min, max+1)) - _ALLOWED_GAPS``.
        """
        numbered = [(n, p) for (n, p) in scanned if n is not None]
        if not numbered:
            return
        nums = [n for n, _ in numbered]
        lo, hi = min(nums), max(nums)
        expected = set(range(lo, hi + 1)) - _ALLOWED_GAPS
        actual = set(nums)
        missing = sorted(expected - actual)
        duplicates = sorted({n for n in nums if nums.count(n) > 1})
        if missing or duplicates:
            file_list = "\n  ".join(p.name for _, p in scanned)
            problems: list[str] = []
            if missing:
                problems.append(
                    f"missing migration numbers: {', '.join(f'{n:03d}' for n in missing)}"
                )
            if duplicates:
                problems.append(
                    f"duplicate migration numbers: {', '.join(f'{n:03d}' for n in duplicates)}"
                )
            raise MigrationGapError(
                f"Migration sequence in {migrations_dir} is broken — "
                + "; ".join(problems)
                + ".\nAdd the missing files (or register the gap in "
                "okuro.db.sqlite._ALLOWED_GAPS if intentional).\n"
                f"Files on disk:\n  {file_list}"
            )

    def _check_db_not_newer(
        self,
        applied: set[str],
        scanned: list[tuple[int | None, Path]],
    ) -> None:
        """Raise if the DB has been migrated past anything on disk.

        Compares the maximum numeric prefix recorded in ``_migrations``
        against the maximum numeric prefix on disk. If the DB is ahead, a
        downgrade has happened — running the older binary's migrate path
        against the newer schema is unsafe."""
        disk_nums = [n for n, _ in scanned if n is not None]
        if not disk_nums:
            return
        disk_max = max(disk_nums)
        applied_nums = [
            n for n in (_parse_migration_number(name) for name in applied) if n is not None
        ]
        if not applied_nums:
            return
        db_max = max(applied_nums)
        if db_max > disk_max:
            raise MigrationNewerThanCodeError(
                f"Database has been migrated by a newer okuro "
                f"(max applied: {db_max:03d}; this binary knows up to {disk_max:03d}). "
                "Upgrade okuro or restore a backup."
            )

    def migration_health(self, migrations_dir: Path | None = None) -> dict[str, Any]:
        """Return a snapshot of migration state for diagnostics.

        Doctor probes call this — it never raises; it reports.
        Keys:
          - ``disk_max``: highest numeric prefix on disk (None if none)
          - ``db_max``: highest numeric prefix recorded in _migrations
          - ``gap_detected``: bool — True if a non-allowed gap exists
          - ``missing``: sorted list[int] of missing numbers (excluding allowed)
          - ``allowed_gaps``: sorted list[int] of registered intentional gaps
          - ``newer_than_code``: bool — True if db_max > disk_max
          - ``code_versions``: dict[name, code_version] from _migrations
        """
        directory = migrations_dir or MIGRATIONS_DIR
        scanned = _scan_migrations(directory)
        nums = [n for n, _ in scanned if n is not None]
        disk_max = max(nums) if nums else None
        missing: list[int] = []
        if nums:
            lo, hi = min(nums), max(nums)
            missing = sorted((set(range(lo, hi + 1)) - _ALLOWED_GAPS) - set(nums))

        try:
            self._ensure_migrations_table()
            rows = self.fetchall("SELECT name, code_version FROM _migrations")
        except sqlite3.DatabaseError:
            rows = []

        applied_nums = [
            n
            for n in (_parse_migration_number(r["name"]) for r in rows)
            if n is not None
        ]
        db_max = max(applied_nums) if applied_nums else None
        return {
            "disk_max": disk_max,
            "db_max": db_max,
            "gap_detected": bool(missing),
            "missing": missing,
            "allowed_gaps": sorted(_ALLOWED_GAPS),
            "newer_than_code": (
                db_max is not None and disk_max is not None and db_max > disk_max
            ),
            "code_versions": {r["name"]: r["code_version"] for r in rows},
        }

    def pending_migrations(self, migrations_dir: Path | None = None) -> list[str]:
        """Names of on-disk migrations not yet applied — READ-ONLY.

        Diffs the migrations directory against the ``_migrations`` table and
        returns the unapplied names. Never applies anything (unlike
        ``migrate()``), so health checks can report drift without the side
        effect of mutating the schema. Does not run migrate()'s gap/newer
        safety checks — those gate the *apply*, which this never does.
        """
        directory = migrations_dir or MIGRATIONS_DIR
        scanned = _scan_migrations(directory)
        try:
            self._ensure_migrations_table()
            applied = {
                r["name"] for r in self.fetchall("SELECT name FROM _migrations")
            }
        except sqlite3.DatabaseError:
            applied = set()
        return [mig.name for (_num, mig) in scanned if mig.name not in applied]

    # ------------------------------------------------------------------
    # migrate()
    # ------------------------------------------------------------------

    def migrate(self, migrations_dir: Path | None = None) -> list[str]:
        """Run pending SQL migrations.

        Safety checks performed BEFORE applying anything:
          1. Numeric prefixes form a contiguous sequence (modulo
             ``_ALLOWED_GAPS``). Otherwise raise ``MigrationGapError``.
          2. The DB has not been migrated past the highest on-disk
             migration. Otherwise raise ``MigrationNewerThanCodeError``.

        Each newly applied migration records ``okuro.__version__`` into
        ``_migrations.code_version`` so future agents can answer "which
        binary applied this row?"."""
        directory = migrations_dir or MIGRATIONS_DIR

        # Lazy import — avoids a circular import at module load time and
        # keeps this file standalone-importable for tests that stub the
        # version.
        from okuro import __version__ as code_version

        self._ensure_migrations_table()

        scanned = _scan_migrations(directory)
        self._check_gap(scanned, directory)

        applied = {
            r["name"]
            for r in self.fetchall("SELECT name FROM _migrations")
        }
        self._check_db_not_newer(applied, scanned)

        pending = [(num, mig) for (num, mig) in scanned if mig.name not in applied]

        # Pre-migration snapshot. On an UPGRADE (the DB already carries
        # applied migrations) take a portable, WAL-consistent online copy of
        # the live DB before mutating schema. This is the recovery net for a
        # half-applied table-rebuild — e.g. 063's PRAGMA foreign_keys=OFF +
        # DROP/RENAME, which by SQLite rule cannot run inside a transaction.
        # Best-effort: a fresh install (no prior migrations) has nothing to
        # lose, and a snapshot failure must never block boot.
        if pending and applied:
            self._snapshot_before_migrate(pending)

        newly_applied: list[str] = []
        for _num, mig in pending:
            sql = mig.read_text()
            try:
                if self._self_manages_txn(sql):
                    # Migration controls its own atomicity: explicit
                    # BEGIN/COMMIT, a PRAGMA-based table rebuild (no-op inside
                    # a txn), or a virtual-table create. Run as authored, then
                    # record. (CREATE TRIGGER bodies contain BEGIN…END and are
                    # caught here too — running them as authored is correct.)
                    self.conn.executescript(sql)
                    self.conn.execute(
                        "INSERT INTO _migrations (name, code_version) VALUES (?, ?)",
                        (mig.name, code_version),
                    )
                else:
                    # Plain DDL with no transaction control. Wrap the body AND
                    # the _migrations marker in ONE atomic script so a
                    # mid-script failure leaves neither partial schema nor an
                    # orphan marker. executescript() COMMITs first (a no-op
                    # under autocommit), then runs our BEGIN…COMMIT.
                    name_lit = mig.name.replace("'", "''")
                    ver_lit = code_version.replace("'", "''")
                    self.conn.executescript(
                        "BEGIN;\n" + sql + "\nINSERT INTO _migrations "
                        f"(name, code_version) VALUES ('{name_lit}', '{ver_lit}');"
                        "\nCOMMIT;"
                    )
            except Exception:
                # Leave the connection usable, then surface which migration
                # broke and where the snapshot is. Re-raise — the caller
                # (daemon boot wrapper / doctor) decides whether to continue;
                # the boot wrapper already fails open.
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                import logging
                logging.getLogger("okuro.db.sqlite").error(
                    "migration %s FAILED — schema may be partial; restore the "
                    "pre-migration snapshot from %s",
                    mig.name, self._backup_dir(),
                )
                raise
            newly_applied.append(mig.name)

        # Realign vec_* tables with the active embedding tier dim. Migrations
        # 001/002/etc declare ``float[384]`` — the pre-Qwen3 default — so a
        # fresh install lands with the wrong dim for any tier ≠ low. Safe
        # mode (empty_only) skips tables with rows so an upgrade install with
        # the embed service still booting can't wipe existing vectors. The
        # manual CLI (``python -m okuro.embed.repair``) handles those.
        if newly_applied:
            try:
                from okuro.embed.repair import ensure_vec_dims
                ensure_vec_dims(empty_only=True)
            except Exception as exc:  # noqa: BLE001
                import logging
                logging.getLogger("okuro.db.sqlite").warning(
                    "post-migrate ensure_vec_dims skipped (%s) — run "
                    "`python -m okuro.embed.repair` manually",
                    exc,
                )

        return newly_applied

    @staticmethod
    def _self_manages_txn(sql: str) -> bool:
        """True if a migration must run via raw executescript (autocommit).

        Three cases run as authored rather than inside an injected
        transaction:
          * explicit ``BEGIN``/``COMMIT`` in the script (already atomic) —
            also matches CREATE TRIGGER ``BEGIN…END`` bodies, which is fine
          * ``PRAGMA`` statements (e.g. foreign_keys=OFF for SQLite's
            table-rebuild procedure, which is ignored inside a transaction)
          * ``VIRTUAL TABLE`` creation (vec0 / FTS5) — kept on the
            as-authored path to avoid any in-transaction edge case
        Everything else is plain DDL we wrap in one atomic script.
        """
        lowered = sql.lower()
        return "begin" in lowered or "pragma" in lowered or "virtual table" in lowered

    def _backup_dir(self) -> Path:
        return Path(self._path).parent / "backups"

    def _snapshot_before_migrate(self, pending: list) -> None:
        """Online-copy the live DB to backups/ before applying migrations.

        Uses the sqlite3 backup API (WAL-consistent, no checkpoint needed).
        Best-effort — logs and proceeds on failure so boot is never blocked.
        """
        import logging
        import os
        log = logging.getLogger("okuro.db.sqlite")
        # Avoid a redundant full-DB copy when the caller (e.g. scripts/update.sh)
        # has ALREADY taken a pre-update backup. On a multi-GB DB the duplicate
        # snapshot doubles the disk hit per update.
        if os.environ.get("OKURO_SKIP_PREMIG_SNAPSHOT") == "1":
            log.info("premig snapshot skipped (OKURO_SKIP_PREMIG_SNAPSHOT=1 — "
                     "caller already backed up)")
            return
        try:
            src = Path(self._path)
            if str(src) == ":memory:" or not src.exists():
                return
            bdir = self._backup_dir()
            bdir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            lo = pending[0][1].name.split("_")[0]
            hi = pending[-1][1].name.split("_")[0]
            dest_path = bdir / f"okuro-premig-{lo}-to-{hi}-{ts}.db"
            dest = sqlite3.connect(str(dest_path))
            try:
                with dest:
                    self.conn.backup(dest)
            finally:
                dest.close()
            log.info("pre-migration snapshot written: %s", dest_path)
            self._prune_backups(bdir)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "pre-migration snapshot FAILED (%s) — proceeding WITHOUT a "
                "backup; restore manually if migration corrupts the DB", exc
            )

    @staticmethod
    def _prune_backups(bdir: Path, keep: int = 10) -> None:
        """Keep only the newest ``keep`` snapshots (timestamped names sort)."""
        try:
            snaps = sorted(bdir.glob("okuro-premig-*.db"))
            for old in snaps[:-keep]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    def vec_search(
        self,
        table: str,
        embedding: bytes,
        limit: int = 10,
        partition: tuple[str, str] | None = None,
    ) -> list[dict]:
        """Search a vec0 virtual table by vector distance.

        Args:
            table: Name of the vec0 virtual table (e.g. 'vec_memory').
            embedding: Query vector as float32 bytes.
            limit: Max results.
            partition: Optional ``(column, value)`` to push an equality filter
                INTO the KNN scan via a partition-key column. For vec_cortex
                this is ``("project", slug)`` so a scoped query is filtered
                inside the scan instead of post-hoc in Python — the fix for
                scoped recall collapsing when one project dominates the index
                (F20/F21). The column MUST be a declared vec0 partition key.

        Returns:
            List of dicts with 'id' and 'distance' keys.
        """
        if partition is not None:
            col, val = partition
            # col is an identifier we control (caller passes a literal column
            # name, never user input); value is parameterised.
            rows = self.fetchall(
                f"SELECT id, distance FROM {table} "
                f"WHERE embedding MATCH ? AND {col} = ? "
                f"ORDER BY distance LIMIT ?",
                (embedding, val, limit),
            )
        else:
            rows = self.fetchall(
                f"SELECT id, distance FROM {table} "
                f"WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (embedding, limit),
            )
        return rows

    def fts_search(
        self,
        fts_table: str,
        content_table: str,
        match_query: str,
        limit: int = 30,
        project: str | None = None,
    ) -> list[dict]:
        """BM25 search over an external-content FTS5 table (F25).

        Joins FTS hits back to ``content_table`` on rowid to recover the TEXT
        ``id`` (and apply the ``deleted_at`` / ``project`` filters), returning
        rows ordered by BM25 relevance (best first). FTS5 ``bm25()`` returns a
        NEGATIVE score where more-negative = more relevant, so we ORDER BY
        bm25 ASC.

        Returns dicts with ``id`` and ``bm25`` keys. Empty list on a malformed
        MATCH query (FTS5 raises on bad syntax) or a missing table, so a stray
        query char or a pre-migration DB never breaks the hybrid search.
        """
        scope = "AND c.project = ? " if project else ""
        params: tuple = (match_query, project) if project else (match_query,)
        sql = (
            f"SELECT c.id AS id, bm25({fts_table}) AS bm25 "
            f"FROM {fts_table} f "
            f"JOIN {content_table} c ON c.rowid = f.rowid "
            f"WHERE {fts_table} MATCH ? "
            f"AND c.deleted_at IS NULL {scope}"
            f"ORDER BY bm25 ASC LIMIT ?"
        )
        try:
            return self.fetchall(sql, params + (limit,))
        except sqlite3.OperationalError as exc:
            # Bad FTS5 MATCH syntax (unbalanced quote, etc.) or missing FTS
            # table — treat as no keyword hits rather than failing search.
            #
            # But NOT a lock. sqlite3.OperationalError also carries "database
            # is locked", and swallowing that silently drops the BM25 arm: the
            # RRF fusion degrades to vector-only and recall falls back to the
            # ~0.68 ceiling migration 101 exists to fix — intermittently, with
            # no signal, looking exactly like "there were no keyword hits".
            #
            # In WAL an ordinary reader is not blocked by an ordinary writer,
            # so the trigger is an exclusive-lock holder: okuro's own backup or
            # checkpoint. That is a transient condition worth retrying against
            # the busy_timeout already configured, not a result worth faking.
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise
            logging.getLogger("okuro.db.sqlite").warning(
                "fts_search on %s degraded to no-hits: %s", fts_table, exc
            )
            return []

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None
